"""Inspect and delete empty (or nearly empty) folders from the grid view.

A folder is offered for deletion only when it holds no images at all. Small
leftovers such as a ``faststack.json`` sidecar are tolerated: the folder is
reported as "almost empty" so the UI can list what is there and ask first.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from faststack.io.indexer import JPG_EXTENSIONS as _INDEXER_JPG_EXTENSIONS
from faststack.io.indexer import RAW_EXTENSIONS as _INDEXER_RAW_EXTENSIONS
from faststack.thumbnail_view.folder_stats import JPG_EXTENSIONS, RAW_EXTENSIONS

log = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset(
    ext.lower()
    for ext in (
        JPG_EXTENSIONS
        | RAW_EXTENSIONS
        | _INDEXER_JPG_EXTENSIONS
        | _INDEXER_RAW_EXTENSIONS
        | {".heic", ".heif", ".avif", ".jxl", ".psd", ".mp4", ".mov", ".avi"}
    )
)

# Anything bigger or busier than this is not "almost empty".
MAX_LEFTOVER_ENTRIES = 25
MAX_LEFTOVER_BYTES = 500 * 1024


@dataclass
class FolderDeleteInspection:
    """Result of checking whether a folder can be offered for deletion."""

    path: Path
    deletable: bool = False
    empty: bool = False
    # Relative paths of everything inside (dirs end with "/"), sorted.
    items: List[str] = field(default_factory=list)
    total_bytes: int = 0
    reason: str = ""

    def to_qml(self) -> dict:
        return {
            "path": str(self.path),
            "deletable": self.deletable,
            "empty": self.empty,
            "items": list(self.items),
            "totalBytes": self.total_bytes,
            "sizeText": format_size(self.total_bytes),
            "reason": self.reason,
        }


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def inspect_folder_for_delete(path: Path) -> FolderDeleteInspection:
    """Walk ``path`` (without following symlinks) and classify it."""
    result = FolderDeleteInspection(path=path)
    try:
        if path.is_symlink() or not path.is_dir():
            result.reason = "not a folder"
            return result
    except OSError as exc:
        result.reason = str(exc)
        return result

    items: List[str] = []
    total = 0
    stack = [path]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as it:
                for entry in it:
                    rel = Path(entry.path).relative_to(path).as_posix()
                    if entry.is_dir(follow_symlinks=False):
                        items.append(rel + "/")
                        stack.append(Path(entry.path))
                    else:
                        if Path(entry.name).suffix.lower() in _IMAGE_EXTENSIONS:
                            result.reason = "contains images"
                            return result
                        items.append(rel)
                        try:
                            total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                    if len(items) > MAX_LEFTOVER_ENTRIES:
                        result.reason = "too many items"
                        return result
                    if total > MAX_LEFTOVER_BYTES:
                        result.reason = "too much data"
                        return result
    except OSError as exc:
        log.warning("Could not inspect folder %s: %s", path, exc)
        result.reason = str(exc)
        return result

    items.sort()
    result.items = items
    result.total_bytes = total
    result.empty = not items
    result.deletable = True
    return result


def delete_inspected_folder(path: Path, expected_items: List[str]) -> str:
    """Delete ``path`` if it still matches what the user was shown.

    Returns "" on success, otherwise a user-facing error message.
    """
    current = inspect_folder_for_delete(path)
    if not current.deletable:
        return f"Folder is no longer empty ({current.reason})"
    if sorted(expected_items) != current.items:
        return "Folder contents changed; not deleted"
    try:
        # Remove only the entries that were inspected. If anything appears
        # after the check, one of the rmdir calls fails instead of recursively
        # deleting the unexpected content.
        for rel in reversed(current.items):
            target = path / rel.rstrip("/")
            if rel.endswith("/"):
                os.rmdir(target)
            else:
                os.unlink(target)
        os.rmdir(path)
    except OSError as exc:
        log.warning("Failed to delete folder %s: %s", path, exc)
        return f"Could not delete folder: {exc}"
    log.info("Deleted folder %s (%d leftover items)", path, len(current.items))
    return ""
