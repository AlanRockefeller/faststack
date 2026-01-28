"""Scans directories for JPGs and pairs them with corresponding RAW files."""

import logging
import os
import time
from pathlib import Path
from typing import List, Dict, Tuple

from faststack.models import ImageFile

log = logging.getLogger(__name__)

RAW_EXTENSIONS = {
    ".ORF", ".RW2", ".CR2", ".CR3", ".ARW", ".NEF", ".RAF", ".DNG",
    ".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng",
}

JPG_EXTENSIONS = { ".JPG", ".JPEG", ".jpg", ".jpeg" }

def find_images(directory: Path) -> List[ImageFile]:
    """Finds all JPGs in a directory and pairs them with RAW files."""
    t_start = time.perf_counter()
    log.info("Scanning directory for images: %s", directory)
    
    # Categorize files
    all_jpgs: List[Tuple[Path, os.stat_result]] = []
    raws: Dict[str, List[Tuple[Path, os.stat_result]]] = {}
    
    try:
        for entry in os.scandir(directory):
            if entry.is_file():
                p = Path(entry.path)
                ext = p.suffix
                if ext in JPG_EXTENSIONS:
                    all_jpgs.append((p, entry.stat()))
                elif ext in RAW_EXTENSIONS:
                    stem = p.stem
                    if stem not in raws:
                        raws[stem] = []
                    raws[stem].append((p, entry.stat()))
    except OSError as e:
        log.exception("Error scanning directory %s", directory)
        return []

    # Separate developed JPGs, build base map, and process normal JPGs
    # base_map: filename.casefold() -> (mtime, name)
    base_map: Dict[str, Tuple[float, str]] = {}
    developed_candidates: List[Tuple[Path, os.stat_result, str]] = [] # path, stat, base_stem
    
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
            raw_pair = _find_raw_pair(p, stat, raws.get(p.stem, []))
            if raw_pair:
                used_raws.add(raw_pair)
            
            img = ImageFile(path=p, raw_pair=raw_pair, timestamp=stat.st_mtime)
            image_entries.append(((stat.st_mtime, p.name.casefold(), 0, p.name.casefold()), img))

    # 2. Process Developed JPGs
    for p, stat, base_stem in developed_candidates:
        # Try to find base image in priority order: .jpg, .jpeg, .jpe
        effective_ts = stat.st_mtime
        effective_name = p.name.casefold()
        
        for ext in [".jpg", ".jpeg", ".jpe"]:
            candidate = (base_stem + ext).casefold()
            if candidate in base_map:
                base_ts, base_name = base_map[candidate]
                effective_ts = base_ts
                effective_name = base_name.casefold()
                break
        
        img = ImageFile(path=p, raw_pair=None, timestamp=stat.st_mtime)
        image_entries.append(((effective_ts, effective_name, 1, p.name.casefold()), img))

    # 3. Handle orphaned RAWs
    for stem, raw_list in raws.items():
        for raw_path, raw_stat in raw_list:
            if raw_path not in used_raws:
                img = ImageFile(path=raw_path, raw_pair=raw_path, timestamp=raw_stat.st_mtime)
                image_entries.append(((raw_stat.st_mtime, raw_path.name.casefold(), 0, raw_path.name.casefold()), img))

    # Final Sort
    image_entries.sort(key=lambda x: x[0])
    image_files = [x[1] for x in image_entries]

    elapsed = time.perf_counter() - t_start
    paired_count = sum(1 for im in image_files if im.raw_pair and im.path.suffix.lower() in JPG_EXTENSIONS)
    raw_only_count = sum(1 for im in image_files if im.path.suffix.lower() not in JPG_EXTENSIONS)
    
    if log.isEnabledFor(logging.DEBUG):
        log.info("Found %d total, %d paired, %d raw-only in %.3fs", 
                 len(image_files), paired_count, raw_only_count, elapsed)
    else:
        log.info("Found %d images (%d paired, %d raw-only).", len(image_files), paired_count, raw_only_count)
    return image_files

def _parse_developed(path: Path) -> Tuple[bool, str]:
    """
    Detects if a file is a developed image.
    Returns (is_developed, base_stem).
    Suffix match for '-developed' is case-insensitive.
    """
    stem = path.stem
    if stem.lower().endswith("-developed"):
        base_stem = stem[:-10] # Remove "-developed"
        return True, base_stem
    return False, ""

def _find_raw_pair(
    jpg_path: Path,
    jpg_stat: os.stat_result,
    potential_raws: List[Tuple[Path, os.stat_result]]
) -> Path | None:
    """Finds the best RAW pair for a JPG from a list of candidates."""
    if not potential_raws:
        return None

    # Find the RAW file with the closest modification time within a 2-second window
    best_match: Path | None = None
    min_dt = 2.0 # seconds

    for raw_path, raw_stat in potential_raws:
        dt = abs(jpg_stat.st_mtime - raw_stat.st_mtime)
        if dt <= min_dt:
            min_dt = dt
            best_match = raw_path

    return best_match
