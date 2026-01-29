"""ThumbnailModel for QML GridView with file/folder entries."""

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Callable

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Qt,
    Signal,
    Slot,
)

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
    folder_stats: Optional[FolderStats] = None
    mtime_ns: int = 0
    thumb_rev: int = 0  # Bumped when thumbnail is ready, forces QML refresh


def _compute_path_hash(path: Path) -> str:
    """Compute a stable hash of the path for cache key purposes."""
    return hashlib.md5(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


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
        self._base_directory = base_directory.resolve()
        self._current_directory = current_directory.resolve()
        self._get_metadata = get_metadata_callback
        self._get_batch_indices = get_batch_indices_callback
        self._get_current_index = get_current_index_callback
        self._thumbnail_size = thumbnail_size
        self._entries: List[ThumbnailEntry] = []
        self._selected_indices: Set[int] = set()
        self._last_selected_index: Optional[int] = None

        # Mapping from thumbnail_id (without query params) to row index
        # id format: "{size}/{path_hash}/{mtime_ns}"
        self._id_to_row: Dict[str, int] = {}

        # Connect our own signal to handle thumbnail ready events
        self.thumbnailReady.connect(self._on_thumbnail_ready)

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
            return self._get_thumbnail_source(entry)
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
            return _compute_path_hash(entry.path)
        elif role == self.MtimeNsRole:
            return entry.mtime_ns
        elif role == self.IsParentFolderRole:
            return entry.name == ".." and entry.is_folder
        elif role == self.IsRestackedRole:
            return entry.is_restacked
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

        return None

    def _get_loupe_index_for_entry(self, entry: ThumbnailEntry) -> Optional[int]:
        """Get the loupe view index for a thumbnail entry."""
        # This requires access to the app controller's _path_to_index
        # We'll use the parent (AppController) to look this up
        parent = self.parent()
        if parent and hasattr(parent, "_path_to_index"):
            return parent._path_to_index.get(entry.path.resolve())
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
        }

    def _get_thumbnail_source(self, entry: ThumbnailEntry) -> str:
        """Build thumbnail URL for QML Image source.

        Format: image://thumbnail/{size}/{path_hash}/{mtime_ns}?r={rev}
        Folders use: image://thumbnail/folder/{path_hash}/{mtime_ns}?r={rev}
        """
        path_hash = _compute_path_hash(entry.path)
        mtime_ns = entry.mtime_ns
        rev = entry.thumb_rev

        if entry.is_folder:
            return f"image://thumbnail/folder/{path_hash}/{mtime_ns}?r={rev}"
        else:
            return f"image://thumbnail/{self._thumbnail_size}/{path_hash}/{mtime_ns}?r={rev}"

    def refresh(self, filter_string: str = ""):
        """Refresh the model by rescanning the current directory.

        Args:
            filter_string: Optional filter to apply to filenames (case-insensitive)
        """
        self.beginResetModel()
        try:
            self._entries.clear()
            self._id_to_row.clear()
            self._selected_indices.clear()
            self._last_selected_index = None

            # Add parent folder entry if not at filesystem root
            # (allows navigating up even above base_directory)
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
                        # Fall back to counting actual files for folders without faststack.json
                        # (e.g., recycle bin)
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
                log.warning(
                    "Error scanning directory %s: %s", self._current_directory, e
                )

            # Sort folders alphabetically
            folders.sort(key=lambda e: e.name.lower())
            self._entries.extend(folders)

            # Get images using existing indexer (respects filter rules)
            images = find_images(self._current_directory)

            # Apply filter if specified
            if filter_string:
                needle = filter_string.lower()
                images = [img for img in images if needle in img.path.stem.lower()]

            # Convert ImageFile to ThumbnailEntry
            for img in images:
                try:
                    stat_info = img.path.stat()
                    mtime_ns = stat_info.st_mtime_ns
                except OSError:
                    mtime_ns = int(img.timestamp * 1e9) if img.timestamp else 0

                # Get metadata if callback provided
                is_stacked = False
                is_uploaded = False
                is_edited = False
                is_restacked = False

                if self._get_metadata:
                    try:
                        meta = self._get_metadata(img.path.stem)
                        is_stacked = meta.get("stacked", False)
                        is_uploaded = meta.get("uploaded", False)
                        is_edited = meta.get("edited", False)
                        is_restacked = meta.get("restacked", False)
                    except Exception:
                        pass

                self._entries.append(
                    ThumbnailEntry(
                        path=img.path,
                        name=img.path.name,
                        is_folder=False,
                        is_stacked=is_stacked,
                        is_uploaded=is_uploaded,
                        is_edited=is_edited,
                        is_restacked=is_restacked,
                        mtime_ns=mtime_ns,
                    )
                )

            # Build id_to_row mapping
            self._rebuild_id_mapping()

        finally:
            self.endResetModel()

        # Selection was cleared during refresh
        self.selectionChanged.emit()
        log.info(
            "ThumbnailModel refreshed: %d entries (%d folders, %d images)",
            len(self._entries),
            sum(1 for e in self._entries if e.is_folder),
            sum(1 for e in self._entries if not e.is_folder),
        )

    def _rebuild_id_mapping(self):
        """Rebuild the id_to_row mapping for all entries."""
        self._id_to_row.clear()
        for row, entry in enumerate(self._entries):
            if entry.name == "..":
                continue  # Don't map parent folder
            thumbnail_id = self._make_thumbnail_id(entry)
            self._id_to_row[thumbnail_id] = row

    def _make_thumbnail_id(self, entry: ThumbnailEntry) -> str:
        """Create thumbnail ID without query params."""
        path_hash = _compute_path_hash(entry.path)
        if entry.is_folder:
            return f"folder/{path_hash}/{entry.mtime_ns}"
        else:
            return f"{self._thumbnail_size}/{path_hash}/{entry.mtime_ns}"

    @Slot(str)
    def _on_thumbnail_ready(self, thumbnail_id: str):
        """Handle thumbnail ready signal - bump revision and emit dataChanged."""
        if thumbnail_id not in self._id_to_row:
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
        self.refresh()

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

    def find_image_index(self, path: Path) -> int:
        """Find the row index of an image by path.

        Returns -1 if not found.
        """
        resolved = path.resolve()
        for i, entry in enumerate(self._entries):
            if not entry.is_folder and entry.path.resolve() == resolved:
                return i
        return -1
