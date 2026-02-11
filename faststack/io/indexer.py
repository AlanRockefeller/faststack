"""Scans directories for JPGs and pairs them with corresponding RAW files."""

import logging
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Tuple

from faststack.models import ImageFile

log = logging.getLogger(__name__)

RAW_EXTENSIONS = {".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng"}

JPG_EXTENSIONS = {".jpg", ".jpeg", ".jpe"}

_DEVELOPED_SUFFIX = "-developed"

# Matches FastStack backup stems: name-backup, name-backup2, name-backup33, etc.
_BACKUP_STEM_RE = re.compile(r"-backup\d*$", re.IGNORECASE)


def find_images(directory: Path) -> List[ImageFile]:
    """Finds all JPGs in a directory and pairs them with RAW files."""
    t_start = time.perf_counter()
    log.info("Scanning directory for images: %s", directory)

    # Categorize files
    all_jpgs: List[Tuple[Path, os.stat_result]] = []
    raws: Dict[str, List[Tuple[Path, os.stat_result]]] = {}  # keyed by stem.casefold()

    try:
        for entry in os.scandir(directory):
            if entry.is_file():
                p = Path(entry.path)
                ext = p.suffix.lower()
                if ext in JPG_EXTENSIONS:
                    # Skip FastStack backup files (name-backup.jpg, name-backup2.jpg, etc.)
                    if _BACKUP_STEM_RE.search(p.stem):
                        continue
                    all_jpgs.append((p, entry.stat()))
                elif ext in RAW_EXTENSIONS:
                    stem = p.stem.casefold()
                    if stem not in raws:
                        raws[stem] = []
                    raws[stem].append((p, entry.stat()))
    except OSError:
        log.exception("Error scanning directory %s", directory)
        return []

    # Separate developed JPGs, build base map, and process normal JPGs
    # base_map: filename.casefold() -> (mtime, name)
    base_map: Dict[str, Tuple[float, str]] = {}
    developed_candidates: List[Tuple[Path, os.stat_result, str]] = (
        []
    )  # path, stat, base_stem

    image_entries: List[Tuple[Tuple[float, str, int, str], ImageFile]] = []
    used_raws = set()

    for p, stat in all_jpgs:
        is_dev, base_stem = _parse_developed(p)
        if is_dev:
            developed_candidates.append((p, stat, base_stem))
        else:
            # Register in base_map for developed images to find their parents
            base_map[p.name.casefold()] = (stat.st_mtime, p.name)

            # Process as normal JPG
            raw_pair = _find_raw_pair(p, stat, raws.get(p.stem.casefold(), []))
            if raw_pair:
                used_raws.add(raw_pair)

            img = ImageFile(path=p, raw_pair=raw_pair, timestamp=stat.st_mtime)
            image_entries.append((image_sort_key(img), img))

    # 2. Process Developed JPGs
    for p, stat, base_stem in developed_candidates:
        # Try to find base image in priority order: .jpg, .jpeg, .jpe
        effective_ts = stat.st_mtime
        effective_name = p.name.casefold()

        for ext in sorted(JPG_EXTENSIONS):
            candidate = (base_stem + ext).casefold()
            if candidate in base_map:
                base_ts, base_name = base_map[candidate]
                effective_ts = base_ts
                effective_name = base_name.casefold()
                break

        # Store effective_name so image_sort_key() reproduces the exact key
        # used here, even when the base file's extension differs from the
        # developed file's extension (e.g. base .jpeg, developed .jpg).
        img = ImageFile(
            path=p, raw_pair=None, timestamp=effective_ts,
            sort_name_cf=effective_name,
        )
        image_entries.append((image_sort_key(img), img))

    # 3. Handle orphaned RAWs
    for stem, raw_list in raws.items():
        for raw_path, raw_stat in raw_list:
            if raw_path not in used_raws:
                img = ImageFile(
                    path=raw_path, raw_pair=raw_path, timestamp=raw_stat.st_mtime
                )
                image_entries.append((image_sort_key(img), img))

    # Final Sort
    image_entries.sort(key=lambda x: x[0])
    image_files = [x[1] for x in image_entries]

    elapsed = time.perf_counter() - t_start
    paired_count = sum(
        1
        for im in image_files
        if im.raw_pair and im.path.suffix.lower() in JPG_EXTENSIONS
    )
    raw_only_count = sum(
        1 for im in image_files if im.path.suffix.lower() not in JPG_EXTENSIONS
    )

    if log.isEnabledFor(logging.DEBUG):
        log.info(
            "Found %d total, %d paired, %d raw-only in %.3fs",
            len(image_files),
            paired_count,
            raw_only_count,
            elapsed,
        )
    else:
        log.info(
            "Found %d images (%d paired, %d raw-only).",
            len(image_files),
            paired_count,
            raw_only_count,
        )
    return image_files


def _parse_developed(path: Path) -> Tuple[bool, str]:
    """
    Detect if a file is a developed image.
    Returns (is_developed, base_stem).

    Matches a trailing '-developed' on the filename stem, case-insensitive.
    Example: 'IMG_0001-developed.jpg' -> ('IMG_0001')
    """
    stem = path.stem
    stem_cf = stem.casefold()
    suf_cf = _DEVELOPED_SUFFIX.casefold()

    if stem_cf.endswith(suf_cf):
        base_stem = stem[: -len(_DEVELOPED_SUFFIX)]
        return True, base_stem

    return False, ""


def image_sort_key(img: ImageFile) -> Tuple[float, str, int, str]:
    """Return the canonical 4-tuple sort key for an ImageFile.

    Key structure: (timestamp, sort_name_cf, is_developed, own_name_cf)

    sort_name_cf controls adjacency: for developed images it equals the base
    image's name so the pair sorts together.  Priority:
      1. img.sort_name_cf — set by find_images() from the base_map lookup
         (handles extension mismatches like base .jpeg / developed .jpg).
      2. Reconstructed base name (base_stem + own extension) — best-effort
         fallback for developed ImageFiles created outside find_images().
      3. Own filename — used for all non-developed images.

    All code paths — find_images(), _reindex_after_save(), etc. — use
    this single function so the sort order is always consistent.
    """
    own_name_cf = img.path.name.casefold()
    is_dev, base_stem = _parse_developed(img.path)
    if img.sort_name_cf:
        sort_name_cf = img.sort_name_cf
    elif is_dev:
        # Best-effort adjacency for developed ImageFiles without sort_name_cf
        sort_name_cf = (base_stem + img.path.suffix).casefold()
    else:
        sort_name_cf = own_name_cf
    return (img.timestamp, sort_name_cf, int(is_dev), own_name_cf)


def _find_raw_pair(
    jpg_path: Path,
    jpg_stat: os.stat_result,
    potential_raws: List[Tuple[Path, os.stat_result]],
) -> Path | None:
    """Finds the best RAW pair for a JPG from a list of candidates."""
    if not potential_raws:
        return None

    # Find the RAW file with the closest modification time within a 2-second window
    best_match: Path | None = None
    min_dt = 2.0  # seconds

    for raw_path, raw_stat in potential_raws:
        dt = abs(jpg_stat.st_mtime - raw_stat.st_mtime)
        if dt <= min_dt:
            min_dt = dt
            best_match = raw_path

    return best_match
