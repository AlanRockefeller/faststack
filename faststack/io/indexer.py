"""Scans directories for JPGs and pairs them with corresponding RAW files."""

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

from faststack.io.variants import (
    VariantGroup,
    build_variant_map,
    norm_path,
    parse_variant_stem,
)
from faststack.models import ImageFile

log = logging.getLogger(__name__)

RAW_EXTENSIONS = {".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng"}

JPG_EXTENSIONS = {".jpg", ".jpeg", ".jpe"}
_BASE_SUFFIX_PREFERENCE = (".jpg", ".jpeg", ".jpe")


def _scan_directory(
    directory: Path,
    *,
    include_visible_jpgs: bool = True,
) -> Tuple[
    List[Tuple[Path, os.stat_result]],
    List[Tuple[Path, os.stat_result]],
    Dict[str, List[Tuple[Path, os.stat_result]]],
]:
    """Single os.scandir pass, returns (visible_jpgs, all_jpgs, raws).

    visible_jpgs: JPGs excluding backups when include_visible_jpgs is True.
    all_jpgs: ALL JPGs including backups (for variant grouping).
    raws: keyed by stem.casefold().
    """
    visible_jpgs: List[Tuple[Path, os.stat_result]] = []
    all_jpgs: List[Tuple[Path, os.stat_result]] = []
    raws: Dict[str, List[Tuple[Path, os.stat_result]]] = {}

    with os.scandir(directory) as entries:
        for entry in entries:
            try:
                if not entry.is_file():
                    continue

                p = Path(entry.path)
                ext = p.suffix.lower()
                if ext in JPG_EXTENSIONS:
                    stat = entry.stat()
                    all_jpgs.append((p, stat))
                    if include_visible_jpgs and not _is_backup_variant(p):
                        visible_jpgs.append((p, stat))
                elif ext in RAW_EXTENSIONS:
                    stat = entry.stat()
                    raws.setdefault(p.stem.casefold(), []).append((p, stat))
            except OSError as exc:
                log.debug(
                    "Skipping unreadable or vanished directory entry %s: %s",
                    entry.path,
                    exc,
                )

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
            # For casefold-colliding base names, keep the first timestamp/sort
            # source for developed files; base files are already added above.
            base_map.setdefault(p.name.casefold(), (stat.st_mtime, p.name))
            raw_pair = _find_raw_pair(p, stat, raws.get(p.stem.casefold(), []))
            if raw_pair:
                used_raws.add(raw_pair)
            img = ImageFile(path=p, raw_pair=raw_pair, timestamp=stat.st_mtime)
            image_entries.append((image_sort_key(img), img))

    for p, stat, base_stem in developed_candidates:
        effective_ts = stat.st_mtime
        effective_name = p.name.casefold()
        for candidate in _base_name_candidates(base_stem, p.suffix):
            if candidate in base_map:
                base_ts, base_name = base_map[candidate]
                effective_ts = base_ts
                effective_name = base_name.casefold()
                break
        img = ImageFile(
            path=p,
            raw_pair=None,
            timestamp=effective_ts,
            sort_name_cf=effective_name,
        )
        image_entries.append((image_sort_key(img), img))

    for raw_list in raws.values():
        for raw_path, raw_stat in raw_list:
            if raw_path not in used_raws:
                img = ImageFile(
                    path=raw_path, raw_pair=raw_path, timestamp=raw_stat.st_mtime
                )
                image_entries.append((image_sort_key(img), img))

    image_entries.sort(key=lambda x: x[0])
    return [x[1] for x in image_entries]


def _is_backup_variant(path: Path) -> bool:
    """Return True when the shared variant parser classifies a path as backup."""
    _, _, backup_number = parse_variant_stem(path.stem)
    return backup_number is not None


def _base_name_candidates(base_stem: str, developed_suffix: str) -> List[str]:
    """Return base filename candidates, preferring the developed file's suffix."""
    candidates = []
    seen = set()
    for ext in (developed_suffix.lower(), *_BASE_SUFFIX_PREFERENCE):
        if ext not in JPG_EXTENSIONS or ext in seen:
            continue
        seen.add(ext)
        candidates.append((base_stem + ext).casefold())
    return candidates


def _variant_groups_by_path(
    variant_map: Dict[str, VariantGroup],
) -> Dict[Path, VariantGroup]:
    groups_by_path = {}
    for group in variant_map.values():
        for info in group.all_files:
            groups_by_path[info.path] = group
    return groups_by_path


def _is_hidden_variant_path(path: Path, group: VariantGroup) -> bool:
    if path == group.main_path:
        return False
    return path == group.developed_path or path in group.backup_paths.values()


def _log_scan_result(image_files: List[ImageFile], elapsed: float) -> None:
    paired_count = sum(
        1
        for im in image_files
        if im.raw_pair and im.path.suffix.lower() in JPG_EXTENSIONS
    )
    raw_only_count = sum(
        1 for im in image_files if im.path.suffix.lower() not in JPG_EXTENSIONS
    )
    log.info(
        "Found %d images (%d paired, %d raw-only) in %.2fs.",
        len(image_files),
        paired_count,
        raw_only_count,
        elapsed,
    )


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
    _log_scan_result(image_files, elapsed)
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
        _, all_jpgs, raws = _scan_directory(directory, include_visible_jpgs=False)
    except OSError:
        log.exception("Error scanning directory %s", directory)
        return [], {}

    # Build variant map from ALL jpgs (including backups)
    all_jpg_paths = [p for p, _ in all_jpgs]
    variant_map = build_variant_map(all_jpg_paths)
    groups_by_path = _variant_groups_by_path(variant_map)

    # Hide only files that are actually exposed by a group's variant badges.
    # Same-stem JPGs that are not selected as developed/backups stay visible.
    visible_variant_jpgs = []
    for p, stat in all_jpgs:
        p_norm = Path(norm_path(p))
        group = groups_by_path.get(p_norm)
        if group and _is_hidden_variant_path(p_norm, group):
            log.debug(
                "Filtering out variant %s (main=%s)",
                p.name,
                group.main_path.name if group.main_path else "?",
            )
            continue
        visible_variant_jpgs.append((p, stat))

    image_files = _build_image_list(visible_variant_jpgs, raws)

    for img in image_files:
        ext = img.path.suffix.lower()
        if ext not in JPG_EXTENSIONS:
            continue
        group = groups_by_path.get(Path(norm_path(img.path)))
        if group:
            img.has_backups = bool(group.backup_paths)
            img.has_developed = (
                group.developed_path is not None
                and group.developed_path != group.main_path
            )

    elapsed = time.perf_counter() - t_start
    _log_scan_result(image_files, elapsed)
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

    if len(potential_raws) == 1:
        return potential_raws[0][0]

    return min(
        potential_raws,
        key=lambda candidate: abs(jpg_stat.st_mtime - candidate[1].st_mtime),
    )[0]
