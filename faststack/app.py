"""Main application entry point for FastStack."""

import logging
import sys
import math
import struct
import shlex
import time
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set
from datetime import date, datetime
import os
import re
import shutil
import uuid
import functools

# Must set before importing PySide6
os.environ["QT_LOGGING_RULES"] = "qt.qpa.mime.warning=false"

# Type Aliases for readability
DeletePair = Tuple[Optional[Path], Optional[Path]]  # (src_path, recycle_bin_path)
DeleteRecord = Tuple[DeletePair, DeletePair]  # (jpg_pair, raw_pair)

import concurrent.futures
import threading
import subprocess
from faststack.ui.provider import ImageProvider, UIState
import PySide6
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtCore import (
    QUrl,
    QTimer,
    QObject,
    QEvent,
    Signal,
    Slot,
    QMimeData,
    Qt,
    QPoint,
    QCoreApplication,
)
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtQml import QQmlApplicationEngine
from PIL import Image

Image.MAX_IMAGE_PIXELS = 200_000_000  # 200 megapixels, enough for most photos
# ⬇️ these are the ones that went missing
from faststack.config import config
from faststack.logging_setup import setup_logging
from faststack.models import ImageFile, DecodedImage
from faststack.io.indexer import find_images
from faststack.io.sidecar import SidecarManager
from faststack.io.watcher import Watcher
from faststack.io.helicon import launch_helicon_focus
from faststack.io.executable_validator import validate_executable_path
from faststack.imaging.cache import (
    ByteLRUCache,
    get_decoded_image_size,
    build_cache_key,
)
from faststack.imaging.prefetch import Prefetcher, clear_icc_caches
from faststack.ui.keystrokes import Keybinder
from faststack.imaging.editor import ImageEditor, ASPECT_RATIOS
from faststack.imaging.metadata import get_exif_data
from faststack.thumbnail_view import (
    ThumbnailModel,
    ThumbnailPrefetcher,
    ThumbnailCache,
    ThumbnailProvider,
    PathResolver,
)
from faststack.thumbnail_view.folder_stats import (
    clear_raw_count_cache,
    get_file_counts_by_extension,
)
import numpy as np
from faststack.io.indexer import RAW_EXTENSIONS


def make_hdrop(paths):
    """
    Build a real CF_HDROP (DROPFILES) payload for Windows drag-and-drop.
    paths: list[str]
    """
    files_part = ("\0".join(paths) + "\0\0").encode("utf-16le")

    # DROPFILES header (20 bytes): <IiiII
    pFiles = 20
    pt_x = 0
    pt_y = 0
    fNC = 0
    fWide = 1  # wide chars
    header = struct.pack("<IiiII", pFiles, pt_x, pt_y, fNC, fWide)
    return header + files_part


log = logging.getLogger(__name__)

# Global flag for debug mode - set by main()
_debug_mode = False

# Cache Thrashing Detection Constants
CACHE_THRASH_WINDOW_SECS = 2.0
CACHE_THRASH_THRESHOLD = 5
CACHE_WARNING_COOLDOWN_SECS = 300


class AppController(QObject):
    dataChanged = Signal()  # New signal for general data changes
    is_zoomed_changed = Signal(bool)  # Signal for zoom state changes
    histogramReady = Signal(object)  # Signal for off-thread histogram result
    previewReady = Signal(object)  # Signal for off-thread preview result
    dialogStateChanged = Signal(bool)  # Signal for dialog open/close state
    # Thread-safe signal for thumbnail ready (emitted from worker thread, received on GUI thread)
    _thumbnailReadySignal = Signal(str)

    class ProgressReporter(QObject):
        progress_updated = Signal(int)
        finished = Signal()

    editSourceModeChanged = Signal(str)  # Notify when JPEG/RAW mode changes

    def __init__(
        self, image_dir: Path, engine: QQmlApplicationEngine, debug_cache: bool = False
    ):
        super().__init__()
        # Histogram Offloading Setup
        self._hist_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._hist_inflight = False
        self._hist_pending = None
        self._hist_token = 0
        self._hist_lock = threading.Lock()
        self.histogramReady.connect(self._apply_histogram_result)
        self.previewReady.connect(self._apply_preview_result)

        # Preview Offloading Setup
        self._preview_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._preview_inflight = False
        self._preview_pending = False
        self._preview_token = 0
        self._preview_lock = threading.Lock()
        self._last_rendered_preview = None  # Store latest valid render
        self._shutting_down = False  # Flag to gate async callbacks during shutdown
        self._opencv_warning_shown = False  # Only show OpenCV warning once per session

        self.image_dir = image_dir
        self.image_files: List[ImageFile] = []  # Filtered list for display
        self._all_images: List[ImageFile] = []  # Cached full list from disk
        self._path_to_index: Dict[
            Path, int
        ] = {}  # Resolved path -> index for O(1) lookup
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None
        self.engine = engine
        self.debug_cache = debug_cache  # New debug_cache flag

        # Ensure clean shutdown of background threads
        QCoreApplication.instance().aboutToQuit.connect(self._shutdown_executors)

        self.display_width = 0
        self.display_height = 0
        self.display_generation = 0
        self._is_decoding = False

        # Cache Warning State
        self._last_cache_warning_time = 0
        self._eviction_timestamps = []  # List of eviction timestamps for rate detection
        self.display_ready = False  # Track if display size has been reported
        self.pending_prefetch_index: Optional[int] = None  # Deferred prefetch index

        # Edit Source Mode State
        # "jpeg" (default) or "raw"
        self.current_edit_source_mode: str = "jpeg"

        # -- Backend Components --
        self.watcher = Watcher(self.image_dir, self.refresh_image_list)
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
        self.image_editor = ImageEditor()  # Initialize the editor
        self._dialog_open_count = 0  # Track nested dialogs

        # -- Caching & Prefetching --
        cache_size_gb = config.getfloat("core", "cache_size_gb", 1.5)
        cache_size_bytes = int(cache_size_gb * 1024**3)
        self._has_warned_cache_full = False
        self.image_cache = ByteLRUCache(
            max_bytes=cache_size_bytes,
            size_of=get_decoded_image_size,
            on_evict=self._on_cache_evict,
        )
        self.image_cache.hits = 0  # Initialize cache hit counter
        self.image_cache.misses = 0  # Initialize cache miss counter
        self.prefetcher = Prefetcher(
            image_files=self.image_files,
            cache_put=self.image_cache.__setitem__,
            prefetch_radius=config.getint("core", "prefetch_radius", 4),
            get_display_info=self.get_display_info,
            debug=_debug_mode,
        )
        self.last_displayed_image: Optional[DecodedImage] = (
            None  # Cache last image to avoid grey squares
        )
        self._last_image_lock = (
            threading.Lock()
        )  # Protect last_displayed_image from race conditions

        # -- Grid View (Thumbnail) Infrastructure --
        self._is_grid_view_active = True  # Default to grid view on startup
        self._grid_nav_history: list[
            Path
        ] = []  # Stack of previous directories for back navigation
        self._thumbnail_cache = ThumbnailCache(
            max_bytes=256 * 1024 * 1024,  # 256 MB
            max_items=5000,
        )
        self._path_resolver = PathResolver()
        self._thumbnail_prefetcher = ThumbnailPrefetcher(
            cache=self._thumbnail_cache,
            on_ready_callback=self._on_thumbnail_ready,
            target_size=200,
        )
        self._thumbnail_model = ThumbnailModel(
            base_directory=self.image_dir,
            current_directory=self.image_dir,
            get_metadata_callback=self._get_metadata_dict,
            get_batch_indices_callback=self._get_batch_indices,
            get_current_index_callback=self._get_current_loupe_index,
            thumbnail_size=200,
            parent=self,  # Ensure proper Qt ownership to prevent GC issues
        )
        self._thumbnail_provider = ThumbnailProvider(
            cache=self._thumbnail_cache,
            prefetcher=self._thumbnail_prefetcher,
            path_resolver=self._path_resolver.resolve,
            default_size=200,
        )
        # Connect thread-safe thumbnail ready signal to GUI thread handler
        # The callback is invoked from worker threads, so we use a signal to hop to GUI thread
        # Explicit QueuedConnection ensures cross-thread safety
        self._thumbnailReadySignal.connect(
            self._on_thumbnail_ready_gui, Qt.QueuedConnection
        )

        # -- UI State --
        self.ui_state = UIState(self)
        self.ui_state.theme = self.get_theme()
        self.ui_state.debugCache = self.debug_cache
        self.ui_state.debugMode = _debug_mode  # Set debug mode from global
        self.keybinder = Keybinder(self)
        self.ui_state.debugCache = self.debug_cache  # Pass debug_cache state to UI
        self.ui_state.isDecoding = False  # Initialize decoding indicator
        self.is_zoomed = False  # Track zoom state for high-res loading logic

        # Connect model selection changes to UIState for QML property notification
        # Must connect to .emit (not the signal itself) for signal-to-signal forwarding
        self._thumbnail_model.selectionChanged.connect(
            self.ui_state.gridSelectedCountChanged.emit
        )

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stacks: List[List[int]] = []

        # -- Batch Selection State (for drag-and-drop) --
        self.batch_start_index: Optional[int] = None
        self.batches: List[List[int]] = []  # List of [start, end] ranges

        self._filter_string: str = ""  # Default filter
        self._filter_enabled: bool = False

        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        with self._last_image_lock:
            self.last_displayed_image = None
        self._logged_empty_metadata = False

        # -- Delete/Undo State --
        self.active_recycle_bins: Set[Path] = (
            set()
        )  # Track all recycle bins created/used
        self.delete_history: List[
            DeleteRecord
        ] = []  # [((jpg_src, jpg_bin), (raw_src, raw_bin)), ...]

        # Track all undoable actions with timestamps
        # [(action_type, action_data, timestamp)]
        self.undo_history: List[Tuple[str, Any, float]] = []

        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._handle_resize)
        self.pending_width = None
        self.pending_height = None

        # Histogram Throttle Timer
        self.histogram_timer = QTimer(self)
        self.histogram_timer.setSingleShot(True)
        self.histogram_timer.setInterval(50)  # 50ms throttle (max 20fps)
        self.histogram_timer.timeout.connect(self._kick_histogram_worker)

        # Preview refresh uses a gate pattern instead of a timer:
        # - _kick_preview_worker() runs immediately if not inflight
        # - If inflight, it sets _preview_pending and returns
        # - When render completes, _apply_preview_result chains immediately if pending
        # This removes the extra 16ms delay in the fast-render case by chaining
        # immediately on completion. QML's 16ms slider timer remains the fps cap.

        # Track if any dialog is open to disable keybindings
        self._dialog_open = False

        self.auto_level_threshold = config.getfloat("core", "auto_level_threshold", 0.1)
        self.auto_level_strength = config.getfloat("core", "auto_level_strength", 1.0)
        self.auto_level_strength_auto = config.getboolean(
            "core", "auto_level_strength_auto", False
        )

        # Connect editor open/close signal for memory cleanup
        self.ui_state.is_editor_open_changed.connect(self._on_editor_open_changed)

    def _move_to_recycle(self, src_path: Path) -> Path:
        """Moves a file to the recycle bin in the same directory.

        Raises:
            OSError/PermissionError if move fails.
        """
        recycle_bin = src_path.parent / "image recycle bin"
        recycle_bin.mkdir(parents=True, exist_ok=True)

        dst_path = recycle_bin / src_path.name
        shutil.move(str(src_path), str(dst_path))

        self.active_recycle_bins.add(recycle_bin)
        return dst_path

    @Slot(bool)
    def _on_editor_open_changed(self, is_open: bool):
        """Handle necessary setup/cleanup when editor opens or closes."""
        if is_open:
            # Warn once if OpenCV is not available (detail sliders will be slower)
            if not self._opencv_warning_shown:
                from faststack.imaging.optional_deps import HAS_OPENCV

                if not HAS_OPENCV:
                    self._opencv_warning_shown = True
                    log.warning(
                        "OpenCV not available - detail sliders (clarity/texture/sharpness) will be slower"
                    )
                    self.update_status_message(
                        "OpenCV not installed - editor performance reduced. Install opencv-python for faster editing.",
                        timeout=8000,
                    )
        else:
            # Cleanup large memory buffers when editor closes
            if self.image_editor:
                log.debug("Editor closed, clearing editor memory buffers")
                self.image_editor.clear()

            # Also clear the cached preview rendering
            with self._preview_lock:
                self._last_rendered_preview = None

    def is_valid_working_tif(self, path: Path) -> bool:
        """Checks if a working TIFF path is valid for editing."""
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    def get_active_edit_path(self, index: int) -> Path:
        """
        Determines the correct file path to use for editing/exporting based on current mode.

        Rules:
        1. If index invalid, raise IndexError or return None (caller handles).
        2. If image is RAW-only (no paired JPEG and path is RAW ext), force "raw" mode functionality.
           (Note: ImageFile.path is usually the JPEG if it exists. If it's a RAW file, it means orphaned RAW).
        3. If mode is "jpeg": return jpg_path (visual/original).
        4. If mode is "raw":
           - Check for valid developed TIFF. If yes, return it.
           - If no TIFF, return the RAW path itself (RawTherapee will need to develop it,
             or we load it if we support direct RAW - here we likely return raw_path so
             load_image_for_editing can decide to develop it).
        """
        if index < 0 or index >= len(self.image_files):
            raise IndexError("Invalid image index")

        img = self.image_files[index]

        # Check if we are strictly RAW-only (orphaned RAW or just RAW opened)
        # ImageFile.path is the main file. ImageFile.raw_pair is the sidecar RAW.
        # If raw_pair is None but path is a RAW extension, it's RAW-only.
        is_raw_only = False
        from faststack.io.indexer import RAW_EXTENSIONS

        if img.raw_pair is None and img.path.suffix.lower() in RAW_EXTENSIONS:
            is_raw_only = True

        mode = self.current_edit_source_mode
        if is_raw_only:
            mode = "raw"

        if mode == "jpeg":
            return img.path

        # Mode is RAW
        if img.has_working_tif and self.is_valid_working_tif(img.working_tif_path):
            return img.working_tif_path

        if img.raw_pair:
            return img.raw_pair

        # Fallback for RAW-only case where path is the RAW
        return img.path

    @Slot(str)
    def apply_filter(self, filter_string: str):
        filter_string = filter_string.strip()

        if not filter_string:
            self.clear_filter()
            return

        self._filter_string = filter_string
        self._filter_enabled = True
        self._apply_filter_to_cached_list()  # Fast in-memory filtering
        self.display_generation += (
            1  # Invalidate cache keys to prevent showing stale images
        )
        self.dataChanged.emit()
        self.ui_state.filterStringChanged.emit()  # Notify UI of filter change

        # reset to start of filtered list
        self.current_index = 0
        self.sync_ui_state()
        self._do_prefetch(self.current_index)

    @Slot(result=str)
    def get_filter_string(self):
        # return current string, or "" if filter off
        return self._filter_string

    @Slot()
    def clear_filter(self):
        if not self._filter_enabled and not self._filter_string:
            return
        self._filter_enabled = False
        self._filter_string = ""
        self._apply_filter_to_cached_list()  # Fast in-memory filtering
        self.display_generation += (
            1  # Invalidate cache keys to prevent showing stale images
        )
        self.dataChanged.emit()
        self.ui_state.filterStringChanged.emit()  # Notify UI of filter change
        self.current_index = min(self.current_index, max(0, len(self.image_files) - 1))
        self.sync_ui_state()
        self._do_prefetch(self.current_index)

    def get_display_info(self):
        if self.is_zoomed:
            return 0, 0, self.display_generation

        return self.display_width, self.display_height, self.display_generation

    def on_display_size_changed(self, width: int, height: int):
        """Debounces display size change events to prevent spamming resizes."""
        log.debug(
            f"on_display_size_changed called with {width}x{height}. Current: {self.display_width}x{self.display_height}"
        )
        if width <= 0 or height <= 0:
            log.debug("Ignoring invalid resize event")
            return

        # Debounce resize events
        self.pending_width = width
        self.pending_height = height
        self.resize_timer.start(150)  # 150ms debounce

    def _handle_resize(self):
        """Actual resize handler, called after debounce period."""
        log.info(
            "Display size changed to: %dx%d (physical pixels)",
            self.pending_width,
            self.pending_height,
        )
        self.display_width = self.pending_width
        self.display_height = self.pending_height
        self.display_generation += 1  # Invalidates old entries via cache key

        # Mark display as ready after first size report
        is_first_resize = not self.display_ready
        if is_first_resize:
            self.display_ready = True
            log.info("Display size now stable, enabling prefetch")

        self.prefetcher.cancel_all()  # Cancel stale tasks to avoid wasted work

        # On first resize, execute deferred prefetch; on subsequent resizes, do normal prefetch
        if is_first_resize and self.pending_prefetch_index is not None:
            self.prefetcher.update_prefetch(self.pending_prefetch_index)
            self.pending_prefetch_index = None
        else:
            self.prefetcher.update_prefetch(self.current_index)

        self.sync_ui_state()  # To refresh the image

    @Slot(bool)
    def set_zoomed(self, zoomed: bool):
        if self.is_zoomed != zoomed:
            if _debug_mode:
                log.info(f"AppController.set_zoomed: {self.is_zoomed} -> {zoomed}")
            self.is_zoomed = zoomed
            self.is_zoomed_changed.emit(zoomed)
        log.info("Zoom state changed to: %s", zoomed)
        self.display_generation += 1  # Invalidates old entries via cache key

        # Invalidate current image to force reload with new resolution logic
        if self.image_files and self.main_window:
            # Force QML to reload the image by updating the URL generation
            self.ui_refresh_generation += 1
            self.ui_state.currentImageSourceChanged.emit()
            self.main_window.update()  # Force repaint

    # -- Zoom Shortcuts --
    def zoom_100(self):
        log.info("Zoom 100% requested")
        self.ui_state.request_absolute_zoom(1.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic

    def zoom_200(self):
        log.info("Zoom 200% requested")
        self.ui_state.request_absolute_zoom(2.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic

    def zoom_300(self):
        log.info("Zoom 300% requested")
        self.ui_state.request_absolute_zoom(3.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic

    def zoom_400(self):
        log.info("Zoom 400% requested")
        self.ui_state.request_absolute_zoom(4.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic
        # NOTE: We don't clear the cache here. The generation increment is enough.
        # Cache keys include display_generation, so zoomed/unzoomed images become
        # naturally unreachable and LRU will evict them. This lets us instantly
        # reuse cached images if user toggles zoom on/off repeatedly.
        self.prefetcher.cancel_all()  # Cancel stale tasks to avoid wasted work
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()
        self.ui_state.isZoomedChanged.emit()

    def eventFilter(self, watched, event) -> bool:
        # Don't handle key events when a dialog is open
        if self._dialog_open:
            return False

        if watched == self.main_window and event.type() == QEvent.Type.KeyPress:
            # QML handles Crop Enter/Esc keys now.
            # We defer to QML to avoid double-triggering or focus conflicts.
            # handled = self.keybinder.handle_key_press(event) ...

            # Esc closes histogram if visible (priority: before editor/grid handling)
            # This works in both grid and loupe view
            if event.key() == Qt.Key_Escape and self.ui_state.isHistogramVisible:
                self.ui_state.isHistogramVisible = False
                return True  # Consume event, histogram closed

            # When cropping (or editing), let QML handle Enter/Esc and related keys.
            # Otherwise keybinder can swallow them before QML sees them.
            if getattr(self.ui_state, "isCropping", False) or getattr(
                self.ui_state, "isEditorOpen", False
            ):
                return False

            # When in grid view, let QML handle navigation and action keys
            if self._is_grid_view_active:
                key = event.key()
                grid_keys = {
                    Qt.Key_Left,
                    Qt.Key_Right,
                    Qt.Key_Up,
                    Qt.Key_Down,
                    Qt.Key_Return,
                    Qt.Key_Enter,
                    Qt.Key_Space,
                    Qt.Key_B,
                    Qt.Key_Escape,
                    Qt.Key_Delete,
                    Qt.Key_Backspace,  # Delete handled by QML with cursor context
                }
                if key in grid_keys:
                    return False  # Let QML handle it

            handled = self.keybinder.handle_key_press(event)
            if handled:
                return True
        return super().eventFilter(watched, event)

    def _do_prefetch(
        self, index: int, is_navigation: bool = False, direction: Optional[int] = None
    ):
        """Helper to defer prefetch until display size is stable.

        Args:
            index: The index to prefetch around
            is_navigation: True if called from user navigation (arrow keys, etc.)
            direction: 1 for forward, -1 for backward, None to use last direction
        """
        # If navigation occurs during resize debounce, cancel timer and apply resize immediately
        # to ensure prefetch uses correct dimensions
        if is_navigation and self.resize_timer.isActive():
            self.resize_timer.stop()
            self._handle_resize()

        if not self.display_ready:
            log.debug("Display not ready, deferring prefetch for index %d", index)
            self.pending_prefetch_index = index
            return
        self.prefetcher.update_prefetch(
            index, is_navigation=is_navigation, direction=direction
        )

    def load(self, skip_thumbnail_refresh: bool = False):
        """Loads images, sidecar data, and starts services.

        Args:
            skip_thumbnail_refresh: If True, skip thumbnail model refresh (caller already did it).
        """
        self.refresh_image_list()  # Initial scan from disk
        if not self.image_files:
            self.current_index = 0
        else:
            self.current_index = max(
                0, min(self.sidecar.data.last_index, len(self.image_files) - 1)
            )
        self.stacks = self.sidecar.data.stacks  # Load stacks from sidecar
        self.dataChanged.emit()  # Emit after stacks are loaded
        self.watcher.start()
        self._do_prefetch(self.current_index)

        # Defer initial UI sync until after images are loaded
        self.sync_ui_state()

        # Initialize grid view if starting in grid mode (unless already done)
        if self._is_grid_view_active and not skip_thumbnail_refresh:
            self._thumbnail_model.refresh()
            self._path_resolver.update_from_model(self._thumbnail_model)

    def refresh_image_list(self):
        """Rescans the directory for images from disk and updates cache.

        This does a full disk scan and should only be called when:
        - Application starts (load())
        - Directory watcher detects file changes
        - User explicitly refreshes

        For filtering, use _apply_filter_to_cached_list() instead.
        """
        # Clear folder stats cache so subfolder counts are fresh
        clear_raw_count_cache()

        self._all_images = find_images(self.image_dir)
        self._apply_filter_to_cached_list()

        # Refresh thumbnail model if it exists (for external file changes)
        if self._thumbnail_model and self._is_grid_view_active:
            self._thumbnail_model.refresh()

    def _apply_filter_to_cached_list(self):
        """Applies current filter to cached image list without disk I/O."""
        if self._filter_enabled and self._filter_string:
            needle = self._filter_string.lower()
            self.image_files = [
                img for img in self._all_images if needle in img.path.stem.lower()
            ]
        else:
            self.image_files = self._all_images

        self._rebuild_path_to_index()
        self.prefetcher.set_image_files(self.image_files)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.ui_state.imageCountChanged.emit()

    def _rebuild_path_to_index(self):
        """Rebuild path-to-index dict for O(1) lookup in grid_open_index.

        Call this whenever self.image_files is mutated (filter, sort, directory change).
        """
        self._path_to_index = {
            img.path.resolve(): i for i, img in enumerate(self.image_files)
        }

    def get_decoded_image(self, index: int) -> Optional[DecodedImage]:
        """Retrieves a decoded image, blocking until ready to ensure correct display.

        This blocks the UI thread on cache miss, but that's acceptable for an image viewer
        where users expect to see the correct image immediately. The prefetcher minimizes
        cache misses by decoding adjacent images in advance.
        """
        if not self.image_files or index < 0 or index >= len(self.image_files):
            log.warning(
                "get_decoded_image called with empty image_files or out of bounds index."
            )
            return None

        # Debug preview condition
        if self.ui_state.isEditorOpen or self.ui_state.isCropping:
            # Robust path comparison
            editor_path = self.image_editor.current_filepath
            file_path = self.image_files[index].path

            match = False
            if editor_path and file_path:
                try:
                    match = Path(editor_path).resolve() == Path(file_path).resolve()
                except (OSError, ValueError):
                    match = str(editor_path) == str(file_path)

            if not match:
                # Debug log if mismatch
                log.debug(
                    "Path mismatch in preview. Editor: %s, File: %s",
                    editor_path,
                    file_path,
                )

            # Return background-rendered preview if Editor is open OR Cropping is active
            if match and self.image_editor.original_image:
                if self._last_rendered_preview:
                    return self._last_rendered_preview

        _, _, display_gen = self.get_display_info()
        image_path = self.image_files[index].path
        path_str = image_path.as_posix()
        cache_key = build_cache_key(image_path, display_gen)

        # Check cache
        if cache_key in self.image_cache:
            self.image_cache.hits += 1  # Increment hit counter
            self._update_cache_stats()  # Update UI with new stats
            decoded = self.image_cache[cache_key]
            with self._last_image_lock:
                self.last_displayed_image = decoded
            return decoded

        self.image_cache.misses += 1  # Increment miss counter
        self._update_cache_stats()  # Update UI with new stats
        if self.debug_cache:
            prefix = f"{path_str}::"
            cached_gens = [
                key.split("::", 1)[1]
                for key in self.image_cache.keys()
                if key.startswith(prefix)
            ]
            cache_usage_gb = self.image_cache.currsize / (1024**3)
            log.info(
                "Cache miss for %s (index=%d gen=%d). Cached gens: %s. Cache usage=%.2fGB entries=%d",
                image_path.name,
                index,
                display_gen,
                cached_gens or "none",
                cache_usage_gb,
                len(self.image_cache),
            )

        # Cache miss: need to decode synchronously to ensure correct image displays
        if _debug_mode:
            decode_start = time.perf_counter()
            log.info(
                "Cache miss for index %d (gen: %d). Blocking decode.",
                index,
                display_gen,
            )

        # Show decoding indicator if debug cache is enabled
        if self.debug_cache:
            self.ui_state.isDecoding = True
            # Note: processEvents() caused crashes, so the indicator might not update immediately
            # QCoreApplication.processEvents()

        try:
            # Submit with priority=True to cancel pending prefetch tasks and free up workers
            future = self.prefetcher.submit_task(
                index, self.prefetcher.generation, priority=True
            )
            if not future:
                with self._last_image_lock:
                    return self.last_displayed_image

            try:
                # Wait for decode to complete (blocking but fast for JPEGs)
                result = future.result(timeout=5.0)  # 5 second timeout as safety
            except concurrent.futures.TimeoutError:
                log.warning("Timeout decoding image at index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
            except concurrent.futures.CancelledError:
                log.debug("Decode cancelled for index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
            except Exception:
                log.exception("Error decoding image at index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image

            if not result:
                if _debug_mode:
                    log.debug("Decode returned no result for index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image

            decoded_path, decoded_display_gen = result
            cache_key = build_cache_key(decoded_path, decoded_display_gen)
            if cache_key in self.image_cache:
                decoded = self.image_cache[cache_key]
                with self._last_image_lock:
                    self.last_displayed_image = decoded
                if _debug_mode:
                    elapsed = time.perf_counter() - decode_start
                    log.info("Decoded image %d in %.3fs", index, elapsed)
                return decoded
            else:
                if _debug_mode:
                    log.debug(
                        "Decode finished but cache_key missing (index=%d, key=%s)",
                        index,
                        cache_key,
                    )
                with self._last_image_lock:
                    return self.last_displayed_image
        finally:
            # Hide decoding indicator
            if self.debug_cache:
                self.ui_state.isDecoding = False

        with self._last_image_lock:
            return self.last_displayed_image

    def _get_decoded_image_safe(self, index: int) -> Optional[DecodedImage]:
        """Thread-safe version of get_decoded_image for background workers.

        Does NOT update UI iteration or access QObjects.
        """
        if not self.image_files or index < 0 or index >= len(self.image_files):
            return None

        # Lock to ensure thread safety when reading shared state if necessary (though simple reads are usually safe)
        # However, get_display_info reads 'self.is_zoomed' which is fine.
        # Accessing self.image_files is safe as long as list isn't cleared concurrently,
        # which only happens on directory change/refresh on main thread.
        # Since we are in a worker, there's a small race risk if directory changes *while* we run,
        # but the worker would likely just fail gracefully or get an old image.

        _, _, display_gen = self.get_display_info()
        try:
            image_path = self.image_files[index].path
        except IndexError:
            return None

        cache_key = build_cache_key(image_path, display_gen)

        # Check cache (thread-safe read)
        if cache_key in self.image_cache:
            # We don't update stats/hits here to avoid race conditions on those counters
            return self.image_cache[cache_key]

        # Cache miss: decode synchronously (in this worker thread)
        try:
            # Submit with priority=True
            # Note: prefetcher.submit_task logic needs to be thread-safe.
            # Assuming futures dict access in submit_task handles strict GIL/thread safety or we might need locks there.
            # But usually submitting to Executor is thread safe.
            # The danger is 'self.futures' management in Prefetcher.
            future = self.prefetcher.submit_task(
                index, self.prefetcher.generation, priority=True
            )
            if future:
                try:
                    result = future.result(timeout=5.0)
                except concurrent.futures.TimeoutError:
                    log.warning(f"Timeout decoding image at index {index} (background)")
                    return None
                except concurrent.futures.CancelledError:
                    log.debug(
                        f"Decode cancelled for image at index {index} (background)"
                    )
                    return None

                if result:
                    decoded_path, decoded_display_gen = result
                    # Re-verify key
                    cache_key = build_cache_key(decoded_path, decoded_display_gen)
                    if cache_key in self.image_cache:
                        return self.image_cache[cache_key]
        except Exception:
            log.exception("_get_decoded_image_safe failed for index %d", index)

        return None

    def sync_ui_state(self):
        """Forces the UI to update by emitting all state change signals."""
        self.ui_refresh_generation += 1
        self._metadata_cache_index = (-1, -1)  # Invalidate cache

        # tell QML that index and image changed
        self.ui_state.currentIndexChanged.emit()
        self.ui_state.currentImageSourceChanged.emit()
        self.ui_state.highlightStateChanged.emit()  # Notify UI of new highlight stats

        # this is the one your footer needs
        self.ui_state.metadataChanged.emit()

        log.debug(
            "UI State Synced: Index=%d, Count=%d",
            self.ui_state.currentIndex,
            self.ui_state.imageCount,
        )
        log.debug(
            "Metadata Synced: Filename=%s, Uploaded=%s, StackInfo='%s', BatchInfo='%s'",
            self.ui_state.currentFilename,
            self.ui_state.isUploaded,
            self.ui_state.stackInfoText,
            self.ui_state.batchInfoText,
        )

    # --- Image Editor Integration ---

    @Slot()
    def save_edited_image(self):
        """Saves functionality delegating to ImageEditor.

        Restores "Old" behavior:
        - Save image
        - Close Editor
        - Clear Editor State
        - Refresh List
        - Re-select saved image
        """
        if not self.image_editor.original_image:
            return

        # Only write developed sidecar when editing from RAW source
        write_sidecar = self.current_edit_source_mode == "raw"
        dev_path = None
        if write_sidecar and 0 <= self.current_index < len(self.image_files):
            dev_path = self.image_files[self.current_index].developed_jpg_path

        try:
            result = self.image_editor.save_image(
                write_developed_jpg=write_sidecar, developed_path=dev_path
            )
        except RuntimeError as e:
            self.update_status_message(str(e))
            return
        except Exception as e:
            log.exception(f"Unexpected error during save: {e}")
            self.update_status_message("Failed to save image")
            return

        if result:
            saved_path, _ = result  # backup_path unused

            # --- Restore Old Behavior ---

            # 1. Close Editor UI
            self.ui_state.isEditorOpen = False

            # 2. Clear Editor State (release memory)
            self.image_editor.clear()

            # 3. Refresh List (to see new file or updated timestamp)
            self.refresh_image_list()

            # 4. Find and Select the saved image
            new_index = self.current_index  # Default to keeping selection if not found

            # Try to find by exact path match
            if saved_path:
                try:
                    target_resolve = saved_path.resolve()
                    for i, img in enumerate(self.image_files):
                        try:
                            # Robust path comparison
                            if img.path.resolve() == target_resolve:
                                new_index = i
                                break
                        except (OSError, RuntimeError):
                            # Fallback to string compare
                            if str(img.path) == str(saved_path):
                                new_index = i
                                break
                except (OSError, RuntimeError):
                    pass  # Keep current selection if resolution fails

            self.current_index = new_index

            # 5. Force UI Sync / Prefetch
            self.image_cache.clear()  # Clear cache to ensure we reload valid image
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            self.update_status_message("Image saved")
        else:
            self.update_status_message("Failed to save image")

    # --- Actions ---

    def _set_current_index(
        self, index: int, direction: int = 0, is_navigation: bool = True
    ):
        """Centralized method to change current image index and reset state."""
        if index < 0 or index >= len(self.image_files):
            return

        # Reset source mode to JPEG unless new image is strictly RAW-only
        # (This implements the "Default state on navigation" requirement)
        img = self.image_files[index]
        is_raw_only = False
        from faststack.io.indexer import RAW_EXTENSIONS, JPG_EXTENSIONS

        # Robust RAW-only check: Main path is RAW and it's not a JPEG
        is_jpeg_main = img.path.suffix.lower() in JPG_EXTENSIONS
        is_raw_main = img.path.suffix.lower() in RAW_EXTENSIONS
        is_raw_only = is_raw_main and not is_jpeg_main

        new_mode = "raw" if is_raw_only else "jpeg"
        if self.current_edit_source_mode != new_mode:
            self.current_edit_source_mode = new_mode
            self.editSourceModeChanged.emit(new_mode)

        self.current_index = index  # Set index first so signals pick up correct image

        self._reset_crop_settings()
        self._do_prefetch(
            self.current_index, is_navigation=is_navigation, direction=direction
        )
        self.sync_ui_state()

        # Update histogram if visible
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    def next_image(self):
        if self.current_index < len(self.image_files) - 1:
            self._set_current_index(self.current_index + 1, direction=1)

    def prev_image(self):
        if self.current_index > 0:
            self._set_current_index(self.current_index - 1, direction=-1)

    @Slot(int)
    def jump_to_image(self, index: int):
        """Jump to a specific image by index (0-based)."""
        if 0 <= index < len(self.image_files):
            if index == self.current_index:
                self.update_status_message(f"Already at image {index + 1}")
                return
            direction = 1 if index > self.current_index else -1
            self._set_current_index(index, direction=direction)
            self.update_status_message(f"Jumped to image {index + 1}")
        else:
            log.warning("Invalid image index: %d", index)
            self.update_status_message("Invalid image number")

    def show_jump_to_image_dialog(self):
        """Shows the jump to image dialog (called from keybinder)."""
        if self.main_window and hasattr(self.main_window, "show_jump_to_image_dialog"):
            self.main_window.show_jump_to_image_dialog()
        else:
            log.warning(
                "Cannot open jump to image dialog: main_window or function not available"
            )

    def show_exif_dialog(self):
        """Shows the EXIF data dialog."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        path = self.image_files[self.current_index].path
        data = get_exif_data(path)

        if self.main_window and hasattr(self.main_window, "openExifDialog"):
            # Pass data as QVariantMap (dict)
            self.main_window.openExifDialog(data)
        else:
            log.warning(
                "Cannot open EXIF dialog: main_window or openExifDialog not available"
            )

    @Slot()
    def dialog_opened(self):
        """Called when any dialog opens to disable global keybindings."""
        self._dialog_open_count += 1
        if self._dialog_open_count == 1:
            self._dialog_open = True
            self.dialogStateChanged.emit(True)
            log.debug("Dialog opened (count=1), disabling global keybindings")

    @Slot()
    def dialog_closed(self):
        """Called when any dialog closes to re-enable global keybindings."""
        prev = self._dialog_open_count
        self._dialog_open_count = max(0, self._dialog_open_count - 1)
        if prev > 0 and self._dialog_open_count == 0:
            self._dialog_open = False
            self.dialogStateChanged.emit(False)
            log.debug("Dialog closed (count=0), re-enabling global keybindings")

    def toggle_grid_view(self):
        """Toggle between grid view and loupe (single image) view."""
        self._set_grid_view_active(not self._is_grid_view_active)

    def switch_to_grid_view(self):
        """Switch to grid view (from loupe view). Called by Esc key."""
        if not self._is_grid_view_active:
            self._set_grid_view_active(True)

    def _set_grid_view_active(self, active: bool):
        """Set grid view active state and handle side effects."""
        if self._is_grid_view_active == active:
            return

        self._is_grid_view_active = active

        if active:
            # Entering grid view - refresh the model
            self._thumbnail_model.refresh()
            # Update path resolver for the current directory
            self._path_resolver.update_from_model(self._thumbnail_model)

            # Find current loupe image in grid and scroll to it
            if self.image_files and 0 <= self.current_index < len(self.image_files):
                current_path = self.image_files[self.current_index].path
                grid_index = self._thumbnail_model.find_image_index(current_path)
                if grid_index >= 0:
                    # Emit after isGridViewActiveChanged so QML has created the view
                    from PySide6.QtCore import QTimer

                    QTimer.singleShot(
                        0, lambda: self.ui_state.gridScrollToIndex.emit(grid_index)
                    )

            log.info("Switched to grid view")
        else:
            log.info("Switched to loupe view")

        # Notify UI state via signal
        self.ui_state.isGridViewActiveChanged.emit(active)

    def grid_navigate_to(self, path: str):
        """Navigate to a folder in grid view.

        This updates both the grid view AND the main working directory,
        so loupe view will show images from the new folder.

        When navigating up above the current base directory (e.g., going to
        parent when at the initial launch directory), updates base_directory
        to allow continued navigation.
        """
        if not self._is_grid_view_active:
            return

        folder_path = Path(path).resolve()
        if not folder_path.is_dir():
            log.warning("Cannot navigate to non-directory: %s", path)
            return

        # Push current directory to history before navigating
        current_dir = self.image_dir.resolve()
        if current_dir != folder_path:
            self._grid_nav_history.append(current_dir)
            self.ui_state.gridCanGoBackChanged.emit()

        # Check if we're navigating above the current base directory
        # This happens when user clicks ".." at the initial launch directory
        update_base = False
        if self._thumbnail_model:
            base_dir = self._thumbnail_model.base_directory
            try:
                folder_path.relative_to(base_dir)
            except ValueError:
                # folder_path is outside base_directory - we're going up
                update_base = True
                log.info(
                    "Navigating above base directory: %s -> %s", base_dir, folder_path
                )

        # Use canonical directory switch
        self._switch_to_directory(folder_path, update_base_directory=update_base)
        log.info("Grid view navigated to: %s", folder_path)

    def grid_go_back(self):
        """Navigate back to the previous directory in grid view history."""
        if not self._grid_nav_history:
            return

        # Pop the previous directory from history
        prev_dir = self._grid_nav_history.pop()
        self.ui_state.gridCanGoBackChanged.emit()

        if not prev_dir.is_dir():
            log.warning("Previous directory no longer exists: %s", prev_dir)
            return

        # Navigate without adding to history (this is going back, not forward)
        update_base = False
        if self._thumbnail_model:
            base_dir = self._thumbnail_model.base_directory
            try:
                prev_dir.relative_to(base_dir)
            except ValueError:
                update_base = True

        self._switch_to_directory(prev_dir, update_base_directory=update_base)
        log.info("Grid view went back to: %s", prev_dir)

    def grid_open_index(self, index: int):
        """Open an image from grid view in loupe view."""
        entry = self._thumbnail_model.get_entry(index)
        if not entry:
            log.warning("grid_open_index: no entry at index %d", index)
            return

        if entry.is_folder:
            # Navigate into folder instead of opening
            self.grid_navigate_to(str(entry.path))
            return

        # Find this image in the main image list using O(1) lookup
        resolved_path = entry.path.resolve()
        loupe_index = self._path_to_index.get(resolved_path)

        if loupe_index is None:
            # Index might be stale - rebuild and retry once
            self._rebuild_path_to_index()
            loupe_index = self._path_to_index.get(resolved_path)

        if loupe_index is None:
            log.warning(
                "grid_open_index: image not found in current list: %s", entry.path
            )
            # Image might be in a different directory - don't switch view
            return

        # Switch to loupe view first (avoids transient work while still in grid)
        self._set_grid_view_active(False)

        # Then set index with navigation=True for proper state reset and prefetch
        self._set_current_index(loupe_index, is_navigation=True)

        log.info("Opened image from grid: %s", entry.path)

    def grid_delete_at_cursor(self, cursor_index: int):
        """Delete images from grid view - either selection or cursor image.

        If there are selected images, deletes all selected images.
        If no selection, deletes the image at the cursor position.
        """
        if not self._thumbnail_model:
            return

        # Check if there are selections
        selected_paths = self._thumbnail_model.get_selected_paths()
        if selected_paths:
            # Delete all selected images
            self._delete_grid_selected_images(selected_paths)
            return

        # No selection - delete the cursor image
        entry = self._thumbnail_model.get_entry(cursor_index)
        if not entry:
            log.warning("grid_delete_at_cursor: no entry at index %d", cursor_index)
            return

        if entry.is_folder:
            self.update_status_message("Cannot delete folders")
            return

        # Delete this single image using its path
        self._delete_grid_selected_images([entry.path])

    def _on_thumbnail_ready(self, thumbnail_id: str):
        """Callback when a thumbnail finishes decoding (called from worker thread).

        This emits a signal to hop to the GUI thread for thread-safe model updates.
        """
        self._thumbnailReadySignal.emit(thumbnail_id)

    @Slot(str)
    def _on_thumbnail_ready_gui(self, thumbnail_id: str):
        """Handle thumbnail ready on GUI thread (thread-safe)."""
        # Guard against callbacks during/after shutdown
        if getattr(self, "_shutting_down", False):
            return
        if self._thumbnail_model:
            self._thumbnail_model.thumbnailReady.emit(thumbnail_id)

    def _get_metadata_dict(self, stem: str) -> dict:
        """Get metadata for a file stem as a dict for thumbnail model."""
        try:
            meta = self.sidecar.get_metadata(stem)
            return {
                "stacked": getattr(meta, "stacked", False),
                "uploaded": getattr(meta, "uploaded", False),
                "edited": getattr(meta, "edited", False),
                "restacked": getattr(meta, "restacked", False),
            }
        except Exception as e:  # Broad catch for UI plumbing - don't crash grid view
            log.debug("Failed to get metadata for %s: %s", stem, e)
            return {
                "stacked": False,
                "uploaded": False,
                "edited": False,
                "restacked": False,
            }

    def _invalidate_batch_cache(self):
        """Clear the batch indices cache. Call after mutating self.batches."""
        if hasattr(self, "_batch_indices_cache"):
            self._batch_indices_cache = set()
            self._batch_indices_cache_key = None

    def _get_batch_indices(self) -> Set[int]:
        """Get set of all indices that are in any batch (for thumbnail model).

        Cached to avoid O(batch_span) computation on every delegate paint.
        """
        # Check if cache is valid (batches haven't changed)
        cache_key = tuple(tuple(b) for b in self.batches)
        if (
            hasattr(self, "_batch_indices_cache_key")
            and self._batch_indices_cache_key == cache_key
        ):
            return self._batch_indices_cache

        # Rebuild cache
        indices: Set[int] = set()
        for start, end in self.batches:
            for i in range(start, end + 1):
                indices.add(i)

        self._batch_indices_cache = indices
        self._batch_indices_cache_key = cache_key
        return indices

    def _get_current_loupe_index(self) -> int:
        """Get current loupe view index (for thumbnail model)."""
        return self.current_index

    def toggle_uploaded(self):
        """Toggle uploaded flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)

        meta.uploaded = not meta.uploaded
        if meta.uploaded:
            meta.uploaded_date = today
        else:
            meta.uploaded_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "uploaded" if meta.uploaded else "not uploaded"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled uploaded flag to %s for %s", meta.uploaded, stem)

    def toggle_edited(self):
        """Toggle edited flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)

        meta.edited = not meta.edited
        if meta.edited:
            meta.edited_date = today
        else:
            meta.edited_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "edited" if meta.edited else "not edited"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled edited flag to %s for %s", meta.edited, stem)

    def toggle_restacked(self):
        """Toggle restacked flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)

        meta.restacked = not meta.restacked
        if meta.restacked:
            meta.restacked_date = today
        else:
            meta.restacked_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "restacked" if meta.restacked else "not restacked"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled restacked flag to %s for %s", meta.restacked, stem)

    def toggle_stacked(self):
        """Toggle stacked flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)

        meta.stacked = not meta.stacked
        if meta.stacked:
            meta.stacked_date = today
        else:
            meta.stacked_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "stacked" if meta.stacked else "not stacked"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled stacked flag to %s for %s", meta.stacked, stem)

    def get_current_metadata(self) -> Dict:
        if not self.image_files or self.current_index >= len(self.image_files):
            if not self._logged_empty_metadata:
                log.debug(
                    "get_current_metadata: image_files is empty or index out of bounds, returning {}."
                )
                self._logged_empty_metadata = True
            return {}
        self._logged_empty_metadata = False

        # Cache hit check
        cache_key = (self.current_index, self.ui_refresh_generation)
        if cache_key == self._metadata_cache_index:
            return self._metadata_cache

        # Compute and cache
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        stack_info = self._get_stack_info(self.current_index)
        batch_info = self._get_batch_info(self.current_index)

        self._metadata_cache = {
            "filename": self.image_files[self.current_index].path.name,
            "stacked": meta.stacked,
            "stacked_date": meta.stacked_date or "",
            "uploaded": meta.uploaded,
            "uploaded_date": meta.uploaded_date or "",
            "edited": meta.edited,
            "edited_date": meta.edited_date or "",
            "restacked": meta.restacked,
            "restacked_date": meta.restacked_date or "",
            "stack_info_text": stack_info,
            "batch_info_text": batch_info,
        }
        self._metadata_cache_index = cache_key
        return self._metadata_cache

    def begin_new_stack(self):
        self.stack_start_index = self.current_index
        log.info("Stack start marked at index %d", self.stack_start_index)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()  # Update UI to show start marker
        self.sync_ui_state()

    def end_current_stack(self):
        log.info(
            "end_current_stack called. stack_start_index: %s", self.stack_start_index
        )
        if self.stack_start_index is not None:
            start = min(self.stack_start_index, self.current_index)
            end = max(self.stack_start_index, self.current_index)
            self.stacks.append([start, end])
            self.stacks.sort()  # Keep stacks sorted by start index
            self.sidecar.data.stacks = self.stacks
            self.sidecar.save()
            log.info("Defined new stack: [%d, %d]", start, end)
            self.stack_start_index = None
            self._metadata_cache_index = (-1, -1)  # Invalidate cache
            self.dataChanged.emit()  # Notify QML of data change
            self.ui_state.stackSummaryChanged.emit()  # Update stack summary in dialog
            self.sync_ui_state()
        else:
            log.warning("No stack start marked. Press '[' first.")

    def begin_new_batch(self):
        """Mark the start of a new batch for drag-and-drop."""
        self.batch_start_index = self.current_index
        log.info("Batch start marked at index %d", self.batch_start_index)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("Batch start marked")

    def end_current_batch(self):
        """End the current batch and save the range."""
        log.info(
            "end_current_batch called. batch_start_index: %s", self.batch_start_index
        )
        if self.batch_start_index is not None:
            start = min(self.batch_start_index, self.current_index)
            end = max(self.batch_start_index, self.current_index)
            self.batches.append([start, end])
            self.batches.sort()  # Keep batches sorted by start index
            self._invalidate_batch_cache()
            log.info("Defined new batch: [%d, %d]", start, end)
            self.batch_start_index = None
            self._metadata_cache_index = (-1, -1)  # Invalidate cache
            self.dataChanged.emit()
            self.sync_ui_state()
            count = end - start + 1
            self.update_status_message(f"Batch defined: {count} images")
        else:
            log.warning("No batch start marked. Press '{' first.")
            self.update_status_message("No batch start marked")

    def grid_add_selection_to_batch(self):
        """Add grid-selected images to batch."""
        if not self._thumbnail_model:
            return

        selected_paths = self._thumbnail_model.get_selected_paths()
        if not selected_paths:
            self.update_status_message("No images selected in grid.")
            return

        # Build path -> index map for the main image list
        path_to_index = {}
        for i, img in enumerate(self.image_files):
            path_to_index[img.path.resolve()] = i

        # Find indices for selected paths
        indices_to_add = []
        for path in selected_paths:
            resolved = path.resolve()
            if resolved in path_to_index:
                indices_to_add.append(path_to_index[resolved])

        if not indices_to_add:
            self.update_status_message("Selected images not found in current list.")
            return

        # Sort indices and create batch ranges (merge consecutive)
        indices_to_add.sort()
        added_count = 0

        for idx in indices_to_add:
            # Check if already in a batch
            in_batch = False
            for start, end in self.batches:
                if start <= idx <= end:
                    in_batch = True
                    break

            if not in_batch:
                # Add as single-item batch (will be merged below)
                self.batches.append([idx, idx])
                added_count += 1

        if added_count > 0:
            # Sort and merge overlapping/adjacent batches
            self.batches.sort()
            merged_batches = [self.batches[0]] if self.batches else []
            for i in range(1, len(self.batches)):
                last_start, last_end = merged_batches[-1]
                current_start, current_end = self.batches[i]
                if current_start <= last_end + 1:
                    merged_batches[-1] = [last_start, max(last_end, current_end)]
                else:
                    merged_batches.append([current_start, current_end])
            self.batches = merged_batches

            self._invalidate_batch_cache()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            # Refresh grid to show batch badges
            self._thumbnail_model.refresh()

            self.update_status_message(f"Added {added_count} image(s) to batch")
            log.info("Added %d image(s) to batch from grid selection", added_count)
        else:
            self.update_status_message("All selected images already in batch.")

    def remove_from_batch_or_stack(self):
        """Remove current image from any batch or stack it's in."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        removed = False

        # Check and remove from batches
        new_batches = []
        batch_modified = False
        for start, end in self.batches:
            if not batch_modified and start <= self.current_index <= end:
                # This is the batch to modify.

                # Single image batch - remove entirely by not adding anything.
                if start == end:
                    pass
                # Remove from beginning - shift start forward
                elif self.current_index == start:
                    new_batches.append([start + 1, end])
                # Remove from end - shift end backward
                elif self.current_index == end:
                    new_batches.append([start, end - 1])
                # Remove from middle - split into two ranges
                else:
                    new_batches.append([start, self.current_index - 1])
                    new_batches.append([self.current_index + 1, end])

                log.info(
                    "Removed index %d from batch [%d, %d]",
                    self.current_index,
                    start,
                    end,
                )
                self.update_status_message("Removed from batch")
                removed = True
                batch_modified = True
            else:
                new_batches.append([start, end])

        if batch_modified:
            self.batches = new_batches
            self._invalidate_batch_cache()

        # Check and remove from stacks
        # Check and remove from stacks
        if not removed:
            new_stacks = []
            stack_modified = False
            for start, end in self.stacks:
                if not stack_modified and start <= self.current_index <= end:
                    # This is the stack to modify.

                    # Single image stack - remove entirely.
                    if start == end:
                        pass
                    # Remove from beginning
                    elif self.current_index == start:
                        new_stacks.append([start + 1, end])
                    # Remove from end
                    elif self.current_index == end:
                        new_stacks.append([start, end - 1])
                    # Remove from middle
                    else:
                        new_stacks.append([start, self.current_index - 1])
                        new_stacks.append([self.current_index + 1, end])

                    log.info(
                        "Removed index %d from stack [%d, %d]",
                        self.current_index,
                        start,
                        end,
                    )
                    self.update_status_message("Removed from stack")
                    removed = True
                    stack_modified = True
                else:
                    new_stacks.append([start, end])

            if stack_modified:
                self.stacks = new_stacks
                self.sidecar.data.stacks = self.stacks
                self.sidecar.save()
        if removed:
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.ui_state.stackSummaryChanged.emit()
            self.sync_ui_state()
        else:
            self.update_status_message("Not in any batch or stack")

    def toggle_batch_membership(self):
        """Toggles the current image's inclusion in a batch."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        index_to_toggle = self.current_index

        # Check if the image is already in a batch
        in_batch = False
        for start, end in self.batches:
            if start <= index_to_toggle <= end:
                in_batch = True
                break

        new_batches = []
        if in_batch:
            # Remove from batch
            item_removed = False
            for start, end in self.batches:
                if not item_removed and start <= index_to_toggle <= end:
                    if start < index_to_toggle:
                        new_batches.append([start, index_to_toggle - 1])
                    if index_to_toggle < end:
                        new_batches.append([index_to_toggle + 1, end])
                    item_removed = True
                else:
                    new_batches.append([start, end])
            self.batches = new_batches
            self.update_status_message("Removed image from batch")
            log.info("Removed index %d from a batch.", index_to_toggle)
        else:
            # Add to batch - merge with adjacent batches if possible
            if not self.batches:
                self.batches.append([index_to_toggle, index_to_toggle])
                self.update_status_message("Created new batch with current image.")
                log.info(
                    "No existing batches. Created new batch for index %d.",
                    index_to_toggle,
                )
            else:
                # Check if adjacent to any existing batch
                merged = False
                for i, (start, end) in enumerate(self.batches):
                    # Adjacent to start of batch
                    if index_to_toggle == start - 1:
                        self.batches[i] = [index_to_toggle, end]
                        merged = True
                        break
                    # Adjacent to end of batch
                    elif index_to_toggle == end + 1:
                        self.batches[i] = [start, index_to_toggle]
                        merged = True
                        break

                if not merged:
                    # Not adjacent to any batch, create new one
                    self.batches.append([index_to_toggle, index_to_toggle])

                # Sort and merge any overlapping batches
                self.batches.sort()
                merged_batches = [self.batches[0]] if self.batches else []
                for i in range(1, len(self.batches)):
                    last_start, last_end = merged_batches[-1]
                    current_start, current_end = self.batches[i]
                    if current_start <= last_end + 1:
                        merged_batches[-1] = [last_start, max(last_end, current_end)]
                    else:
                        merged_batches.append([current_start, current_end])
                self.batches = merged_batches

                self.update_status_message("Added image to batch")
                log.info("Added index %d to batch.", index_to_toggle)

        self._invalidate_batch_cache()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()

    def toggle_stack_membership(self):
        """Toggles the current image's inclusion in a stack."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        index_to_toggle = self.current_index

        # Check if the image is already in a stack
        stack_to_modify_idx = -1
        for i, (start, end) in enumerate(self.stacks):
            if start <= index_to_toggle <= end:
                stack_to_modify_idx = i
                break

        if stack_to_modify_idx != -1:
            # --- Remove from existing stack ---
            new_stacks = []
            item_removed = False
            for i, (start, end) in enumerate(self.stacks):
                if not item_removed and i == stack_to_modify_idx:
                    if start < index_to_toggle:
                        new_stacks.append([start, index_to_toggle - 1])
                    if index_to_toggle < end:
                        new_stacks.append([index_to_toggle + 1, end])
                    item_removed = True
                else:
                    new_stacks.append([start, end])
            self.stacks = new_stacks
            self.update_status_message("Removed image from stack")
            log.info(
                "Removed index %d from stack #%d.",
                index_to_toggle,
                stack_to_modify_idx + 1,
            )

        else:
            # --- Add to nearest stack ---
            if not self.stacks:
                self.stacks.append([index_to_toggle, index_to_toggle])
                self.update_status_message("Created new stack with current image.")
                log.info(
                    "No existing stacks. Created new stack for index %d.",
                    index_to_toggle,
                )
            else:
                # Find closest stack
                dist_backward = float("inf")
                stack_idx_backward = -1
                for i in range(index_to_toggle - 1, -1, -1):
                    for j, (start, end) in enumerate(self.stacks):
                        if start <= i <= end:
                            dist_backward = index_to_toggle - i
                            stack_idx_backward = j
                            break
                    if stack_idx_backward != -1:
                        break

                dist_forward = float("inf")
                stack_idx_forward = -1
                for i in range(index_to_toggle + 1, len(self.image_files)):
                    for j, (start, end) in enumerate(self.stacks):
                        if start <= i <= end:
                            dist_forward = i - index_to_toggle
                            stack_idx_forward = j
                            break
                    if stack_idx_forward != -1:
                        break

                if stack_idx_backward == -1 and stack_idx_forward == -1:
                    # This case should not be reached if `if not self.stacks` handles it.
                    self.stacks.append([index_to_toggle, index_to_toggle])
                    self.update_status_message("Created new stack with current image.")
                    log.info(
                        "No stacks found nearby. Created new stack for index %d.",
                        index_to_toggle,
                    )
                else:
                    if dist_backward <= dist_forward:
                        stack_to_join_idx = stack_idx_backward
                    else:
                        stack_to_join_idx = stack_idx_forward

                    start, end = self.stacks[stack_to_join_idx]
                    self.stacks[stack_to_join_idx] = [
                        min(start, index_to_toggle),
                        max(end, index_to_toggle),
                    ]

                    # Merge overlapping stacks
                    self.stacks.sort()
                    merged_stacks = [self.stacks[0]] if self.stacks else []
                    for i in range(1, len(self.stacks)):
                        last_start, last_end = merged_stacks[-1]
                        current_start, current_end = self.stacks[i]
                        if current_start <= last_end + 1:
                            merged_stacks[-1] = [last_start, max(last_end, current_end)]
                        else:
                            merged_stacks.append([current_start, current_end])
                    self.stacks = merged_stacks

                    # Find the new stack index for the status message
                    new_stack_idx = -1
                    for i, (start, end) in enumerate(self.stacks):
                        if start <= index_to_toggle <= end:
                            new_stack_idx = i
                            break

                    self.update_status_message(
                        f"Added image to Stack #{new_stack_idx + 1}"
                    )
                    log.info(
                        "Added index %d to stack #%d.",
                        index_to_toggle,
                        new_stack_idx + 1,
                    )

        self.sidecar.data.stacks = self.stacks
        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()

    def _reset_crop_settings(self):
        """Resets crop settings to default (full image) and exits crop mode, and resets rotation."""
        if self.ui_state.isCropping:
            self.ui_state.isCropping = False
            self.update_status_message("Crop mode exited")
        self.ui_state.currentCropBox = (0, 0, 1000, 1000)
        # Also clear any editor-side crop box in case it's not fully synced yet
        self.image_editor.set_crop_box((0, 0, 1000, 1000))
        # Reset rotation and straighten angle
        self.image_editor.set_edit_param("rotation", 0)
        self.image_editor.set_edit_param("straighten_angle", 0.0)
        # Also update UI state for rotation values if they are exposed
        if hasattr(self.ui_state, "rotation"):
            self.ui_state.rotation = 0
        if hasattr(
            self.ui_state, "cropRotation"
        ):  # This is used by Components.qml for the overlay
            self.ui_state.cropRotation = 0.0

        # Also reset the straighten angle in current_edits since it affects rotation logic
        if "straighten_angle" in self.image_editor.current_edits:
            self.image_editor.current_edits["straighten_angle"] = 0.0

    def launch_helicon(self):
        """Launches Helicon with selected files (RAW preferred, JPG fallback) or stacks."""
        if self.stacks:
            log.info("Launching Helicon for %d defined stacks.", len(self.stacks))
            any_success = False
            for start, end in self.stacks:
                files_to_process = []
                for idx in range(start, end + 1):
                    if idx < len(self.image_files):
                        img_file = self.image_files[idx]
                        # Use RAW if available, otherwise use JPG
                        file_to_use = (
                            img_file.raw_pair if img_file.raw_pair else img_file.path
                        )
                        files_to_process.append(file_to_use)

                if files_to_process:
                    success = self._launch_helicon_with_files(files_to_process)
                    if success:
                        any_success = True
                else:
                    log.warning("No valid files found for stack [%d, %d].", start, end)

            # Only clear stacks if at least one launch succeeded
            if any_success:
                self.clear_all_stacks()

        else:
            log.warning("No selection or stacks defined to launch Helicon Focus.")
            return

        self.sync_ui_state()

    def _launch_helicon_with_files(self, files: List[Path]) -> bool:
        """Helper to launch Helicon with a specific list of files (RAW or JPG).

        Returns:
            True if Helicon was successfully launched, False otherwise.
        """
        log.info("Launching Helicon Focus with %d files.", len(files))
        unique_files = sorted(list(set(files)))
        success, tmp_path = launch_helicon_focus(unique_files)
        if success and tmp_path:
            # Schedule delayed deletion of the temporary file
            QTimer.singleShot(5000, lambda: self._delete_temp_file(tmp_path))

            # Record stacking metadata
            today = date.today().isoformat()
            for file_path in unique_files:
                # Find the corresponding image file to get the stem
                for img_file in self.image_files:
                    # Match by either RAW pair or JPG path
                    if img_file.raw_pair == file_path or img_file.path == file_path:
                        stem = img_file.path.stem
                        meta = self.sidecar.get_metadata(stem)
                        meta.stacked = True
                        meta.stacked_date = today
                        break
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)  # Invalidate cache

        return success

    def _delete_temp_file(self, tmp_path: Path):
        """Deletes the temporary file list passed to Helicon Focus."""
        if tmp_path.exists():
            try:
                os.remove(tmp_path)
                log.info("Deleted temporary file: %s", tmp_path)
            except OSError as e:
                log.error("Error deleting temporary file %s: %s", tmp_path, e)

    def clear_all_stacks(self):
        log.info("Clearing all defined stacks.")
        self.stacks = []
        self.stack_start_index = None
        # Do NOT clear batches here

        self.sidecar.data.stacks = self.stacks
        self.sidecar.save()

        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()
        self.update_status_message("All stacks cleared")

    def clear_all_batches(self):
        """Clear all defined batches."""
        log.info("Clearing all defined batches.")
        self.batches = []
        self.batch_start_index = None
        self._invalidate_batch_cache()

        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("All batches cleared")

    def get_helicon_path(self):
        return config.get("helicon", "exe")

    def set_helicon_path(self, path):
        config.set("helicon", "exe", path)
        config.save()

    def get_photoshop_path(self):
        return config.get("photoshop", "exe")

    def set_photoshop_path(self, path):
        config.set("photoshop", "exe", path)
        config.save()

    def get_rawtherapee_path(self):
        return config.get("rawtherapee", "exe")

    def set_rawtherapee_path(self, path):
        config.set("rawtherapee", "exe", path)
        config.save()

    def open_file_dialog(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilter("Executables (*.exe)")
        if dialog.exec():
            return dialog.selectedFiles()[0]
        return ""

    def check_path_exists(self, path):
        return os.path.exists(path)

    def get_cache_size(self):
        return config.getfloat("core", "cache_size_gb")

    def get_cache_usage_gb(self):
        """Returns current cache usage in GB."""
        return self.image_cache.currsize / (1024**3)

    def set_cache_size(self, size):
        """Update cache size at runtime and persist to config."""
        size = max(0.5, min(size, 16.0))  # enforce sane bounds
        config.set("core", "cache_size_gb", size)
        config.save()

        old_max_bytes = self.image_cache.max_bytes
        new_max_bytes = int(size * 1024**3)
        if old_max_bytes == new_max_bytes:
            return

        log.info(
            "Resizing decoded image cache from %.2f GB to %.2f GB",
            old_max_bytes / (1024**3),
            size,
        )
        self.image_cache.max_bytes = new_max_bytes

        # If the new size is smaller than current usage, evict until under limit
        while self.image_cache.currsize > new_max_bytes and len(self.image_cache) > 0:
            try:
                self.image_cache.popitem()
            except KeyError:
                break

        # Allow future warnings after expanding the cache
        if new_max_bytes > old_max_bytes:
            self._has_warned_cache_full = False

    def get_prefetch_radius(self):
        return config.getint("core", "prefetch_radius")

    def set_prefetch_radius(self, radius):
        config.set("core", "prefetch_radius", radius)
        config.save()
        self.prefetcher.prefetch_radius = radius
        self.prefetcher.update_prefetch(self.current_index)

    def get_theme(self):
        return 0 if config.get("core", "theme") == "dark" else 1

    def set_theme(self, theme_index):
        # update Python-side state
        self.ui_state.theme = theme_index

        # persist it
        theme = "dark" if theme_index == 0 else "light"
        config.set("core", "theme", theme)
        config.save()

        # tell QML it changed (once is enough)
        self.ui_state.themeChanged.emit()

    @Slot(result=str)
    def get_color_mode(self):
        """Returns current color management mode: 'none', 'saturation', or 'icc'."""
        return config.get("color", "mode", fallback="none")

    @Slot(str)
    def set_color_mode(self, mode: str):
        """Sets color management mode and clears cache to force re-decode."""
        mode = mode.lower()
        if mode not in ["none", "saturation", "icc"]:
            log.error("Invalid color mode: %s", mode)
            return

        log.info("Setting color mode to: %s", mode)
        config.set("color", "mode", mode)
        config.save()

        # Clear ICC caches when color mode changes
        clear_icc_caches()

        # Clear cache and restart prefetcher to apply new color mode
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.display_generation += 1
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()

        # Notify QML that color mode changed
        self.ui_state.colorModeChanged.emit()

        # Update status message
        mode_names = {
            "none": "Original Colors",
            "saturation": "Saturation Compensation",
            "icc": "Full ICC Profile",
        }
        self.update_status_message(f"Color mode: {mode_names.get(mode, mode)}")

    @Slot(result=float)
    def get_saturation_factor(self):
        """Returns current saturation factor (0.0-1.0)."""
        return config.getfloat("color", "saturation_factor", fallback=0.85)

    @Slot(float)
    def set_saturation_factor(self, factor: float):
        """Sets saturation factor and refreshes images."""
        factor = max(0.0, min(1.0, factor))  # Clamp to 0-1
        log.info("Setting saturation factor to: %.2f", factor)
        config.set("color", "saturation_factor", str(factor))
        config.save()

        # Only refresh if in saturation mode
        if self.get_color_mode() == "saturation":
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self.display_generation += 1
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

        # Notify QML
        self.ui_state.saturationFactorChanged.emit()

    @Slot(result=str)
    def get_awb_mode(self):
        return config.get("awb", "mode")

    @Slot(str)
    def set_awb_mode(self, mode):
        config.set("awb", "mode", mode)
        config.save()

    @Slot(result=float)
    def get_awb_strength(self):
        return config.getfloat("awb", "strength")

    @Slot(float)
    def set_awb_strength(self, value):
        config.set("awb", "strength", value)
        config.save()

        # Refresh if AWB was recently applied
        if self.get_color_mode() in ["saturation", "icc"]:
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self.display_generation += 1
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

    @Slot(float)
    @Slot(float, float)
    def set_straighten_angle(self, angle: float, target_aspect_ratio: float = -1.0):
        """Sets the straighten angle for the image editor and updates current view."""
        if not (self.ui_state.isEditorOpen or self.ui_state.isCropping):
            return

        # Optimization: Assume image is loaded by toggle_crop_mode or open_editor.
        # Avoid disk I/O here to prevent stutter during drag.
        if not self.image_editor.original_image:
            return

        # log.info(f"AppController.set_straighten_angle: {angle}, AR: {target_aspect_ratio}")

        # Update Aspect Ratio Compensation for Crop Box
        # If we have a target aspect ratio, we need to adjust the normalized crop box
        # because the underlying canvas aspect ratio changes with rotation (expand=True).
        if target_aspect_ratio > 0 and self.ui_state.currentCropBox:
            left, top, right, bottom = self.ui_state.currentCropBox
            w_norm = right - left
            h_norm = bottom - top

            if w_norm > 0 and h_norm > 0:
                # Calculate new canvas dimensions
                # PIL expand=True logic:
                im_w, im_h = self.image_editor.original_image.size
                # math imported at top level
                rad = math.radians(abs(angle))
                # New dimensions
                new_w = abs(im_w * math.cos(rad)) + abs(im_h * math.sin(rad))
                new_h = abs(im_w * math.sin(rad)) + abs(im_h * math.cos(rad))

                if new_w > 0 and new_h > 0:
                    canvas_aspect = new_w / new_h

                    # We want PixelAspect = (w_norm * new_w/1000) / (h_norm * new_h/1000) = target_aspect
                    # (w_norm / h_norm) * (new_w / new_h) = target_aspect
                    # w_norm / h_norm = target_aspect / canvas_aspect

                    target_norm_ratio = target_aspect_ratio / canvas_aspect

                    # Adjust dimensions to match target_norm_ratio
                    # Simple: Preserve Width, adjust Height.

                    new_h_norm = w_norm / target_norm_ratio

                    # If new height exceeds bounds (1000), constrain and adjust width instead
                    if new_h_norm > 1000:
                        new_h_norm = 1000
                        w_norm = new_h_norm * target_norm_ratio
                    # Recenter height
                    cy = (top + bottom) / 2
                    top = cy - new_h_norm / 2
                    bottom = cy + new_h_norm / 2

                    # Clamp vertical
                    if top < 0:
                        bottom -= top  # shift down
                        top = 0
                    if bottom > 1000:
                        top -= bottom - 1000  # shift up
                        bottom = 1000
                        if top < 0:
                            top = 0  # double clamp

                    # Recenter width (if changed)
                    cx = (left + right) / 2
                    left = cx - w_norm / 2
                    right = cx + w_norm / 2

                    # Clamp horizontal
                    if left < 0:
                        right -= left
                        left = 0
                    if right > 1000:
                        left -= right - 1000
                        right = 1000
                        if left < 0:
                            left = 0

                    self.ui_state.currentCropBox = (left, top, right, bottom)
                    self.image_editor.set_crop_box((left, top, right, bottom))

        log.debug(f"AppController.set_straighten_angle: {angle}")
        # Pass the angle as-is (degrees CW).
        # QML rotation is CW-positive.
        # ImageEditor expects CW-positive and handles the inversion for PIL internally.
        self.image_editor.set_edit_param("straighten_angle", angle)

        # Trigger refresh. Since we are editing, we are viewing the preview.
        # Incrementing display generation invalidates cache, but for preview it just ensures freshness if logic depends on it.
        # Crucially, sync_ui_state emits currentImageSourceChanged, forcing QML to reload.
        # self.display_generation += 1
        # self.sync_ui_state() # DISABLE TO PREVENT FLASHING - QML handles preview live

    @Slot(result=int)
    def get_awb_warm_bias(self):
        return config.getint("awb", "warm_bias")

    @Slot(int)
    def set_awb_warm_bias(self, value):
        config.set("awb", "warm_bias", value)
        config.save()

    @Slot(result=int)
    def get_awb_tint_bias(self):
        return config.getint("awb", "tint_bias", fallback=0)

    @Slot(int)
    def set_awb_tint_bias(self, value):
        config.set("awb", "tint_bias", value)
        config.save()

    @Slot(result=int)
    def get_awb_luma_lower_bound(self):
        return config.getint("awb", "luma_lower_bound")

    @Slot(int)
    def set_awb_luma_lower_bound(self, value):
        config.set("awb", "luma_lower_bound", value)
        config.save()

    @Slot(result=int)
    def get_awb_luma_upper_bound(self):
        return config.getint("awb", "luma_upper_bound")

    @Slot(int)
    def set_awb_luma_upper_bound(self, value):
        config.set("awb", "luma_upper_bound", value)
        config.save()

    @Slot(result=int)
    def get_awb_rgb_lower_bound(self):
        return config.getint("awb", "rgb_lower_bound")

    @Slot(int)
    def set_awb_rgb_lower_bound(self, value):
        config.set("awb", "rgb_lower_bound", value)
        config.save()

    @Slot(result=int)
    def get_awb_rgb_upper_bound(self):
        return config.getint("awb", "rgb_upper_bound")

    @Slot(int)
    def set_awb_rgb_upper_bound(self, value):
        config.set("awb", "rgb_upper_bound", value)
        config.save()

    def get_default_directory(self):
        return config.get("core", "default_directory")

    def set_default_directory(self, path):
        config.set("core", "default_directory", path)
        config.save()

    def get_optimize_for(self):
        return config.get("core", "optimize_for", fallback="speed")

    def set_optimize_for(self, optimize_for):
        old_value = config.get("core", "optimize_for", fallback="speed")
        config.set("core", "optimize_for", optimize_for)
        config.save()

        # If the setting changed, clear cache and redraw current image
        if old_value != optimize_for:
            log.info(
                f"Optimize for changed from {old_value} to {optimize_for}, clearing cache and redrawing"
            )
            self.image_cache.clear()
            # Force redraw of current image
            if self.current_index >= 0 and self.current_index < len(self.image_files):
                self.ui_state.currentImageSourceChanged.emit()

    @Slot(result=float)
    def get_auto_level_clipping_threshold(self):
        return self.auto_level_threshold

    @Slot(float)
    def set_auto_level_clipping_threshold(self, value):
        # Clamp to 0-1 range for safety
        value = max(0.0, min(1.0, value))
        self.auto_level_threshold = value
        # Store as formatted string to avoid scientific notation weirdness or precision issues
        config.set("core", "auto_level_threshold", f"{value:.6g}")
        config.save()

    @Slot(result=float)
    def get_auto_level_strength(self):
        return self.auto_level_strength

    @Slot(float)
    def set_auto_level_strength(self, value):
        # Clamp to 0-1 range
        value = max(0.0, min(1.0, value))
        self.auto_level_strength = value
        config.set("core", "auto_level_strength", f"{value:.6g}")
        config.save()

    @Slot(result=bool)
    def get_auto_level_strength_auto(self):
        return self.auto_level_strength_auto

    @Slot(bool)
    def set_auto_level_strength_auto(self, value):
        self.auto_level_strength_auto = value
        # Store as canonical lowercase string
        config.set("core", "auto_level_strength_auto", "true" if value else "false")
        config.save()

    def open_directory_dialog(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        if dialog.exec():
            return dialog.selectedFiles()[0]
        return ""

    def _switch_to_directory(
        self, folder_path: Path, update_base_directory: bool = True
    ):
        """Canonical directory switch - used by both open_folder() and grid_navigate_to().

        Args:
            folder_path: The directory to switch to.
            update_base_directory: If True, also updates the thumbnail model's base directory
                                   (for File -> Open Folder). If False, keeps existing base
                                   (for grid navigation within current base).
        """
        # Stop the old watcher
        if self.watcher:
            self.watcher.stop()

        # Update the directory path
        self.image_dir = folder_path

        # Reinitialize directory-bound components
        self.watcher = Watcher(self.image_dir, self.refresh_image_list)
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)

        # Only update recycle bin when switching base directories (not subfolder navigation)
        # This ensures all deleted files go to the same recycle bin
        if update_base_directory:
            self.recycle_bin_dir = self.image_dir / "image recycle bin"
            # Only clear history when switching base directories
            self.delete_history = []
            self.undo_history = []
            # Clear grid navigation history (don't allow "back" to previous base folder)
            self._grid_nav_history.clear()
            self.ui_state.gridCanGoBackChanged.emit()

        # Clear directory-specific state
        self.stacks = []
        self.batches = []
        self.batch_start_index = None
        self.stack_start_index = None

        # Clear caches since they reference old directory's images
        with self._last_image_lock:
            self.last_displayed_image = None
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.display_generation += 1
        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)

        # Clear batch indices cache (avoids stale batch membership checks)
        if hasattr(self, "_batch_indices_cache"):
            self._batch_indices_cache = set()
            self._batch_indices_cache_key = None

        # Clear editor state if open
        self.image_editor.clear()

        # Clear thumbnail cache BEFORE refresh to avoid stale thumbs
        if self._thumbnail_cache:
            self._thumbnail_cache.clear()

        # Update thumbnail view infrastructure
        if self._thumbnail_model:
            if update_base_directory:
                self._thumbnail_model.set_directories(self.image_dir, self.image_dir)
                self._thumbnail_model.refresh()  # set_directories doesn't refresh
            else:
                # navigate_to() already calls refresh() internally
                self._thumbnail_model.navigate_to(self.image_dir)
            self._path_resolver.update_from_model(self._thumbnail_model)
            self.ui_state.gridDirectoryChanged.emit(str(self.image_dir))

        # Notify that the current directory changed (for window title)
        self.ui_state.currentDirectoryChanged.emit()

        # Load images from new directory (thumbnail model already refreshed above)
        self.load(skip_thumbnail_refresh=True)

    @Slot()
    def open_folder(self):
        """Opens a directory dialog and reloads the application with the selected folder."""
        path = self.open_directory_dialog()
        if path:
            self._switch_to_directory(Path(path), update_base_directory=True)

    def preload_all_images(self):
        if self.ui_state.isPreloading:
            log.info("Preloading is already in progress.")
            return

        log.info("Starting to preload all images, skipping cached.")
        self.ui_state.isPreloading = True
        self.ui_state.preloadProgress = 0

        self.reporter = self.ProgressReporter()
        self.reporter.progress_updated.connect(self._update_preload_progress)
        self.reporter.finished.connect(self._finish_preloading)

        total_images = len(self.image_files)
        if total_images == 0:
            log.info("No images to preload.")
            self.ui_state.isPreloading = False
            self.ui_state.preloadProgress = 0
            return

        # --- Check for cached images ---
        images_to_preload = []
        already_cached_count = 0
        _, _, display_gen = self.get_display_info()

        # We want to load images furthest from the current index FIRST,
        # and images closest to the current index LAST.
        # This ensures that the images the user is currently looking at (and their neighbors)
        # are the most recently added to the LRU cache, so they won't be evicted.

        # Calculate distance for all images
        # (index, distance_from_current)
        all_images_with_dist = []
        for i in range(total_images):
            dist = abs(i - self.current_index)
            all_images_with_dist.append((i, dist))

        # Sort by distance descending (furthest first)
        all_images_with_dist.sort(key=lambda x: x[1], reverse=True)

        # Determine which images are "nearby" (e.g. within prefetch radius * 2)
        # We will FORCE these to be re-cached even if they are already in cache,
        # to ensure they are moved to the front of the LRU queue.
        nearby_radius = self.prefetcher.prefetch_radius * 2

        for i, dist in all_images_with_dist:
            if i >= len(self.image_files):
                continue
            image_path = self.image_files[i].path
            cache_key = build_cache_key(image_path, display_gen)
            is_cached = cache_key in self.image_cache
            is_nearby = dist <= nearby_radius

            if is_cached and not is_nearby:
                already_cached_count += 1
            else:
                # Add to preload list if it's not cached OR if it's nearby (to refresh LRU)
                images_to_preload.append(i)

        log.info(
            f"Found {already_cached_count} cached images (skipping). Preloading {len(images_to_preload)} images (including nearby refreshes)."
        )

        if not images_to_preload:
            log.info("All images are already cached.")
            self._update_preload_progress(100)
            self._finish_preloading()
            return

        # --- Setup progress tracking ---
        # `completed` starts at the number of images already cached (that we are skipping).
        completed = already_cached_count

        # Update initial progress
        initial_progress = int((completed / total_images) * 100)
        self._update_preload_progress(initial_progress)

        def _on_done(_future):
            nonlocal completed
            completed += 1
            progress = int((completed / total_images) * 100)
            self.reporter.progress_updated.emit(progress)
            # Check if all images (including cached ones) are accounted for
            if completed == total_images:
                self.reporter.finished.emit()

        # --- Submit tasks ---
        # images_to_preload is already sorted furthest -> nearest
        for i in images_to_preload:
            # For nearby images that we are forcing to re-cache, we might need to remove them first
            # to ensure the cache actually updates the LRU position (depending on cache implementation).
            # ByteLRUCache (cachetools) updates LRU on access (get/set), so just overwriting is fine.
            # But we need to make sure we don't skip the task in prefetcher if it thinks it's already done.
            # The prefetcher checks self.futures, but we are submitting new ones.

            future = self.prefetcher.submit_task(i, self.prefetcher.generation)
            if future:
                future.add_done_callback(_on_done)

    def _update_preload_progress(self, progress: int):
        log.debug("Updating preload progress in UI: %d%%", progress)
        self.ui_state.preloadProgress = progress

    def _finish_preloading(self):
        self.ui_state.isPreloading = False
        self.ui_state.preloadProgress = 0
        log.info("Finished preloading all images.")

    @Slot(result=int)
    def get_batch_count_for_current_image(self) -> int:
        """Get the count of images in the batch that contains the current image."""
        if not self.image_files:
            return 0

        # Check if current image is in any batch
        for start, end in self.batches:
            if start <= self.current_index <= end:
                # Calculate total count across all batches
                total_count = sum(end - start + 1 for start, end in self.batches)
                return total_count

        return 0

    @Slot()
    def delete_current_image(self):
        """Moves current JPG and RAW to recycle bin. Shows dialog if multiple images in batch."""
        # Check if in grid view with selections - delete grid selection instead
        if self._is_grid_view_active and self._thumbnail_model:
            selected_paths = self._thumbnail_model.get_selected_paths()
            if selected_paths:
                self._delete_grid_selected_images(selected_paths)
                return

        if not self.image_files:
            self.update_status_message("No image to delete.")
            return

        # Check if current image is in a batch with multiple images
        batch_count = self.get_batch_count_for_current_image()

        if batch_count > 1:
            # Show dialog asking what to delete
            if hasattr(self, "main_window") and self.main_window:
                # Set batch count in dialog and open it
                self.main_window.show_delete_batch_dialog(batch_count)
            return

        # Single image deletion - proceed normally
        self._delete_single_image(self.current_index)

    def _move_to_recycle(self, src: Path) -> Optional[Path]:
        """Moves a file to the recycle bin safely, handling collisions and cross-device moves."""
        if not src.exists() or not src.is_file():
            return None

        # Create recycle bin in the same folder as the source file
        recycle_bin = src.parent / "image recycle bin"

        # Ensure recycle bin exists
        try:
            recycle_bin.mkdir(parents=True, exist_ok=True)
            self.active_recycle_bins.add(recycle_bin)
        except OSError as e:
            log.error("Failed to create recycle bin: %s", e)
            return None

        dest = recycle_bin / src.name

        # Handle collisions with timestamp loop
        if dest.exists():
            timestamp = int(time.time())
            base_name = f"{src.stem}.{timestamp}"
            dest = recycle_bin / f"{base_name}{src.suffix}"
            counter = 1
            while dest.exists():
                dest = recycle_bin / f"{base_name}_{counter}{src.suffix}"
                counter += 1

        try:
            shutil.move(str(src), str(dest))
            log.info("Moved %s to recycle bin: %s", src.name, dest.name)
            return dest
        except OSError as e:
            log.error("Failed to recycle %s: %s", src.name, e)
            return None

    def _ensure_recycle_bin_dir(self) -> bool:
        """Try to create the recycle bin directory.

        Returns:
            True if recycle bin exists or was created successfully.
            False if creation failed (e.g., permission denied).
        """
        from faststack.io.deletion import ensure_recycle_bin_dir

        return ensure_recycle_bin_dir(self.recycle_bin_dir)

    def _confirm_permanent_delete(self, image_file, reason: str = "") -> bool:
        """Show a confirmation dialog for permanent deletion of a single image.

        Args:
            image_file: The ImageFile to delete permanently.
            reason: Reason for permanent deletion (e.g., "Recycle bin unavailable").

        Returns:
            True if user confirms deletion, False if cancelled.
        """
        from faststack.io.deletion import confirm_permanent_delete

        return confirm_permanent_delete(image_file, reason)

    def _confirm_batch_permanent_delete(self, images: list, reason: str = "") -> bool:
        """Show a confirmation dialog for permanent deletion of multiple images.

        Args:
            images: List of ImageFile objects to delete permanently.
            reason: Reason for permanent deletion.

        Returns:
            True if user confirms deletion, False if cancelled.
        """
        from faststack.io.deletion import confirm_batch_permanent_delete

        return confirm_batch_permanent_delete(images, reason)

    def _permanently_delete_image_files(self, image_file) -> bool:
        """Permanently delete an image and its RAW pair from disk.

        This does NOT add to undo history since deletion is permanent.

        Args:
            image_file: The ImageFile to delete.

        Returns:
            True if at least one file was deleted, False otherwise.
        """
        from faststack.io.deletion import permanently_delete_image_files

        return permanently_delete_image_files(image_file)

    def _delete_single_image(self, index: int):
        """Internal method to delete a single image by index."""
        if not self.image_files or index < 0 or index >= len(self.image_files):
            self.update_status_message("No image to delete.")
            return

        previous_index = self.current_index
        image_file = self.image_files[index]
        jpg_path = image_file.path
        raw_path = image_file.raw_pair

        # Try to ensure recycle bin is available
        recycle_bin_available = self._ensure_recycle_bin_dir()

        if recycle_bin_available:
            # Normal path: move to recycle bin
            recycled_jpg = self._move_to_recycle(jpg_path)
            recycled_raw = (
                self._move_to_recycle(raw_path)
                if (raw_path and raw_path.exists())
                else None
            )

            # Add to delete history if anything was moved
            if recycled_jpg or recycled_raw:
                import time

                timestamp = time.time()
                # Store tuple of (src, bin_path) for each file
                # Format: ( (jpg_src, jpg_bin), (raw_src, raw_bin) )
                record = ((jpg_path, recycled_jpg), (raw_path, recycled_raw))

                self.delete_history.append(record)
                self.undo_history.append(("delete", record, timestamp))
                status_msg = f"Deleted {jpg_path.name}"
            else:
                self.update_status_message("Delete failed")
                return
        else:
            # Fallback: permanent delete with confirmation
            if not self._confirm_permanent_delete(
                image_file,
                reason="Recycle bin could not be created due to permissions.",
            ):
                self.update_status_message("Deletion cancelled")
                return

            if self._permanently_delete_image_files(image_file):
                # Do NOT add to undo_history - permanent deletion is not undoable
                status_msg = (
                    f"Permanently deleted {jpg_path.name} (recycle bin unavailable)"
                )
            else:
                self.update_status_message("Delete failed")
                return

        # Clear folder stats cache so recycle bin count updates
        clear_raw_count_cache()

        # Refresh image list and move to next image
        self.refresh_image_list()

        # Refresh thumbnail model so folder stats (e.g., recycle bin count) update
        if self._thumbnail_model:
            self._thumbnail_model.refresh()
        if self.image_files:
            self._reposition_after_delete(None, previous_index)
            # Clear cache and invalidate display generation to force image reload
            self.display_generation += 1
            self.image_cache.clear()
            self.prefetcher.cancel_all()  # Cancel stale tasks since image list changed
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

        self.update_status_message(status_msg)

    def _reposition_after_delete(
        self, preserved_path: Optional[Path], previous_index: int
    ):
        """Reposition current_index after the image list refreshed post-deletion."""
        if not self.image_files:
            self.current_index = 0
            return

        if preserved_path:
            for i, img_file in enumerate(self.image_files):
                if img_file.path == preserved_path:
                    self.current_index = i
                    return

        self.current_index = min(previous_index, len(self.image_files) - 1)

    @Slot()
    def delete_current_image_only(self):
        """Delete only the current image, ignoring batch selection."""
        if not self.image_files:
            self.update_status_message("No image to delete.")
            return
        self._delete_single_image(self.current_index)

    @Slot()
    def delete_batch_images(self):
        """Delete all images in the current batch."""
        if not self.image_files:
            self.update_status_message("No images to delete.")
            return

        # Collect all indices in batches
        indices_to_delete = set()
        for start, end in self.batches:
            for i in range(start, end + 1):
                if 0 <= i < len(self.image_files):
                    indices_to_delete.add(i)

        if not indices_to_delete:
            self.update_status_message("No images in batch to delete.")
            return

        # Sort indices in reverse order so we delete from end to start
        # This way indices don't shift as we delete
        sorted_indices = sorted(indices_to_delete, reverse=True)

        # Determine where to land after deletion
        min_deleted_index = min(sorted_indices)

        # Try to ensure recycle bin is available
        recycle_bin_available = self._ensure_recycle_bin_dir()

        deleted_count = 0
        permanent_delete_mode = not recycle_bin_available
        import time

        timestamp = time.time()

        # Collect images to delete first
        images_to_delete = [
            self.image_files[i] for i in sorted_indices if i < len(self.image_files)
        ]

        if permanent_delete_mode and images_to_delete:
            # Show single batch confirmation for all images
            if not self._confirm_batch_permanent_delete(
                images_to_delete,
                reason="Recycle bin could not be created due to permissions.",
            ):
                self.update_status_message("Deletion cancelled.")
                return

            # Delete all confirmed images
            for image_file in images_to_delete:
                if self._permanently_delete_image_files(image_file):
                    deleted_count += 1
        else:
            # Normal path: move to recycle bin
            for image_file in images_to_delete:
                jpg_path = image_file.path
                raw_path = image_file.raw_pair
                try:
                    recycled_jpg = self._move_to_recycle(jpg_path)
                    recycled_raw = (
                        self._move_to_recycle(raw_path)
                        if (raw_path and raw_path.exists())
                        else None
                    )

                    if recycled_jpg or recycled_raw:
                        record = ((jpg_path, recycled_jpg), (raw_path, recycled_raw))
                        self.delete_history.append(record)
                        self.undo_history.append(("delete", record, timestamp))
                        deleted_count += 1

                except OSError as e:
                    log.exception("Failed to delete image %s: %s", jpg_path.name, e)

        if deleted_count > 0:
            # Clear all batches after deletion
            self.batches = []
            self.batch_start_index = None
            self._invalidate_batch_cache()

            # Clear folder stats cache so recycle bin count updates
            clear_raw_count_cache()

            # Refresh image list
            self.refresh_image_list()

            if self.image_files:
                # Calculate new index
                new_index = min_deleted_index
                new_index = max(0, min(new_index, len(self.image_files) - 1))

                self.current_index = new_index

                # Clear cache and invalidate display generation to force image reload
                self.display_generation += 1
                self.image_cache.clear()
                self.prefetcher.cancel_all()  # Cancel stale tasks since image list changed
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()

            if permanent_delete_mode:
                self.update_status_message(
                    f"Permanently deleted {deleted_count} image(s) (recycle bin unavailable)"
                )
            else:
                self.update_status_message(f"Deleted {deleted_count} image(s)")
            log.info("Deleted %d image(s) from batch", deleted_count)
        else:
            self.update_status_message("No images were deleted.")

    def _delete_grid_selected_images(self, selected_paths: list):
        """Delete images selected in grid view."""
        if not selected_paths:
            self.update_status_message("No images selected.")
            return

        # Build a path -> index map for the main image list
        path_to_index = {}
        for i, img in enumerate(self.image_files):
            path_to_index[img.path.resolve()] = i

        # Find indices for selected paths
        indices_to_delete = set()
        for path in selected_paths:
            resolved = path.resolve()
            if resolved in path_to_index:
                indices_to_delete.add(path_to_index[resolved])

        if not indices_to_delete:
            self.update_status_message("Selected images not found in current list.")
            return

        # Sort indices in reverse order so we delete from end to start
        sorted_indices = sorted(indices_to_delete, reverse=True)
        min_deleted_index = min(sorted_indices)

        # Try to ensure recycle bin is available - NO LONGER NEEDED, we try per-file
        # recycle_bin_available = self._ensure_recycle_bin_dir()

        deleted_count = 0
        permanent_delete_mode = (
            False  # Default to recycle bin, fallback if needed per file
        )
        import time

        timestamp = time.time()

        # Collect images to delete first
        images_to_delete = [
            self.image_files[i] for i in sorted_indices if i < len(self.image_files)
        ]

        # Normal path: move to recycle bin
        failed_deletes = []  # List of (ImageFile, exception)

        for image_file in images_to_delete:
            jpg_path = image_file.path
            raw_path = image_file.raw_pair

            try:
                # Use new per-folder move
                recycled_jpg = self._move_to_recycle(jpg_path)
                # Only try to recycle RAW if it exists
                recycled_raw = (
                    self._move_to_recycle(raw_path)
                    if (raw_path and raw_path.exists())
                    else None
                )

                if recycled_jpg or recycled_raw:
                    record = ((jpg_path, recycled_jpg), (raw_path, recycled_raw))
                    self.delete_history.append(record)
                    self.undo_history.append(("delete", record, timestamp))
                    deleted_count += 1

            except OSError as e:
                # Move failed - likely permission or lock issue
                failed_deletes.append((image_file, e))

        # Handle failures reactively
        if failed_deletes:
            log.warning(
                "%d files failed to recycle, prompting for permanent delete",
                len(failed_deletes),
            )
            # Prompt to permanent delete the ones that failed
            failed_images = [err[0] for err in failed_deletes]

            if self._confirm_batch_permanent_delete(
                failed_images,
                reason="Recycle bin partial failure (files failed to move).",
            ):
                for img in failed_images:
                    if self._permanently_delete_image_files(img):
                        deleted_count += 1
            else:
                self.update_status_message(
                    f"Deleted {deleted_count} files. {len(failed_deletes)} failed."
                )

        if deleted_count > 0:
            # Clear grid selection
            self._thumbnail_model.clear_selection()

            # Clear folder stats cache so recycle bin count updates
            clear_raw_count_cache()

            # Refresh image list
            self.refresh_image_list()

            # Refresh the grid model to remove deleted images
            self._thumbnail_model.refresh()
            self._path_resolver.update_from_model(self._thumbnail_model)

            if self.image_files:
                # Calculate new index for loupe view
                new_index = min_deleted_index
                new_index = max(0, min(new_index, len(self.image_files) - 1))
                self.current_index = new_index

                # Clear cache and invalidate display generation
                self.display_generation += 1
                self.image_cache.clear()
                self.prefetcher.cancel_all()
                # Restart prefetch for the new current image
                self.prefetcher.update_prefetch(self.current_index)

            self.sync_ui_state()
            if permanent_delete_mode:
                self.update_status_message(
                    f"Permanently deleted {deleted_count} image(s) (recycle bin unavailable)"
                )
            else:
                self.update_status_message(f"Deleted {deleted_count} image(s)")
            log.info("Deleted %d image(s) from grid selection", deleted_count)
        else:
            self.update_status_message("No images were deleted.")

    def _restore_backup_safe(self, saved_path_str: str, backup_path_str: str) -> bool:
        """
        Robustly restores a backup file to its original location, handling
        locking and permission errors using a unique temporary file strategy.
        Verifies success.
        """
        saved_path = Path(saved_path_str)
        backup_path = Path(backup_path_str)

        if not backup_path.exists():
            if saved_path.exists():
                self.update_status_message("Already restored (backup missing)")
                log.warning("Backup %s missing but original exists.", backup_path)
            else:
                self.update_status_message("Backup not found")
                log.warning(
                    "Backup %s disappeared before it could be restored.", backup_path
                )
            return False

        # Generate a unique temporary path to avoid collisions
        temp_path = saved_path.with_suffix(f".{uuid.uuid4().hex}.tmp_restore")

        try:
            # 1. If the target exists, we need to move the backup to the temp location first,
            #    then try to swap. If target is locked, we can't delete it directly.
            if saved_path.exists():
                try:
                    saved_path.unlink()  # Try the easy way first
                except PermissionError as pe:
                    log.warning(
                        "File %s locked, attempting safe restore strategy: %s",
                        saved_path,
                        pe,
                    )

                    # Move backup to temp
                    try:
                        shutil.move(str(backup_path), str(temp_path))
                    except OSError as e:
                        log.error("Failed to move backup to temp: %s", e)
                        raise

                    if not temp_path.exists():
                        log.error("Temp file %s not found after move!", temp_path)
                        raise OSError(f"Failed to create temp file {temp_path}")

                    # Try to force-move the temp file over the target (replace)
                    try:
                        os.replace(str(temp_path), str(saved_path))
                    except OSError:
                        # If replace fails, try to move back
                        log.error("Could not overwrite locked file %s", saved_path)
                        shutil.move(str(temp_path), str(backup_path))
                        raise

            # 2. If target doesn't exist (successfully unlinked or didn't exist), move backup to target
            if not saved_path.exists():
                # If we moved to temp, move temp -> target
                source = temp_path if temp_path.exists() else backup_path
                shutil.move(str(source), str(saved_path))

            # Verify restoration
            if not saved_path.exists():
                raise OSError(
                    f"Restoration failed: {saved_path} does not exist after move."
                )

            if saved_path.stat().st_size == 0:
                log.warning("Restored file %s is 0 bytes!", saved_path)

            log.info("Successfully restored %s from %s", saved_path, backup_path_str)
            return True

        except Exception as e:
            # Attempt cleanup
            if temp_path.exists():
                try:
                    if backup_path.exists():
                        temp_path.unlink()  # Backup still there, just kill temp
                    else:
                        shutil.move(str(temp_path), str(backup_path))  # Put it back
                except OSError:
                    pass
            log.exception("Detailed error in _restore_backup_safe")
            raise e

    @Slot()
    def _restore_from_recycle_bin_safe(
        self, src_path: Path, bin_path: Path
    ) -> Tuple[bool, str]:
        """Restores file from recycle bin safely.

        Returns:
            (success: bool, reason: str)
            Reasons: "ok", "missing_in_bin", "dest_exists", "move_failed"
        """
        if not bin_path.exists():
            return False, "missing_in_bin"
        if src_path.exists():
            return False, "dest_exists"

        try:
            src_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bin_path), str(src_path))
            if src_path.exists():
                return True, "ok"
            else:
                return False, "move_failed"
        except OSError as e:
            log.error(f"Failed to restore {bin_path.name}: {e}")
            return False, "move_failed"

    def _post_undo_refresh_and_select(
        self, target: Path, *, update_hist: bool = False
    ) -> None:
        """Centralized logic for refreshing state after an undo action."""
        self.refresh_image_list()

        # Find index of restored image
        target_resolve = target.resolve()
        for i, img_file in enumerate(self.image_files):
            try:
                if img_file.path.resolve() == target_resolve:
                    self.current_index = i
                    break
            except OSError:
                continue

        self.display_generation += 1
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()

        if update_hist and self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def undo_delete(self):
        """Unified undo that handles both delete and auto white balance operations."""
        if not self.undo_history:
            self.update_status_message("Nothing to undo.")
            return

        # Get the most recent action
        action_type, action_data, timestamp = self.undo_history.pop()

        if action_type == "delete":
            try:
                # Guard unpacking to prevent crashes on old history formats
                (jpg_pair, raw_pair) = action_data
                (jpg_src, jpg_bin) = jpg_pair
                (raw_src, raw_bin) = raw_pair
            except Exception:
                self.update_status_message("Undo failed: unexpected undo record format")
                log.exception("Unexpected undo record format: %r", action_data)
                return

            # Remove from delete_history only if it matches (prevent duplicates)
            popped_delete_history = False
            if self.delete_history and self.delete_history[-1] == action_data:
                self.delete_history.pop()
                popped_delete_history = True

            restored_files = []
            jpg_res_ok = False

            # --- Jpeg Restore ---
            success, reason = self._restore_from_recycle_bin_safe(jpg_src, jpg_bin)
            if success:
                jpg_res_ok = True
                restored_files.append(jpg_src.name)
                log.info("Restored %s from recycle bin", jpg_src.name)
            elif reason == "dest_exists":
                log.warning(
                    "Restore skipped for %s: destination already exists", jpg_src.name
                )
                # We consider this "success" enough to proceed to RAW, but we didn't restore it.
            else:
                # Failed hard
                self.update_status_message(f"Undo failed: {reason} for {jpg_src.name}")
                # Put back history
                self.undo_history.append(("delete", action_data, timestamp))
                if popped_delete_history:
                    self.delete_history.append(action_data)
                return

            # --- Raw Restore ---
            if raw_src and raw_bin:
                success, reason = self._restore_from_recycle_bin_safe(raw_src, raw_bin)
                if success:
                    restored_files.append(raw_src.name)
                    log.info("Restored %s from recycle bin", raw_src.name)
                elif reason == "dest_exists":
                    # Non-fatal: just warn that we kept the existing RAW
                    log.warning(
                        "Restore skipped for %s: destination already exists",
                        raw_src.name,
                    )
                    restored_files.append(f"{raw_src.name} (existed)")
                else:
                    # RAW restore failed (move failed or bin missing).
                    # If we restored JPG, we should probably rollback for consistency
                    # UNLESS the user prefers partial restore.
                    # Current plan: Rollback JPG if RAW failed hard.
                    if jpg_res_ok:
                        log.warning(
                            "RAW restore failed (%s), rolling back JPG for atomicity",
                            reason,
                        )
                        try:
                            # Attempt to move JPG back to bin
                            shutil.move(str(jpg_src), str(jpg_bin))
                        except OSError as e:
                            log.error("Failed to rollback JPG: %s", e)
                            self.update_status_message(
                                "Partial restore error (manual cleanup needed)"
                            )
                            return  # Do not put back in history, state is mixed

                    self.update_status_message(
                        f"Undo failed: {reason} for {raw_src.name}"
                    )
                    self.undo_history.append(("delete", action_data, timestamp))
                    if popped_delete_history:
                        self.delete_history.append(action_data)
                    return

            # --- Success Path ---
            if restored_files:
                files_str = ", ".join(restored_files)
                self.update_status_message(f"Restored: {files_str}")
            else:
                self.update_status_message("No files restored (destinations existed)")

            # Use helper to refresh
            self._post_undo_refresh_and_select(jpg_src, update_hist=False)

            # Refresh grid explicitly for recycle bin counts
            if self._thumbnail_model and self._is_grid_view_active:
                self._thumbnail_model.refresh()

        elif action_type == "auto_white_balance":
            saved_path, backup_path = action_data
            try:
                if self._restore_backup_safe(saved_path, backup_path):
                    self._post_undo_refresh_and_select(
                        Path(saved_path), update_hist=True
                    )
                    self.update_status_message("Undid auto white balance")
            except Exception as e:
                self.update_status_message(f"Undo failed: {e}")
                if Path(backup_path).exists():
                    self.undo_history.append(
                        ("auto_white_balance", action_data, timestamp)
                    )

        elif action_type == "auto_levels":
            saved_path, backup_path = action_data
            try:
                if self._restore_backup_safe(saved_path, backup_path):
                    self._post_undo_refresh_and_select(
                        Path(saved_path), update_hist=True
                    )
                    self.update_status_message("Undid auto levels")
            except Exception as e:
                self.update_status_message(f"Undo failed: {e}")
                if Path(backup_path).exists():
                    self.undo_history.append(("auto_levels", action_data, timestamp))

        elif action_type == "crop":
            saved_path, backup_path = action_data
            try:
                if self._restore_backup_safe(saved_path, backup_path):
                    self._post_undo_refresh_and_select(
                        Path(saved_path), update_hist=False
                    )
                    self.update_status_message("Undid crop")
            except Exception as e:
                self.update_status_message(f"Undo failed: {e}")
                if Path(backup_path).exists():
                    self.undo_history.append(("crop", action_data, timestamp))

    def shutdown(self):
        log.info("Application shutting down.")

        # Check tracked recycle bins plus explicit base bin check if it exists
        bins_to_check = set(self.active_recycle_bins)
        # Also check local "image recycle bin" in current/base folders just in case
        try:
            bins_to_check.add(self.image_dir / "image recycle bin")
        except Exception:
            pass

        total_files = 0
        bin_stats = {}  # bin_path -> count

        for bin_dir in bins_to_check:
            if bin_dir.exists() and bin_dir.is_dir():
                try:
                    stats = get_file_counts_by_extension(bin_dir)
                    count = sum(stats.values())
                    if count > 0:
                        total_files += count
                        bin_stats[bin_dir] = count
                except OSError:
                    pass

        if total_files > 0:
            # In a GUI app, we can't easily show a blocking QMessageBox here because shutdown()
            # might be called after loop exit or during cleanup.
            # However, we implemented the QML close interception (Main.qml) which calls cleanup.
            # This python-side check is a fallback or for CLI usage.
            # Since we moved the logic to QML's onClosing, we mainly just log here.
            log.info(
                "Shutdown with %d files in recycle bins: %s",
                total_files,
                list(bin_stats.keys()),
            )

        # Clear QML context property to prevent TypeErrors during shutdown
        if self.engine:
            log.info("Clearing uiState context property in QML.")
            del self.engine  # Explicitly delete the engine

        self.watcher.stop()
        self.prefetcher.shutdown()
        # Guard against partial init
        if getattr(self, "_thumbnail_prefetcher", None):
            self._thumbnail_prefetcher.shutdown()
        self.sidecar.set_last_index(self.current_index)
        self.sidecar.save()

    def _shutdown_executors(self):
        """Explicitly shuts down thread pools on app exit to prevent hanging."""
        self._shutting_down = True
        log.info("Shutting down background executors...")
        self._hist_executor.shutdown(wait=False, cancel_futures=True)
        self._preview_executor.shutdown(wait=False, cancel_futures=True)

    def empty_recycle_bin(self):
        """Permanently deletes all files in all tracked recycle bins."""
        # Clean up tracked bins
        bins_to_clean = set(self.active_recycle_bins)
        # Check base bin too
        try:
            bins_to_clean.add(self.image_dir / "image recycle bin")
        except Exception:
            pass

        for bin_path in bins_to_clean:
            if bin_path.exists():
                try:
                    shutil.rmtree(bin_path)
                except OSError:
                    log.exception("Failed to empty recycle bin %s", bin_path)

        self.active_recycle_bins.clear()
        self.delete_history.clear()
        clear_raw_count_cache()
        log.info("Emptied recycle bins and cleared delete history")

    def _on_cache_evict(self):
        """Callback for when the image cache evicts an item."""
        now = time.time()

        # 1. Record eviction timestamp
        self._eviction_timestamps.append(now)

        # 2. Prune timestamps older than window
        # Keep list short
        cutoff = now - CACHE_THRASH_WINDOW_SECS
        self._eviction_timestamps = [t for t in self._eviction_timestamps if t > cutoff]

        # 3. Check for thrashing (e.g., > threshold evictions in window)
        if len(self._eviction_timestamps) > CACHE_THRASH_THRESHOLD:
            # 4. Rate limit the warning
            if now - self._last_cache_warning_time > CACHE_WARNING_COOLDOWN_SECS:
                self._last_cache_warning_time = now
                self._has_warned_cache_full = True

                # Format usage info
                used_gb = self.image_cache.currsize / (1024**3)
                max_gb = self.image_cache.max_bytes / (1024**3)

                msg = f"Cache thrashing! {len(self._eviction_timestamps)} evictions in {CACHE_THRASH_WINDOW_SECS}s. Usage: {used_gb:.1f}GB / {max_gb:.1f}GB."

                # Use QTimer.singleShot to ensure this runs on the main thread
                QTimer.singleShot(0, lambda: self.update_status_message(msg))
                log.warning(msg)

    def restore_all_from_recycle_bin(self):
        """Restores all files from tracked recycle bins to their parent folders."""
        restored_count = 0

        bins_to_restore = set(self.active_recycle_bins)
        try:
            bins_to_restore.add(self.image_dir / "image recycle bin")
        except Exception:
            pass

        for bin_path in bins_to_restore:
            if not bin_path.exists():
                continue

            restore_target = bin_path.parent
            try:
                for file_in_bin in bin_path.iterdir():
                    dest_path = restore_target / file_in_bin.name
                    if dest_path.exists():
                        log.warning("File already exists, skipping: %s", dest_path)
                        continue

                    try:
                        shutil.move(str(file_in_bin), str(dest_path))
                        restored_count += 1
                        log.info("Restored %s from %s", file_in_bin.name, bin_path.name)
                    except OSError as e:
                        log.error("Failed to restore %s: %s", file_in_bin.name, e)
            except OSError:
                log.exception("Failed to iterate recycle bin %s", bin_path)

        # Clear delete history since we restored everything
        self.delete_history.clear()

        log.info("Restored %d files from recycle bins", restored_count)

    @Slot()
    def edit_in_photoshop(self):
        if not self.image_files:
            self.update_status_message("No image to edit.")
            return

        # Prefer RAW file if it exists, otherwise use JPG
        image_file = self.image_files[self.current_index]
        jpg_path = image_file.path

        # Handle backup images: strip -backup, -backup2, -backup-1, etc. to find original RAW
        original_stem = jpg_path.stem
        # Remove -backup with optional digits or -backup-digits (handles both formats)
        original_stem = re.sub(r"-backup(-?\d+)?$", "", original_stem)

        # Look for RAW file with the original stem
        raw_path = None
        if image_file.raw_pair and image_file.raw_pair.exists():
            # Use the paired RAW if it exists
            raw_path = image_file.raw_pair
        else:
            # Search for RAW file manually by original stem
            for ext in RAW_EXTENSIONS:
                potential_raw = jpg_path.parent / f"{original_stem}{ext}"
                if potential_raw.exists():
                    raw_path = potential_raw
                    break

        if raw_path and raw_path.exists():
            current_image_path = raw_path
            log.info("Using RAW file for Photoshop: %s", raw_path)
        else:
            current_image_path = jpg_path
            log.info(
                "Using JPG file for Photoshop (no RAW found): %s", current_image_path
            )

        photoshop_exe = config.get("photoshop", "exe")
        photoshop_args = config.get("photoshop", "args")

        # Validate executable path securely
        is_valid, error_msg = validate_executable_path(
            photoshop_exe, app_type="photoshop", allow_custom_paths=True
        )

        if not is_valid:
            self.update_status_message(f"Photoshop validation failed: {error_msg}")
            log.error("Photoshop executable validation failed: %s", error_msg)
            return

        # Validate that the file path exists and is a file
        if not current_image_path.exists() or not current_image_path.is_file():
            self.update_status_message(
                f"Image file not found: {current_image_path.name}"
            )
            log.error("Image file not found or not a file: %s", current_image_path)
            return

        try:
            # Build command list safely
            command = [photoshop_exe]

            # Parse additional args safely using shlex (handles quotes and escapes properly)
            if photoshop_args:
                try:
                    # Use shlex to properly parse arguments with quotes/escapes
                    # On Windows, use posix=False to handle Windows-style paths
                    parsed_args = shlex.split(photoshop_args, posix=(os.name != "nt"))
                    command.extend(parsed_args)
                except ValueError as e:
                    log.error("Invalid photoshop_args format: %s", e)
                    self.update_status_message("Invalid Photoshop arguments configured")
                    return

            # Add the file path as the last argument
            # Convert to string but keep it as a list element (not shell-interpolated)
            command.append(str(current_image_path.resolve()))

            # SECURITY: Explicitly disable shell execution
            subprocess.Popen(
                command,
                shell=False,  # CRITICAL: Never use shell=True with user input
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,  # Close unused file descriptors
            )

            # Mark as edited on successful launch
            today = datetime.now().strftime("%Y-%m-%d")
            stem = image_file.path.stem
            meta = self.sidecar.get_metadata(stem)
            meta.edited = True
            meta.edited_date = today
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            self.update_status_message(
                f"Opened {current_image_path.name} in Photoshop."
            )
            log.info("Launched Photoshop with: %s", command)
        except FileNotFoundError as e:
            self.update_status_message(f"Photoshop executable not found: {e}")
            log.exception("Photoshop executable not found")
            # Don't mark as edited if launch failed
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.update_status_message(f"Failed to open in Photoshop: {e}")
            log.exception("Error launching Photoshop")
            # Don't mark as edited if launch failed
            return

    @Slot()
    def copy_path_to_clipboard(self):
        if not self.image_files:
            self.update_status_message("No image path to copy.")
            return

        current_image_path = str(self.image_files[self.current_index].path)
        QApplication.clipboard().setText(current_image_path)
        self.update_status_message(f"Copied: {current_image_path}")
        log.info("Copied path to clipboard: %s", current_image_path)

    @Slot()
    def reset_zoom_pan(self):
        """Resets zoom and pan to fit the image in the window (like Ctrl+0 in Photoshop)."""
        log.info("Resetting zoom and pan to fit window")
        self.ui_state.resetZoomPan()
        self.update_status_message("Reset zoom and pan")

    def update_status_message(self, message: str, timeout: int = 3000):
        """
        Updates the UI status message and clears it after a timeout.
        """

        def clear_message():
            if self.ui_state.statusMessage == message:
                self.ui_state.statusMessage = ""

        self.ui_state.statusMessage = message
        QTimer.singleShot(timeout, clear_message)

    @Slot()
    def start_drag_current_image(self):
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        # Collect all files: current + any in defined batches
        files_to_drag = set()
        files_to_drag.add(self.current_index)

        # Add all files from defined batches
        for start, end in self.batches:
            for idx in range(start, end + 1):
                if 0 <= idx < len(self.image_files):
                    files_to_drag.add(idx)

        # Convert to sorted list and get only existing paths
        file_indices = sorted(files_to_drag)
        existing_indices = [
            idx for idx in file_indices if self.image_files[idx].path.exists()
        ]

        # Prefer dragging the developed JPG if it exists (for external export),
        # but only when RAW mode is active or we are dragging a developed file itself.
        file_paths = []
        for idx in existing_indices:
            img = self.image_files[idx]

            # Suggestion: only prefer -developed.jpg when RAW mode is active
            # or when the current entry is itself the working/developed artifact.
            is_developed_artifact = img.path.stem.lower().endswith("-developed")
            in_raw_mode = getattr(self, "current_edit_source_mode", "jpeg") == "raw"

            if (
                in_raw_mode or is_developed_artifact
            ) and img.developed_jpg_path.exists():
                file_paths.append(img.developed_jpg_path)
            else:
                file_paths.append(img.path)

        if not file_paths:
            log.error("No valid files to drag")
            return

        if self.main_window is None:
            return

        drag = QDrag(self.main_window)
        mime_data = QMimeData()

        # Use Qt's standard setUrls - it handles both browser and native app compatibility
        urls = [QUrl.fromLocalFile(str(p)) for p in file_paths]
        mime_data.setUrls(urls)

        drag.setMimeData(mime_data)

        # --- thumbnail / drag preview ---
        pix = QPixmap(str(file_paths[0]))
        if not pix.isNull():
            # scale it down so it's not huge
            scaled = pix.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            drag.setPixmap(scaled)
            # hotspot = center of image
            drag.setHotSpot(QPoint(scaled.width() // 2, scaled.height() // 2))

        log.info(
            "Starting drag for %d file(s): %s",
            len(file_paths),
            [str(p) for p in file_paths],
        )
        # Support both Copy and Move actions for browser compatibility
        result = drag.exec(Qt.CopyAction | Qt.MoveAction)
        log.info("Drag completed with result: %s", result)

        # Reset zoom/pan after drag completes (drag can cause unwanted panning)
        self.ui_state.resetZoomPan()

        # Mark all dragged files as uploaded if drag was successful
        if result in (Qt.CopyAction, Qt.MoveAction):
            from datetime import datetime

            today = datetime.now().strftime("%Y-%m-%d")

            for idx in existing_indices:
                stem = self.image_files[idx].path.stem
                meta = self.sidecar.get_metadata(stem)
                meta.uploaded = True
                meta.uploaded_date = today

            self.sidecar.save()

            # Clear all batches after successful drag (like pressing \)
            self.batches = []
            self.batch_start_index = None
            self._invalidate_batch_cache()

            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()
            log.info(
                "Marked %d file(s) as uploaded on %s. Cleared all batches.",
                len(existing_indices),
                today,
            )

    @Slot()
    def enable_raw_editing(self):
        """Switches the current image to RAW mode (using developed TIFF)."""
        if not self.image_files:
            return

        # 1. Update State
        # 1. Update State
        if self.current_edit_source_mode != "raw":
            self.current_edit_source_mode = "raw"
            self.editSourceModeChanged.emit("raw")
        self.sync_ui_state()

        # 2. Check if we have a valid TIFF ready
        path = self.get_active_edit_path(self.current_index)

        # If the path returned IS the working TIFF (and it exists), we can just load it.
        # Check specific condition:
        image_file = self.image_files[self.current_index]
        if path == image_file.working_tif_path and self.is_valid_working_tif(path):
            log.info("Valid working TIFF exists, switching to RAW mode immediately.")
            self.load_image_for_editing()  # This will now pick up the TIFF via get_active_edit_path
            return

        # 3. If not ready, trigger development
        # (Pass through to existing backend logic)
        self._develop_raw_backend()

    def _develop_raw_backend(self):
        """Internal: Triggers the actual RawTherapee process."""
        if not self.image_files:
            return

        image_file = self.image_files[self.current_index]
        if not image_file.has_raw:
            self.update_status_message("No RAW file available.")
            return

        raw_path = image_file.raw_path
        tif_path = image_file.working_tif_path

        # Resolve RawTherapee Executable
        from faststack.config import config

        rt_exe = config.get("rawtherapee", "exe")
        if not rt_exe or not os.path.exists(rt_exe):
            self.update_status_message("RawTherapee not found. Check settings.")
            log.error("RawTherapee executable not configured or missing: %s", rt_exe)
            return
        self.update_status_message("Developing RAW... please wait.")
        log.info("Starting RAW development: %s -> %s", raw_path, tif_path)

        def worker():
            # Check for optional args in config
            rt_args = config.get("rawtherapee", "args")

            # Build command: rawtherapee-cli -t -Y -o <out.tif> -c <in.raw>
            # -t: TIFF output
            # -b16: 16-bit depth (Critical! Default is often 8-bit)
            # -Y: Overwrite existing
            # -o: Output file
            # -c: Input file (must be last)
            cmd = [rt_exe, "-t", "-b16", "-Y", "-o", str(tif_path)]

            if rt_args:
                try:
                    # Use shlex to properly parse arguments with quotes/escapes
                    # On Windows, use posix=False to handle Windows-style paths
                    parsed_args = shlex.split(rt_args, posix=(os.name != "nt"))
                    cmd.extend(parsed_args)
                except ValueError as e:
                    log.error("Invalid rawtherapee args format: %s", e)

            cmd.extend(["-c", str(raw_path)])
            cmd_str = " ".join(cmd)  # For logging

            # Run process
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": 60,  # 60 second timeout
            }
            if sys.platform == "win32":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            try:
                result = subprocess.run(cmd, **run_kwargs)

                if result.returncode == 0:
                    if tif_path.exists() and tif_path.stat().st_size > 0:
                        log.info("RAW development successful.")
                        # Use partial to bind variable deeply
                        QTimer.singleShot(
                            0, functools.partial(self._on_develop_finished, True, None)
                        )
                        return  # Success path
                    else:
                        msg = f"RawTherapee exited successfully but output file is missing or empty.\nCommand: {cmd_str}"
                        log.error(msg)
                        QTimer.singleShot(
                            0, functools.partial(self._on_develop_finished, False, msg)
                        )
                else:
                    stderr = result.stderr.strip() if result.stderr else "(no stderr)"
                    stdout = result.stdout.strip() if result.stdout else "(no stdout)"
                    err_msg = f"RawTherapee failed (exit code {result.returncode}):\nCommand: {cmd_str}\nstderr: {stderr}\nstdout: {stdout}"
                    log.error(err_msg)
                    QTimer.singleShot(
                        0, functools.partial(self._on_develop_finished, False, err_msg)
                    )

            except subprocess.TimeoutExpired:
                err_msg = f"RawTherapee timed out after 60 seconds.\nCommand: {cmd_str}"
                log.error(err_msg)
                QTimer.singleShot(
                    0, functools.partial(self._on_develop_finished, False, err_msg)
                )
            except Exception as e:
                err_msg = f"Unexpected error running RawTherapee: {str(e)}"
                log.exception(err_msg)
                QTimer.singleShot(
                    0, functools.partial(self._on_develop_finished, False, err_msg)
                )
            finally:
                # Cleanup if we failed and left a bad file or 0-byte file (unless success logic already returned)
                # Note: success logic returns early. If we are here, we likely failed or fell through (e.g. 0 byte file case did not return)
                # Actually, the 0-byte case calls on_finished but doesn't return, so it falls here.
                # Let's check specifically if we need to cleanup.
                # If we succeeded, we returned.
                if tif_path.exists() and "result" in locals():
                    # Only cleanup if result was assigned (subprocess ran)
                    # If it's 0 bytes or we are in an error state (which implies we didn't return early)
                    try:
                        if tif_path.stat().st_size == 0:
                            tif_path.unlink()
                        elif result.returncode != 0:
                            # If we crashed but left a file, delete it
                            tif_path.unlink()
                    except (OSError, AttributeError):
                        # AttributeError if result is None
                        pass

        threading.Thread(target=worker, daemon=True).start()

    # Preserving legacy slot name for compatibility if QML calls it directly,
    # but QML should call enable_raw_editing now.
    # Actually provider.py calls this. I will update provider.py to call enable_raw_editing.
    # But I'll keep this as a proxy to the new method just in case.
    @Slot()
    def develop_raw_for_current_image(self):
        self.enable_raw_editing()

    @Slot()
    def load_image_for_editing(self):
        """
        Loads the currently viewed image into the editor using active path logic.
        This provides a centralized entry point for loading the editor correctly.
        """
        try:
            active_path = self.get_active_edit_path(self.current_index)
            filepath = str(active_path)

            # Fetch cached preview if available for faster initial display
            cached_preview = self.get_decoded_image(self.current_index)

            # Determine if we should capture source EXIF (e.g., for RAW mode)
            source_exif = None
            if self.current_edit_source_mode == "raw":
                # Capture EXIF from the original JPEG to preserve in developed JPG
                image_file = self.image_files[self.current_index]
                jpeg_path = image_file.path
                # Only if the main path isn't itself a TIFF (avoid recursion)
                if (
                    jpeg_path.suffix.lower() not in (".tif", ".tiff")
                    and jpeg_path.exists()
                ):
                    try:
                        with Image.open(jpeg_path) as src_im:
                            source_exif = src_im.info.get("exif")
                    except Exception as e:
                        log.warning(
                            f"Failed to capture source EXIF from {jpeg_path}: {e}"
                        )

            # Load into editor
            if self.image_editor.load_image(
                filepath, cached_preview=cached_preview, source_exif=source_exif
            ):
                # Notify UIState to update bindings
                # We do this via signals or by calling the update function on UIState if available
                # But UIState listens to editor signals?
                # Actually, the previous implementation in UIState pushed edits to itself.
                # We need to preserve that behavior.
                # For now, simpler to emit a signal that UIState listens to,
                # OR just manually update UIState here if we have reference.
                if self.ui_state:
                    self._sync_editor_state_to_ui()

                return True
        except Exception as e:
            log.exception("Failed to load image for editing: %s", e)
            self.update_status_message(f"Error loading editor: {e}")

        return False

    def _sync_editor_state_to_ui(self):
        """Helper to push editor state (initial edits) to UIState."""
        initial_edits = self.image_editor._initial_edits()
        for key, value in initial_edits.items():
            if hasattr(self.ui_state, key):
                setattr(self.ui_state, key, value)

        # Reset visual components
        if hasattr(self.ui_state, "aspectRatioNames"):
            # This requires IMPORTs? No, just pass list.
            from faststack.imaging.editor import ASPECT_RATIOS

            self.ui_state.aspectRatioNames = [r["name"] for r in ASPECT_RATIOS]
            self.ui_state.currentAspectRatioIndex = 0
            self.ui_state.currentCropBox = (0, 0, 1000, 1000)

        # Kick off background render
        self._kick_preview_worker()
        # Notify UI
        self.ui_state.editorImageChanged.emit()

    def _on_develop_finished(self, success: bool, error_msg: Optional[str]):
        """Callback on main thread after RAW development."""
        if success:
            self.update_status_message("RAW Development complete.")
            # Load active path (which should now be the developed TIFF)
            self.load_image_for_editing()
        else:
            self.update_status_message(f"Development failed: {error_msg}")
            # Ensure UI reflects failure (maybe revert mode? or just show error)
            # Staying in RAW mode but failing to load allows user to try again or see error.

    @Slot(result=DecodedImage)
    def get_preview_data(self) -> Optional[DecodedImage]:
        """Gets the preview data of the currently edited image as a DecodedImage."""
        return self.image_editor.get_preview_data()

    @Slot(str, "QVariant")
    def set_edit_parameter(self, key: str, value: Any):
        """Sets an edit parameter and updates the UIState for the slider visual."""
        # Robust guard: only allow edits if the editor is actually holding an image.
        if not self.image_editor:
            return
        if self.image_editor.current_filepath is None:
            return
        # Must have either a float image (working copy) or original loaded
        if (
            self.image_editor.float_image is None
            and self.image_editor.original_image is None
        ):
            return

        try:
            # Update actual edit state (this bumps _edits_rev and invalidates preview cache)
            changed = self.image_editor.set_edit_param(key, value)

            # Sync UI state with backend (e.g., rotation might be rounded)
            final_value = value
            if changed:
                # Use thread-safe accessor to get the actual value applied
                actual = self.image_editor.get_edit_value(key)
                if actual is not None:
                    final_value = actual

            # Update UI state regardless (visual sliders need to match what user dragged, OR the clamped backend value)
            if hasattr(self.ui_state, key):
                setattr(self.ui_state, key, final_value)

            # Trigger a refresh of the image to show the edit, ONLY if something changed
            # Uses gate pattern: runs immediately if not inflight, else queues for next
            if changed:
                self._kick_preview_worker()
        except Exception as e:
            log.error("Error setting edit parameter %s=%s: %s", key, value, e)

    @Slot(int, int, int, int)
    def set_crop_box(self, left: int, top: int, right: int, bottom: int):
        """Sets the normalized crop box (0-1000) in the editor."""
        from typing import Tuple

        crop_box: Tuple[int, int, int, int] = (left, top, right, bottom)
        self.image_editor.set_crop_box(crop_box)
        self.ui_state.currentCropBox = crop_box  # Update QML visual (if implemented)

    @Slot()
    def reset_edit_parameters(self):
        """Resets all editing parameters in the editor."""
        self.image_editor.reset_edits()
        if hasattr(self.ui_state, "reset_editor_state"):
            self.ui_state.reset_editor_state()

        self.update_status_message("Edits reset")

        # Trigger a refresh to show the reset image
        self.ui_refresh_generation += 1
        self._kick_preview_worker()

        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def rotate_image_cw(self):
        """Rotate the edited image 90 degrees clockwise."""
        current = self.image_editor.current_edits.get("rotation", 0)
        new_rotation = (current - 90) % 360
        self.set_edit_parameter("rotation", new_rotation)
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def rotate_image_ccw(self):
        """Rotate the edited image 90 degrees counter-clockwise."""
        current = self.image_editor.current_edits.get("rotation", 0)
        new_rotation = (current + 90) % 360
        self.set_edit_parameter("rotation", new_rotation)
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def toggle_histogram(self):
        """Toggle histogram window visibility."""
        self.ui_state.isHistogramVisible = not self.ui_state.isHistogramVisible
        if self.ui_state.isHistogramVisible:
            self.update_histogram()
            log.info("Histogram window opened")
        else:
            log.info("Histogram window closed")

    @Slot()
    @Slot(float, float, float, float)  # zoom, panX, panY, imageScale
    def update_histogram(
        self,
        zoom: float = 1.0,
        pan_x: float = 0.0,
        pan_y: float = 0.0,
        image_scale: float = 1.0,
    ):
        """Throttled request to update histogram. Updates continuously but capped at interval.

        Args:
            zoom: Zoom scale factor (1.0 = no zoom)
            pan_x: Pan offset in X direction (in image coordinates)
            pan_y: Pan offset in Y direction (in image coordinates)
            image_scale: Scale factor of displayed image vs original
        """
        # Early guard: don't even schedule if nothing is showing the histogram
        if not (self.ui_state.isHistogramVisible or self.ui_state.isEditorOpen):
            with self._hist_lock:
                self._hist_pending = None
            return

        with self._hist_lock:
            self._hist_pending = (zoom, pan_x, pan_y, image_scale)
            inflight = self._hist_inflight

        if not self.histogram_timer.isActive() and not inflight:
            self.histogram_timer.start()

    def _kick_histogram_worker(self):
        if getattr(self, "_shutting_down", False):
            return

        with self._hist_lock:
            if self._hist_inflight:
                return
            if self._hist_pending is None:
                return

            args = self._hist_pending
            self._hist_pending = None

            self._hist_token += 1
            token = self._hist_token
            # Mark as inflight while holding the lock to prevent others from entering
            self._hist_inflight = True

        # Snap the currently known preview data to avoid racing with the editor
        preview_data = self._last_rendered_preview
        if not preview_data:
            # Fallback for initial load if no edit preview yet (could use get_decoded_image?)
            # But histogram is mostly for edits. If preview_data is None, we likely can't compute anyway.
            # We can try to peek at the image editor if _last_rendered_preview is unset.
            preview_data = self.image_editor.get_preview_data_cached(
                allow_compute=False
            )

        # Fallback: If still no preview data (e.g. editor not open), we need to fetch the main image.
        # But doing get_decoded_image() here blocks the main thread.
        # Instead, we pass the index to the worker and let it fetch/decode if needed.
        target_index = -1
        if not preview_data and 0 <= self.current_index < len(self.image_files):
            target_index = self.current_index

        # If no preview data AND no valid index, we can't compute.
        if not preview_data and target_index == -1:
            # We must clear inflight if we abort, otherwise we deadlock future updates
            # Keep lock held while modifying shared state AND checking timer to prevent race
            with self._hist_lock:
                self._hist_inflight = False
                # Restore pending args so the next timer tick (or preview completion) retries
                if self._hist_pending is None:
                    self._hist_pending = args
                # Make sure timer is running to retry (check under lock to avoid race)
                should_start_timer = not self.histogram_timer.isActive()

            if should_start_timer:
                self.histogram_timer.start()
            return

        try:
            # Pass simple data + controller reference + target_index
            fut = self._hist_executor.submit(
                self._compute_histogram_worker,
                token,
                args,
                preview_data,
                self,
                target_index,
            )
            fut.add_done_callback(self._on_histogram_done)
        except Exception as e:
            log.error(f"Histogram executor failed to submit task: {e}")
            with self._hist_lock:
                self._hist_inflight = False

    @staticmethod
    def _compute_histogram_worker(
        token, args, decoded, controller=None, target_index=-1
    ):
        # IMPORTANT: do not touch QObjects here except thread-safe plain data
        zoom, pan_x, pan_y, image_scale = args

        # If data wasn't provided, try to fetch it safely using the controller
        if not decoded and controller and target_index >= 0:
            decoded = controller._get_decoded_image_safe(target_index)

        # Use explicitly passed or fetched decoded data
        if not decoded:
            return token, None

        try:
            # Validate buffer size before reshape to prevent ValueError
            expected_size = decoded.height * decoded.width * 3
            if len(decoded.buffer) != expected_size:
                log.warning(
                    "Histogram: Buffer size mismatch. Expected %d bytes, got %d",
                    expected_size,
                    len(decoded.buffer),
                )
                return token, None

            arr = np.frombuffer(decoded.buffer, dtype=np.uint8).reshape(
                (decoded.height, decoded.width, 3)
            )

            # If zoomed in, calculate visible region and only use that portion
            if zoom > 1.1:
                visible_width = decoded.width / zoom
                visible_height = decoded.height / zoom
                center_x = decoded.width / 2
                center_y = decoded.height / 2
                pan_x_image = pan_x / image_scale if image_scale > 0 else 0
                pan_y_image = pan_y / image_scale if image_scale > 0 else 0
                visible_center_x = center_x - (pan_x_image / zoom)
                visible_center_y = center_y - (pan_y_image / zoom)

                visible_x_start = max(0, int(visible_center_x - visible_width / 2))
                visible_y_start = max(0, int(visible_center_y - visible_height / 2))
                visible_x_end = min(
                    decoded.width, int(visible_center_x + visible_width / 2)
                )
                visible_y_end = min(
                    decoded.height, int(visible_center_y + visible_height / 2)
                )

                if visible_x_end > visible_x_start and visible_y_end > visible_y_start:
                    arr = arr[
                        visible_y_start:visible_y_end, visible_x_start:visible_x_end, :
                    ]

            bins = 256
            value_range = (0, 256)

            r_hist = np.histogram(arr[:, :, 0], bins=bins, range=value_range)[0]
            g_hist = np.histogram(arr[:, :, 1], bins=bins, range=value_range)[0]
            b_hist = np.histogram(arr[:, :, 2], bins=bins, range=value_range)[0]

            r_clip_count = int(r_hist[255])
            g_clip_count = int(g_hist[255])
            b_clip_count = int(b_hist[255])

            r_preclip_count = int(np.sum(r_hist[250:255]))
            g_preclip_count = int(np.sum(g_hist[250:255]))
            b_preclip_count = int(np.sum(b_hist[250:255]))

            log_r_hist = [float(x) for x in np.log1p(r_hist)]
            log_g_hist = [float(x) for x in np.log1p(g_hist)]
            log_b_hist = [float(x) for x in np.log1p(b_hist)]

            hist = {
                "r": log_r_hist,
                "g": log_g_hist,
                "b": log_b_hist,
                "r_clip": r_clip_count,
                "g_clip": g_clip_count,
                "b_clip": b_clip_count,
                "r_preclip": r_preclip_count,
                "g_preclip": g_preclip_count,
                "b_preclip": b_preclip_count,
            }
            return token, hist
        except Exception:
            return token, None

    def _on_histogram_done(self, fut):
        if getattr(self, "_shutting_down", False):
            return

        try:
            token, hist = fut.result()
        except Exception:
            token, hist = None, None

        # bounce back to UI thread via signal
        self.histogramReady.emit((token, hist))

    @Slot(object)
    def _apply_histogram_result(self, payload):
        if getattr(self, "_shutting_down", False):
            return

        token, hist = payload

        with self._hist_lock:
            self._hist_inflight = False

            if hist is not None:
                if token == self._hist_token:
                    self.ui_state.histogramData = hist
                    self.ui_state.highlightStateChanged.emit()

            # If more updates arrived while we computed, run again soon
            pending = self._hist_pending is not None

        if pending:
            self.histogram_timer.start()

    def _kick_preview_worker(self):
        """Kicks off a background preview render task."""
        if getattr(self, "_shutting_down", False):
            return

        with self._preview_lock:
            if self._preview_inflight:
                self._preview_pending = True
                return

            self._preview_inflight = True
            self._preview_pending = False
            self._preview_token += 1
            token = self._preview_token

        # Submit task to dedicated preview executor
        try:
            fut = self._preview_executor.submit(
                self._render_preview_worker, token, self.image_editor
            )
            fut.add_done_callback(self._on_preview_done)
        except RuntimeError:
            log.warning("Preview executor failed (shutting down?)")
            with self._preview_lock:
                self._preview_inflight = False

    @staticmethod
    def _render_preview_worker(token, image_editor):
        # Heavy work (PIL apply_edits) happens here off-thread
        try:
            # allow_compute=True ensures we actually do the work
            decoded = image_editor.get_preview_data_cached(allow_compute=True)
            return token, decoded
        except Exception:
            log.exception("Preview render failed")
            return token, None

    def _on_preview_done(self, fut):
        if getattr(self, "_shutting_down", False):
            return

        try:
            token, decoded = fut.result()
        except Exception:
            token, decoded = None, None

        # Emit from worker thread; Qt will queue to UI thread
        self.previewReady.emit((token, decoded))

    @Slot(object)
    def _apply_preview_result(self, payload):
        if getattr(self, "_shutting_down", False):
            return

        token, decoded = payload
        should_kick = False
        should_accept = False

        with self._preview_lock:
            self._preview_inflight = False

            # Accept result only if:
            # 1. We got valid decoded data
            # 2. Token matches (not stale from an old request)
            # 3. No pending request waiting (avoid "snap back" stale frame flash)
            if (
                decoded is not None
                and token == self._preview_token
                and not self._preview_pending
            ):
                self._last_rendered_preview = decoded
                self.ui_refresh_generation += 1
                self._last_rendered_preview_index = self.current_index
                self._last_rendered_preview_gen = self.ui_refresh_generation
                should_accept = True

            # Consume pending flag atomically before scheduling
            if self._preview_pending:
                self._preview_pending = False
                should_kick = True

        # Emit outside lock to avoid holding lock during UI work
        if should_accept:
            self.ui_state.currentImageSourceChanged.emit()
            self.ui_state.highlightStateChanged.emit()
            self.update_histogram()

        # Call directly (not via singleShot) since we're on the UI thread.
        # This prevents race where a new slider event could interleave between
        # scheduling and execution, causing a spurious extra render.
        if should_kick:
            self._kick_preview_worker()

    @Slot()
    def cancel_crop_mode(self):
        """Cancel crop mode without applying changes."""
        if self.ui_state.isCropping:
            self.ui_state.isCropping = False
            self.ui_state.currentCropBox = [0, 0, 1000, 1000]
            # Ensure preview rotation is cleared
            self.image_editor.set_edit_param("straighten_angle", 0.0)
            # Force QML to refresh if it's showing provider preview frames
            self.ui_refresh_generation += 1
            self.ui_state.currentImageSourceChanged.emit()
            self.update_status_message("Crop cancelled")
            log.info("Crop mode cancelled")

    @Slot()
    def toggle_crop_mode(self):
        """Toggle crop mode on/off."""
        self.ui_state.isCropping = not self.ui_state.isCropping
        if self.ui_state.isCropping:
            # Reset crop box when entering crop mode
            self.ui_state.currentCropBox = (0, 0, 1000, 1000)
            # Set aspect ratios for QML dropdown
            self.ui_state.aspectRatioNames = [r["name"] for r in ASPECT_RATIOS]
            self.ui_state.currentAspectRatioIndex = 0

            # Pre-load image into editor to ensure smooth rotation
            if self.image_files and self.current_index < len(self.image_files):
                image_file = self.image_files[self.current_index]
                filepath = image_file.path
                editor_path = self.image_editor.current_filepath

                # Robust comparison
                match = False
                if editor_path:
                    try:
                        match = Path(editor_path).resolve() == Path(filepath).resolve()
                    except (OSError, ValueError):
                        match = str(editor_path) == str(filepath)

                if not match:
                    log.debug(f"toggle_crop_mode: Loading {filepath} into editor")
                    # Use cached preview if available to speed up using get_decoded_image(self.current_index)
                    # note: get_decoded_image verifies index bounds
                    cached_preview = self.get_decoded_image(self.current_index)
                    self.image_editor.load_image(
                        str(filepath), cached_preview=cached_preview
                    )

            # Reset rotation to 0 when starting fresh crop mode
            self.image_editor.set_edit_param("straighten_angle", 0.0)

            self.update_status_message("Crop mode: Drag to select area, Enter to crop")
            log.info("Crop mode enabled")
        else:  # Exiting crop mode
            self.ui_state.isCropping = False
            self.ui_state.currentCropBox = (0, 0, 1000, 1000)
            self.update_status_message("Crop cancelled")
            log.info("Crop mode disabled")

    @Slot()
    def stack_source_raws(self):
        """
        Finds the source RAW files for the current stacked JPG and launches Helicon Focus.
        """
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image selected.")
            return

        current_image_path = self.image_files[self.current_index].path
        filename = current_image_path.name

        # Ensure it's a stacked JPG
        if not filename.lower().endswith(" stacked.jpg"):
            self.update_status_message("Current image is not a stacked JPG.")
            return

        # Extract base name and number, e.g., "PB210633" from "20251121-PB210633 stacked.JPG"
        match = re.search(r"([A-Z]+)(\d+)\s+stacked\.JPG", filename, re.IGNORECASE)
        if not match:
            self.update_status_message("Could not parse stacked JPG filename format.")
            log.error("Could not parse stacked JPG filename: %s", filename)
            return

        base_prefix = match.group(1)  # e.g., "PB"
        base_number_str = match.group(2)  # e.g., "210633"
        base_number = int(base_number_str)

        # Determine the RAW source directory
        raw_source_dir_str = config.get("raw", "source_dir")
        if not raw_source_dir_str:
            self.update_status_message(
                "RAW source directory not configured in settings."
            )
            log.warning("RAW source directory (raw.source_dir) is not set in config.")
            return

        raw_base_dir = Path(raw_source_dir_str)
        if not raw_base_dir.is_dir():
            self.update_status_message(
                f"RAW source directory not found: {raw_base_dir}"
            )
            log.warning(
                "Configured RAW source directory does not exist: %s", raw_base_dir
            )
            return

        # Get the mirror base from config
        mirror_base_str = config.get("raw", "mirror_base")
        if not mirror_base_str:
            self.update_status_message(
                "RAW mirror base directory not configured in settings."
            )
            log.warning("RAW mirror base (raw.mirror_base) is not set in config.")
            return

        mirror_base_dir = Path(mirror_base_str)
        if not mirror_base_dir.is_dir():
            self.update_status_message(
                f"RAW mirror base directory not found: {mirror_base_dir}"
            )
            log.warning(
                "Configured RAW mirror base directory does not exist: %s",
                mirror_base_dir,
            )
            return

        # The date structure in the RAW directory mirrors the structure relative to the mirror_base
        try:
            relative_part = current_image_path.parent.relative_to(mirror_base_dir)
        except ValueError:
            self.update_status_message(
                "Current image is not in the configured mirror base directory."
            )
            log.error(
                "Could not find relative path for '%s' from base '%s'. Check 'mirror_base' config.",
                current_image_path.parent,
                mirror_base_dir,
            )
            return

        raw_search_dir = raw_base_dir / relative_part

        if not raw_search_dir.is_dir():
            self.update_status_message(
                f"RAW directory for this date not found: {raw_search_dir}"
            )
            log.warning("RAW search directory does not exist: %s", raw_search_dir)
            return

        # Find RAW files by decrementing the number
        found_raw_files: List[Path] = []
        # Start one number less than the stacked image number
        current_raw_number = base_number - 1

        # Limit to reasonable number of RAWs to avoid infinite loop or too many files
        max_raw_search = 15  # As per user request, typically between 3 and 15
        search_count = 0

        while current_raw_number >= 0 and search_count < max_raw_search:
            raw_filename_stem = (
                f"{base_prefix}{current_raw_number:06d}"  # e.g., PB210632
            )

            # Look for any of the common RAW extensions
            potential_raw_paths = []
            for ext in RAW_EXTENSIONS:
                potential_raw_paths.append(raw_search_dir / f"{raw_filename_stem}{ext}")

            found_this_number = False
            for p in potential_raw_paths:
                if p.is_file():
                    found_raw_files.append(p)
                    found_this_number = True
                    break

            if not found_this_number:
                # User specified "continue until there is a gap in the numbers"
                # If we don't find any RAW for a number, assume it's a gap and stop
                if (
                    found_raw_files
                ):  # Only break if we've found at least one file before this gap
                    break

            current_raw_number -= 1
            search_count += 1

        if not found_raw_files:
            self.update_status_message(
                f"No source RAW files found in {raw_search_dir} for {filename}."
            )
            log.info("No source RAWs found for %s in %s", filename, raw_search_dir)
            return

        # Sort the files by name to ensure Helicon Focus receives them in sequence
        found_raw_files.sort()

        self.update_status_message(
            f"Launching Helicon Focus with {len(found_raw_files)} RAWs..."
        )
        log.info(
            "Launching Helicon Focus for %s with RAWs: %s",
            filename,
            [str(p) for p in found_raw_files],
        )
        success = self._launch_helicon_with_files(found_raw_files)

        if success:
            # Mark as restacked on success
            from datetime import datetime

            today = datetime.now().strftime("%Y-%m-%d")
            stem = self.image_files[self.current_index].path.stem
            meta = self.sidecar.get_metadata(stem)
            meta.restacked = True
            meta.restacked_date = today
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            self.update_status_message("Helicon Focus launched successfully.")
        else:
            self.update_status_message("Failed to launch Helicon Focus.")

    @Slot()
    def execute_crop(self):
        """Execute the crop operation: crop image, save, backup, and refresh."""
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image to crop")
            return

        if not self.ui_state.isCropping:
            return

        # Capture current rotation (straighten_angle) from editor state BEFORE any reload
        # This is the single source of truth since set_straighten_angle updates it live.
        current_rotation = float(
            self.image_editor.current_edits.get("straighten_angle", 0.0)
        )

        crop_box_raw = self.ui_state.currentCropBox

        # Normalize crop_box_raw to a tuple of 4 ints
        try:
            # Handle QJSValue/QVariant wrapper if present
            if hasattr(crop_box_raw, "toVariant"):
                crop_box_raw = crop_box_raw.toVariant()

            # Convert list to tuple if needed
            if isinstance(crop_box_raw, list):
                crop_box_raw = tuple(crop_box_raw)

            if not isinstance(crop_box_raw, tuple) or len(crop_box_raw) != 4:
                raise ValueError(
                    f"Expected 4-item tuple, got {type(crop_box_raw)}: {crop_box_raw}"
                )

            # Coerce elements to int and clamp to [0, 1000]
            l, t, r, b = [max(0, min(1000, int(x))) for x in crop_box_raw]

            # Ensure correct order (left <= right, top <= bottom)
            crop_box_raw = (min(l, r), min(t, b), max(l, r), max(t, b))

        except (ValueError, TypeError, AttributeError) as e:
            log.warning("Invalid crop box format: %s", e)
            self.update_status_message("Invalid crop selection")
            return

        if crop_box_raw == (0, 0, 1000, 1000):
            self.update_status_message("No crop area selected")
            return

        # Ensure image is loaded in editor
        image_file = self.image_files[self.current_index]
        filepath = image_file.path

        # Robust path comparison
        editor_path = self.image_editor.current_filepath
        paths_match = False
        if editor_path:
            try:
                paths_match = Path(editor_path).resolve() == Path(filepath).resolve()
            except (OSError, ValueError):
                paths_match = str(editor_path) == str(filepath)

        if not paths_match:
            log.debug(
                f"execute_crop reloading image due to path mismatch. Editor: {editor_path}, File: {filepath}"
            )
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(
                str(filepath), cached_preview=cached_preview
            ):
                self.update_status_message("Failed to load image for cropping")
                return

        self.image_editor.set_crop_box(crop_box_raw)

        # Re-apply the captured rotation.
        # This handles cases where we reloaded the image (resetting edits) or where UI state sync was flaky.
        self.image_editor.set_edit_param("straighten_angle", current_rotation)

        # Save via ImageEditor (handles rotation + crop correctly)
        try:
            save_result = self.image_editor.save_image()
        except RuntimeError as e:
            log.warning(f"execute_crop: Save failed: {e}")
            self.update_status_message(f"Failed to save cropped image: {e}")
            return
        except Exception as e:
            log.exception(f"execute_crop: Unexpected error during save: {e}")
            self.update_status_message("Failed to save cropped image")
            return

        if save_result:
            saved_path, backup_path = save_result

            # Track for undo
            import time

            timestamp = time.time()
            self.undo_history.append(
                ("crop", (str(saved_path), str(backup_path)), timestamp)
            )

            # Exit crop mode
            self.ui_state.isCropping = False
            self.ui_state.currentCropBox = (0, 0, 1000, 1000)

            # Refresh the view
            self.refresh_image_list()

            # Find the edited image
            for i, img_file in enumerate(self.image_files):
                if img_file.path == saved_path:
                    self.current_index = i
                    break

            # Invalidate cache and refresh display
            self.display_generation += 1
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            # Reset zoom/pan
            self.ui_state.resetZoomPan()

            if self.ui_state.isHistogramVisible:
                self.update_histogram()

            self.update_status_message("Image cropped and saved")
            log.info("Crop operation completed for %s", saved_path)

            # Force reload of editor to ensure subsequent edits operate on the cropped image
            self.image_editor.clear()
            self.reset_edit_parameters()

        else:
            self.update_status_message("Failed to save cropped image")

    @Slot()
    def auto_levels(self):
        """Calculates and applies auto levels (preview only). Returns False if skipped."""
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return False

        image_file = self.image_files[self.current_index]
        filepath = str(image_file.path)

        # Ensure image is loaded in editor
        if (
            not self.image_editor.current_filepath
            or str(self.image_editor.current_filepath) != filepath
        ):
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(
                filepath, cached_preview=cached_preview
            ):
                self.update_status_message("Failed to load image")
                return False

        # Calculate auto levels
        # Calculate auto levels - now returns (blacks, whites, p_low, p_high)
        blacks, whites, p_low, p_high = self.image_editor.auto_levels(
            self.auto_level_threshold
        )

        # Auto-strength computation using stretch-factor capping
        #
        # Philosophy: threshold_percent defines acceptable clipping (e.g., 0.1% at each end).
        # Auto-strength should NOT prevent that clipping - it's intentional.
        # Instead, auto-strength prevents INSANE levels on low-dynamic-range images.
        #
        # Approach: Cap the stretch factor to a reasonable maximum (e.g., 3-4x).
        # - Full strength: stretch = 255 / (p_high - p_low)
        # - If stretch is reasonable (<= cap), use full strength
        # - If stretch is extreme (> cap), blend to limit effective stretch to cap
        #
        if self.auto_level_strength_auto:
            # Calculate full-strength stretch factor
            dynamic_range = p_high - p_low
            if dynamic_range < 1.0:
                # Degenerate case: nearly flat image
                strength = 0.0
                log.debug(
                    f"Auto levels: degenerate dynamic range ({dynamic_range:.2f}), strength=0"
                )
            else:
                stretch_full = 255.0 / dynamic_range

                # Cap stretch to prevent insane levels
                # E.g., if image spans only 50-200 (range=150), full stretch would be 255/150 = 1.7x (fine)
                # But if image spans 100-110 (range=10), full stretch would be 255/10 = 25.5x (insane!)
                STRETCH_CAP = 4.0  # Maximum allowed stretch factor

                if stretch_full <= STRETCH_CAP:
                    # Reasonable stretch, use full strength
                    strength = 1.0
                else:
                    # Excessive stretch - blend to cap it
                    # effective_stretch = 1 + strength * (stretch_full - 1) = STRETCH_CAP
                    # solving for strength: strength = (STRETCH_CAP - 1) / (stretch_full - 1)
                    strength = (STRETCH_CAP - 1.0) / (stretch_full - 1.0)
                    strength = max(0.0, min(1.0, strength))

                log.debug(
                    f"Auto levels: p_low={p_low:.1f}, p_high={p_high:.1f}, "
                    f"range={dynamic_range:.1f}, stretch_full={stretch_full:.2f}, strength={strength:.3f}"
                )
        else:
            strength = self.auto_level_strength

        # Apply strength scaling to blacks and whites parameters
        blacks *= strength
        whites *= strength

        # Apply scaled values
        self.image_editor.set_edit_param("blacks", blacks)
        self.image_editor.set_edit_param("whites", whites)

        # Update UI state
        self.ui_state.blacks = blacks
        self.ui_state.whites = whites

        # Trigger preview update
        self.ui_state.currentImageSourceChanged.emit()

        if self.ui_state.isHistogramVisible:
            self.update_histogram()

        # Determine status message based on whether endpoints were pinned (clipping detected)
        # We check p_high/p_low directly because whites/blacks might be small due to strength scaling
        # even if not pinned.
        msg = "Auto levels applied"

        # Check for essentially no-op (degenerate or already full range)
        # Degenerate: dynamic range is tiny (< 1.0)
        # Full range: p_low is near 0 and p_high near 255
        if abs(p_high - p_low) < 1.0:
            msg = "Auto levels: no changes (degenerate range)"
        elif p_low <= 0 and p_high >= 255:
            # We already cover the full range
            msg = "Auto levels: no changes (image already covers full range)"
        # Check for pinning
        elif p_high >= 255.0:
            msg = "Auto levels: highlights already clipped; only adjusting shadows"
        elif p_low <= 0.0:
            msg = "Auto levels: shadows already clipped; only adjusting highlights"

        self._kick_preview_worker()

        self.update_status_message(f"{msg} (preview only)")
        log.info(
            "Auto levels preview applied to %s (clip %.2f%%, str %.2f). Msg: %s",
            filepath,
            self.auto_level_threshold,
            strength,
            msg,
        )
        return True

    @Slot()
    def quick_auto_levels(self):
        """Applies auto levels and immediately saves (with undo)."""
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        # Apply the preview first (loads image + sets params)
        applied = self.auto_levels()

        # If in auto mode and no changes were made (skipped), don't save
        if self.auto_level_strength_auto and not applied:
            # Status message already set by auto_levels ("No changes made...")
            return

        # Save
        import time

        try:
            save_result = self.image_editor.save_image()
        except RuntimeError as e:
            log.warning(f"quick_auto_levels: Save failed: {e}")
            self.update_status_message(f"Failed to save image: {e}")
            return
        except Exception as e:
            log.exception(f"quick_auto_levels: Unexpected error during save: {e}")
            self.update_status_message("Failed to save image")
            return

        if save_result:
            saved_path, backup_path = save_result
            timestamp = time.time()
            self.undo_history.append(
                ("auto_levels", (saved_path, backup_path), timestamp)
            )

            # Force reload to ensure disk consistency
            self.image_editor.clear()

            # Refresh list/cache/UI (standard save pattern)
            # Note: We must locate the saved_path again because the list order
            # might have changed (e.g., if a backup file was inserted before it).
            self.refresh_image_list()

            # Find image again using robust path matching
            new_index = -1
            target_name = Path(saved_path).name

            for i, img_file in enumerate(self.image_files):
                # Match by filename alone - safest for flat directory structures
                # avoiding drive letter/symlink/casing issues with full paths
                if img_file.path.name == target_name:
                    new_index = i
                    break

            if new_index != -1:
                self.current_index = new_index
            else:
                log.warning(
                    "Auto levels: Could not find saved image %s (name: %s) in refreshed list",
                    saved_path,
                    target_name,
                )

            self.display_generation += 1
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            if self.ui_state.isHistogramVisible:
                self.update_histogram()

            self.update_status_message("Auto levels applied and saved")
            log.info(
                "Quick auto levels saved for %s. New index: %d",
                saved_path,
                self.current_index,
            )
        else:
            self.update_status_message("Failed to save image")

    @Slot()
    def quick_auto_white_balance(self):
        """Quickly apply auto white balance, save the image, and track for undo."""
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        import time

        image_file = self.image_files[self.current_index]
        filepath = str(image_file.path)

        # Load the image into the editor if not already loaded
        cached_preview = self.get_decoded_image(self.current_index)
        if not self.image_editor.load_image(filepath, cached_preview=cached_preview):
            self.update_status_message("Failed to load image")
            return

        # Calculate and apply auto white balance
        self.auto_white_balance()

        # Save the edited image (this creates a backup automatically)
        try:
            save_result = self.image_editor.save_image()
        except RuntimeError as e:
            log.warning(f"quick_auto_white_balance: Save failed: {e}")
            self.update_status_message(f"Failed to save image: {e}")
            return
        except Exception as e:
            log.exception(
                f"quick_auto_white_balance: Unexpected error during save: {e}"
            )
            self.update_status_message("Failed to save image")
            return

        if save_result:
            saved_path, backup_path = save_result
            # Track this action for undo
            timestamp = time.time()
            self.undo_history.append(
                ("auto_white_balance", (saved_path, backup_path), timestamp)
            )

            # Force the image editor to clear its current state so it reloads fresh
            self.image_editor.clear()

            # Refresh the view - need to refresh image list since backup file was created
            original_path = Path(filepath)
            self.refresh_image_list()

            # Find the edited image (not the backup) in the refreshed list
            for i, img_file in enumerate(self.image_files):
                if img_file.path == original_path:
                    self.current_index = i
                    break

            # Invalidate cache for the edited image so it's reloaded from disk
            # This ensures the Image Editor will see the updated version
            self.display_generation += 1
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            # Update histogram if visible
            if self.ui_state.isHistogramVisible:
                self.update_histogram()

            self.update_status_message("Auto white balance applied and saved")
            log.info("Quick auto white balance applied to %s", filepath)
        else:
            self.update_status_message("Failed to save image")

    @Slot()
    def auto_white_balance(self):
        """
        Dispatcher for auto white balance. Calls the appropriate method based on
        the mode set in the config ('lab' or 'rgb').
        """
        mode = config.get("awb", "mode", fallback="lab")
        if mode == "lab":
            self.auto_white_balance_lab()
        elif mode == "rgb":
            self.auto_white_balance_legacy()
        else:
            log.error(f"Unknown AWB mode: {mode}")
            self.update_status_message(f"Error: Unknown AWB mode '{mode}'")

    def auto_white_balance_legacy(self):
        """
        Calculates and applies auto white balance using the legacy grey world
        assumption on the entire RGB image.
        """
        if not self.image_editor.original_image:
            log.warning("No image loaded in editor for auto white balance")
            return

        try:
            import numpy as np
        except ImportError:
            log.error("NumPy not found. Please install with: pip install numpy")
            self.update_status_message("Error: NumPy not installed")
            return

        log.info("Applying legacy (RGB Grey World) Auto White Balance")

        img = self.image_editor.original_image
        arr = np.array(img, dtype=np.float32)

        r_mean = arr[:, :, 0].mean()
        g_mean = arr[:, :, 1].mean()
        b_mean = arr[:, :, 2].mean()

        grey_target = (r_mean + g_mean + b_mean) / 3.0

        r_diff = r_mean - grey_target
        g_diff = g_mean - grey_target

        by_shift = -(r_diff + g_diff) / 2.0
        mg_shift = -(r_diff - g_diff) / 2.0

        by_value = by_shift / 63.75
        mg_value = mg_shift / 63.75

        by_value = float(np.clip(by_value, -1.0, 1.0))
        mg_value = float(np.clip(mg_value, -1.0, 1.0))

        self.image_editor.set_edit_param("white_balance_by", by_value)
        self.image_editor.set_edit_param("white_balance_mg", mg_value)

        self.ui_state.white_balance_by = by_value
        self.ui_state.white_balance_mg = mg_value

        self.ui_refresh_generation += 1
        self.ui_state.currentImageSourceChanged.emit()
        self.update_status_message("Auto white balance applied (Legacy)")

    def auto_white_balance_lab(self):
        """
        Calculates and applies auto white balance using the Lab color space,
        filtering out clipped and saturated pixels for a more robust result.
        """
        if not self.image_editor.original_image:
            log.warning("No image loaded in editor for auto white balance")
            return

        try:
            import cv2
            import numpy as np
        except ImportError:
            log.error(
                "OpenCV or NumPy not found. Please install with: pip install opencv-python numpy"
            )
            self.update_status_message("Error: OpenCV or NumPy not installed")
            return

        img = self.image_editor.original_image
        # Ensure image is RGB before processing
        if img.mode != "RGB":
            img = img.convert("RGB")

        arr = np.array(img, dtype=np.uint8)

        # --- Tunable Constants for Auto White Balance (from config) ---
        _LOWER_BOUND_RGB = config.getint("awb", "rgb_lower_bound", 5)
        _UPPER_BOUND_RGB = config.getint("awb", "rgb_upper_bound", 250)
        _LUMA_LOWER_BOUND = config.getint("awb", "luma_lower_bound", 30)
        _LUMA_UPPER_BOUND = config.getint("awb", "luma_upper_bound", 220)
        warm_bias = config.getint("awb", "warm_bias", 6)
        tint_bias = config.getint("awb", "tint_bias", 0)
        _TARGET_A_LAB = 128.0 + tint_bias
        _TARGET_B_LAB = 128.0 + warm_bias
        _SCALING_FACTOR_LAB_TO_SLIDER = 128.0
        _CORRECTION_STRENGTH = config.getfloat("awb", "strength", 0.7)

        # --- 1. Reject clipped channels and use a luma midtone mask ---
        mask = (
            (arr[:, :, 0] > _LOWER_BOUND_RGB)
            & (arr[:, :, 0] < _UPPER_BOUND_RGB)
            & (arr[:, :, 1] > _LOWER_BOUND_RGB)
            & (arr[:, :, 1] < _UPPER_BOUND_RGB)
            & (arr[:, :, 2] > _LOWER_BOUND_RGB)
            & (arr[:, :, 2] < _UPPER_BOUND_RGB)
        )

        luma = 0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2]
        mask &= (luma > _LUMA_LOWER_BOUND) & (luma < _LUMA_UPPER_BOUND)

        if not np.any(mask):
            log.warning(
                "Auto white balance: No pixels found after clipping and luma filter. Aborting."
            )
            self.update_status_message("AWB failed: no valid pixels found")
            return

        # --- 2. Work in Lab color space ---
        lab_image = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)

        a_channel = lab_image[:, :, 1]
        b_channel = lab_image[:, :, 2]

        masked_a = a_channel[mask]
        masked_b = b_channel[mask]

        a_mean = masked_a.mean()
        b_mean = masked_b.mean()

        a_shift = _TARGET_A_LAB - a_mean
        b_shift = _TARGET_B_LAB - b_mean

        log.info(
            "Auto WB (Lab) - means: a*=%.1f, b*=%.1f; targets: a*=%.1f, b*=%.1f; shifts: a*=%.1f, b*=%.1f",
            a_mean,
            b_mean,
            _TARGET_A_LAB,
            _TARGET_B_LAB,
            a_shift,
            b_shift,
        )

        # --- 3. Convert Lab shift to our slider values with strength factor ---
        by_value = (b_shift / _SCALING_FACTOR_LAB_TO_SLIDER) * _CORRECTION_STRENGTH
        mg_value = (a_shift / _SCALING_FACTOR_LAB_TO_SLIDER) * _CORRECTION_STRENGTH

        by_value = float(np.clip(by_value, -1.0, 1.0))
        mg_value = float(np.clip(mg_value, -1.0, 1.0))

        log.info(f"Auto white balance values: B/Y={by_value:.3f}, M/G={mg_value:.3f}")

        self.image_editor.set_edit_param("white_balance_by", by_value)
        self.image_editor.set_edit_param("white_balance_mg", mg_value)

        self.ui_state.white_balance_by = by_value
        self.ui_state.white_balance_mg = mg_value

        self.ui_refresh_generation += 1
        self.ui_state.currentImageSourceChanged.emit()
        self.update_status_message("Auto white balance applied")

    def _get_stack_info(self, index: int) -> str:
        info = ""
        for i, (start, end) in enumerate(self.stacks):
            if start <= index <= end:
                count_in_stack = end - start + 1
                pos_in_stack = index - start + 1
                info = f"Stack {i + 1} ({pos_in_stack}/{count_in_stack})"
                break
        if (
            not info
            and self.stack_start_index is not None
            and self.stack_start_index == index
        ):
            info = "Stack Start Marked"
        log.debug("_get_stack_info for index %d: %s", index, info)
        return info

    def _get_batch_info(self, index: int) -> str:
        """Get batch info for the given index."""
        info = ""
        # Check if current image is in any batch
        in_batch = False
        for start, end in self.batches:
            if start <= index <= end:
                in_batch = True
                break

        if in_batch:
            # Calculate total count across all batches
            total_count = sum(end - start + 1 for start, end in self.batches)
            info = f"{total_count} in Batch"
        elif self.batch_start_index is not None and self.batch_start_index == index:
            info = "Batch Start Marked"

        log.debug("_get_batch_info for index %d: %s", index, info)
        return info

    def get_stack_summary(self) -> str:
        if not self.stacks:
            return "No stacks defined."
        summary = []
        for i, (start, end) in enumerate(self.stacks):
            summary.append(f"Stack {i + 1}: {start}-{end}")
        return "; ".join(summary)

    def is_stacked(self) -> bool:
        if not self.image_files or self.current_index >= len(self.image_files):
            return False
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        return meta.stacked

    def _update_cache_stats(self):
        if self.debug_cache:
            hits = self.image_cache.hits
            misses = self.image_cache.misses
            total = hits + misses
            hit_rate = (hits / total * 100) if total > 0 else 0
            size_mb = self.image_cache.currsize / (1024 * 1024)
            self.ui_state.cacheStats = f"Cache: {hits} hits, {misses} misses ({hit_rate:.1f}%), {size_mb:.1f} MB"

    def get_recycle_bin_stats(self) -> List[Dict[str, Any]]:
        """Get stats for all tracked recycle bins.

        Returns:
            List of dicts: [{"path": absolute_path, "count": num_files}, ...]
        """
        stats = []
        # Filter out bins that don't exist anymore
        active_bins = {p for p in self.active_recycle_bins if p.exists() and p.is_dir()}
        self.active_recycle_bins = active_bins

        for bin_path in self.active_recycle_bins:
            try:
                # Count files
                count = sum(1 for p in bin_path.iterdir() if p.is_file())
                if count > 0:
                    stats.append({"path": str(bin_path), "count": count})
            except OSError:
                continue

        return stats

    def cleanup_recycle_bins(self):
        """Delete all tracked recycle bins."""
        active_bins = {p for p in self.active_recycle_bins if p.exists() and p.is_dir()}

        for bin_path in active_bins:
            try:
                shutil.rmtree(bin_path)
                log.info("Cleaned up recycle bin: %s", bin_path)
            except OSError as e:
                log.error("Failed to delete recycle bin %s: %s", bin_path, e)

        self.active_recycle_bins.clear()

        # Clear stats cache since we deleted files/folders
        clear_raw_count_cache()


def main(image_dir: str = "", debug: bool = False, debug_cache: bool = False):
    """FastStack Application Entry Point"""
    global _debug_mode
    _debug_mode = debug

    t0 = time.perf_counter()
    setup_logging(debug)
    if debug:
        log.info("Startup: after setup_logging: %.3fs", time.perf_counter() - t0)
    log.info("Starting FastStack")

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"
    os.environ["QML2_IMPORT_PATH"] = os.path.join(os.path.dirname(__file__), "qml")

    app = QApplication(
        sys.argv
    )  # QApplication is correct for desktop apps with widgets
    if debug:
        log.info("Startup: after QApplication: %.3fs", time.perf_counter() - t0)

    if not image_dir:
        image_dir_str = config.get("core", "default_directory")
        if not image_dir_str:
            log.warning(
                "No image directory provided and no default directory set. Opening directory selection dialog."
            )
            selected_dir = QFileDialog.getExistingDirectory(
                None, "Select Image Directory"
            )
            if not selected_dir:
                log.error("No image directory selected. Exiting.")
                sys.exit(1)
            image_dir_str = selected_dir
        image_dir_path = Path(image_dir_str)
    else:
        image_dir_path = Path(image_dir)

    if not image_dir_path.is_dir():
        log.error("Image directory not found: %s", image_dir_path)
        sys.exit(1)
    app.setOrganizationName("FastStack")
    app.setOrganizationDomain("faststack.dev")
    app.setApplicationName("FastStack")

    engine = QQmlApplicationEngine()
    engine.addImportPath(os.path.join(os.path.dirname(PySide6.__file__), "qml"))
    engine.addImportPath("qrc:/qt-project.org/imports")
    engine.addImportPath(os.path.join(os.path.dirname(__file__), "qml"))
    # Add the path to Qt5Compat.GraphicalEffects to QML import paths
    engine.addImportPath(
        os.path.join(os.path.dirname(PySide6.__file__), "qml", "Qt5Compat")
    )

    controller = AppController(image_dir_path, engine, debug_cache=debug_cache)
    if debug:
        log.info("Startup: after AppController: %.3fs", time.perf_counter() - t0)
    image_provider = ImageProvider(controller)
    engine.addImageProvider("provider", image_provider)
    # Register thumbnail provider for grid view
    engine.addImageProvider("thumbnail", controller._thumbnail_provider)

    # Expose controller and UI state to QML
    context = engine.rootContext()
    context.setContextProperty("uiState", controller.ui_state)
    context.setContextProperty("controller", controller)
    context.setContextProperty("thumbnailModel", controller._thumbnail_model)

    qml_file = Path(__file__).parent / "qml" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    if debug:
        log.info("Startup: after engine.load(QML): %.3fs", time.perf_counter() - t0)

    if not engine.rootObjects():
        log.error("Failed to load QML.")
        sys.exit(-1)

    # Connect key events from the main window
    main_window = engine.rootObjects()[0]
    controller.main_window = main_window
    main_window.installEventFilter(controller)

    # Load data and start services
    controller.load()
    if debug:
        log.info("Startup: after controller.load(): %.3fs", time.perf_counter() - t0)

    # Graceful shutdown
    app.aboutToQuit.connect(controller.shutdown)

    sys.exit(app.exec())


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="FastStack - Ultra-fast JPG Viewer for Focus Stacking Selection"
    )
    parser.add_argument(
        "image_dir", nargs="?", default="", help="Directory of images to view"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and timing information",
    )
    parser.add_argument(
        "--debugcache", action="store_true", help="Enable debug cache features"
    )
    args = parser.parse_args()
    main(image_dir=args.image_dir, debug=args.debug, debug_cache=args.debugcache)


if __name__ == "__main__":
    cli()
