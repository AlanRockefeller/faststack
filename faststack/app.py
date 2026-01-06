"""Main application entry point for FastStack."""

import logging
import sys
import math
import struct
import shlex
import time
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import date
import os
import shutil
# Must set before importing PySide6
os.environ["QT_LOGGING_RULES"] = "qt.qpa.mime.warning=false"

# Type Aliases for readability
DeletePair = Tuple[Optional[Path], Optional[Path]]  # (src_path, recycle_bin_path)
DeleteRecord = Tuple[DeletePair, DeletePair]        # (jpg_pair, raw_pair)

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
    QCoreApplication
)
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from PySide6.QtQml import QQmlApplicationEngine
from PIL import Image
Image.MAX_IMAGE_PIXELS = 200_000_000  # 200 megapixels, enough for most photos
# ⬇️ these are the ones that went missing
from faststack.config import config
from faststack.logging_setup import setup_logging
from faststack.models import ImageFile, DecodedImage, EntryMetadata
from faststack.io.indexer import find_images
from faststack.io.sidecar import SidecarManager
from faststack.io.watcher import Watcher
from faststack.io.helicon import launch_helicon_focus
from faststack.io.executable_validator import validate_executable_path
from faststack.imaging.cache import ByteLRUCache, get_decoded_image_size, build_cache_key
from faststack.imaging.prefetch import Prefetcher, clear_icc_caches
from faststack.ui.provider import ImageProvider
from faststack.ui.keystrokes import Keybinder
from faststack.imaging.editor import ImageEditor, ASPECT_RATIOS, create_backup_file
from faststack.imaging.metadata import get_exif_data
import re
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
    dataChanged = Signal() # New signal for general data changes
    is_zoomed_changed = Signal(bool) # Signal for zoom state changes
    histogramReady = Signal(object) # Signal for off-thread histogram result
    previewReady = Signal(object) # Signal for off-thread preview result
    dialogStateChanged = Signal(bool) # Signal for dialog open/close state

    class ProgressReporter(QObject):
        progress_updated = Signal(int)
        finished = Signal()

    def __init__(self, image_dir: Path, engine: QQmlApplicationEngine, debug_cache: bool = False):
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
        self._last_rendered_preview = None # Store latest valid render
        self._shutting_down = False # Flag to gate async callbacks during shutdown

        self.image_dir = image_dir
        self.image_files: List[ImageFile] = []  # Filtered list for display
        self._all_images: List[ImageFile] = []  # Cached full list from disk
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None
        self.engine = engine
        self.debug_cache = debug_cache # New debug_cache flag
        
        # Ensure clean shutdown of background threads
        QCoreApplication.instance().aboutToQuit.connect(self._shutdown_executors)

        self.display_width = 0
        self.display_height = 0
        self.display_generation = 0
        self._is_decoding = False
        
        # Cache Warning State
        self._last_cache_warning_time = 0
        self._eviction_timestamps = [] # List of eviction timestamps for rate detection
        self.display_ready = False  # Track if display size has been reported
        self.pending_prefetch_index: Optional[int] = None  # Deferred prefetch index

        # -- Backend Components --
        self.watcher = Watcher(self.image_dir, self.refresh_image_list)
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
        self.image_editor = ImageEditor() # Initialize the editor
        self._dialog_open_count = 0 # Track nested dialogs
        
        # -- Caching & Prefetching --
        cache_size_gb = config.getfloat('core', 'cache_size_gb', 1.5)
        cache_size_bytes = int(cache_size_gb * 1024**3)
        self._has_warned_cache_full = False
        self.image_cache = ByteLRUCache(
            max_bytes=cache_size_bytes, 
            size_of=get_decoded_image_size,
            on_evict=self._on_cache_evict
        )
        self.image_cache.hits = 0 # Initialize cache hit counter
        self.image_cache.misses = 0 # Initialize cache miss counter
        self.prefetcher = Prefetcher(
            image_files=self.image_files,
            cache_put=self.image_cache.__setitem__,
            prefetch_radius=config.getint('core', 'prefetch_radius', 4),
            get_display_info=self.get_display_info,
            debug=_debug_mode
        )
        self.last_displayed_image: Optional[DecodedImage] = None  # Cache last image to avoid grey squares
        self._last_image_lock = threading.Lock()  # Protect last_displayed_image from race conditions

        # -- UI State --
        self.ui_state = UIState(self)
        self.ui_state.theme = self.get_theme()
        self.ui_state.debugCache = self.debug_cache
        self.ui_state.debugMode = _debug_mode # Set debug mode from global
        self.keybinder = Keybinder(self)
        self.ui_state.debugCache = self.debug_cache # Pass debug_cache state to UI
        self.ui_state.isDecoding = False # Initialize decoding indicator
        self.is_zoomed = False # Track zoom state for high-res loading logic

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stacks: List[List[int]] = []

        
        # -- Batch Selection State (for drag-and-drop) --
        self.batch_start_index: Optional[int] = None
        self.batches: List[List[int]] = []  # List of [start, end] ranges
        
        self._filter_string: str = "" # Default filter
        self._filter_enabled: bool = False

        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        with self._last_image_lock:
            self.last_displayed_image = None
        self._logged_empty_metadata = False
        
        # -- Delete/Undo State --
        self.recycle_bin_dir = self.image_dir / "image recycle bin"
        self.delete_history: List[DeleteRecord] = []  # [((jpg_src, jpg_bin), (raw_src, raw_bin)), ...]
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

        # Preview Refresh Timer (Coalescing)
        self._preview_refresh_pending = False
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(33) # ~30fps cap for smoother dragging
        self.preview_timer.timeout.connect(self._do_preview_refresh)

        # Track if any dialog is open to disable keybindings
        self._dialog_open = False
        
        self.auto_level_threshold = config.getfloat('core', 'auto_level_threshold', 0.1)
        self.auto_level_strength = config.getfloat('core', 'auto_level_strength', 1.0)
        self.auto_level_strength_auto = config.getboolean('core', 'auto_level_strength_auto', False)


    @Slot(str)
    def apply_filter(self, filter_string: str):
        filter_string = filter_string.strip()

        if not filter_string:
            self.clear_filter()
            return

        self._filter_string = filter_string
        self._filter_enabled = True
        self._apply_filter_to_cached_list()  # Fast in-memory filtering
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
        log.debug(f"on_display_size_changed called with {width}x{height}. Current: {self.display_width}x{self.display_height}")
        if width <= 0 or height <= 0:
            log.debug("Ignoring invalid resize event")
            return

        # Debounce resize events
        self.pending_width = width
        self.pending_height = height
        self.resize_timer.start(150)  # 150ms debounce

    def _handle_resize(self):
        """Actual resize handler, called after debounce period."""
        log.info("Display size changed to: %dx%d (physical pixels)", self.pending_width, self.pending_height)
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
        
        self.sync_ui_state() # To refresh the image

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
            self.main_window.update() # Force repaint
            
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
            
            # When cropping (or editing), let QML handle Enter/Esc and related keys.
            # Otherwise keybinder can swallow them before QML sees them.
            if getattr(self.ui_state, "isCropping", False) or getattr(self.ui_state, "isEditorOpen", False):
                return False

            handled = self.keybinder.handle_key_press(event)
            if handled:
                return True
        return super().eventFilter(watched, event)

    def _do_prefetch(self, index: int, is_navigation: bool = False, direction: Optional[int] = None):
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
        self.prefetcher.update_prefetch(index, is_navigation=is_navigation, direction=direction)
    
    def load(self):
        """Loads images, sidecar data, and starts services."""
        self.refresh_image_list()  # Initial scan from disk
        if not self.image_files:
            self.current_index = 0
        else:
            self.current_index = max(0, min(self.sidecar.data.last_index, len(self.image_files) - 1))
        self.stacks = self.sidecar.data.stacks # Load stacks from sidecar
        self.dataChanged.emit() # Emit after stacks are loaded
        self.watcher.start()
        self._do_prefetch(self.current_index)
        
        # Defer initial UI sync until after images are loaded
        self.sync_ui_state()


    def refresh_image_list(self):
        """Rescans the directory for images from disk and updates cache.
        
        This does a full disk scan and should only be called when:
        - Application starts (load())
        - Directory watcher detects file changes
        - User explicitly refreshes
        
        For filtering, use _apply_filter_to_cached_list() instead.
        """
        self._all_images = find_images(self.image_dir)
        self._apply_filter_to_cached_list()
    
    def _apply_filter_to_cached_list(self):
        """Applies current filter to cached image list without disk I/O."""
        if self._filter_enabled and self._filter_string:
            needle = self._filter_string.lower()
            self.image_files = [
                img for img in self._all_images
                if needle in img.path.stem.lower()
            ]
        else:
            self.image_files = self._all_images

        self.prefetcher.set_image_files(self.image_files)
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.ui_state.imageCountChanged.emit()

    def get_decoded_image(self, index: int) -> Optional[DecodedImage]:
        """Retrieves a decoded image, blocking until ready to ensure correct display.
        
        This blocks the UI thread on cache miss, but that's acceptable for an image viewer
        where users expect to see the correct image immediately. The prefetcher minimizes
        cache misses by decoding adjacent images in advance.
        """
        if not self.image_files or index < 0 or index >= len(self.image_files):
            log.warning("get_decoded_image called with empty image_files or out of bounds index.")
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
                log.debug("Path mismatch in preview. Editor: %s, File: %s", editor_path, file_path)
            
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
            self.image_cache.hits += 1 # Increment hit counter
            self._update_cache_stats() # Update UI with new stats
            decoded = self.image_cache[cache_key]
            with self._last_image_lock:
                self.last_displayed_image = decoded
            return decoded
        
        self.image_cache.misses += 1 # Increment miss counter
        self._update_cache_stats() # Update UI with new stats
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
            log.info("Cache miss for index %d (gen: %d). Blocking decode.", index, display_gen)
        
        # Show decoding indicator if debug cache is enabled
        if self.debug_cache:
            self.ui_state.isDecoding = True
            # Note: processEvents() caused crashes, so the indicator might not update immediately
            # QCoreApplication.processEvents()
        
        try:
            # Submit with priority=True to cancel pending prefetch tasks and free up workers
            future = self.prefetcher.submit_task(index, self.prefetcher.generation, priority=True)
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
                    log.debug("Decode finished but cache_key missing (index=%d, key=%s)", index, cache_key)
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
            future = self.prefetcher.submit_task(index, self.prefetcher.generation, priority=True)
            if future:
                try:
                    result = future.result(timeout=5.0)
                except concurrent.futures.TimeoutError:
                    log.warning(f"Timeout decoding image at index {index} (background)")
                    return None
                except concurrent.futures.CancelledError:
                    log.debug(f"Decode cancelled for image at index {index} (background)")
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

        # this is the one your footer needs
        self.ui_state.metadataChanged.emit()

        log.debug(
            "UI State Synced: Index=%d, Count=%d",
            self.ui_state.currentIndex,
            self.ui_state.imageCount
        )
        log.debug(
            "Metadata Synced: Filename=%s, Uploaded=%s, StackInfo='%s', BatchInfo='%s'",
            self.ui_state.currentFilename,
            self.ui_state.isUploaded,
            self.ui_state.stackInfoText,
            self.ui_state.batchInfoText
        )


    # --- Actions --- 

    def next_image(self):
        if self.current_index < len(self.image_files) - 1:
            self.current_index += 1
            self._reset_crop_settings()
            self._do_prefetch(self.current_index, is_navigation=True, direction=1)
            self.sync_ui_state()
            # Update histogram if visible
            if self.ui_state.isHistogramVisible:
                self.update_histogram()

    def prev_image(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._reset_crop_settings()
            self._do_prefetch(self.current_index, is_navigation=True, direction=-1)
            self.sync_ui_state()
            # Update histogram if visible
            if self.ui_state.isHistogramVisible:
                self.update_histogram()

    @Slot(int)
    def jump_to_image(self, index: int):
        """Jump to a specific image by index (0-based)."""
        if 0 <= index < len(self.image_files):
            if index == self.current_index:
                self.update_status_message(f"Already at image {index + 1}")
                return
            direction = 1 if index > self.current_index else -1
            self.current_index = index
            self._reset_crop_settings()
            self._do_prefetch(self.current_index, is_navigation=True, direction=direction)
            self.sync_ui_state()
            # Update histogram if visible
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
            self.update_status_message(f"Jumped to image {index + 1}")
        else:
            log.warning("Invalid image index: %d", index)
            self.update_status_message("Invalid image number")

    def show_jump_to_image_dialog(self):
        """Shows the jump to image dialog (called from keybinder)."""
        if self.main_window and hasattr(self.main_window, 'show_jump_to_image_dialog'):
            self.main_window.show_jump_to_image_dialog()
        else:
            log.warning("Cannot open jump to image dialog: main_window or function not available")

    def show_exif_dialog(self):
        """Shows the EXIF data dialog."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        path = self.image_files[self.current_index].path
        data = get_exif_data(path)
        
        if self.main_window and hasattr(self.main_window, 'openExifDialog'):
            # Pass data as QVariantMap (dict)
            self.main_window.openExifDialog(data)
        else:
            log.warning("Cannot open EXIF dialog: main_window or openExifDialog not available")
    
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
        log.warning("Grid view not implemented yet.")
    
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
                log.debug("get_current_metadata: image_files is empty or index out of bounds, returning {}.")
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
            "batch_info_text": batch_info
        }
        self._metadata_cache_index = cache_key
        return self._metadata_cache

    def begin_new_stack(self):
        self.stack_start_index = self.current_index
        log.info("Stack start marked at index %d", self.stack_start_index)
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit() # Update UI to show start marker
        self.sync_ui_state()

    def end_current_stack(self):
        log.info("end_current_stack called. stack_start_index: %s", self.stack_start_index)
        if self.stack_start_index is not None:
            start = min(self.stack_start_index, self.current_index)
            end = max(self.stack_start_index, self.current_index)
            self.stacks.append([start, end])
            self.stacks.sort() # Keep stacks sorted by start index
            self.sidecar.data.stacks = self.stacks
            self.sidecar.save()
            log.info("Defined new stack: [%d, %d]", start, end)
            self.stack_start_index = None
            self._metadata_cache_index = (-1, -1) # Invalidate cache
            self.dataChanged.emit() # Notify QML of data change
            self.ui_state.stackSummaryChanged.emit() # Update stack summary in dialog
            self.sync_ui_state()
        else:
            log.warning("No stack start marked. Press '[' first.")
    
    def begin_new_batch(self):
        """Mark the start of a new batch for drag-and-drop."""
        self.batch_start_index = self.current_index
        log.info("Batch start marked at index %d", self.batch_start_index)
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("Batch start marked")
    
    def end_current_batch(self):
        """End the current batch and save the range."""
        log.info("end_current_batch called. batch_start_index: %s", self.batch_start_index)
        if self.batch_start_index is not None:
            start = min(self.batch_start_index, self.current_index)
            end = max(self.batch_start_index, self.current_index)
            self.batches.append([start, end])
            self.batches.sort() # Keep batches sorted by start index
            log.info("Defined new batch: [%d, %d]", start, end)
            self.batch_start_index = None
            self._metadata_cache_index = (-1, -1) # Invalidate cache
            self.dataChanged.emit()
            self.sync_ui_state()
            count = end - start + 1
            self.update_status_message(f"Batch defined: {count} images")
        else:
            log.warning("No batch start marked. Press '{' first.")
            self.update_status_message("No batch start marked")
    
    
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
                
                log.info("Removed index %d from batch [%d, %d]", self.current_index, start, end)
                self.update_status_message(f"Removed from batch")
                removed = True
                batch_modified = True
            else:
                new_batches.append([start, end])
        
        if batch_modified:
            self.batches = new_batches
        
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
                    
                    log.info("Removed index %d from stack [%d, %d]", self.current_index, start, end)
                    self.update_status_message(f"Removed from stack")
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
                log.info("No existing batches. Created new batch for index %d.", index_to_toggle)
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
            log.info("Removed index %d from stack #%d.", index_to_toggle, stack_to_modify_idx + 1)

        else:
            # --- Add to nearest stack ---
            if not self.stacks:
                self.stacks.append([index_to_toggle, index_to_toggle])
                self.update_status_message("Created new stack with current image.")
                log.info("No existing stacks. Created new stack for index %d.", index_to_toggle)
            else:
                # Find closest stack
                dist_backward = float('inf')
                stack_idx_backward = -1
                for i in range(index_to_toggle - 1, -1, -1):
                    for j, (start, end) in enumerate(self.stacks):
                        if start <= i <= end:
                            dist_backward = index_to_toggle - i
                            stack_idx_backward = j
                            break
                    if stack_idx_backward != -1:
                        break

                dist_forward = float('inf')
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
                    log.info("No stacks found nearby. Created new stack for index %d.", index_to_toggle)
                else:
                    if dist_backward <= dist_forward:
                        stack_to_join_idx = stack_idx_backward
                    else:
                        stack_to_join_idx = stack_idx_forward

                    start, end = self.stacks[stack_to_join_idx]
                    self.stacks[stack_to_join_idx] = [min(start, index_to_toggle), max(end, index_to_toggle)]
                    
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

                    self.update_status_message(f"Added image to Stack #{new_stack_idx + 1}")
                    log.info("Added index %d to stack #%d.", index_to_toggle, new_stack_idx + 1)

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
        self.image_editor.set_edit_param('rotation', 0)
        self.image_editor.set_edit_param('straighten_angle', 0.0)
        # Also update UI state for rotation values if they are exposed
        if hasattr(self.ui_state, 'rotation'):
            self.ui_state.rotation = 0
        if hasattr(self.ui_state, 'cropRotation'): # This is used by Components.qml for the overlay
            self.ui_state.cropRotation = 0.0
        
        # Also reset the straighten angle in current_edits since it affects rotation logic
        if 'straighten_angle' in self.image_editor.current_edits:
             self.image_editor.current_edits['straighten_angle'] = 0.0

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
                        file_to_use = img_file.raw_pair if img_file.raw_pair else img_file.path
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
            self._metadata_cache_index = (-1, -1) # Invalidate cache
        
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
        
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("All batches cleared")

    def get_helicon_path(self):
        return config.get('helicon', 'exe')

    def set_helicon_path(self, path):
        config.set('helicon', 'exe', path)
        config.save()

    def get_photoshop_path(self):
        return config.get('photoshop', 'exe')

    def set_photoshop_path(self, path):
        config.set('photoshop', 'exe', path)
        config.save()

    def get_rawtherapee_path(self):
        return config.get('rawtherapee', 'exe')

    def set_rawtherapee_path(self, path):
        config.set('rawtherapee', 'exe', path)
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
        return config.getfloat('core', 'cache_size_gb')
    
    def get_cache_usage_gb(self):
        """Returns current cache usage in GB."""
        return self.image_cache.currsize / (1024**3)

    def set_cache_size(self, size):
        """Update cache size at runtime and persist to config."""
        size = max(0.5, min(size, 16.0))  # enforce sane bounds
        config.set('core', 'cache_size_gb', size)
        config.save()
        
        old_max_bytes = self.image_cache.max_bytes
        new_max_bytes = int(size * 1024**3)
        if old_max_bytes == new_max_bytes:
            return
        
        log.info("Resizing decoded image cache from %.2f GB to %.2f GB",
                 old_max_bytes / (1024**3), size)
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
        return config.getint('core', 'prefetch_radius')

    def set_prefetch_radius(self, radius):
        config.set('core', 'prefetch_radius', radius)
        config.save()
        self.prefetcher.prefetch_radius = radius
        self.prefetcher.update_prefetch(self.current_index)

    def get_theme(self):
        return 0 if config.get('core', 'theme') == 'dark' else 1

    def set_theme(self, theme_index):
        # update Python-side state
        self.ui_state.theme = theme_index

        # persist it
        theme = 'dark' if theme_index == 0 else 'light'
        config.set('core', 'theme', theme)
        config.save()

        # tell QML it changed (once is enough)
        self.ui_state.themeChanged.emit()

    @Slot(result=str)
    def get_color_mode(self):
        """Returns current color management mode: 'none', 'saturation', or 'icc'."""
        return config.get('color', 'mode', fallback='none')

    @Slot(str)
    def set_color_mode(self, mode: str):
        """Sets color management mode and clears cache to force re-decode."""
        mode = mode.lower()
        if mode not in ['none', 'saturation', 'icc']:
            log.error("Invalid color mode: %s", mode)
            return
        
        log.info("Setting color mode to: %s", mode)
        config.set('color', 'mode', mode)
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
            'none': 'Original Colors',
            'saturation': 'Saturation Compensation',
            'icc': 'Full ICC Profile'
        }
        self.update_status_message(f"Color mode: {mode_names.get(mode, mode)}")

    @Slot(result=float)
    def get_saturation_factor(self):
        """Returns current saturation factor (0.0-1.0)."""
        return config.getfloat('color', 'saturation_factor', fallback=0.85)

    @Slot(float)
    def set_saturation_factor(self, factor: float):
        """Sets saturation factor and refreshes images."""
        factor = max(0.0, min(1.0, factor))  # Clamp to 0-1
        log.info("Setting saturation factor to: %.2f", factor)
        config.set('color', 'saturation_factor', str(factor))
        config.save()
        
        # Only refresh if in saturation mode
        if self.get_color_mode() == 'saturation':
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
        if self.get_color_mode() in ['saturation', 'icc']:
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
                        bottom -= top # shift down
                        top = 0
                    if bottom > 1000:
                        top -= (bottom - 1000) # shift up
                        bottom = 1000
                        if top < 0:
                            top = 0 # double clamp
                    
                    # Recenter width (if changed)
                    cx = (left + right) / 2
                    left = cx - w_norm / 2
                    right = cx + w_norm / 2
                     
                    # Clamp horizontal
                    if left < 0:
                        right -= left
                        left = 0
                    if right > 1000:
                        left -= (right - 1000)
                        right = 1000
                        if left < 0:
                            left = 0

                    self.ui_state.currentCropBox = (left, top, right, bottom)
                    self.image_editor.set_crop_box((left, top, right, bottom))

        log.debug(f"AppController.set_straighten_angle: {angle}")
        # Invert angle because QML rotation is CW but PIL rotation (used in editor) handles direction logic internally 
        # (ImageEditor._apply_edits uses negative angle for PIL).
        # We pass the raw angle from QML (degrees CW for UI rotation) to the editor.
        # Editor takes care of sign.
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
        return config.get('core', 'default_directory')

    def set_default_directory(self, path):
        config.set('core', 'default_directory', path)
        config.save()

    def get_optimize_for(self):
        return config.get('core', 'optimize_for', fallback='speed')
    
    def set_optimize_for(self, optimize_for):
        old_value = config.get('core', 'optimize_for', fallback='speed')
        config.set('core', 'optimize_for', optimize_for)
        config.save()
        
        # If the setting changed, clear cache and redraw current image
        if old_value != optimize_for:
            log.info(f"Optimize for changed from {old_value} to {optimize_for}, clearing cache and redrawing")
            self.image_cache.clear()
            # Force redraw of current image
            if self.current_index >= 0 and self.current_index < len(self.image_files):
                self.ui_state.currentImageSourceChanged.emit()
        
    def open_directory_dialog(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        if dialog.exec():
            return dialog.selectedFiles()[0]
        return ""

    @Slot()
    def open_folder(self):
        """Opens a directory dialog and reloads the application with the selected folder."""
        path = self.open_directory_dialog()
        if path:
            # Stop the old watcher
            if self.watcher:
                self.watcher.stop()
            
            # Update the directory path
            self.image_dir = Path(path)
            
            # Reinitialize directory-bound components
            self.watcher = Watcher(self.image_dir, self.refresh_image_list)
            self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
            self.recycle_bin_dir = self.image_dir / "image recycle bin"
            
            # Clear directory-specific state
            self.delete_history = []
            self.undo_history = []
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
            # Clear last displayed image since it references the old directory
            with self._last_image_lock:
                self.last_displayed_image = None
            # Clear editor state if open
            self.image_editor.clear()
            
            # Load images from new directory
            self.load()


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
        
        log.info(f"Found {already_cached_count} cached images (skipping). Preloading {len(images_to_preload)} images (including nearby refreshes).")

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
        if not self.image_files:
            self.update_status_message("No image to delete.")
            return
        
        # Check if current image is in a batch with multiple images
        batch_count = self.get_batch_count_for_current_image()
        
        if batch_count > 1:
            # Show dialog asking what to delete
            if hasattr(self, 'main_window') and self.main_window:
                # Set batch count in dialog and open it
                self.main_window.show_delete_batch_dialog(batch_count)
            return
        
        # Single image deletion - proceed normally
        self._delete_single_image(self.current_index)

    def _move_to_recycle(self, src: Path) -> Optional[Path]:
        """Moves a file to the recycle bin safely, handling collisions and cross-device moves."""
        if not src.exists() or not src.is_file():
            return None
        
        # Ensure recycle bin exists
        try:
            self.recycle_bin_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("Failed to create recycle bin: %s", e)
            return None

        dest = self.recycle_bin_dir / src.name
        
        # Handle collisions with timestamp loop
        if dest.exists():
            import time
            timestamp = int(time.time())
            base_name = f"{src.stem}.{timestamp}"
            dest = self.recycle_bin_dir / f"{base_name}{src.suffix}"
            counter = 1
            while dest.exists():
                dest = self.recycle_bin_dir / f"{base_name}_{counter}{src.suffix}"
                counter += 1
        
        try:
            shutil.move(str(src), str(dest))
            log.info("Moved %s to recycle bin: %s", src.name, dest.name)
            return dest
        except OSError as e:
            log.error("Failed to recycle %s: %s", src.name, e)
            return None

    def _delete_single_image(self, index: int):
        """Internal method to delete a single image by index."""
        if not self.image_files or index < 0 or index >= len(self.image_files):
            self.update_status_message("No image to delete.")
            return
        
        previous_index = self.current_index
        image_file = self.image_files[index]
        jpg_path = image_file.path
        raw_path = image_file.raw_pair
        
        # Move files to recycle bin
        recycled_jpg = self._move_to_recycle(jpg_path)
        recycled_raw = self._move_to_recycle(raw_path) if (raw_path and raw_path.exists()) else None
        
        # Add to delete history if anything was moved
        if recycled_jpg or recycled_raw:
            import time
            timestamp = time.time()
            # Store tuple of (src, bin_path) for each file
            # Format: ( (jpg_src, jpg_bin), (raw_src, raw_bin) )
            record = ( (jpg_path, recycled_jpg), (raw_path, recycled_raw) )
            
            self.delete_history.append(record)
            self.undo_history.append(("delete", record, timestamp))
            
        if not recycled_jpg and not recycled_raw:
            self.update_status_message("Delete failed")
            return
        
        # Refresh image list and move to next image
        self.refresh_image_list()
        if self.image_files:
            self._reposition_after_delete(None, previous_index)
            # Clear cache and invalidate display generation to force image reload
            self.display_generation += 1
            self.image_cache.clear()
            self.prefetcher.cancel_all()  # Cancel stale tasks since image list changed
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

    def _reposition_after_delete(self, preserved_path: Optional[Path], previous_index: int):
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
        # We prefer to land on the image that was *conceptually* at the same position,
        # which means following the last deleted index if we were deleting from right to left,
        # or just staying at the start index of the batch.
        
        # If we just deleted a batch at the end of the list, we clamp to new length-1
        # If we deleted a batch in the middle, we want to be at the index that *was* 
        # immediately after the batch (which now shifts down by deleted_count).
        
        # Simpler logic:
        # If we had a batch starting at index S with N items.
        # After deleting N items, the item that was at S+N matches the new item at S.
        # So we should generally effectively stay at 'start' (which finds the next image).
        # We need to find the smallest index that was part of the deletion.
        min_deleted_index = min(sorted_indices)
        
        # Create recycle bin if it doesn't exist
        try:
            self.recycle_bin_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.update_status_message(f"Failed to create recycle bin: {e}")
            log.error("Failed to create recycle bin directory: %s", e)
            return
        
        deleted_count = 0
        import time
        timestamp = time.time()
        
        # Delete all images in the batch
        for index in sorted_indices:
            if index >= len(self.image_files):
                continue
            
            image_file = self.image_files[index]
            jpg_path = image_file.path
            raw_path = image_file.raw_pair
            
            try:
                recycled_jpg = self._move_to_recycle(jpg_path)
                recycled_raw = self._move_to_recycle(raw_path) if (raw_path and raw_path.exists()) else None
                
                if recycled_jpg or recycled_raw:
                    record = ( (jpg_path, recycled_jpg), (raw_path, recycled_raw) )
                    self.delete_history.append(record)
                    self.undo_history.append(("delete", record, timestamp))
                    deleted_count += 1
                
            except OSError as e:
                log.exception("Failed to delete image at index %d: %s", index, e)
        
        if deleted_count > 0:
            # Clear all batches after deletion
            self.batches = []
            self.batch_start_index = None
            
            # Refresh image list
            self.refresh_image_list()
            
            if self.image_files:
                # Calculate new index
                # We essentially want to be at 'min_deleted_index'
                # But clamped to boundaries.
                new_index = min_deleted_index
                new_index = max(0, min(new_index, len(self.image_files) - 1))
                
                self.current_index = new_index
                
                # Clear cache and invalidate display generation to force image reload
                self.display_generation += 1
                self.image_cache.clear()
                self.prefetcher.cancel_all()  # Cancel stale tasks since image list changed
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()
            
            self.update_status_message(f"Deleted {deleted_count} image(s)")
            log.info("Deleted %d image(s) from batch", deleted_count)
        else:
            self.update_status_message("No images were deleted.")

    @Slot()
    def undo_delete(self):
        """Unified undo that handles both delete and auto white balance operations."""
        if not self.undo_history:
            self.update_status_message("Nothing to undo.")
            return
        
        # Get the most recent action
        action_type, action_data, timestamp = self.undo_history.pop()
        
        if action_type == "delete":
            # New record format: ( (jpg_src, jpg_bin), (raw_src, raw_bin) )
            (jpg_src, jpg_bin), (raw_src, raw_bin) = action_data
            
            # Remove from delete_history if it matches
            if self.delete_history and self.delete_history[-1] == action_data:
                self.delete_history.pop()
            
            restored_files = []
            try:
                # Helper to move back safely
                def restore_file(src_path: Optional[Path], bin_path: Optional[Path]):
                    if not src_path or not bin_path or not bin_path.exists():
                        return False
                    if src_path.exists():
                        log.warning("Cannot restore %s: User file already exists at %s", bin_path.name, src_path)
                        return False # Or maybe restore with new name? For now, skip to prevent overwrite
                    
                    shutil.move(str(bin_path), str(src_path))
                    return True

                # Restore JPG
                if restore_file(jpg_src, jpg_bin):
                    restored_files.append(jpg_src.name)
                    log.info("Restored %s from recycle bin", jpg_src.name)
                
                # Restore RAW
                if restore_file(raw_src, raw_bin):
                    restored_files.append(raw_src.name)
                    log.info("Restored %s from recycle bin", raw_src.name)
                
                # Update status
                if restored_files:
                    files_str = ", ".join(restored_files)
                    self.update_status_message(f"Restored: {files_str}")
                else:
                    self.update_status_message("No files to restore")
                
                # Refresh image list
                self.refresh_image_list()
                
                # Find and navigate to the restored image
                for i, img_file in enumerate(self.image_files):
                    if img_file.path == jpg_src:
                        self.current_index = i
                        break
                
                # Clear cache and invalidate display generation to force image reload
                self.display_generation += 1
                self.image_cache.clear()
                self.prefetcher.cancel_all()  # Cancel stale tasks since image list changed
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()
                
            except OSError as e:
                self.update_status_message(f"Undo failed: {e}")
                log.exception("Failed to restore image")
                # Put it back in history if it failed
                self.undo_history.append(("delete", action_data, timestamp))
                self.delete_history.append(action_data)
        
        elif action_type == "auto_white_balance":
            saved_path, backup_path = action_data
            filepath_obj = Path(saved_path)

            try:
                backup_path_obj = Path(backup_path)
                if backup_path_obj.exists():
                    # Restore the backup
                    filepath_obj.unlink()  # Remove the edited version
                    backup_path_obj.rename(filepath_obj)  # Restore backup
                    log.info("Restored backup %s for %s", backup_path_obj.name, saved_path)
                    
                    # Refresh the view
                    self.refresh_image_list()
                    
                    # Find the restored image
                    for i, img_file in enumerate(self.image_files):
                        if img_file.path == filepath_obj:
                            self.current_index = i
                            break
                    
                    self.display_generation += 1
                    self.image_cache.clear()
                    self.prefetcher.cancel_all()
                    self.prefetcher.update_prefetch(self.current_index)
                    self.sync_ui_state()
                    
                    if self.ui_state.isHistogramVisible:
                        self.update_histogram()
                    
                    self.update_status_message("Undid auto white balance")
                else:
                    # This case should not be reached if glob finds files
                    self.update_status_message("Backup not found")
                    log.warning("Backup %s disappeared before it could be restored.", backup_path)
                    self.undo_history.append(("auto_white_balance", (saved_path, backup_path), timestamp))
            except OSError as e:
                self.update_status_message(f"Undo failed: {e}")
                log.exception("Failed to undo auto white balance")
                # Put it back in history if it failed
                self.undo_history.append(("auto_white_balance", (saved_path, backup_path), timestamp))

        elif action_type == "auto_levels":
            saved_path, backup_path = action_data
            filepath_obj = Path(saved_path)

            try:
                backup_path_obj = Path(backup_path)
                if backup_path_obj.exists():
                    # Restore the backup
                    filepath_obj.unlink()  # Remove the edited version
                    backup_path_obj.rename(filepath_obj)  # Restore backup
                    log.info("Restored backup %s for %s", backup_path_obj.name, saved_path)
                    
                    # Refresh the view
                    self.refresh_image_list()
                    
                    # Find the restored image
                    for i, img_file in enumerate(self.image_files):
                        if img_file.path == filepath_obj:
                            self.current_index = i
                            break
                    
                    self.display_generation += 1
                    self.image_cache.clear()
                    self.prefetcher.cancel_all()
                    self.prefetcher.update_prefetch(self.current_index)
                    self.sync_ui_state()
                    
                    if self.ui_state.isHistogramVisible:
                        self.update_histogram()
                        
                    self.update_status_message("Undid auto levels")
                else:
                    self.update_status_message("Backup not found")
                    log.warning("Backup %s disappeared before it could be restored.", backup_path)
                    self.undo_history.append(("auto_levels", (saved_path, backup_path), timestamp))
            except OSError as e:
                self.update_status_message(f"Undo failed: {e}")
                log.exception("Failed to undo auto levels")
                # Put it back in history if it failed
                self.undo_history.append(("auto_levels", (saved_path, backup_path), timestamp))
        
        elif action_type == "crop":
            saved_path, backup_path = action_data
            filepath_obj = Path(saved_path)

            try:
                backup_path_obj = Path(backup_path)
                if backup_path_obj.exists():
                    # Restore the backup
                    filepath_obj.unlink()  # Remove the cropped version
                    backup_path_obj.rename(filepath_obj)  # Restore backup
                    log.info("Restored backup %s for %s", backup_path_obj.name, saved_path)
                    
                    # Refresh the view
                    self.refresh_image_list()
                    
                    # Find the restored image
                    for i, img_file in enumerate(self.image_files):
                        if img_file.path == filepath_obj:
                            self.current_index = i
                            break
                    
                    self.display_generation += 1
                    self.image_cache.clear()
                    self.prefetcher.cancel_all()
                    self.prefetcher.update_prefetch(self.current_index)
                    self.sync_ui_state()
                    
                    self.update_status_message("Undid crop")
                else:
                    self.update_status_message("Backup not found")
                    log.warning("Backup %s disappeared before it could be restored.", backup_path)
                    self.undo_history.append(("crop", (saved_path, backup_path), timestamp))
            except OSError as e:
                self.update_status_message(f"Undo failed: {e}")
                log.exception("Failed to undo crop")
                # Put it back in history if it failed
                self.undo_history.append(("crop", (saved_path, backup_path), timestamp))

    def shutdown(self):
        log.info("Application shutting down.")
        
        # Check if recycle bin has files and prompt to empty
        if self.recycle_bin_dir.exists():
            files_in_bin = list(self.recycle_bin_dir.glob("*"))
            if files_in_bin:
                file_count = len(files_in_bin)
                msg_box = QMessageBox()
                msg_box.setWindowTitle("Recycle Bin")
                msg_box.setText(f"There are {file_count} files in the recycle bin.")
                msg_box.setInformativeText("What would you like to do?")
                
                # Add custom buttons
                delete_btn = msg_box.addButton("Delete Permanently", QMessageBox.YesRole)
                restore_btn = msg_box.addButton(f"Restore {file_count} deleted files", QMessageBox.ActionRole)
                keep_btn = msg_box.addButton("Keep in Recycle Bin", QMessageBox.NoRole)
                
                msg_box.setDefaultButton(keep_btn)
                msg_box.exec()
                
                clicked_button = msg_box.clickedButton()
                if clicked_button == delete_btn:
                    self.empty_recycle_bin()
                elif clicked_button == restore_btn:
                    self.restore_all_from_recycle_bin()
        
        # Clear QML context property to prevent TypeErrors during shutdown
        if self.engine:
            log.info("Clearing uiState context property in QML.")
            del self.engine # Explicitly delete the engine

        self.watcher.stop()
        self.prefetcher.shutdown()
        self.sidecar.set_last_index(self.current_index)
        self.sidecar.save()

    def _shutdown_executors(self):
        """Explicitly shuts down thread pools on app exit to prevent hanging."""
        self._shutting_down = True
        log.info("Shutting down background executors...")
        self._hist_executor.shutdown(wait=False, cancel_futures=True)
        self._preview_executor.shutdown(wait=False, cancel_futures=True)

    def empty_recycle_bin(self):
        """Permanently deletes all files in the recycle bin."""
        if not self.recycle_bin_dir.exists():
            return
        
        try:
            import shutil
            shutil.rmtree(self.recycle_bin_dir)
            self.delete_history.clear()
            log.info("Emptied recycle bin and cleared delete history")
        except OSError:
            log.exception("Failed to empty recycle bin")
    
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
        """Restores all files from recycle bin to working directory."""
        if not self.recycle_bin_dir.exists():
            return
        
        try:
            files_in_bin = list(self.recycle_bin_dir.glob("*"))
            restored_count = 0
            
            for file_in_bin in files_in_bin:
                # Restore to original location (working directory)
                dest_path = self.image_dir / file_in_bin.name
                
                # If file already exists, skip (don't overwrite)
                if dest_path.exists():
                    log.warning("File already exists, skipping: %s", dest_path)
                    continue
                
                try:
                    file_in_bin.rename(dest_path)
                    restored_count += 1
                    log.info("Restored %s from recycle bin", file_in_bin.name)
                except OSError as e:
                    log.error("Failed to restore %s: %s", file_in_bin.name, e)
            
            # Clear delete history since we restored everything
            self.delete_history.clear()
            
            log.info("Restored %d files from recycle bin", restored_count)
            
        except OSError:
            log.exception("Failed to restore files from recycle bin")

    @Slot()
    def edit_in_photoshop(self):
        if not self.image_files:
            self.update_status_message("No image to edit.")
            return

        # Prefer RAW file if it exists, otherwise use JPG
        image_file = self.image_files[self.current_index]
        jpg_path = image_file.path
        
        # Handle backup images: strip -backup, -backup2, -backup-1, etc. to find original RAW
        import re
        original_stem = jpg_path.stem
        # Remove -backup with optional digits or -backup-digits (handles both formats)
        original_stem = re.sub(r'-backup(-?\d+)?$', '', original_stem)
        
        # Look for RAW file with the original stem
        raw_path = None
        if image_file.raw_pair and image_file.raw_pair.exists():
            # Use the paired RAW if it exists
            raw_path = image_file.raw_pair
        else:
            # Search for RAW file manually by original stem
            from faststack.io.indexer import RAW_EXTENSIONS
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
            log.info("Using JPG file for Photoshop (no RAW found): %s", current_image_path)
        
        photoshop_exe = config.get('photoshop', 'exe')
        photoshop_args = config.get('photoshop', 'args')

        # Validate executable path securely
        is_valid, error_msg = validate_executable_path(
            photoshop_exe,
            app_type="photoshop",
            allow_custom_paths=True
        )
        
        if not is_valid:
            self.update_status_message(f"Photoshop validation failed: {error_msg}")
            log.error("Photoshop executable validation failed: %s", error_msg)
            return
        
        # Validate that the file path exists and is a file
        if not current_image_path.exists() or not current_image_path.is_file():
            self.update_status_message(f"Image file not found: {current_image_path.name}")
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
                    parsed_args = shlex.split(photoshop_args, posix=(os.name != 'nt'))
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
                close_fds=True  # Close unused file descriptors
            )
            
            # Mark as edited on successful launch
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")
            stem = image_file.path.stem
            meta = self.sidecar.get_metadata(stem)
            meta.edited = True
            meta.edited_date = today
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()
            
            self.update_status_message(f"Opened {current_image_path.name} in Photoshop.")
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
        existing_indices = [idx for idx in file_indices if self.image_files[idx].path.exists()]
        file_paths = [self.image_files[idx].path for idx in existing_indices]
        
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

        log.info("Starting drag for %d file(s): %s", len(file_paths), [str(p) for p in file_paths])
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
            
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()
            log.info("Marked %d file(s) as uploaded on %s. Cleared all batches.", len(existing_indices), today)

    # --- Image Editor Logic ---

    @Slot(result=bool)
    def load_image_for_editing(self):
        """Loads the currently viewed image into the editor."""
        if self.image_files and self.current_index < len(self.image_files):
            filepath = str(self.image_files[self.current_index].path)
            # Only load if the editor is not already open for this file
            if str(self.image_editor.current_filepath) == filepath and self.image_editor.original_image is not None:
                 # Already loaded, just reset UI state for a fresh start
                 self.reset_edit_parameters()
                 return True

            # Get the cached, display-sized image to use for fast previews
            cached_preview = self.get_decoded_image(self.current_index)

            if self.image_editor.load_image(filepath, cached_preview=cached_preview):
                # Pass initial edits to uiState
                initial_edits = self.image_editor._initial_edits()
                for key, value in initial_edits.items():
                    if hasattr(self.ui_state, key):
                        setattr(self.ui_state, key, value)

                # Set aspect ratios for QML dropdown
                self.ui_state.aspectRatioNames = [r['name'] for r in ASPECT_RATIOS]
                self.ui_state.currentAspectRatioIndex = 0
                self.ui_state.currentCropBox = (0, 0, 1000, 1000) # Reset crop box visually
                
                # Kick off initial background preview render
                self._kick_preview_worker()
                
                return True
        return False

    @Slot(result=DecodedImage)
    def get_preview_data(self) -> Optional[DecodedImage]:
        """Gets the preview data of the currently edited image as a DecodedImage."""
        return self.image_editor.get_preview_data()

    def _do_preview_refresh(self):
        self._preview_refresh_pending = False
        self._kick_preview_worker()

    @Slot(str, "QVariant")
    def set_edit_parameter(self, key: str, value: Any):
        """Sets an edit parameter and updates the UIState for the slider visual."""
        try:
            # Update actual edit state (this bumps _edits_rev and invalidates preview cache)
            changed = False
            if self.ui_state.isEditorOpen:
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

            # Trigger a refresh of the image to show the edit (throttled), ONLY if something changed
            if changed:
                if not self._preview_refresh_pending:
                    self._preview_refresh_pending = True
                    self.preview_timer.start()
        except Exception as e:
            log.error("Error setting edit parameter %s=%s: %s", key, value, e)

    @Slot(int, int, int, int)
    def set_crop_box(self, left: int, top: int, right: int, bottom: int):
        """Sets the normalized crop box (0-1000) in the editor."""
        from typing import Tuple
        crop_box: Tuple[int, int, int, int] = (left, top, right, bottom)
        self.image_editor.set_crop_box(crop_box)
        self.ui_state.currentCropBox = crop_box # Update QML visual (if implemented)

    @Slot()
    def reset_edit_parameters(self):
        """Resets all editing parameters in the editor."""
        self.image_editor.reset_edits()
        if hasattr(self.ui_state, 'reset_editor_state'):
            self.ui_state.reset_editor_state()

        # Trigger a refresh to show the reset image
        self.ui_refresh_generation += 1
        self._kick_preview_worker()

    @Slot()
    def save_edited_image(self):
        """Saves the edited image."""
        save_result = self.image_editor.save_image()
        if not save_result:
            self.update_status_message("Failed to save image")
            log.error("Failed to save edited image")
            return

        saved_path, _ = save_result
        self.update_status_message(f"Edits saved to {saved_path.name}")
        # Clear the image editor state so it will reload fresh next time
        self.image_editor.clear()
            
        # Reset all edit parameters in the controller/UI
        self.reset_edit_parameters()

        # Refresh the view - need to refresh image list since backup file was created
        original_path = saved_path
        self.refresh_image_list()
        
        # Find the edited image (not the backup) in the refreshed list
        for i, img_file in enumerate(self.image_files):
            if img_file.path == original_path:
                self.current_index = i
                break
        
        # Invalidate cache and refresh display
        self.display_generation += 1
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()

    
    @Slot()
    def rotate_image_cw(self):
        """Rotate the edited image 90 degrees clockwise."""
        current = self.image_editor.current_edits.get('rotation', 0)
        new_rotation = (current - 90) % 360
        self.set_edit_parameter('rotation', new_rotation)

    @Slot()
    def rotate_image_ccw(self):
        """Rotate the edited image 90 degrees counter-clockwise."""
        current = self.image_editor.current_edits.get('rotation', 0)
        new_rotation = (current + 90) % 360
        self.set_edit_parameter('rotation', new_rotation)
    
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
    def update_histogram(self, zoom: float = 1.0, pan_x: float = 0.0, pan_y: float = 0.0, image_scale: float = 1.0):
        """Throttled request to update histogram. Updates continuously but capped at interval.
        
        Args:
            zoom: Zoom scale factor (1.0 = no zoom)
            pan_x: Pan offset in X direction (in image coordinates)
            pan_y: Pan offset in Y direction (in image coordinates)
            image_scale: Scale factor of displayed image vs original
        """
        # Early guard: don't even schedule if nothing is showing the histogram
        if not (self.ui_state.isHistogramVisible or self.ui_state.isEditorOpen):
            self._hist_pending = None
            return

        self._hist_pending = (zoom, pan_x, pan_y, image_scale)
        if not self.histogram_timer.isActive() and not self._hist_inflight:
            self.histogram_timer.start()

    def _kick_histogram_worker(self):
        if getattr(self, "_shutting_down", False):
            return
        if self._hist_inflight:
            return
        if self._hist_pending is None:
            return

        args = self._hist_pending
        self._hist_pending = None

        with self._hist_lock:
            self._hist_token += 1
            token = self._hist_token
        self._hist_inflight = True

        # Snap the currently known preview data to avoid racing with the editor
        preview_data = self._last_rendered_preview
        if not preview_data:
            # Fallback for initial load if no edit preview yet (could use get_decoded_image?)
            # But histogram is mostly for edits. If preview_data is None, we likely can't compute anyway.
            # We can try to peek at the image editor if _last_rendered_preview is unset.
            preview_data = self.image_editor.get_preview_data_cached(allow_compute=False)

        # Fallback: If still no preview data (e.g. editor not open), we need to fetch the main image.
        # But doing get_decoded_image() here blocks the main thread.
        # Instead, we pass the index to the worker and let it fetch/decode if needed.
        target_index = -1
        if not preview_data and 0 <= self.current_index < len(self.image_files):
            target_index = self.current_index
        
        # If no preview data AND no valid index, we can't compute.
        if not preview_data and target_index == -1:
            self._hist_inflight = False
            # Restore pending args so the next timer tick (or preview completion) retries
            self._hist_pending = args
            # Make sure timer is running to retry
            if not self.histogram_timer.isActive():
                self.histogram_timer.start()
            return

        try:
            # Pass simple data + controller reference + target_index
            fut = self._hist_executor.submit(self._compute_histogram_worker, token, args, preview_data, self, target_index)
            fut.add_done_callback(self._on_histogram_done)
        except RuntimeError:
            log.warning("Histogram executor failed (shutting down?)")
            self._hist_inflight = False

    @staticmethod
    def _compute_histogram_worker(token, args, decoded, controller=None, target_index=-1):
        # IMPORTANT: do not touch QObjects here except thread-safe plain data
        zoom, pan_x, pan_y, image_scale = args

        # If data wasn't provided, try to fetch it safely using the controller
        if not decoded and controller and target_index >= 0:
            decoded = controller._get_decoded_image_safe(target_index)

        # Use explicitly passed or fetched decoded data
        if not decoded:
            return token, None

        import numpy as np
        try:
            arr = np.frombuffer(decoded.buffer, dtype=np.uint8).reshape((decoded.height, decoded.width, 3))
            
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
                 visible_x_end = min(decoded.width, int(visible_center_x + visible_width / 2))
                 visible_y_end = min(decoded.height, int(visible_center_y + visible_height / 2))
                 
                 if visible_x_end > visible_x_start and visible_y_end > visible_y_start:
                     arr = arr[visible_y_start:visible_y_end, visible_x_start:visible_x_end, :]

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
                'r': log_r_hist,
                'g': log_g_hist,
                'b': log_b_hist,
                'r_clip': r_clip_count,
                'g_clip': g_clip_count,
                'b_clip': b_clip_count,
                'r_preclip': r_preclip_count,
                'g_preclip': g_preclip_count,
                'b_preclip': b_preclip_count,
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
        self._hist_inflight = False

        if hist is not None:
            with self._hist_lock:
                if token == self._hist_token:
                    self.ui_state.histogramData = hist

        # If more updates arrived while we computed, run again soon
        if self._hist_pending is not None:
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
            fut = self._preview_executor.submit(self._render_preview_worker, token, self.image_editor)
            fut.add_done_callback(self._on_preview_done)
        except RuntimeError:
            log.warning("Preview executor failed (shutting down?)")
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
        
        with self._preview_lock:
            self._preview_inflight = False
            
            if decoded is not None:
                # Only accept if it matches the latest requested token
                if token == self._preview_token:
                    self._last_rendered_preview = decoded
                    # Ensure QML/provider URL changes so we don't get a cached frame
                    self.ui_refresh_generation += 1
                    self.ui_state.currentImageSourceChanged.emit()
                    
                    # Trigger histogram update (always call, let update_histogram handle visibility guards)
                    self.update_histogram()
            
            # If new requests arrived while we were rendering, start the next one immediately
            if self._preview_pending:
                QTimer.singleShot(0, self._kick_preview_worker)
    


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
            self.ui_state.aspectRatioNames = [r['name'] for r in ASPECT_RATIOS]
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
                     self.image_editor.load_image(str(filepath), cached_preview=cached_preview)
            
            # Reset rotation to 0 when starting fresh crop mode
            self.image_editor.set_edit_param("straighten_angle", 0.0)

            self.update_status_message("Crop mode: Drag to select area, Enter to crop")
            log.info("Crop mode enabled")
        else: # Exiting crop mode
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
        match = re.search(r'([A-Z]+)(\d+)\s+stacked\.JPG', filename, re.IGNORECASE)
        if not match:
            self.update_status_message("Could not parse stacked JPG filename format.")
            log.error("Could not parse stacked JPG filename: %s", filename)
            return

        base_prefix = match.group(1) # e.g., "PB"
        base_number_str = match.group(2) # e.g., "210633"
        base_number = int(base_number_str)

        # Determine the RAW source directory
        raw_source_dir_str = config.get('raw', 'source_dir')
        if not raw_source_dir_str:
            self.update_status_message("RAW source directory not configured in settings.")
            log.warning("RAW source directory (raw.source_dir) is not set in config.")
            return
        
        raw_base_dir = Path(raw_source_dir_str)
        if not raw_base_dir.is_dir():
            self.update_status_message(f"RAW source directory not found: {raw_base_dir}")
            log.warning("Configured RAW source directory does not exist: %s", raw_base_dir)
            return

        # Get the mirror base from config
        mirror_base_str = config.get('raw', 'mirror_base')
        if not mirror_base_str:
            self.update_status_message("RAW mirror base directory not configured in settings.")
            log.warning("RAW mirror base (raw.mirror_base) is not set in config.")
            return
        
        mirror_base_dir = Path(mirror_base_str)
        if not mirror_base_dir.is_dir():
            self.update_status_message(f"RAW mirror base directory not found: {mirror_base_dir}")
            log.warning("Configured RAW mirror base directory does not exist: %s", mirror_base_dir)
            return

        # The date structure in the RAW directory mirrors the structure relative to the mirror_base
        try:
            relative_part = current_image_path.parent.relative_to(mirror_base_dir)
        except ValueError:
            self.update_status_message("Current image is not in the configured mirror base directory.")
            log.error(
                "Could not find relative path for '%s' from base '%s'. Check 'mirror_base' config.",
                current_image_path.parent,
                mirror_base_dir
            )
            return

        raw_search_dir = raw_base_dir / relative_part
        
        if not raw_search_dir.is_dir():
            self.update_status_message(f"RAW directory for this date not found: {raw_search_dir}")
            log.warning("RAW search directory does not exist: %s", raw_search_dir)
            return

        # Find RAW files by decrementing the number
        found_raw_files: List[Path] = []
        # Start one number less than the stacked image number
        current_raw_number = base_number - 1 
        
        # Limit to reasonable number of RAWs to avoid infinite loop or too many files
        max_raw_search = 15 # As per user request, typically between 3 and 15
        search_count = 0

        while current_raw_number >= 0 and search_count < max_raw_search:
            raw_filename_stem = f"{base_prefix}{current_raw_number:06d}" # e.g., PB210632
            
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
                if found_raw_files: # Only break if we've found at least one file before this gap
                    break
            
            current_raw_number -= 1
            search_count += 1
        
        if not found_raw_files:
            self.update_status_message(f"No source RAW files found in {raw_search_dir} for {filename}.")
            log.info("No source RAWs found for %s in %s", filename, raw_search_dir)
            return

        # Sort the files by name to ensure Helicon Focus receives them in sequence
        found_raw_files.sort()

        self.update_status_message(f"Launching Helicon Focus with {len(found_raw_files)} RAWs...")
        log.info("Launching Helicon Focus for %s with RAWs: %s", filename, [str(p) for p in found_raw_files])
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
        current_rotation = float(self.image_editor.current_edits.get("straighten_angle", 0.0))

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
                raise ValueError(f"Expected 4-item tuple, got {type(crop_box_raw)}: {crop_box_raw}")
            
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
            log.debug(f"execute_crop reloading image due to path mismatch. Editor: {editor_path}, File: {filepath}")
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(str(filepath), cached_preview=cached_preview):
                self.update_status_message("Failed to load image for cropping")
                return

        self.image_editor.set_crop_box(crop_box_raw)
        
        # Re-apply the captured rotation.
        # This handles cases where we reloaded the image (resetting edits) or where UI state sync was flaky.
        self.image_editor.set_edit_param('straighten_angle', current_rotation)
        
        # Save via ImageEditor (handles rotation + crop correctly)
        save_result = self.image_editor.save_image()
        
        if save_result:
            saved_path, backup_path = save_result
            
            # Track for undo
            import time
            timestamp = time.time()
            self.undo_history.append(("crop", (str(saved_path), str(backup_path)), timestamp))
            
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
        if not self.image_editor.current_filepath or str(self.image_editor.current_filepath) != filepath:
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(filepath, cached_preview=cached_preview):
                self.update_status_message("Failed to load image")
                return False

        # Calculate auto levels
        # Calculate auto levels - now returns (blacks, whites, p_low, p_high)
        blacks, whites, p_low, p_high = self.image_editor.auto_levels(self.auto_level_threshold)
        
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
                log.debug(f"Auto levels: degenerate dynamic range ({dynamic_range:.2f}), strength=0")
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
                
                log.debug(f"Auto levels: p_low={p_low:.1f}, p_high={p_high:.1f}, "
                         f"range={dynamic_range:.1f}, stretch_full={stretch_full:.2f}, strength={strength:.3f}")
        else:
            strength = self.auto_level_strength

        # Apply strength scaling to blacks and whites parameters
        blacks *= strength
        whites *= strength
        
        # Apply scaled values
        self.image_editor.set_edit_param('blacks', blacks)
        self.image_editor.set_edit_param('whites', whites)
        
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

        self.update_status_message(f"{msg} (preview only)")
        log.info("Auto levels preview applied to %s (clip %.2f%%, str %.2f). Msg: %s", 
                 filepath, self.auto_level_threshold, strength, msg)
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
        save_result = self.image_editor.save_image()
        if save_result:
            saved_path, backup_path = save_result
            timestamp = time.time()
            self.undo_history.append(("auto_levels", (saved_path, backup_path), timestamp))
            
            # Force reload to ensure disk consistency
            self.image_editor.clear()
            
            # Refresh list/cache/UI (standard save pattern)
            image_file = self.image_files[self.current_index]
            original_path = image_file.path
            self.refresh_image_list()
            
            # Find image again
            for i, img_file in enumerate(self.image_files):
                if img_file.path == original_path:
                    self.current_index = i
                    break
            
            self.display_generation += 1
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()
            
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
            
            self.update_status_message("Auto levels applied and saved")
            log.info("Quick auto levels saved for %s", original_path)
        else:
            self.update_status_message("Failed to save image")

    @Slot(result=float)
    def get_auto_level_clipping_threshold(self):
        return self.auto_level_threshold

    @Slot(float)
    def set_auto_level_clipping_threshold(self, value):
        if self.auto_level_threshold != value:
            self.auto_level_threshold = value
            config.set('core', 'auto_level_threshold', value)
            config.save()

    @Slot(result=float)
    def get_auto_level_strength(self):
        return self.auto_level_strength

    @Slot(float)
    def set_auto_level_strength(self, value: float):
        value = max(0.0, min(1.0, value))
        if self.auto_level_strength != value:
            self.auto_level_strength = value
            config.set('core', 'auto_level_strength', str(value))
            config.save()

    @Slot(result=bool)
    def get_auto_level_strength_auto(self):
        return self.auto_level_strength_auto

    @Slot(bool)
    def set_auto_level_strength_auto(self, value: bool):
        if self.auto_level_strength_auto != value:
            self.auto_level_strength_auto = value
            config.set('core', 'auto_level_strength_auto', str(value))
            config.save()

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
        save_result = self.image_editor.save_image()
        if save_result:
            saved_path, backup_path = save_result
            # Track this action for undo
            timestamp = time.time()
            self.undo_history.append(("auto_white_balance", (saved_path, backup_path), timestamp))
            
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
        mode = config.get('awb', 'mode', fallback='lab')
        if mode == 'lab':
            self.auto_white_balance_lab()
        elif mode == 'rgb':
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
        
        self.image_editor.set_edit_param('white_balance_by', by_value)
        self.image_editor.set_edit_param('white_balance_mg', mg_value)
        
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
            log.error("OpenCV or NumPy not found. Please install with: pip install opencv-python numpy")
            self.update_status_message("Error: OpenCV or NumPy not installed")
            return

        img = self.image_editor.original_image
        # Ensure image is RGB before processing
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        arr = np.array(img, dtype=np.uint8)

        # --- Tunable Constants for Auto White Balance (from config) ---
        _LOWER_BOUND_RGB = config.getint('awb', 'rgb_lower_bound', 5)
        _UPPER_BOUND_RGB = config.getint('awb', 'rgb_upper_bound', 250)
        _LUMA_LOWER_BOUND = config.getint('awb', 'luma_lower_bound', 30)
        _LUMA_UPPER_BOUND = config.getint('awb', 'luma_upper_bound', 220)
        warm_bias = config.getint('awb', 'warm_bias', 6)
        tint_bias = config.getint('awb', 'tint_bias', 0)
        _TARGET_A_LAB = 128.0 + tint_bias
        _TARGET_B_LAB = 128.0 + warm_bias
        _SCALING_FACTOR_LAB_TO_SLIDER = 128.0 
        _CORRECTION_STRENGTH = config.getfloat('awb', 'strength', 0.7)

        # --- 1. Reject clipped channels and use a luma midtone mask ---
        mask = (
            (arr[:, :, 0] > _LOWER_BOUND_RGB) & (arr[:, :, 0] < _UPPER_BOUND_RGB) &
            (arr[:, :, 1] > _LOWER_BOUND_RGB) & (arr[:, :, 1] < _UPPER_BOUND_RGB) &
            (arr[:, :, 2] > _LOWER_BOUND_RGB) & (arr[:, :, 2] < _UPPER_BOUND_RGB)
        )
        
        luma = (0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1] + 0.0722 * arr[:, :, 2])
        mask &= (luma > _LUMA_LOWER_BOUND) & (luma < _LUMA_UPPER_BOUND)
        
        if not np.any(mask):
            log.warning("Auto white balance: No pixels found after clipping and luma filter. Aborting.")
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
            a_mean, b_mean, _TARGET_A_LAB, _TARGET_B_LAB, a_shift, b_shift
        )

        # --- 3. Convert Lab shift to our slider values with strength factor ---
        by_value = (b_shift / _SCALING_FACTOR_LAB_TO_SLIDER) * _CORRECTION_STRENGTH
        mg_value = (a_shift / _SCALING_FACTOR_LAB_TO_SLIDER) * _CORRECTION_STRENGTH
        
        by_value = float(np.clip(by_value, -1.0, 1.0))
        mg_value = float(np.clip(mg_value, -1.0, 1.0))
        
        log.info(f"Auto white balance values: B/Y={by_value:.3f}, M/G={mg_value:.3f}")
        
        self.image_editor.set_edit_param('white_balance_by', by_value)
        self.image_editor.set_edit_param('white_balance_mg', mg_value)
        
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
                info = f"Stack {i+1} ({pos_in_stack}/{count_in_stack})"
                break
        if not info and self.stack_start_index is not None and self.stack_start_index == index:
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
            summary.append(f"Stack {i+1}: {start}-{end}")
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

    app = QApplication(sys.argv) # QApplication is correct for desktop apps with widgets
    if debug:
        log.info("Startup: after QApplication: %.3fs", time.perf_counter() - t0)

    if not image_dir:
        image_dir_str = config.get('core', 'default_directory')
        if not image_dir_str:
            log.warning("No image directory provided and no default directory set. Opening directory selection dialog.")
            selected_dir = QFileDialog.getExistingDirectory(None, "Select Image Directory")
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
    engine.addImportPath(os.path.join(os.path.dirname(PySide6.__file__), "qml", "Qt5Compat"))

    controller = AppController(image_dir_path, engine, debug_cache=debug_cache)
    if debug:
        log.info("Startup: after AppController: %.3fs", time.perf_counter() - t0)
    image_provider = ImageProvider(controller)
    engine.addImageProvider("provider", image_provider)

    # Expose controller and UI state to QML
    context = engine.rootContext()
    context.setContextProperty("uiState", controller.ui_state)
    context.setContextProperty("controller", controller)

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
    parser = argparse.ArgumentParser(description="FastStack - Ultra-fast JPG Viewer for Focus Stacking Selection")
    parser.add_argument("image_dir", nargs="?", default="", help="Directory of images to view")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging and timing information")
    parser.add_argument("--debugcache", action="store_true", help="Enable debug cache features")
    args = parser.parse_args()
    main(image_dir=args.image_dir, debug=args.debug, debug_cache=args.debugcache)

if __name__ == "__main__":
    cli()
