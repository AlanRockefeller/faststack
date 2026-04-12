"""Parse faststack.json for folder statistics display in thumbnail grid."""

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

SIDECAR_FILENAME = "faststack.json"


@dataclass
class FolderStats:
    """Statistics parsed from a folder's faststack.json file."""

    total_images: int
    stacked_count: int
    uploaded_count: int
    edited_count: int
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


# Cache by (folder_path, json_mtime_ns, folder_mtime_ns) to avoid re-parsing during scroll
# IMPORTANT: Both json_mtime_ns and folder_mtime_ns are needed:
# - json_mtime_ns: changes when faststack.json is modified (flags, metadata)
# - folder_mtime_ns: changes when files are added/removed/renamed in folder
# - folder_mtime_ns: changes when files are added/removed/renamed in folder
_stats_cache: Dict[Tuple[Path, int, int], Optional[FolderStats]] = {}
MAX_CACHE_SIZE = 1000


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
    cache_key = (folder_path.resolve(), json_mtime_ns, folder_mtime_ns)
    if cache_key in _stats_cache:
        return _stats_cache[cache_key]

    # Parse the JSON file
    stats = _parse_faststack_json(json_path)

    # Cache the result (even if None)
    # Cache the result (even if None)
    _check_cache_size(_stats_cache)
    _stats_cache[cache_key] = stats

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
        for entry in os.scandir(folder_path):
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

    # Sort JPG files alphabetically (matches find_images default sort)
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

    for stem, meta in entries.items():
        if not isinstance(meta, dict):
            continue

        if meta.get("stacked", False):
            stacked_count += 1
        if meta.get("uploaded", False):
            uploaded_count += 1
        if meta.get("edited", False):
            edited_count += 1

    # Single-pass scan: count file types AND collect JPG filenames for sparkline
    folder_path = json_path.parent
    jpg_count, raw_count, jpg_files = _scan_folder_files(folder_path)

    # Compute coverage buckets for sparkline (using pre-collected JPG list)
    coverage_buckets = _compute_coverage_buckets(jpg_files, entries)

    return FolderStats(
        total_images=total_images,
        stacked_count=stacked_count,
        uploaded_count=uploaded_count,
        edited_count=edited_count,
        jpg_count=jpg_count,
        raw_count=raw_count,
        coverage_buckets=coverage_buckets,
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
    if not jpg_files:
        return []

    total_files = len(jpg_files)
    if total_files < num_buckets:
        num_buckets = total_files

    # Single-pass accumulation into buckets to avoid redundant list processing
    # Each entry is [uploaded_count, edited_count, stacked_count, todo_count, total_in_bucket]
    accumulators = [[0, 0, 0, 0, 0] for _ in range(num_buckets)]

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

        accumulators[bucket_idx][4] += 1

    # Convert counts to ratios
    buckets = []
    for uploaded, edited, stacked, todo, count in accumulators:
        if count == 0:
            buckets.append((0.0, 0.0, 0.0, 0.0))
        else:
            buckets.append(
                (uploaded / count, edited / count, stacked / count, todo / count)
            )

    return buckets


def clear_stats_cache():
    """Clear the folder stats cache."""
    global _stats_cache
    _stats_cache.clear()
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
    cache_key = (folder_path.resolve(), mtime_ns)
    # Check if we have this in a separate "raw count" cache
    if cache_key in _raw_count_cache:
        return _raw_count_cache[cache_key]

    # Count image files using shared scan function
    jpg_count, raw_count, _ = _scan_folder_files(folder_path)
    total_count = jpg_count + raw_count
    if total_count == 0:
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
    _check_cache_size(_raw_count_cache)
    _raw_count_cache[cache_key] = stats
    return stats


# Separate cache for raw file counts (folders without faststack.json)
_raw_count_cache: Dict[Tuple[Path, int], Optional[FolderStats]] = {}


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
        for entry in os.scandir(folder_path):
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
    global _raw_count_cache
    _raw_count_cache.clear()
    log.debug("Cleared raw count cache")
