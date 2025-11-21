"""Main application entry point for FastStack."""

import logging
import sys
import struct
import shlex
import time
import argparse
from pathlib import Path
from typing import Optional, List, Dict
from datetime import date
import os
import concurrent.futures
import threading
import subprocess
from faststack.ui.provider import ImageProvider, UIState
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
    QPoint
)
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from PySide6.QtQml import QQmlApplicationEngine

# ⬇️ these are the ones that went missing
from faststack.config import config
from faststack.logging_setup import setup_logging
from faststack.models import ImageFile, DecodedImage, EntryMetadata
from faststack.io.indexer import find_images
from faststack.io.sidecar import SidecarManager
from faststack.io.watcher import Watcher
from faststack.io.helicon import launch_helicon_focus
from faststack.io.executable_validator import validate_executable_path
from faststack.imaging.cache import ByteLRUCache, get_decoded_image_size
from faststack.imaging.prefetch import Prefetcher, clear_icc_caches
from faststack.ui.provider import ImageProvider
from faststack.ui.keystrokes import Keybinder

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

class AppController(QObject):
    dataChanged = Signal() # New signal for general data changes

    class ProgressReporter(QObject):
        progress_updated = Signal(int)
        finished = Signal()

    def __init__(self, image_dir: Path, engine: QQmlApplicationEngine):
        super().__init__()
        self.image_dir = image_dir
        self.image_files: List[ImageFile] = []  # Filtered list for display
        self._all_images: List[ImageFile] = []  # Cached full list from disk
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None
        self.engine = engine

        self.display_width = 0
        self.display_height = 0
        self.display_generation = 0
        self.is_zoomed = False
        self.display_ready = False  # Track if display size has been reported
        self.pending_prefetch_index: Optional[int] = None  # Deferred prefetch index

        # -- Backend Components --
        self.watcher = Watcher(self.image_dir, self.refresh_image_list)
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
        
        # -- Caching & Prefetching --
        cache_size_gb = config.getfloat('core', 'cache_size_gb', 1.5)
        cache_size_bytes = int(cache_size_gb * 1024**3)
        self.image_cache = ByteLRUCache(max_bytes=cache_size_bytes, size_of=get_decoded_image_size)
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
        self.keybinder = Keybinder(self)

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stacks: List[List[int]] = []
        self.selected_raws: set[Path] = set()
        
        # -- Batch Selection State (for drag-and-drop) --
        self.batch_start_index: Optional[int] = None
        self.batches: List[List[int]] = []  # List of [start, end] ranges
        
        self._filter_string: str = "" # Default filter
        self._filter_enabled: bool = False

        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        self._logged_empty_metadata = False
        
        # -- Delete/Undo State --
        self.recycle_bin_dir = self.image_dir / "image recycle bin"
        self.delete_history: List[tuple[Path, Optional[Path]]] = []  # [(jpg_path, raw_path), ...]

        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._handle_resize)
        self.pending_width = None
        self.pending_height = None
        
        # Track if any dialog is open to disable keybindings
        self._dialog_open = False


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
        if self.display_width == width and self.display_height == height:
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

    def set_zoomed(self, zoomed: bool):
        if self.is_zoomed == zoomed:
            return
        self.is_zoomed = zoomed
        log.info("Zoom state changed to: %s", zoomed)
        self.display_generation += 1  # Invalidates old entries via cache key
        
        # NOTE: We don't clear the cache here. The generation increment is enough.
        # Cache keys include display_generation, so zoomed/unzoomed images become
        # naturally unreachable and LRU will evict them. This lets us instantly
        # reuse cached images if user toggles zoom on/off repeatedly.
        self.prefetcher.cancel_all()  # Cancel stale tasks to avoid wasted work
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()
        self.ui_state.isZoomedChanged.emit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Don't handle key events when a dialog is open
        if self._dialog_open:
            return False
            
        if watched == self.main_window and event.type() == QEvent.Type.KeyPress:
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

        _, _, display_gen = self.get_display_info()
        cache_key = f"{index}_{display_gen}"

        # Check cache first
        if cache_key in self.image_cache:
            decoded = self.image_cache[cache_key]
            with self._last_image_lock:
                self.last_displayed_image = decoded
            return decoded
        
        # Cache miss: need to decode synchronously to ensure correct image displays
        if _debug_mode:
            decode_start = time.perf_counter()
            log.info("Cache miss for index %d (gen: %d). Blocking decode.", index, display_gen)
        
        # Submit with priority=True to cancel pending prefetch tasks and free up workers
        future = self.prefetcher.submit_task(index, self.prefetcher.generation, priority=True)
        if future:
            try:
                # Wait for decode to complete (blocking but fast for JPEGs)
                result = future.result(timeout=5.0)  # 5 second timeout as safety
                if result:
                    decoded_index, decoded_display_gen = result
                    cache_key = f"{decoded_index}_{decoded_display_gen}"
                    if cache_key in self.image_cache:
                        decoded = self.image_cache[cache_key]
                        with self._last_image_lock:
                            self.last_displayed_image = decoded
                        if _debug_mode:
                            elapsed = time.perf_counter() - decode_start
                            log.info("Decoded image %d in %.3fs", index, elapsed)
                        return decoded
            except concurrent.futures.TimeoutError:
                log.exception("Timeout decoding image at index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
            except concurrent.futures.CancelledError:
                log.warning("Decode cancelled for index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
            except Exception as e:
                log.exception("Error decoding image at index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
        
        with self._last_image_lock:
            return self.last_displayed_image

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
            self._do_prefetch(self.current_index, is_navigation=True, direction=1)
            self.sync_ui_state()

    def prev_image(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._do_prefetch(self.current_index, is_navigation=True, direction=-1)
            self.sync_ui_state()

    @Slot(int)
    def jump_to_image(self, index: int):
        """Jump to a specific image by index (0-based)."""
        if 0 <= index < len(self.image_files):
            direction = 1 if index > self.current_index else -1
            self.current_index = index
            self._do_prefetch(self.current_index, is_navigation=True, direction=direction)
            self.sync_ui_state()
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
    
    @Slot()
    def dialog_opened(self):
        """Called when any dialog opens to disable global keybindings."""
        self._dialog_open = True
        log.debug("Dialog opened, disabling global keybindings")
    
    @Slot()
    def dialog_closed(self):
        """Called when any dialog closes to re-enable global keybindings."""
        self._dialog_open = False
        log.debug("Dialog closed, re-enabling global keybindings")

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
            log.warning("No batch start marked. Press '{{' first.")
            self.update_status_message("No batch start marked")
    
    def clear_all_batches(self):
        """Clear all defined batches."""
        log.info("Clearing all defined batches.")
        self.batches = []
        self.batch_start_index = None
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("All batches cleared")
    
    def remove_from_batch_or_stack(self):
        """Remove current image from any batch or stack it's in."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return
        
        removed = False
        
        # Check and remove from batches
        for i in range(len(self.batches)):
            start, end = self.batches[i]
            if start <= self.current_index <= end:
                # Build new ranges excluding current_index
                new_ranges = []
                if start == end:
                    # Single image batch - remove entirely (don't add anything)
                    pass
                elif self.current_index == start:
                    # Remove from beginning - shift start forward
                    new_ranges.append([start + 1, end])
                elif self.current_index == end:
                    # Remove from end - shift end backward
                    new_ranges.append([start, end - 1])
                else:
                    # Remove from middle - split into two ranges
                    new_ranges.append([start, self.current_index - 1])
                    new_ranges.append([self.current_index + 1, end])
                
                # Replace the old range with new range(s)
                self.batches[i:i+1] = new_ranges
                
                log.info("Removed index %d from batch [%d, %d]", self.current_index, start, end)
                self.update_status_message(f"Removed from batch")
                removed = True
                break
        
        # Check and remove from stacks
        if not removed:
            for i in range(len(self.stacks)):
                start, end = self.stacks[i]
                if start <= self.current_index <= end:
                    # Build new ranges excluding current_index
                    new_ranges = []
                    if start == end:
                        # Single image stack - remove entirely (don't add anything)
                        pass
                    elif self.current_index == start:
                        # Remove from beginning - shift start forward
                        new_ranges.append([start + 1, end])
                    elif self.current_index == end:
                        # Remove from end - shift end backward
                        new_ranges.append([start, end - 1])
                    else:
                        # Remove from middle - split into two ranges
                        new_ranges.append([start, self.current_index - 1])
                        new_ranges.append([self.current_index + 1, end])
                    
                    # Replace the old range with new range(s)
                    self.stacks[i:i+1] = new_ranges
                    
                    self.sidecar.data.stacks = self.stacks
                    self.sidecar.save()
                    log.info("Removed index %d from stack [%d, %d]", self.current_index, start, end)
                    self.update_status_message(f"Removed from stack")
                    removed = True
                    break
        
        if removed:
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.ui_state.stackSummaryChanged.emit()
            self.sync_ui_state()
        else:
            self.update_status_message("Not in any batch or stack")

    def toggle_selection(self):
        """Toggles the selection status of the current image's file (RAW if available, otherwise JPG)."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        image_file = self.image_files[self.current_index]
        # Use RAW if available, otherwise use JPG
        file_to_select = image_file.raw_pair if image_file.raw_pair else image_file.path
        
        if file_to_select in self.selected_raws:
            self.selected_raws.remove(file_to_select)
            log.info("Removed %s from selection.", file_to_select.name)
        else:
            self.selected_raws.add(file_to_select)
            log.info("Added %s to selection.", file_to_select.name)
        
        # In a real app, we'd update a selection indicator in the UI.
        # For now, we just log and can use it for batch operations.
        self.sync_ui_state() # This will trigger a UI refresh


    def launch_helicon(self):
        """Launches Helicon Focus with selected files (RAW preferred, JPG fallback) or stacks."""
        if self.selected_raws:
            log.info("Launching Helicon with %d selected files.", len(self.selected_raws))
            success = self._launch_helicon_with_files(sorted(list(self.selected_raws)))
            if success:
                self.selected_raws.clear()

        elif self.stacks:
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
        log.info("Clearing all defined stacks and stack start marker.")
        self.stacks = []
        self.stack_start_index = None  # Clear the stack start marker too
        self.sidecar.data.stacks = self.stacks
        self.sidecar.save()
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit() # Notify QML of data change
        self.ui_state.stackSummaryChanged.emit() # Update stack summary in dialog
        self.sync_ui_state()

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
        config.set('core', 'cache_size_gb', size)
        config.save()

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

    def get_default_directory(self):
        return config.get('core', 'default_directory')

    def set_default_directory(self, path):
        config.set('core', 'default_directory', path)
        config.save()

    def open_directory_dialog(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        if dialog.exec():
            return dialog.selectedFiles()[0]
        return ""

    def preload_all_images(self):
        if self.ui_state.isPreloading:
            log.info("Preloading is already in progress.")
            return

        log.info("Starting to preload all images.")
        self.ui_state.isPreloading = True
        self.ui_state.preloadProgress = 0

        self.reporter = self.ProgressReporter()
        self.reporter.progress_updated.connect(self._update_preload_progress)
        self.reporter.finished.connect(self._finish_preloading)

        # Use existing prefetch executor (better resource utilization)
        total = len(self.image_files)
        
        if total == 0:
            log.info("No images to preload.")
            self.reporter.progress_updated.emit(100) # Or 0, depending on desired UX
            self.reporter.finished.emit()
            return

        completed = 0
        
        def _on_done(_future):
            nonlocal completed
            completed += 1
            progress = int((completed / total) * 100)
            self.reporter.progress_updated.emit(progress)
            if completed == total:
                self.reporter.finished.emit()
        
        for i in range(total):
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

    @Slot()
    def delete_current_image(self):
        """Moves current JPG and RAW to recycle bin."""
        if not self.image_files:
            self.update_status_message("No image to delete.")
            return
        
        image_file = self.image_files[self.current_index]
        jpg_path = image_file.path
        raw_path = image_file.raw_pair
        
        # Create recycle bin if it doesn't exist
        try:
            self.recycle_bin_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.update_status_message(f"Failed to create recycle bin: {e}")
            log.error("Failed to create recycle bin directory: %s", e)
            return
        
        # Move files to recycle bin
        deleted_files = []
        try:
            if jpg_path.exists():
                dest = self.recycle_bin_dir / jpg_path.name
                jpg_path.rename(dest)
                deleted_files.append(jpg_path.name)
                log.info("Moved %s to recycle bin", jpg_path.name)
            
            if raw_path and raw_path.exists():
                dest = self.recycle_bin_dir / raw_path.name
                raw_path.rename(dest)
                deleted_files.append(raw_path.name)
                log.info("Moved %s to recycle bin", raw_path.name)
            
            # Add to delete history only if at least one file was moved
            if deleted_files:
                self.delete_history.append((jpg_path, raw_path))
            
            # Update status
            if deleted_files:
                files_str = ", ".join(deleted_files)
                self.update_status_message(f"Deleted: {files_str}")
            else:
                self.update_status_message("No files to delete")
            
            # Refresh image list and move to next image
            self.refresh_image_list()
            if self.image_files:
                # Stay at same index (which now shows the next image)
                self.current_index = min(self.current_index, len(self.image_files) - 1)
                # Clear cache and invalidate display generation to force image reload
                self.display_generation += 1
                self.image_cache.clear()
                self.prefetcher.cancel_all()  # Cancel stale tasks since image list changed
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()
            
        except OSError as e:
            self.update_status_message(f"Delete failed: {e}")
            log.exception("Failed to delete image")

    @Slot()
    def undo_delete(self):
        """Restores the last deleted image from recycle bin."""
        if not self.delete_history:
            self.update_status_message("Nothing to undo.")
            return
        
        jpg_path, raw_path = self.delete_history.pop()
        
        restored_files = []
        try:
            # Restore JPG
            jpg_in_bin = self.recycle_bin_dir / jpg_path.name
            if jpg_in_bin.exists():
                jpg_in_bin.rename(jpg_path)
                restored_files.append(jpg_path.name)
                log.info("Restored %s from recycle bin", jpg_path.name)
            
            # Restore RAW
            if raw_path:
                raw_in_bin = self.recycle_bin_dir / raw_path.name
                if raw_in_bin.exists():
                    raw_in_bin.rename(raw_path)
                    restored_files.append(raw_path.name)
                    log.info("Restored %s from recycle bin", raw_path.name)
            
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
                if img_file.path == jpg_path:
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
            self.delete_history.append((jpg_path, raw_path))

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
        raw_path = image_file.raw_pair
        
        if raw_path and raw_path.exists():
            current_image_path = raw_path
            log.info("Using RAW file for Photoshop: %s", raw_path)
        else:
            current_image_path = image_file.path
            log.info("Using JPG file for Photoshop: %s", current_image_path)
        
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
        
        # Convert to sorted list and get paths
        file_indices = sorted(files_to_drag)
        file_paths = [self.image_files[idx].path for idx in file_indices if self.image_files[idx].path.exists()]
        
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
            
            for idx in file_indices:
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
            log.info("Marked %d file(s) as uploaded on %s. Cleared all batches.", len(file_indices), today)

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
        for i, (start, end) in enumerate(self.batches):
            if start <= index <= end:
                count_in_batch = end - start + 1
                pos_in_batch = index - start + 1
                info = f"Batch {i+1} ({pos_in_batch}/{count_in_batch})"
                break
        if not info and self.batch_start_index is not None and self.batch_start_index == index:
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

def main(image_dir: str = "", debug: bool = False):
    """FastStack Application Entry Point"""
    global _debug_mode
    _debug_mode = debug
    
    t0 = time.perf_counter()
    setup_logging(debug)
    if debug:
        log.info("Startup: after setup_logging: %.3fs", time.perf_counter() - t0)
    log.info("Starting FastStack")

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"

    app = QApplication(sys.argv) # Moved here
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
    controller = AppController(image_dir_path, engine)
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
    args = parser.parse_args()
    main(image_dir=args.image_dir, debug=args.debug)

if __name__ == "__main__":
    cli()
