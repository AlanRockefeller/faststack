"""Parse faststack.json for folder statistics display in thumbnail grid."""

import dataclasses
import json
import logging
import os
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

from faststack.logging_setup import get_app_data_dir

log = logging.getLogger(__name__)

SIDECAR_FILENAME = "faststack.json"


@dataclass
class FolderStats:
    """Statistics parsed from a folder's faststack.json file."""

    total_images: int
    stacked_count: int
    uploaded_count: int
    edited_count: int
    # Number of entries currently flagged as part of a batch.
    batch_count: int = 0
    # Count of image-like files (JPG, JPEG, PNG, GIF, BMP, TIFF, TIF, WEBP)
    # Named 'jpg_count' for historical reasons; displayed as "IMG" in UI
    jpg_count: int = 0
    raw_count: int = 0
    # Coverage sparkline data: list of (upload_ratio, edited_ratio, stack_ratio, todo_ratio) tuples per bucket
    # Each ratio is 0.0-1.0, representing the fraction of JPGs in that bucket
    # that have the flag set. Empty list if no faststack.json or no JPGs.
    coverage_buckets: list[tuple[float, float, float, float]] = field(
        default_factory=list
    )
    # Batch coverage sparkline data aligned with coverage_buckets.
    batch_coverage_buckets: list[float] = field(default_factory=list)


# Cache by (folder_path, json_mtime_ns, folder_mtime_ns) to avoid re-parsing during scroll
# IMPORTANT: Both json_mtime_ns and folder_mtime_ns are needed:
# - json_mtime_ns: changes when faststack.json is modified (flags, metadata)
# - folder_mtime_ns: changes when files are added/removed/renamed in folder
# - folder_mtime_ns: changes when files are added/removed/renamed in folder
_stats_cache: Dict[Tuple[str, int, int], Optional[FolderStats]] = {}
MAX_CACHE_SIZE = 1000
# Folder stats are read from the UI thread and from thumbnail workers; the
# FIFO trim iterates the dict, which must not race another thread's insert.
_cache_lock = threading.Lock()


# The same results, kept in the app-data folder between runs. Opening a folder
# with many subfolders parsed every child's faststack.json and listed every
# child directory on the GUI thread at each start; with this, an unchanged
# child costs the two stat() calls that validate its entry. Entries are keyed
# by folder path and checked against the same two mtimes as _stats_cache.
_PERSISTED_FILENAME = "folder_stats_cache.json"
_PERSISTED_VERSION = 1
_PERSIST_DELAY_SECONDS = 2.0
# abspath -> {"json": mtime_ns, "dir": mtime_ns, "stats": dict or None}.
# None until first use; guarded by _cache_lock.
_persisted: Optional[Dict[str, dict]] = None
_persist_timer: Optional[threading.Timer] = None


def _persisted_path() -> Path:
    return get_app_data_dir() / _PERSISTED_FILENAME


def _load_persisted_locked() -> Dict[str, dict]:
    """Return the on-disk entries, reading the file on first use."""
    global _persisted
    if _persisted is None:
        _persisted = {}
        try:
            with open(_persisted_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if (
                isinstance(data, dict)
                and data.get("version") == _PERSISTED_VERSION
                and isinstance(data.get("folders"), dict)
            ):
                _persisted = data["folders"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.debug("Ignoring unreadable folder stats cache: %s", e)
    return _persisted


def _stats_from_persisted(payload) -> Optional["FolderStats"]:
    """Rebuild a FolderStats from its JSON form; raises on anything unexpected."""
    if payload is None:
        return None
    values = dict(payload)
    values["coverage_buckets"] = [
        (float(a), float(b), float(c), float(d))
        for a, b, c, d in values.get("coverage_buckets", [])
    ]
    values["batch_coverage_buckets"] = [
        float(v) for v in values.get("batch_coverage_buckets", [])
    ]
    for name in (
        "total_images",
        "stacked_count",
        "uploaded_count",
        "edited_count",
        "batch_count",
        "jpg_count",
        "raw_count",
    ):
        if name in values:
            values[name] = int(values[name])
    return FolderStats(**values)


def _persisted_lookup_locked(
    cache_key: Tuple[str, int, int],
) -> Tuple[Optional["FolderStats"], bool]:
    """Return ``(stats, found)`` for a key whose mtimes still match on disk."""
    folder, json_mtime_ns, folder_mtime_ns = cache_key
    entry = _load_persisted_locked().get(folder)
    if not isinstance(entry, dict):
        return None, False
    if entry.get("json") != json_mtime_ns or entry.get("dir") != folder_mtime_ns:
        return None, False
    try:
        return _stats_from_persisted(entry.get("stats")), True
    except (TypeError, ValueError):
        return None, False


def _persisted_store_locked(
    cache_key: Tuple[str, int, int], stats: Optional["FolderStats"]
) -> None:
    folder, json_mtime_ns, folder_mtime_ns = cache_key
    entries = _load_persisted_locked()
    entries.pop(folder, None)  # re-insert at the end: the trim is FIFO
    entries[folder] = {
        "json": json_mtime_ns,
        "dir": folder_mtime_ns,
        "stats": dataclasses.asdict(stats) if stats is not None else None,
    }
    _check_cache_size(entries)
    _schedule_persist_locked()


def _schedule_persist_locked() -> None:
    """Write the entries out shortly, once, however many just changed."""
    global _persist_timer
    if _persist_timer is not None:
        return
    timer = threading.Timer(_PERSIST_DELAY_SECONDS, _flush_persisted)
    timer.daemon = True
    timer.name = "FolderStatsCache"
    _persist_timer = timer
    timer.start()


def _flush_persisted() -> None:
    """Timer thread: replace the cache file with the current entries."""
    global _persist_timer
    with _cache_lock:
        _persist_timer = None
        payload = json.dumps(
            {"version": _PERSISTED_VERSION, "folders": _persisted or {}}
        )
    # Only a cache: no fsync, and a failed write just means a slower start.
    try:
        path = _persisted_path()
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
    except OSError as e:
        log.debug("Could not write folder stats cache: %s", e)


def _check_cache_size(cache_dict):
    """Enforce maximum cache size by removing oldest entries (FIFO)."""
    if len(cache_dict) > MAX_CACHE_SIZE:
        # Remove a chunk to amortize cost
        excess = len(cache_dict) - MAX_CACHE_SIZE + 10
        for _ in range(excess):
            if not cache_dict:
                break
            # dict order is insertion order in modern Python, so this is FIFO
            cache_dict.pop(next(iter(cache_dict)))


def read_folder_stats(folder_path: Path) -> Optional[FolderStats]:
    """Parse faststack.json in folder. Stat the json file for mtime_ns. Tolerant to errors.

    Args:
        folder_path: Path to the folder containing faststack.json

    Returns:
        FolderStats if valid faststack.json exists, None otherwise.
        Caches results by (folder_path, json_mtime_ns, folder_mtime_ns) to avoid
        re-parsing during scrolling. Cache invalidates when either faststack.json
        or the folder contents change.

    Note:
        Cache key uses both json file's mtime_ns and folder mtime_ns for invalidation.
        On some filesystems with coarse time granularity (e.g., FAT32, some network
        mounts), rapid edits within the same second may not trigger cache invalidation.
        This is rare and acceptable for UI display purposes. Call clear_stats_cache()
        explicitly if stale data is suspected.
    """
    json_path = folder_path / SIDECAR_FILENAME

    # Check if file exists
    try:
        stat_info = json_path.stat()
        json_mtime_ns = stat_info.st_mtime_ns
    except (OSError, FileNotFoundError):
        # No faststack.json in this folder
        return None

    # Get folder mtime for cache invalidation when files are added/removed
    try:
        folder_mtime_ns = folder_path.stat().st_mtime_ns
    except OSError:
        folder_mtime_ns = 0  # Fallback if stat fails

    # Check cache using both mtime values for invalidation
    # abspath, not resolve(): this runs per folder tile while scrolling, and
    # resolve() costs a syscall per path component (slow on network shares).
    cache_key = (os.path.abspath(folder_path), json_mtime_ns, folder_mtime_ns)
    with _cache_lock:
        if cache_key in _stats_cache:
            return _stats_cache[cache_key]
        # A result from an earlier run, still valid for these two mtimes.
        stats, found = _persisted_lookup_locked(cache_key)
        if found:
            _check_cache_size(_stats_cache)
            _stats_cache[cache_key] = stats
            return stats

    # Parse the JSON file
    stats = _parse_faststack_json(json_path)

    # Cache the result (even if None)
    with _cache_lock:
        _check_cache_size(_stats_cache)
        _stats_cache[cache_key] = stats
        _persisted_store_locked(cache_key, stats)

    return stats


# Extensions considered as JPG (processed images)
JPG_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp"}

# Extensions considered as RAW (camera raw files)
RAW_EXTENSIONS = {
    ".orf",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".rw2",
    ".raf",
    ".pef",
}


def _scan_folder_files(folder_path: Path) -> Tuple[int, int, list]:
    """Single-pass scan to count files and collect JPG names.

    Performs one os.scandir pass to gather all file statistics needed
    for folder stats and coverage sparkline computation.

    Returns:
        Tuple of (jpg_count, raw_count, jpg_filenames_sorted)
        jpg_filenames_sorted is a list of JPG filenames sorted alphabetically.
    """
    jpg_count = 0
    raw_count = 0
    jpg_files = []
    try:
        with os.scandir(folder_path) as entries:
            scanned = list(entries)
        for entry in scanned:
            if entry.is_file():
                # FASTER: os.path.splitext is string-based, avoids Path object creation
                _, suffix = os.path.splitext(entry.name)
                suffix = suffix.lower()
                if suffix in JPG_EXTENSIONS:
                    jpg_count += 1
                    jpg_files.append(entry.name)
                elif suffix in RAW_EXTENSIONS:
                    raw_count += 1
    except OSError:
        pass

    # Sort JPG files alphabetically. The loupe's default order is by file
    # timestamp first (see indexer.image_sort_key); for camera-named files the
    # two orders agree, and the sparkline only needs a coarse 40-bucket
    # progression, so this avoids a stat() per file in every subfolder.
    jpg_files.sort(key=str.lower)
    return jpg_count, raw_count, jpg_files


def _parse_faststack_json(json_path: Path) -> Optional[FolderStats]:
    """Parse a faststack.json file and extract statistics.

    Tolerant to:
    - Missing keys (uses defaults)
    - Old formats (version < 2)
    - Parse errors (returns None)
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        log.debug("Failed to parse %s: %s", json_path, e)
        return None

    # Validate JSON root is a dict
    if not isinstance(data, dict):
        log.debug("Invalid JSON root in %s (expected dict)", json_path)
        return None

    # Handle different sidecar formats
    entries = data.get("entries", {})
    if not isinstance(entries, dict):
        log.debug("Invalid entries format in %s", json_path)
        return None

    # Count statistics from entries
    total_images = len(entries)
    stacked_count = 0
    uploaded_count = 0
    edited_count = 0
    batch_count = 0

    for stem, meta in entries.items():
        if not isinstance(meta, dict):
            continue

        if meta.get("stacked", False):
            stacked_count += 1
        if meta.get("uploaded", False):
            uploaded_count += 1
        if meta.get("edited", False):
            edited_count += 1
        if meta.get("batch", False):
            batch_count += 1

    # Single-pass scan: count file types AND collect JPG filenames for sparkline
    folder_path = json_path.parent
    jpg_count, raw_count, jpg_files = _scan_folder_files(folder_path)

    # Compute coverage buckets for sparkline (using pre-collected JPG list)
    coverage_buckets, batch_coverage_buckets = _compute_folder_coverage(
        jpg_files, entries
    )

    return FolderStats(
        total_images=total_images,
        stacked_count=stacked_count,
        uploaded_count=uploaded_count,
        edited_count=edited_count,
        batch_count=batch_count,
        jpg_count=jpg_count,
        raw_count=raw_count,
        coverage_buckets=coverage_buckets,
        batch_coverage_buckets=batch_coverage_buckets,
    )


def _compute_coverage_buckets(
    jpg_files: list, entries: Dict[str, dict], num_buckets: int = 40
) -> list:
    """Compute coverage sparkline buckets for uploads, edits, stacks, and todos.

    Returns a list of (upload_ratio, edited_ratio, stack_ratio, todo_ratio) tuples.
    Each ratio is 0.0-1.0, representing the fraction of JPGs in that bucket
    with the respective flag set.

    Args:
        jpg_files: Pre-sorted list of JPG filenames (from _scan_folder_files)
        entries: Dict of {stem: metadata} from faststack.json
        num_buckets: Number of buckets to divide files into (default 40)

    Returns:
        List of (upload_ratio, edited_ratio, stack_ratio, todo_ratio) tuples.
    """
    coverage_buckets, _ = _compute_folder_coverage(jpg_files, entries, num_buckets)
    return coverage_buckets


def _compute_folder_coverage(
    jpg_files: list, entries: Dict[str, dict], num_buckets: int = 40
) -> tuple[list[tuple[float, float, float, float]], list[float]]:
    """Compute aligned coverage buckets for the folder sparkline."""
    if not jpg_files:
        return [], []

    total_files = len(jpg_files)
    if total_files < num_buckets:
        num_buckets = total_files

    # Single-pass accumulation into buckets to avoid redundant list processing.
    # Each entry is [uploaded, edited, stacked, todo, batch, total_in_bucket].
    accumulators = [[0, 0, 0, 0, 0, 0] for _ in range(num_buckets)]

    # Lazy dictionary for case-insensitive lookup (only built if direct matching fails)
    entries_lower = None

    for i, filename in enumerate(jpg_files):
        # Map file index to bucket index using floor division
        bucket_idx = (i * num_buckets) // total_files

        # Efficient stem extraction and metadata lookup
        stem, _ = os.path.splitext(filename)

        # Priority: 1. Exact filename, 2. Stem, 3. Case-insensitive filename, 4. Case-insensitive stem
        meta = entries.get(filename)
        if meta is None:
            meta = entries.get(stem)

        if meta is None and entries:
            if entries_lower is None:
                entries_lower = {
                    k.lower(): v for k, v in entries.items() if isinstance(k, str)
                }
            meta = entries_lower.get(filename.lower())
            if meta is None:
                meta = entries_lower.get(stem.lower())

        if isinstance(meta, dict):
            if meta.get("uploaded", False):
                accumulators[bucket_idx][0] += 1
            if meta.get("edited", False):
                accumulators[bucket_idx][1] += 1
            if meta.get("stacked", False):
                accumulators[bucket_idx][2] += 1
            if meta.get("todo", False):
                accumulators[bucket_idx][3] += 1
            if meta.get("batch", False):
                accumulators[bucket_idx][4] += 1

        accumulators[bucket_idx][5] += 1

    # Convert counts to ratios
    coverage_buckets = []
    batch_coverage_buckets = []
    for uploaded, edited, stacked, todo, batch, count in accumulators:
        if count == 0:
            coverage_buckets.append((0.0, 0.0, 0.0, 0.0))
            batch_coverage_buckets.append(0.0)
        else:
            coverage_buckets.append(
                (uploaded / count, edited / count, stacked / count, todo / count)
            )
            batch_coverage_buckets.append(batch / count)

    return coverage_buckets, batch_coverage_buckets


def clear_stats_cache():
    """Clear the folder stats cache, including the copy kept between runs."""
    global _persisted
    with _cache_lock:
        _stats_cache.clear()
        had_persisted = bool(_load_persisted_locked())
        _persisted = {}
        if had_persisted:
            _schedule_persist_locked()
    log.debug("Cleared folder stats cache")


def count_images_in_folder(folder_path: Path) -> Optional[FolderStats]:
    """Count actual image files in a folder (for folders without faststack.json).

    Uses folder mtime for cache key since there's no faststack.json to track.
    This is less efficient than faststack.json but works for special folders
    like the recycle bin.

    Args:
        folder_path: Path to the folder to count images in

    Returns:
        FolderStats with total_images count and jpg/raw breakdown (other counts will be 0)
    """
    try:
        stat_info = folder_path.stat()
        mtime_ns = stat_info.st_mtime_ns
    except (OSError, FileNotFoundError):
        return None

    # Use a different cache key prefix to avoid collision with faststack.json cache
    cache_key = (os.path.abspath(folder_path), mtime_ns)
    # Check if we have this in a separate "raw count" cache
    with _cache_lock:
        if cache_key in _raw_count_cache:
            return _raw_count_cache[cache_key]

    # Count image files using shared scan function
    jpg_count, raw_count, _ = _scan_folder_files(folder_path)
    total_count = jpg_count + raw_count
    if total_count == 0:
        with _cache_lock:
            _check_cache_size(_raw_count_cache)
            _raw_count_cache[cache_key] = None
        return None

    stats = FolderStats(
        total_images=total_count,
        stacked_count=0,
        uploaded_count=0,
        edited_count=0,
        jpg_count=jpg_count,
        raw_count=raw_count,
    )
    with _cache_lock:
        _check_cache_size(_raw_count_cache)
        _raw_count_cache[cache_key] = stats
    return stats


# Separate cache for raw file counts (folders without faststack.json)
_raw_count_cache: Dict[Tuple[str, int], Optional[FolderStats]] = {}


def get_file_counts_by_extension(folder_path: Path) -> Dict[str, int]:
    """Count files by extension in a folder, excluding faststack.json.

    Image-like extensions (.jpg, .jpeg, .png, etc. from JPG_EXTENSIONS) are
    grouped under "IMG" for cleaner display. RAW extensions keep their real
    labels. Other extensions are shown as-is.

    Args:
        folder_path: Path to the folder to count files in

    Returns:
        Dictionary mapping uppercase extension (without dot) to count.
        Example: {"IMG": 10, "ORF": 10, "TXT": 1}
    """
    counts: Counter[str] = Counter()
    try:
        with os.scandir(folder_path) as entries:
            scanned = list(entries)
        for entry in scanned:
            if entry.is_file():
                name = entry.name
                # Skip faststack.json
                if name == SIDECAR_FILENAME:
                    continue
                # FASTER: os.path.splitext is string-based, avoids Path object creation
                _, suffix = os.path.splitext(name)
                suffix_lower = suffix.lower()
                if suffix_lower:
                    # Group image-like extensions under "IMG"
                    if suffix_lower in JPG_EXTENSIONS:
                        counts["IMG"] += 1
                    else:
                        # Convert to uppercase without dot for display
                        ext = suffix_lower[1:].upper()
                        counts[ext] += 1
    except OSError as e:
        log.debug("Failed to scan %s: %s", folder_path, e)

    return dict(counts)


def clear_raw_count_cache():
    """Clear the raw file count cache."""
    with _cache_lock:
        _raw_count_cache.clear()
    log.debug("Cleared raw count cache")
