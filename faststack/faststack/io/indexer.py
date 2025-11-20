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
    log.info(f"Scanning directory for images: {directory}")
    jpgs: List[Tuple[Path, os.stat_result]] = []
    raws: Dict[str, List[Tuple[Path, os.stat_result]]] = {}

    try:
        for entry in os.scandir(directory):
            if entry.is_file():
                p = Path(entry.path)
                ext = p.suffix
                if ext in JPG_EXTENSIONS:
                    jpgs.append((p, entry.stat()))
                elif ext in RAW_EXTENSIONS:
                    stem = p.stem
                    if stem not in raws:
                        raws[stem] = []
                    raws[stem].append((p, entry.stat()))
    except OSError as e:
        log.error(f"Error scanning directory {directory}: {e}")
        return []

    # Sort JPGs by filename
    jpgs.sort(key=lambda x: x[0].name)

    image_files: List[ImageFile] = []
    for jpg_path, jpg_stat in jpgs:
        raw_pair = _find_raw_pair(jpg_path, jpg_stat, raws.get(jpg_path.stem, []))
        image_files.append(ImageFile(
            path=jpg_path,
            raw_pair=raw_pair,
            timestamp=jpg_stat.st_mtime,
        ))

    elapsed = time.perf_counter() - t_start
    # Import debug flag from app module
    from faststack.app import _debug_mode
    if _debug_mode:
        log.info(f"find_images: found {len(image_files)} images in {elapsed:.3f}s")
    log.info(f"Found {len(image_files)} JPG files and paired {sum(1 for im in image_files if im.raw_pair)} with RAWs.")
    return image_files

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
        if dt < min_dt:
            min_dt = dt
            best_match = raw_path

    if best_match:
        log.debug(f"Paired {jpg_path.name} with {best_match.name} (dt={min_dt:.3f}s)")
    else:
        log.debug(f"No close RAW match found for {jpg_path.name}")

    return best_match
