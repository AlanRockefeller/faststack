"""Utilities for filesystem operations, path normalization, and hashing."""

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)


def fsync_directory(path: Path) -> None:
    """Best-effort fsync for a directory entry update."""
    if os.name == "nt":
        return

    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY)
        os.fsync(fd)
    except OSError as e:
        log.debug("Failed to fsync directory %s: %s", path, e)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON atomically and flush the file and directory to disk."""
    temp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    with temp_path.open("w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(path)
    fsync_directory(path.parent)


def normalize_path_key(path: Union[Path, str]) -> str:
    """Normalize a path for use as a stable dictionary key.

    Handles Windows case-insensitivity by case-folding, and standardizes separators.
    This is critical for ensuring that paths from scanners match paths from resolved logic.
    """
    # str(path) converts Path to string using native separators (e.g. \ on Windows)
    p_str = str(path)
    # normalize separators to current OS standard first (handles mixed usage)
    p_str = p_str.replace("/", os.sep).replace("\\", os.sep)
    # os.path.normcase on Windows: lowercases and converts / to \
    # os.path.normcase on Linux: returns as-is
    # os.path.abspath: ensures absolute path and collapses ..
    # Note: abspath does NOT resolve symlinks (that's realpath).
    return os.path.normcase(os.path.abspath(p_str)).replace("\\", "/")


def compute_path_hash(path: Union[Path, str]) -> str:
    """Compute a fast, stable hash of the path for UI/Thumbnail IDs.

    Uses MD5 of the normalized path string.
    CRITICAL: Does NOT access the filesystem (no .resolve() calls).
    """
    # normalize_path_key handles the canonicalization pure-string wise
    norm_path = normalize_path_key(path)

    # MD5 is used for ID generation, not security.
    # It must map the same path to the same ID across app restarts.
    return hashlib.md5(norm_path.encode("utf-8")).hexdigest()[:16]  # noqa: S324
