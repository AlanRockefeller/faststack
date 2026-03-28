# faststack/thumbnail_view/model.py
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from faststack.io.indexer import find_images
from faststack.io.utils import compute_path_hash


def _is_filesystem_root(path: Path) -> bool:
    """
    True if `path` is a filesystem root.

    Supports:
      - Unix: /
      - Windows drive roots: C:\\
      - UNC share roots: \\\\server\\share
    """
    try:
        p = path.resolve()
    except Exception:
        p = path

    # Unix root: "/" -> parent is itself
    if sys.platform != "win32":
        return p.parent == p

    # Windows handling
    s = str(p)

    # UNC roots: \\server\share
    if s.startswith("\\\\"):
        # Normalize separators
        parts = [x for x in s.strip("\\").split("\\") if x]
        # UNC share root has exactly 2 parts: server, share
        return len(parts) == 2

    # Drive roots: C:\  (parent is itself)
    try:
        return p.parent == p
    except Exception:
        return False


@dataclass
class ThumbnailEntry:
    path: Path
    name: str
    is_folder: bool

    # flag-like state (from metadata)
    is_stacked: bool = False
    is_uploaded: bool = False
    is_edited: bool = False
    is_restacked: bool = False
    is_favorite: bool = False

    # file time / thumb invalidation
    mtime_ns: int = 0
    thumb_rev: int = 0

    # selection
    is_selected: bool = False

    # convenience for QML path/url usage
    @property
    def file_path(self) -> str:
        return str(self.path)


class ThumbnailModel(QAbstractListModel):
    """
    A lightweight QAbstractListModel backing the thumbnail grid.

    Key behaviors tested:
      - refresh() populates entries
      - folders come before images
      - ".." is shown unless at filesystem root
      - selection logic (ctrl/shift)
      - navigation is confined to base_directory
      - text + flag filters apply as AND logic
      - get_metadata_callback may return dict OR EntryMetadata-like object
    """

    FILE_PATH_ROLE = int(Qt.UserRole) + 1
    FILE_NAME_ROLE = int(Qt.UserRole) + 2
    IS_FOLDER_ROLE = int(Qt.UserRole) + 3
    IS_STACKED_ROLE = int(Qt.UserRole) + 4
    IS_UPLOADED_ROLE = int(Qt.UserRole) + 5
    IS_EDITED_ROLE = int(Qt.UserRole) + 6
    THUMBNAIL_SOURCE_ROLE = int(Qt.UserRole) + 7
    IS_SELECTED_ROLE = int(Qt.UserRole) + 8
    IS_FAVORITE_ROLE = int(Qt.UserRole) + 9

    def __init__(
        self,
        base_directory: Path,
        current_directory: Path,
        get_metadata_callback: Optional[Callable[[str], Any]],
        thumbnail_size: int = 200,
        parent=None,
    ):
        super().__init__(parent)
        self.base_directory = Path(base_directory).resolve()
        self.current_directory = Path(current_directory).resolve()
        self._get_metadata = get_metadata_callback
        self.thumbnail_size = int(thumbnail_size)

        self._entries: list[ThumbnailEntry] = []
        self._selected_paths: set[Path] = set()
        self._last_selected_index: Optional[int] = None

        # Text filter (substring match on filename)
        self._active_filter: str = ""

        # Flag filters (AND logic)
        self._filter_flags: list[str] = []

    # -------------------------
    # Qt Model API
    # -------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def roleNames(self) -> dict[int, bytes]:
        return {
            self.FILE_PATH_ROLE: b"filePath",
            self.FILE_NAME_ROLE: b"fileName",
            self.IS_FOLDER_ROLE: b"isFolder",
            self.IS_STACKED_ROLE: b"isStacked",
            self.IS_UPLOADED_ROLE: b"isUploaded",
            self.IS_EDITED_ROLE: b"isEdited",
            self.THUMBNAIL_SOURCE_ROLE: b"thumbnailSource",
            self.IS_SELECTED_ROLE: b"isSelected",
            self.IS_FAVORITE_ROLE: b"isFavorite",
        }

    def data(self, index: QModelIndex, role: int = int(Qt.DisplayRole)) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._entries):
            return None

        e = self._entries[row]
        if role in (int(Qt.DisplayRole), self.FILE_NAME_ROLE):
            return e.name
        if role == self.FILE_PATH_ROLE:
            return e.file_path
        if role == self.IS_FOLDER_ROLE:
            return e.is_folder
        if role == self.IS_STACKED_ROLE:
            return e.is_stacked
        if role == self.IS_UPLOADED_ROLE:
            return e.is_uploaded
        if role == self.IS_EDITED_ROLE:
            return e.is_edited
        if role == self.IS_SELECTED_ROLE:
            return e.is_selected
        if role == self.IS_FAVORITE_ROLE:
            return e.is_favorite
        if role == self.THUMBNAIL_SOURCE_ROLE:
            # QML image provider typically keys off a hash + mtime + rev.
            # Keep it stable and deterministic for caching.
            h = compute_path_hash(e.path)
            return f"image://thumb/{h}/{e.mtime_ns}/{e.thumb_rev}"
        return None

    # -------------------------
    # Helpers
    # -------------------------
    def get_entry(self, idx: int) -> Optional[ThumbnailEntry]:
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def _normalize_meta_flags(self, meta: Any) -> dict[str, bool]:
        """
        Normalize metadata from SidecarManager into booleans.

        Supports both:
          - dict-style metadata (older tests/callers)
          - EntryMetadata-like objects (newer code)
        """
        if meta is None:
            return {
                "uploaded": False,
                "stacked": False,
                "edited": False,
                "restacked": False,
                "favorite": False,
            }

        # Dict-style (tests use this)
        if isinstance(meta, Mapping):
            return {
                "uploaded": bool(meta.get("uploaded", False)),
                "stacked": bool(meta.get("stacked", False)),
                "edited": bool(meta.get("edited", False)),
                "restacked": bool(meta.get("restacked", False)),
                "favorite": bool(meta.get("favorite", False)),
            }

        # Object-style (EntryMetadata)
        stack_id = getattr(meta, "stack_id", None)
        stacked_attr = bool(getattr(meta, "stacked", False))
        return {
            "uploaded": bool(getattr(meta, "uploaded", False)),
            "stacked": stacked_attr or (stack_id is not None),
            "edited": bool(getattr(meta, "edited", False)),
            "restacked": bool(getattr(meta, "restacked", False)),
            "favorite": bool(getattr(meta, "favorite", False)),
        }

    def _passes_text_filter(self, name: str) -> bool:
        f = (self._active_filter or "").strip().lower()
        if not f:
            return True
        return f in name.lower()

    def _passes_flag_filter(self, flags: dict[str, bool]) -> bool:
        if not self._filter_flags:
            return True
        for f in self._filter_flags:
            if not flags.get(f, False):
                return False
        return True

    # -------------------------
    # Public API used by tests
    # -------------------------
    def refresh(self) -> None:
        """
        Rebuild the entries list based on filesystem + filters.
        """
        cur = self.current_directory.resolve()

        folders: list[ThumbnailEntry] = []
        files: list[ThumbnailEntry] = []

        # Parent folder entry: shown unless at filesystem root.
        # (Note: navigating outside base is blocked by navigate_to.)
        if not _is_filesystem_root(cur):
            folders.append(
                ThumbnailEntry(
                    path=cur.parent,
                    name="..",
                    is_folder=True,
                    mtime_ns=0,
                )
            )

        # Subdirectories
        try:
            for p in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
                if p.is_dir():
                    folders.append(
                        ThumbnailEntry(
                            path=p,
                            name=p.name,
                            is_folder=True,
                            mtime_ns=self._safe_mtime_ns(p),
                        )
                    )
        except FileNotFoundError:
            # Directory disappeared; keep model empty-ish
            pass

        # Images (from indexer)
        try:
            image_files = find_images(cur)
        except Exception:
            image_files = []

        for img in image_files:
            p = Path(img.path).resolve() if getattr(img, "path", None) else None
            if p is None:
                continue

            name = p.name

            # text filter
            if not self._passes_text_filter(name):
                continue

            meta = self._get_metadata(p.stem) if self._get_metadata else None
            mflags = self._normalize_meta_flags(meta)

            # flag filter (AND)
            if not self._passes_flag_filter(mflags):
                continue

            mtime_ns = self._safe_mtime_ns(p)
            entry = ThumbnailEntry(
                path=p,
                name=name,
                is_folder=False,
                is_stacked=mflags["stacked"],
                is_uploaded=mflags["uploaded"],
                is_edited=mflags["edited"],
                is_restacked=mflags["restacked"],
                is_favorite=mflags["favorite"],
                mtime_ns=mtime_ns,
            )
            entry.is_selected = p in self._selected_paths
            files.append(entry)

        # Folders first, then files
        new_entries = folders + files

        self.beginResetModel()
        self._entries = new_entries
        self.endResetModel()

    def _safe_mtime_ns(self, p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except Exception:
            return 0

    def set_filter(self, text: str) -> None:
        self._active_filter = text or ""
        self.refresh()

    def set_filter_flags(self, flags: list[str]) -> None:
        # Normalize and keep order stable
        self._filter_flags = [str(f) for f in (flags or []) if str(f)]
        # Tests expect this to take effect immediately
        self.refresh()

    def navigate_to(self, new_directory: Path) -> None:
        """
        Navigate to new_directory if it is within base_directory (inclusive).
        """
        target = Path(new_directory).resolve()
        base = self.base_directory.resolve()

        # Confine to base
        try:
            target.relative_to(base)
            allowed = True
        except Exception:
            allowed = target == base

        if not allowed:
            # Stay where we are
            self.current_directory = base
        else:
            self.current_directory = target

        self.clear_selection()
        self.refresh()

    # -------------------------
    # Selection
    # -------------------------
    def get_selected_paths(self) -> list[Path]:
        return sorted(self._selected_paths)

    def clear_selection(self) -> None:
        self._selected_paths.clear()
        self._last_selected_index = None
        for e in self._entries:
            e.is_selected = False
        if self._entries:
            # emit a cheap reset for selection changes
            top = self.index(0, 0)
            bot = self.index(len(self._entries) - 1, 0)
            self.dataChanged.emit(top, bot, [self.IS_SELECTED_ROLE])

    def select_index(self, idx: int, shift: bool = False, ctrl: bool = False) -> None:
        e = self.get_entry(idx)
        if e is None or e.is_folder:
            return

        def apply_selection(paths: Iterable[Path], replace: bool) -> None:
            if replace:
                self._selected_paths = set(paths)
            else:
                self._selected_paths |= set(paths)

        if shift and self._last_selected_index is not None:
            a = min(self._last_selected_index, idx)
            b = max(self._last_selected_index, idx)
            paths = []
            for i in range(a, b + 1):
                ei = self.get_entry(i)
                if ei and not ei.is_folder:
                    paths.append(ei.path)
            apply_selection(paths, replace=not ctrl)
        elif ctrl:
            # toggle
            if e.path in self._selected_paths:
                self._selected_paths.remove(e.path)
            else:
                self._selected_paths.add(e.path)
        else:
            # single-select
            self._selected_paths = {e.path}

        self._last_selected_index = idx

        # Update entry flags
        for ent in self._entries:
            ent.is_selected = (not ent.is_folder) and (ent.path in self._selected_paths)

        # Notify view
        if self._entries:
            top = self.index(0, 0)
            bot = self.index(len(self._entries) - 1, 0)
            self.dataChanged.emit(top, bot, [self.IS_SELECTED_ROLE])
