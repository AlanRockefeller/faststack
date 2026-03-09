"""ThumbnailModel for QML GridView with file/folder entries."""

import hashlib
import logging
import os
import time
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Callable

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QThread,
    QTimer,
    Qt,
    Signal,
    Slot,
)

from faststack.models import ImageFile
from faststack.io.utils import compute_path_hash, normalize_path_key
from faststack.io.indexer import find_images
from faststack.thumbnail_view.folder_stats import (
    FolderStats,
    count_images_in_folder,
    read_folder_stats,
)

log = logging.getLogger(__name__)


def _is_filesystem_root(path: Path) -> bool:
    r"""Check if a path is a filesystem root.

    Handles:
    - Unix roots: /
    - Windows drive roots: C:\, D:\, etc.
    - UNC roots: \\server\share (the share level is treated as root)

    Args:
        path: Path to check

    Returns:
        True if the path is a filesystem root.
    """
    resolved = path.resolve()

    # Check if path equals its own parent (root condition)
    if resolved == resolved.parent:
        return True

    # On Windows, check for drive root (e.g., C:\)
    path_str = str(resolved)
    if len(path_str) <= 3 and path_str[1:3] == ":\\":
        return True

    # Check for UNC root (\\server\share)
    if path_str.startswith("\\\\"):
        # UNC paths: \\server\share is the root level
        # Count backslashes after the initial \\
        parts = path_str[2:].split("\\")
        # \\server\share has 2 parts (server, share)
        if len(parts) <= 2:
            return True

    return False


@dataclass
class ThumbnailEntry:
    """A single entry in the thumbnail grid (file or folder)."""

    path: Path
    name: str
    is_folder: bool
    is_stacked: bool = False
    is_uploaded: bool = False
    is_edited: bool = False
    is_restacked: bool = False
    is_favorite: bool = False
    is_todo: bool = False
    folder_stats: Optional[FolderStats] = None
    has_backups: bool = False
    has_developed: bool = False
    mtime_ns: int = 0
    thumb_rev: int = 0  # Bumped when thumbnail is ready, forces QML refresh


class ThumbnailModel(QAbstractListModel):
    """Qt model for thumbnail grid view.

    Provides entries for both folders and images, with support for:
    - Selection state for batch operations
    - Thumbnail revision tracking for QML refresh
    - Parent folder navigation (..)
    """

    # Custom roles for QML
    FilePathRole = Qt.ItemDataRole.UserRole + 1
    FileNameRole = Qt.ItemDataRole.UserRole + 2
    IsFolderRole = Qt.ItemDataRole.UserRole + 3
    IsStackedRole = Qt.ItemDataRole.UserRole + 4
    IsUploadedRole = Qt.ItemDataRole.UserRole + 5
    IsEditedRole = Qt.ItemDataRole.UserRole + 6
    ThumbnailSourceRole = Qt.ItemDataRole.UserRole + 7
    FolderStatsRole = Qt.ItemDataRole.UserRole + 8
    IsSelectedRole = Qt.ItemDataRole.UserRole + 9
    ThumbRevRole = Qt.ItemDataRole.UserRole + 10
    PathHashRole = Qt.ItemDataRole.UserRole + 11
    MtimeNsRole = Qt.ItemDataRole.UserRole + 12
    IsParentFolderRole = Qt.ItemDataRole.UserRole + 13
    IsRestackedRole = Qt.ItemDataRole.UserRole + 14
    IsInBatchRole = Qt.ItemDataRole.UserRole + 15
    IsCurrentRole = Qt.ItemDataRole.UserRole + 16
    IsFavoriteRole = Qt.ItemDataRole.UserRole + 17
    HasBackupsRole = Qt.ItemDataRole.UserRole + 18
    HasDevelopedRole = Qt.ItemDataRole.UserRole + 19
    IsTodoRole = Qt.ItemDataRole.UserRole + 20

    # Signal emitted when a thumbnail is ready (id = "{size}/{path_hash}/{mtime_ns}")
    thumbnailReady = Signal(str)
    # Signal emitted when selection changes (for UIState to forward to QML)
    selectionChanged = Signal()

    def __init__(
        self,
        base_directory: Path,
        current_directory: Path,
        get_metadata_callback: Optional[Callable[[str], dict]] = None,
        get_batch_indices_callback: Optional[Callable[[], Set[int]]] = None,
        get_current_index_callback: Optional[Callable[[], int]] = None,
        thumbnail_size: int = 200,
        parent=None,
    ):
        super().__init__(parent)
        self._next_source_reason: Optional[str] = None
        self._base_directory = base_directory.resolve()
        self._current_directory = current_directory.resolve()
        self._get_metadata = get_metadata_callback
        self._get_batch_indices = get_batch_indices_callback
        self._get_current_index = get_current_index_callback
        self._thumbnail_size = thumbnail_size
        self._entries: List[ThumbnailEntry] = []
        self._folder_count: int = 0  # cached; updated on mutation
        self._selected_indices: Set[int] = set()
        self._last_selected_index: Optional[int] = None
        self._active_filter: str = ""  # current filename filter (set by AppController)
        self._active_filter_flags: list = (
            []
        )  # current flag filters (e.g. ["uploaded", "stacked"])

        # Mapping from thumbnail_id (without query params) to row index
        # id format: "{size}/{path_hash}/{mtime_ns}"
        self._id_to_row: Dict[str, int] = {}
        # Normalized-path → row index for O(1) find_image_index() lookups
        self._path_to_row: Dict[str, int] = {}

        # Connect our own signal to handle thumbnail ready events
        self.thumbnailReady.connect(self._on_thumbnail_ready)

    @property
    def thumbnail_size(self) -> int:
        """Target thumbnail size in pixels (read-only)."""
        return self._thumbnail_size

    @property
    def current_directory(self) -> Path:
        """Current directory being displayed."""
        return self._current_directory

    @property
    def base_directory(self) -> Path:
        """Base directory (can't navigate above this)."""
        return self._base_directory

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    @property
    def folder_count(self) -> int:
        """Total number of folder entries currently in the model (cached)."""
        return self._folder_count

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._entries):
            return None

        entry = self._entries[index.row()]
        row = index.row()

        if role == Qt.ItemDataRole.DisplayRole or role == self.FileNameRole:
            return entry.name
        elif role == self.FilePathRole:
            return str(entry.path)
        elif role == self.IsFolderRole:
            return entry.is_folder
        elif role == self.IsStackedRole:
            return entry.is_stacked
        elif role == self.IsUploadedRole:
            return entry.is_uploaded
        elif role == self.IsEditedRole:
            return entry.is_edited
        elif role == self.ThumbnailSourceRole:
            reason = self._next_source_reason or "scroll"
            return self._get_thumbnail_source(entry, reason=reason)
        elif role == self.FolderStatsRole:
            if entry.folder_stats:
                return {
                    "total_images": entry.folder_stats.total_images,
                    "stacked_count": entry.folder_stats.stacked_count,
                    "uploaded_count": entry.folder_stats.uploaded_count,
                    "edited_count": entry.folder_stats.edited_count,
                    "jpg_count": entry.folder_stats.jpg_count,  # Actually image-like files: JPG, PNG, etc.
                    "raw_count": entry.folder_stats.raw_count,
                    # Convert tuples to lists for safer QML type conversion
                    "coverage_buckets": [
                        list(t) for t in entry.folder_stats.coverage_buckets
                    ],
                }
            return None
        elif role == self.IsSelectedRole:
            return row in self._selected_indices
        elif role == self.ThumbRevRole:
            return entry.thumb_rev
        elif role == self.PathHashRole:
            return compute_path_hash(entry.path)
        elif role == self.MtimeNsRole:
            return entry.mtime_ns
        elif role == self.IsParentFolderRole:
            return entry.name == ".." and entry.is_folder
        elif role == self.IsRestackedRole:
            return entry.is_restacked
        elif role == self.IsFavoriteRole:
            return entry.is_favorite
        elif role == self.IsTodoRole:
            return entry.is_todo
        elif role == self.IsInBatchRole:
            # Check if this row's corresponding loupe index is in any batch
            if self._get_batch_indices and not entry.is_folder:
                batch_indices = self._get_batch_indices()
                # Find the loupe index for this entry
                loupe_idx = self._get_loupe_index_for_entry(entry)
                return loupe_idx is not None and loupe_idx in batch_indices
            return False
        elif role == self.IsCurrentRole:
            # Check if this entry is the current image in loupe view
            if self._get_current_index and not entry.is_folder:
                current_idx = self._get_current_index()
                loupe_idx = self._get_loupe_index_for_entry(entry)
                return loupe_idx is not None and loupe_idx == current_idx
            return False
        elif role == self.HasBackupsRole:
            return entry.has_backups
        elif role == self.HasDevelopedRole:
            return entry.has_developed

        return None

    def _get_loupe_index_for_entry(self, entry: ThumbnailEntry) -> Optional[int]:
        """Get the loupe view index for a thumbnail entry."""
        # This requires access to the app controller's _path_to_index
        # We'll use the parent (AppController) to look this up
        parent = self.parent()
        if parent and hasattr(parent, "_path_to_index"):
            # Must use the same key format as _rebuild_path_to_index (abspath,
            # not realpath/resolve) so the lookup hits on Windows and Linux.
            key = os.path.normcase(os.path.abspath(str(entry.path)))
            return parent._path_to_index.get(key)
        return None

    def roleNames(self) -> Dict[int, bytes]:
        return {
            Qt.ItemDataRole.DisplayRole: b"display",
            self.FilePathRole: b"filePath",
            self.FileNameRole: b"fileName",
            self.IsFolderRole: b"isFolder",
            self.IsStackedRole: b"isStacked",
            self.IsUploadedRole: b"isUploaded",
            self.IsEditedRole: b"isEdited",
            self.ThumbnailSourceRole: b"thumbnailSource",
            self.FolderStatsRole: b"folderStats",
            self.IsSelectedRole: b"isSelected",
            self.ThumbRevRole: b"thumbRev",
            self.PathHashRole: b"pathHash",
            self.MtimeNsRole: b"mtimeNs",
            self.IsParentFolderRole: b"isParentFolder",
            self.IsRestackedRole: b"isRestacked",
            self.IsInBatchRole: b"isInBatch",
            self.IsCurrentRole: b"isCurrent",
            self.IsFavoriteRole: b"isFavorite",
            self.HasBackupsRole: b"hasBackups",
            self.HasDevelopedRole: b"hasDeveloped",
            self.IsTodoRole: b"isTodo",
        }

    def _get_thumbnail_source(
        self, entry: ThumbnailEntry, reason: str = "scroll"
    ) -> str:
        """Build thumbnail URL for QML Image source.

        Format: image://thumbnail/{size}/{path_hash}/{mtime_ns}?r={rev}&reason={reason}
        Folders use: image://thumbnail/folder/{path_hash}/{mtime_ns}?r={rev}
        """
        path_hash = compute_path_hash(entry.path)
        mtime_ns = entry.mtime_ns
        rev = entry.thumb_rev

        if entry.is_folder:
            return f"image://thumbnail/folder/{path_hash}/{mtime_ns}?r={rev}"
        else:
            return f"image://thumbnail/{self._thumbnail_size}/{path_hash}/{mtime_ns}?r={rev}&reason={reason}"

    def set_filter(self, filter_string: str, refresh: bool = True) -> None:
        """Set the active filename filter and optionally refresh the model."""
        self._active_filter = filter_string
        self._next_source_reason = "filter"
        if refresh:
            self.refresh()

    def set_filter_flags(self, flags: list, refresh: bool = True) -> None:
        """Set the active flag filters and optionally refresh the model."""
        self._active_filter_flags = list(flags)
        self._next_source_reason = "filter"
        if refresh:
            self.refresh()

    def _add_folders_to_entries(self):
        """Scan for folders and add them to self._entries."""
        # Add parent folder entry if not at filesystem root
        if not _is_filesystem_root(self._current_directory):
            parent_path = self._current_directory.parent
            self._entries.append(
                ThumbnailEntry(
                    path=parent_path,
                    name="..",
                    is_folder=True,
                    mtime_ns=0,
                )
            )

        # Scan for folders
        folders: List[ThumbnailEntry] = []
        try:
            for entry in os.scandir(self._current_directory):
                if entry.is_dir() and not entry.name.startswith("."):
                    folder_path = Path(entry.path)
                    try:
                        stat_info = entry.stat()
                        mtime_ns = stat_info.st_mtime_ns
                    except OSError:
                        mtime_ns = 0

                    folder_stats = read_folder_stats(folder_path)
                    if folder_stats is None:
                        folder_stats = count_images_in_folder(folder_path)

                    folders.append(
                        ThumbnailEntry(
                            path=folder_path,
                            name=entry.name,
                            is_folder=True,
                            folder_stats=folder_stats,
                            mtime_ns=mtime_ns,
                        )
                    )
        except OSError as e:
            log.warning("Error scanning directory %s: %s", self._current_directory, e)

        # Sort folders alphabetically
        folders.sort(key=lambda e: e.name.lower())
        self._entries.extend(folders)

    def refresh(self):
        """Refresh the model by rescanning the current directory."""
        cur, own = QThread.currentThread(), self.thread()
        assert (
            cur == own
        ), f"ThumbnailModel.refresh() thread mismatch: current={cur}, owner={own}"
        self.beginResetModel()
        t0 = time.perf_counter()
        try:
            self._entries.clear()
            self._id_to_row.clear()
            self._path_to_row.clear()
            self._selected_indices.clear()
            self._last_selected_index = None

            self._add_folders_to_entries()
            t1 = time.perf_counter()

            # Get images using existing indexer (respects filter rules)
            images = find_images(self._current_directory)

            # Apply active filename filter if set
            if self._active_filter:
                needle = self._active_filter.lower()
                images = [img for img in images if needle in img.path.stem.lower()]

            # Apply active flag filters (AND logic)
            if self._active_filter_flags and self._get_metadata:
                flags = self._active_filter_flags
                filtered = []
                for img in images:
                    try:
                        meta = self._get_metadata(img.path.stem)
                        if not isinstance(meta, dict):
                            # Ensure it's a dict before .get()
                            log.debug(
                                "Metadata for %s is not a dict: %r", img.path.stem, meta
                            )
                            continue

                        if all(meta.get(flag, False) for flag in flags):
                            filtered.append(img)
                    except Exception as e:
                        log.debug("Error filtering image %s: %s", img.path, e)
                        # Skip images with metadata errors
                images = filtered

            self._add_images_to_entries(images)
            t2 = time.perf_counter()
            self._rebuild_id_mapping()
            t3 = time.perf_counter()

        finally:
            self.endResetModel()

        # Defer clear so QML's next render frame still sees the reason
        QTimer.singleShot(0, self._clear_next_source_reason)
        self._folder_count = sum(1 for e in self._entries if e.is_folder)
        self.selectionChanged.emit()
        log.info(
            "ThumbnailModel refreshed: %d entries (%d folders, %d images)",
            len(self._entries),
            self._folder_count,
            len(self._entries) - self._folder_count,
        )
        log.info(
            "refresh timings: folders=%.3f images=%.3f idmap=%.3f total=%.3f n=%d",
            t1 - t0,
            t2 - t1,
            t3 - t2,
            t3 - t0,
            len(images),
        )

    def remove_rows_by_path(self, paths: List[Path]) -> None:
        """Targeted removal of rows by path without full model reset."""
        if not paths or not self._entries:
            return

        # 1. Map paths to rows (normalized for Windows case/separator consistency)
        path_keys = {os.path.normcase(os.path.abspath(str(p))) for p in paths}
        indices_to_remove = []
        for i, entry in enumerate(self._entries):
            if os.path.normcase(os.path.abspath(str(entry.path))) in path_keys:
                indices_to_remove.append(i)

        if not indices_to_remove:
            return

        # Count folders being removed (for cached counter)
        folders_removed = sum(
            1 for i in indices_to_remove if self._entries[i].is_folder
        )

        # 2. Sort in reverse to maintain index validity during removal
        indices_to_remove.sort(reverse=True)

        # 3. Group consecutive indices for batch removal calls
        ranges = []
        if indices_to_remove:
            current_range = [
                indices_to_remove[0],
                indices_to_remove[0],
            ]  # [last, first]
            for idx in indices_to_remove[1:]:
                if idx == current_range[1] - 1:
                    current_range[1] = idx
                else:
                    ranges.append(current_range)
                    current_range = [idx, idx]
            ranges.append(current_range)

        # 4. Perform removals
        for last, first in ranges:
            self.beginRemoveRows(QModelIndex(), first, last)
            del self._entries[first : last + 1]
            self.endRemoveRows()

        # 5. Update cached folder count
        self._folder_count -= folders_removed

        # 6. Fix selection state (indices have shifted) — O(n log n) via bisect
        sorted_removed = sorted(indices_to_remove)
        removed_set = set(indices_to_remove)
        new_selection = set()
        for idx in self._selected_indices:
            if idx not in removed_set:
                offset = bisect_left(sorted_removed, idx)
                new_selection.add(idx - offset)
        self._selected_indices = new_selection
        self._last_selected_index = None

        # 7. Rebuild mapping
        self._rebuild_id_mapping()
        self.selectionChanged.emit()
        log.info(
            "ThumbnailModel removed %d rows via targeted removal",
            len(indices_to_remove),
        )

    def refresh_from_controller(
        self, images: List, metadata_map: Optional[Dict[str, dict]] = None
    ):
        """Refresh images from a pre-loaded list without scanning disk.

        Folders are still scanned, but image entries are built from the
        provided objects.
        """
        self._next_source_reason = "refresh"
        cur, own = QThread.currentThread(), self.thread()
        assert cur == own, "ThumbnailModel refresh thread mismatch"

        self.beginResetModel()
        try:
            self._entries.clear()
            self._id_to_row.clear()
            self._path_to_row.clear()
            self._selected_indices.clear()
            self._last_selected_index = None

            t0 = time.perf_counter()
            self._add_folders_to_entries()
            t1 = time.perf_counter()

            # Apply active filename filter if set
            if self._active_filter:
                needle = self._active_filter.lower()
                images = [img for img in images if needle in img.path.stem.lower()]

            # Apply active flag filters (AND logic)
            if self._active_filter_flags:
                flags = self._active_filter_flags
                filtered = []
                for img in images:
                    try:
                        if metadata_map:
                            meta = metadata_map.get(img.path.stem, {})
                        elif self._get_metadata:
                            meta = self._get_metadata(img.path.stem)
                        else:
                            continue

                        if not isinstance(meta, dict):
                            log.debug(
                                "Metadata for %s is not a dict: %r", img.path.stem, meta
                            )
                            continue

                        if all(meta.get(flag, False) for flag in flags):
                            filtered.append(img)
                    except Exception as e:
                        log.debug("Error filtering image %s: %s", img.path, e)
                        # Skip images with metadata errors
                images = filtered

            self._add_images_to_entries(images, metadata_map)
            t2 = time.perf_counter()
            self._rebuild_id_mapping()
            t3 = time.perf_counter()
        finally:
            self.endResetModel()

        QTimer.singleShot(0, self._clear_next_source_reason)
        self._folder_count = sum(1 for e in self._entries if e.is_folder)
        self.selectionChanged.emit()
        log.info(
            "refresh_from_controller timings: folders=%.3f images=%.3f idmap=%.3f total=%.3f n=%d (bulk_meta=%s)",
            t1 - t0,
            t2 - t1,
            t3 - t2,
            t3 - t0,
            len(images),
            metadata_map is not None,
        )

    def _add_images_to_entries(
        self, images: List, metadata_map: Optional[Dict[str, dict]] = None
    ):
        """Convert list of objects (ImageFile or similar) to ThumbnailEntry."""
        for img in images:
            try:
                # Use mtime from object if available to avoid stat()
                if hasattr(img, "timestamp") and img.timestamp:
                    mtime_ns = int(img.timestamp * 1e9)
                else:
                    mtime_ns = img.path.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0

            # Get metadata
            is_stacked = False
            is_uploaded = False
            is_edited = False
            is_restacked = False
            is_favorite = False
            is_todo = False

            if metadata_map:
                meta = metadata_map.get(img.path.stem, {})
                is_stacked = meta.get("stacked", False)
                is_uploaded = meta.get("uploaded", False)
                is_edited = meta.get("edited", False)
                is_restacked = meta.get("restacked", False)
                is_favorite = meta.get("favorite", False)
                is_todo = meta.get("todo", False)
            elif self._get_metadata:
                try:
                    meta = self._get_metadata(img.path.stem)
                    if isinstance(meta, dict):
                        is_stacked = meta.get("stacked", False)
                        is_uploaded = meta.get("uploaded", False)
                        is_edited = meta.get("edited", False)
                        is_restacked = meta.get("restacked", False)
                        is_favorite = meta.get("favorite", False)
                        is_todo = meta.get("todo", False)
                    else:
                        log.debug(
                            "Metadata for %s is not a dict: %r", img.path.stem, meta
                        )
                except Exception as e:
                    log.debug("Error getting metadata for %s: %s", img.path, e)

            has_backups = getattr(img, "has_backups", False)
            has_developed = getattr(img, "has_developed", False)

            self._entries.append(
                ThumbnailEntry(
                    path=img.path,
                    name=img.path.name,
                    is_folder=False,
                    is_stacked=is_stacked,
                    is_uploaded=is_uploaded,
                    is_edited=is_edited,
                    is_restacked=is_restacked,
                    is_favorite=is_favorite,
                    is_todo=is_todo,
                    has_backups=has_backups,
                    has_developed=has_developed,
                    mtime_ns=mtime_ns,
                )
            )

    def _rebuild_id_mapping(self):
        """Rebuilds the path/stack_id -> row mapping."""
        self._id_to_row.clear()
        self._path_to_row.clear()

        # Key must match the thumbnail_id format emitted by the prefetcher:
        # "{size}/{path_hash}/{mtime_ns}" — same as _make_thumbnail_id()
        for i, e in enumerate(self._entries):
            if not e.is_folder:
                tid = self._make_thumbnail_id(e)
                self._id_to_row[tid] = i
                # Normalized key for O(1) find_image_index() lookups
                self._path_to_row[normalize_path_key(e.path)] = i

    def _make_thumbnail_id(self, entry: ThumbnailEntry) -> str:
        """Create thumbnail ID without query params."""
        path_hash = compute_path_hash(entry.path)
        if entry.is_folder:
            return f"folder/{path_hash}/{entry.mtime_ns}"
        else:
            return f"{self._thumbnail_size}/{path_hash}/{entry.mtime_ns}"

    @Slot(str)
    def _on_thumbnail_ready(self, thumbnail_id: str):
        """Handle thumbnail ready signal - bump revision and emit dataChanged."""
        if thumbnail_id not in self._id_to_row:
            if log.isEnabledFor(logging.DEBUG):
                actual_size = (
                    thumbnail_id.split("/", 1)[0] if "/" in thumbnail_id else "?"
                )
                log.debug(
                    "thumbnail ready id not in model map: id=%s expected_size=%s actual_size=%s",
                    thumbnail_id,
                    self._thumbnail_size,
                    actual_size,
                )
            return

        row = self._id_to_row[thumbnail_id]
        if row < 0 or row >= len(self._entries):
            return

        # Bump the revision
        self._entries[row].thumb_rev += 1

        # Emit dataChanged for thumbnailSource role
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.ThumbnailSourceRole, self.ThumbRevRole])

    def set_directories(self, base_directory: Path, current_directory: Path):
        """Set both base and current directories (for open folder).

        This resets the model to a new root directory.

        Args:
            base_directory: The new base/root directory.
            current_directory: The new current directory (usually same as base).
        """
        self._base_directory = base_directory.resolve()
        self._current_directory = current_directory.resolve()
        self._selected_indices.clear()
        self._last_selected_index = None
        # Don't call refresh() here - caller should do it after updating other state

    def navigate_to(self, path: Path, update_base_if_above: bool = False):
        """Navigate to a different directory.

        Args:
            path: Directory to navigate to.
            update_base_if_above: If True and path is above base_directory,
                                  update base_directory to path. Used for
                                  "go up" navigation above initial directory.
        """
        resolved = path.resolve()

        if not resolved.is_dir():
            log.warning("Cannot navigate to non-directory: %s", resolved)
            return

        # Check if navigating above base directory
        try:
            resolved.relative_to(self._base_directory)
        except ValueError:
            # path is outside base_directory
            if update_base_if_above:
                # Allow navigation up - update base_directory
                log.info(
                    "Navigating above base directory, updating base to: %s", resolved
                )
                self._base_directory = resolved
            else:
                log.warning(
                    "Attempted to navigate outside base directory: %s", resolved
                )
                return

        self._current_directory = resolved
        self._selected_indices.clear()
        self._last_selected_index = None
        self._next_source_reason = "jump"
        self.refresh()

    def _clear_next_source_reason(self):
        """Deferred clear of _next_source_reason (called via QTimer.singleShot)."""
        self._next_source_reason = None

    # Selection methods

    def select_index(self, idx: int, shift: bool = False, ctrl: bool = False):
        """Handle selection at index with modifier keys.

        Args:
            idx: Index to select
            shift: Shift key held (range select)
            ctrl: Ctrl key held (toggle individual)
        """
        if idx < 0 or idx >= len(self._entries):
            return

        # Don't allow selecting folders or parent
        entry = self._entries[idx]
        if entry.is_folder:
            return

        old_selection = self._selected_indices.copy()

        if shift and self._last_selected_index is not None:
            # Range selection
            start = min(self._last_selected_index, idx)
            end = max(self._last_selected_index, idx)
            for i in range(start, end + 1):
                if not self._entries[i].is_folder:
                    self._selected_indices.add(i)
        elif ctrl:
            # Toggle individual
            if idx in self._selected_indices:
                self._selected_indices.discard(idx)
            else:
                self._selected_indices.add(idx)
                self._last_selected_index = idx
        else:
            # Simple click - clear and select single
            self._selected_indices.clear()
            self._selected_indices.add(idx)
            self._last_selected_index = idx

        # Emit dataChanged for affected rows
        changed_rows = old_selection.symmetric_difference(self._selected_indices)
        for row in changed_rows:
            row_idx = self.index(row, 0)
            self.dataChanged.emit(row_idx, row_idx, [self.IsSelectedRole])

        # Notify if selection actually changed
        if changed_rows:
            self.selectionChanged.emit()

    def clear_selection(self):
        """Clear all selections."""
        if not self._selected_indices:
            return

        old_selection = self._selected_indices.copy()
        self._selected_indices.clear()
        self._last_selected_index = None

        for row in old_selection:
            row_idx = self.index(row, 0)
            self.dataChanged.emit(row_idx, row_idx, [self.IsSelectedRole])

        self.selectionChanged.emit()

    def get_selected_paths(self) -> List[Path]:
        """Get list of selected image paths."""
        return [
            self._entries[idx].path
            for idx in sorted(self._selected_indices)
            if idx < len(self._entries) and not self._entries[idx].is_folder
        ]

    @property
    def selected_count(self) -> int:
        """Get count of selected items (efficient, no list copy)."""
        return len(self._selected_indices)

    def get_entry(self, row: int) -> Optional[ThumbnailEntry]:
        """Get entry at row."""
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def _compute_path_hash(self, path: Path) -> str:
        """Computes a stable hash for the given path.

        Now uses centralized helper which is purely string-based (no .resolve() calls).
        """
        return compute_path_hash(path)

    def find_image_index(self, path: Path) -> int:
        """Find the row index of an image by path.

        Returns -1 if not found.
        Uses O(1) dict lookup instead of O(n) linear scan with .resolve().
        """
        key = normalize_path_key(path)
        return self._path_to_row.get(key, -1)
