"""Parse faststack.json for folder statistics display in thumbnail grid."""

import json
import logging
from dataclasses import dataclass
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


# Cache by (folder_path, json_mtime_ns) to avoid re-parsing during scroll
# IMPORTANT: json_mtime_ns = stat(folder_path / "faststack.json").st_mtime_ns
# NOT the folder's mtime (folder mtime changes when any file is added/removed)
_stats_cache: Dict[Tuple[Path, int], Optional[FolderStats]] = {}


def read_folder_stats(folder_path: Path) -> Optional[FolderStats]:
    """Parse faststack.json in folder. Stat the json file for mtime_ns. Tolerant to errors.

    Args:
        folder_path: Path to the folder containing faststack.json

    Returns:
        FolderStats if valid faststack.json exists, None otherwise.
        Caches results by (folder_path, json_mtime_ns) to avoid re-parsing.
    """
    json_path = folder_path / SIDECAR_FILENAME

    # Check if file exists
    try:
        stat_info = json_path.stat()
        mtime_ns = stat_info.st_mtime_ns
    except (OSError, FileNotFoundError):
        # No faststack.json in this folder
        return None

    # Check cache
    cache_key = (folder_path.resolve(), mtime_ns)
    if cache_key in _stats_cache:
        return _stats_cache[cache_key]

    # Parse the JSON file
    stats = _parse_faststack_json(json_path)

    # Cache the result (even if None)
    _stats_cache[cache_key] = stats

    return stats


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

    return FolderStats(
        total_images=total_images,
        stacked_count=stacked_count,
        uploaded_count=uploaded_count,
        edited_count=edited_count,
    )


def clear_stats_cache():
    """Clear the folder stats cache."""
    global _stats_cache
    _stats_cache.clear()
    log.debug("Cleared folder stats cache")
