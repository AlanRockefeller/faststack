"""Utilities for filesystem operations, path normalization, and hashing."""

import errno
import functools
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)

_WINDOWS_DRIVE_PATH_RE = re.compile(r"^([A-Za-z]):[\\/]*(.*)$")


def scandir_unique(directory: Union[str, os.PathLike]) -> list[os.DirEntry]:
    """List a directory once, dropping entries the kernel returns twice.

    readdir may repeat entries when the directory changes during the walk.
    Linux's ntfs3 driver goes further: a file created or renamed mid-walk
    (FastStack's own atomic sidecar write is enough) makes it replay the
    whole directory, so every file used to appear two or more times in the
    image list. The first entry for a name wins.
    """
    seen: set[str] = set()
    unique: list[os.DirEntry] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name in seen:
                continue
            seen.add(entry.name)
            unique.append(entry)
    return unique


def resolve_configured_path(value: str) -> Path:
    """Turn a user-configured directory string into a usable local Path.

    Strips quotes/whitespace and expands ``~`` and environment variables. On
    Linux (WSL, or a dual-boot machine sharing its config with Windows), a
    Windows drive path such as ``C:\\Users\\me\\Pictures`` can never resolve
    as written, so it is mapped to the conventional mount point
    ``/mnt/c/Users/me/Pictures``.
    """
    cleaned = os.path.expanduser(os.path.expandvars(str(value).strip().strip('"')))
    if os.name != "nt":
        match = _WINDOWS_DRIVE_PATH_RE.match(cleaned)
        if match:
            rest = match.group(2).replace("\\", "/").strip("/")
            mounted = Path("/mnt") / match.group(1).lower()
            return mounted / rest if rest else mounted
    return Path(cleaned)


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


# fsync errnos that mean "this filesystem cannot sync", not "the data was
# lost": some network/FUSE filesystems reject fsync outright.
_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if code is not None
)


def fsync_file(path: Path, *, strict: bool = False) -> None:
    """Flush an already-written file's data to stable storage.

    Used before an atomic ``os.replace`` so a power loss or card pull cannot
    leave the destination name pointing at a truncated file. A filesystem
    that does not support fsync is tolerated (logged). Any other failure --
    a real write-back error such as EIO or ENOSPC -- is raised when
    ``strict`` is set, so the caller does not commit data that never reached
    the disk; otherwise it is logged and ignored.
    """
    # Windows' FlushFileBuffers needs a handle opened with write access.
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        os.fsync(fd)
    except OSError as e:
        if strict and e.errno not in _FSYNC_UNSUPPORTED_ERRNOS:
            raise
        # In strict mode the caller is about to commit without durability;
        # say so where the user can see it.
        log.log(
            logging.WARNING if strict else logging.DEBUG,
            "Failed to fsync file %s: %s",
            path,
            e,
        )
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON atomically and flush the file and directory to disk."""
    atomic_write_text(path, json.dumps(payload, indent=2))


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically and flush the file and directory to disk."""
    temp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        temp_path.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            log.warning(
                "Could not remove failed atomic-write temporary file %s: %s",
                temp_path,
                cleanup_error,
            )
        raise


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


def _hash_path_uncached(path_str: str) -> str:
    # normalize_path_key handles the canonicalization pure-string wise
    norm_path = normalize_path_key(path_str)

    # MD5 is used for ID generation, not security.
    # It must map the same path to the same ID across app restarts.
    return hashlib.md5(norm_path.encode("utf-8")).hexdigest()[:16]  # noqa: S324


# The thumbnail grid asks for every visible row's hash on each repaint, so
# repeat lookups are served from memory. Bounded so a long session browsing
# many folders cannot grow it without limit.
_hash_path_cached = functools.lru_cache(maxsize=65536)(_hash_path_uncached)


def compute_path_hash(path: Union[Path, str]) -> str:
    """Compute a fast, stable hash of the path for UI/Thumbnail IDs.

    Uses MD5 of the normalized path string.
    CRITICAL: Does NOT access the filesystem (no .resolve() calls).
    """
    path_str = os.fspath(path)
    if os.path.isabs(path_str):
        return _hash_path_cached(path_str)
    # A relative path's normalized form depends on the working directory.
    return _hash_path_uncached(path_str)
