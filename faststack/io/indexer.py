"""Scans directories for JPGs and pairs them with corresponding RAW files."""

import logging
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Tuple

from faststack.models import ImageFile
from faststack.io.variants import VariantGroup, build_variant_map, parse_variant_stem

log = logging.getLogger(__name__)

RAW_EXTENSIONS = {".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng"}

JPG_EXTENSIONS = {".jpg", ".jpeg", ".jpe"}

# Matches FastStack backup stems: name-backup, name-backup2, name-backup33, etc.
_BACKUP_STEM_RE = re.compile(r"-backup\d*$", re.IGNORECASE)


def _scan_directory(
    directory: Path,
) -> Tuple[
    List[Tuple[Path, os.stat_result]],
    List[Tuple[Path, os.stat_result]],
    Dict[str, List[Tuple[Path, os.stat_result]]],
]:
    """Single os.scandir pass, returns (visible_jpgs, all_jpgs, raws).

    visible_jpgs: JPGs excluding backups (legacy behavior).
    all_jpgs: ALL JPGs including backups (for variant grouping).
    raws: keyed by stem.casefold().
    """
    visible_jpgs: List[Tuple[Path, os.stat_result]] = []
    all_jpgs: List[Tuple[Path, os.stat_result]] = []
    raws: Dict[str, List[Tuple[Path, os.stat_result]]] = {}

    for entry in os.scandir(directory):
        if entry.is_file():
            p = Path(entry.path)
            ext = p.suffix.lower()
            if ext in JPG_EXTENSIONS:
                stat = entry.stat()
                all_jpgs.append((p, stat))
                if not _BACKUP_STEM_RE.search(p.stem):
                    visible_jpgs.append((p, stat))
            elif ext in RAW_EXTENSIONS:
                stem = p.stem.casefold()
                if stem not in raws:
                    raws[stem] = []
                raws[stem].append((p, entry.stat()))

    return visible_jpgs, all_jpgs, raws


def _build_image_list(
    visible_jpgs: List[Tuple[Path, os.stat_result]],
    raws: Dict[str, List[Tuple[Path, os.stat_result]]],
) -> List[ImageFile]:
    """Build sorted image list from visible JPGs and RAWs."""
    base_map: Dict[str, Tuple[float, str]] = {}
    developed_candidates: List[Tuple[Path, os.stat_result, str]] = []
    image_entries: List[Tuple[Tuple[float, str, int, str], ImageFile]] = []
    used_raws = set()

    for p, stat in visible_jpgs:
        is_dev, base_stem = _parse_developed(p)
        if is_dev:
            developed_candidates.append((p, stat, base_stem))
        else:
            base_map[p.name.casefold()] = (stat.st_mtime, p.name)
            raw_pair = _find_raw_pair(p, stat, raws.get(p.stem.casefold(), []))
            if raw_pair:
                used_raws.add(raw_pair)
            img = ImageFile(path=p, raw_pair=raw_pair, timestamp=stat.st_mtime)
            image_entries.append((image_sort_key(img), img))

    for p, stat, base_stem in developed_candidates:
        effective_ts = stat.st_mtime
        effective_name = p.name.casefold()
        for ext in sorted(JPG_EXTENSIONS):
            candidate = (base_stem + ext).casefold()
            if candidate in base_map:
                base_ts, base_name = base_map[candidate]
                effective_ts = base_ts
                effective_name = base_name.casefold()
                break
        img = ImageFile(
            path=p, raw_pair=None, timestamp=effective_ts,
            sort_name_cf=effective_name,
        )
        image_entries.append((image_sort_key(img), img))

    for stem, raw_list in raws.items():
        for raw_path, raw_stat in raw_list:
            if raw_path not in used_raws:
                img = ImageFile(
                    path=raw_path, raw_pair=raw_path, timestamp=raw_stat.st_mtime
                )
                image_entries.append((image_sort_key(img), img))

    image_entries.sort(key=lambda x: x[0])
    return [x[1] for x in image_entries]


def find_images(directory: Path) -> List[ImageFile]:
    """Finds all JPGs in a directory and pairs them with RAW files.

    Backward-compatible: does NOT filter developed files or annotate variant flags.
    For variant-aware loading, use find_images_with_variants() instead.
    """
    t_start = time.perf_counter()
    log.info("Scanning directory for images: %s", directory)

    try:
        visible_jpgs, _, raws = _scan_directory(directory)
    except OSError:
        log.exception("Error scanning directory %s", directory)
        return []

    image_files = _build_image_list(visible_jpgs, raws)

    elapsed = time.perf_counter() - t_start
    paired_count = sum(
        1 for im in image_files
        if im.raw_pair and im.path.suffix.lower() in JPG_EXTENSIONS
    )
    raw_only_count = sum(
        1 for im in image_files if im.path.suffix.lower() not in JPG_EXTENSIONS
    )
    log.info(
        "Found %d images (%d paired, %d raw-only).",
        len(image_files), paired_count, raw_only_count,
    )
    return image_files


def find_images_with_variants(
    directory: Path,
) -> Tuple[List[ImageFile], Dict[str, VariantGroup]]:
    """Finds images and builds variant map in a single scan.

    Returns:
        (visible_image_list, variant_map) where variant_map is keyed by
        group_key.casefold().
    """
    t_start = time.perf_counter()
    log.info("Scanning directory for images: %s", directory)

    try:
        visible_jpgs, all_jpgs, raws = _scan_directory(directory)
    except OSError:
        log.exception("Error scanning directory %s", directory)
        return [], {}

    # Build the visible image list (legacy behavior)
    image_files = _build_image_list(visible_jpgs, raws)

    # Build variant map from ALL jpgs (including backups)
    all_jpg_paths = [p for p, _ in all_jpgs]
    variant_map = build_variant_map(all_jpg_paths)

    # Filter visible list: keep only entries whose path equals their group's main_path.
    # This removes developed files that are reachable via badges while keeping
    # orphan developed files that ARE their group's main.
    filtered = []
    for img in image_files:
        ext = img.path.suffix.lower()
        if ext not in JPG_EXTENSIONS:
            # RAW-only: always keep
            filtered.append(img)
            continue

        group_key, _, _ = parse_variant_stem(img.path.stem)
        key_cf = group_key.casefold()
        group = variant_map.get(key_cf)
        if group is None or len(group.all_files) <= 1:
            # No variant group or singleton: keep as-is
            filtered.append(img)
        elif group.main_path == img.path:
            # This IS the main: keep it
            filtered.append(img)
        else:
            # This is a developed file reachable via badge: remove from visible list
            log.debug("Filtering out variant %s (main=%s)", img.path.name, group.main_path.name if group.main_path else "?")

    # Annotate images with variant flags
    for img in filtered:
        ext = img.path.suffix.lower()
        if ext not in JPG_EXTENSIONS:
            continue
        group_key, _, _ = parse_variant_stem(img.path.stem)
        key_cf = group_key.casefold()
        group = variant_map.get(key_cf)
        if group:
            img.has_backups = bool(group.backup_paths)
            img.has_developed = group.developed_path is not None and group.developed_path != group.main_path

    image_files = filtered

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
    return image_files, variant_map


def _parse_developed(path: Path) -> Tuple[bool, str]:
    """
    Detect if a file is a developed image.
    Returns (is_developed, base_stem).

    Uses the robust parse_variant_stem from variants.py.
    """
    group_key, is_developed, _ = parse_variant_stem(path.stem)
    if is_developed:
        return True, group_key
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
