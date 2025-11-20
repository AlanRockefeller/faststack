"""Main application entry point for FastStack."""

import logging
import sys
import struct
import shlex
import time
from pathlib import Path
from typing import Optional, List, Dict
from datetime import date
import os
import typer
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
from faststack.imaging.prefetch import Prefetcher
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
        self.image_files: List[ImageFile] = []
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None
        self.engine = engine

        self.display_width = 0
        self.display_height = 0
        self.display_generation = 0
        self.is_zoomed = False

        # -- Backend Components --
        self.watcher = Watcher(self.image_dir, self.refresh_image_list)
        self.sidecar = SidecarManager(self.image_dir, self.watcher)
        
        # -- Caching & Prefetching --
        cache_size_gb = config.getfloat('core', 'cache_size_gb', 1.5)
        cache_size_bytes = int(cache_size_gb * 1024**3)
        self.image_cache = ByteLRUCache(max_bytes=cache_size_bytes, size_of=get_decoded_image_size)
        self.prefetcher = Prefetcher(
            image_files=self.image_files,
            cache_put=self.image_cache.__setitem__,
            prefetch_radius=config.getint('core', 'prefetch_radius', 4),
            get_display_info=self.get_display_info
        )
        self.last_displayed_image: Optional[DecodedImage] = None  # Cache last image to avoid grey squares

        # -- UI State --
        self.ui_state = UIState(self)
        self.ui_state.theme = self.get_theme()
        self.keybinder = Keybinder(self)

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stacks: List[List[int]] = []
        self.selected_raws: set[Path] = set()
        self._filter_string: str = "" # Default filter
        self._filter_enabled: bool = False

        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        
        # -- Delete/Undo State --
        self.recycle_bin_dir = self.image_dir / "image recycle bin"
        self.delete_history: List[tuple[Path, Optional[Path]]] = []  # [(jpg_path, raw_path), ...]

        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._handle_resize)
        self.pending_width = None
        self.pending_height = None


    @Slot(str)
    def apply_filter(self, filter_string: str):
        filter_string = filter_string.strip()

        if not filter_string:
            self.clear_filter()
            return

        self._filter_string = filter_string
        self._filter_enabled = True
        self.refresh_image_list()
        self.dataChanged.emit()
        self.ui_state.filterStringChanged.emit()  # Notify UI of filter change

        # reset to start of filtered list
        self.current_index = 0
        self.sync_ui_state()
        self.prefetcher.update_prefetch(self.current_index)

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
        self.refresh_image_list()
        self.dataChanged.emit()
        self.ui_state.filterStringChanged.emit()  # Notify UI of filter change
        self.current_index = min(self.current_index, max(0, len(self.image_files) - 1))
        self.sync_ui_state()
        self.prefetcher.update_prefetch(self.current_index)



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
        log.info(f"Display size changed to: {self.pending_width}x{self.pending_height} (physical pixels)")
        self.display_width = self.pending_width
        self.display_height = self.pending_height
        self.display_generation += 1
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state() # To refresh the image

    def set_zoomed(self, zoomed: bool):
        if self.is_zoomed == zoomed:
            return
        self.is_zoomed = zoomed
        log.info(f"Zoom state changed to: {zoomed}")
        self.display_generation += 1 # Invalidate cache
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()
        self.ui_state.isZoomedChanged.emit()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched == self.main_window and event.type() == QEvent.Type.KeyPress:
            handled = self.keybinder.handle_key_press(event)
            if handled:
                return True
        return super().eventFilter(watched, event)

    def load(self):
        """Loads images, sidecar data, and starts services."""
        self.refresh_image_list()
        if not self.image_files:
            self.current_index = 0
        else:
            self.current_index = max(0, min(self.sidecar.data.last_index, len(self.image_files) - 1))
        self.stacks = self.sidecar.data.stacks # Load stacks from sidecar
        self.dataChanged.emit() # Emit after stacks are loaded
        self.watcher.start()
        self.prefetcher.update_prefetch(self.current_index)
        
        # Defer initial UI sync until after images are loaded
        self.sync_ui_state()


    def refresh_image_list(self):
        """Rescans the directory for images and applies the current filter."""
        all_images = find_images(self.image_dir)
        if self._filter_enabled and self._filter_string:
            needle = self._filter_string.lower()
            self.image_files = [
                img for img in all_images
                if needle in img.path.stem.lower()
            ]
        else:
            self.image_files = all_images

        self.prefetcher.set_image_files(self.image_files)
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.ui_state.imageCountChanged.emit()

    def get_decoded_image(self, index: int) -> Optional[DecodedImage]:
        """Retrieves a decoded image, blocking until ready to ensure correct display."""
        if not self.image_files: # Handle empty image list
            log.warning("get_decoded_image called with empty image_files.")
            return None

        _, _, display_gen = self.get_display_info()
        cache_key = f"{index}_{display_gen}"

        # Check cache first
        if cache_key in self.image_cache:
            decoded = self.image_cache[cache_key]
            self.last_displayed_image = decoded
            return decoded
        
        # Cache miss: need to decode synchronously to ensure correct image displays
        if _debug_mode:
            decode_start = time.perf_counter()
            log.info(f"Cache miss for index {index} (gen: {display_gen}). Blocking decode.")
        
        future = self.prefetcher.submit_task(index, self.prefetcher.generation)
        if future:
            try:
                # Wait for decode to complete (blocking but fast for JPEGs)
                result = future.result(timeout=5.0)  # 5 second timeout as safety
                if result:
                    decoded_index, decoded_display_gen = result
                    cache_key = f"{decoded_index}_{decoded_display_gen}"
                    if cache_key in self.image_cache:
                        decoded = self.image_cache[cache_key]
                        self.last_displayed_image = decoded
                        if _debug_mode:
                            elapsed = time.perf_counter() - decode_start
                            log.info(f"Decoded image {index} in {elapsed:.3f}s")
                        return decoded
            except concurrent.futures.TimeoutError:
                log.error(f"Timeout decoding image at index {index}")
                return self.last_displayed_image
            except concurrent.futures.CancelledError:
                log.warning(f"Decode cancelled for index {index}")
                return self.last_displayed_image
            except Exception as e:
                log.error(f"Error decoding image at index {index}: {e}")
                return self.last_displayed_image
        
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
            f"UI State Synced: Index={self.ui_state.currentIndex}, "
            f"Count={self.ui_state.imageCount}"
        )
        log.debug(
            f"Metadata Synced: Filename={self.ui_state.currentFilename}, "
            f"Flagged={self.ui_state.isFlagged}, Rejected={self.ui_state.isRejected}, "
            f"StackInfo='{self.ui_state.stackInfoText}'"
        )


    # --- Actions --- 

    def next_image(self):
        if self.current_index < len(self.image_files) - 1:
            self.current_index += 1
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

    def prev_image(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

    def toggle_grid_view(self):
        log.warning("Grid view not implemented yet.")

    def get_current_metadata(self) -> Dict:
        if not self.image_files:
            log.debug("get_current_metadata: image_files is empty, returning {}.")
            return {}
        
        # Cache hit check
        cache_key = (self.current_index, self.ui_refresh_generation)
        if cache_key == self._metadata_cache_index:
            return self._metadata_cache
        
        # Compute and cache
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        stack_info = self._get_stack_info(self.current_index)
        
        self._metadata_cache = {
            "filename": self.image_files[self.current_index].path.name,
            "flag": meta.flag,
            "reject": meta.reject,
            "stacked": meta.stacked,
            "stacked_date": meta.stacked_date or "",
            "stack_info_text": stack_info
        }
        self._metadata_cache_index = cache_key
        return self._metadata_cache

    def toggle_current_flag(self):
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        meta.flag = not meta.flag
        self.sidecar.save()
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit()

    def toggle_current_reject(self):
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        meta.reject = not meta.reject
        self.sidecar.save()
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit()

    def begin_new_stack(self):
        self.stack_start_index = self.current_index
        log.info(f"Stack start marked at index {self.stack_start_index}")
        self._metadata_cache_index = (-1, -1) # Invalidate cache
        self.dataChanged.emit() # Update UI to show start marker
        self.sync_ui_state()

    def end_current_stack(self):
        log.info(f"end_current_stack called. stack_start_index: {self.stack_start_index}")
        if self.stack_start_index is not None:
            start = min(self.stack_start_index, self.current_index)
            end = max(self.stack_start_index, self.current_index)
            self.stacks.append([start, end])
            self.stacks.sort() # Keep stacks sorted by start index
            self.sidecar.data.stacks = self.stacks
            self.sidecar.save()
            log.info(f"Defined new stack: [{start}, {end}]")
            self.stack_start_index = None
            self._metadata_cache_index = (-1, -1) # Invalidate cache
            self.dataChanged.emit() # Notify QML of data change
            self.ui_state.stackSummaryChanged.emit() # Update stack summary in dialog
            self.sync_ui_state()
        else:
            log.warning("No stack start marked. Press '[' first.")

    def toggle_selection(self):
        """Toggles the selection status of the current image's RAW file."""
        if not self.image_files:
            return

        image_file = self.image_files[self.current_index]
        if image_file.raw_pair:
            if image_file.raw_pair in self.selected_raws:
                self.selected_raws.remove(image_file.raw_pair)
                log.info(f"Removed {image_file.raw_pair.name} from selection.")
            else:
                self.selected_raws.add(image_file.raw_pair)
                log.info(f"Added {image_file.raw_pair.name} to selection.")
            
            # In a real app, we'd update a selection indicator in the UI.
            # For now, we just log and can use it for batch operations.
            self.sync_ui_state() # This will trigger a UI refresh


    def launch_helicon(self):
        """Launches Helicon Focus with selected RAWs or all RAWs in defined stacks."""
        if self.selected_raws:
            log.info(f"Launching Helicon with {len(self.selected_raws)} selected RAW files.")
            self._launch_helicon_with_files(sorted(list(self.selected_raws)))
            self.selected_raws.clear()

        elif self.stacks:
            log.info(f"Launching Helicon for {len(self.stacks)} defined stacks.")
            for start, end in self.stacks:
                raw_files_to_process = []
                for idx in range(start, end + 1):
                    if idx < len(self.image_files) and self.image_files[idx].raw_pair:
                        raw_files_to_process.append(self.image_files[idx].raw_pair)
                
                if raw_files_to_process:
                    self._launch_helicon_with_files(raw_files_to_process)
                else:
                    log.warning(f"No valid RAW files found for stack [{start}, {end}].")
            
            # clear_all_stacks() already emits stackSummaryChanged
            self.clear_all_stacks()

        else:
            log.warning("No selection or stacks defined to launch Helicon Focus.")
            return

        self.sync_ui_state()

    def _launch_helicon_with_files(self, raw_files: List[Path]):
        """Helper to launch Helicon with a specific list of files."""
        log.info(f"Launching Helicon Focus with {len(raw_files)} RAW files.")
        unique_raw_files = sorted(list(set(raw_files)))
        success, tmp_path = launch_helicon_focus(unique_raw_files)
        if success and tmp_path:
            # Schedule delayed deletion of the temporary file
            QTimer.singleShot(5000, lambda: self._delete_temp_file(tmp_path))

            # Record stacking metadata
            today = date.today().isoformat()
            for raw_path in unique_raw_files:
                # Find the corresponding image file to get the stem
                for img_file in self.image_files:
                    if img_file.raw_pair == raw_path:
                        stem = img_file.path.stem
                        meta = self.sidecar.get_metadata(stem)
                        meta.stacked = True
                        meta.stacked_date = today
                        break
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1) # Invalidate cache

    def _delete_temp_file(self, tmp_path: Path):
        if tmp_path.exists():
            try:
                # os.remove(tmp_path)
                log.info(f"Keeping temporary file: {tmp_path}")
            except OSError as e:
                log.error(f"Error deleting temporary file {tmp_path}: {e}")

    def clear_all_stacks(self):
        log.info("Clearing all defined stacks.")
        self.stacks = []
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
        if mode not in ['none', 'saturation', 'icc']:
            log.error(f"Invalid color mode: {mode}")
            return
        
        log.info(f"Setting color mode to: {mode}")
        config.set('color', 'mode', mode)
        config.save()
        
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
        log.info(f"Setting saturation factor to: {factor}")
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
        log.debug(f"Updating preload progress in UI: {progress}%")
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
            log.error(f"Failed to create recycle bin directory: {e}")
            return
        
        # Move files to recycle bin
        deleted_files = []
        try:
            if jpg_path.exists():
                dest = self.recycle_bin_dir / jpg_path.name
                jpg_path.rename(dest)
                deleted_files.append(jpg_path.name)
                log.info(f"Moved {jpg_path.name} to recycle bin")
            
            if raw_path and raw_path.exists():
                dest = self.recycle_bin_dir / raw_path.name
                raw_path.rename(dest)
                deleted_files.append(raw_path.name)
                log.info(f"Moved {raw_path.name} to recycle bin")
            
            # Add to delete history for undo
            self.delete_history.append((jpg_path, raw_path))
            
            # Update status
            files_str = ", ".join(deleted_files)
            self.update_status_message(f"Deleted: {files_str}")
            
            # Refresh image list and move to next image
            self.refresh_image_list()
            if self.image_files:
                # Stay at same index (which now shows the next image)
                self.current_index = min(self.current_index, len(self.image_files) - 1)
                # Clear cache and invalidate display generation to force image reload
                self.display_generation += 1
                self.image_cache.clear()
                # update_prefetch will handle cancelling stale tasks and incrementing generation
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()
            
        except OSError as e:
            self.update_status_message(f"Delete failed: {e}")
            log.error(f"Failed to delete image: {e}")

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
                log.info(f"Restored {jpg_path.name} from recycle bin")
            
            # Restore RAW
            if raw_path:
                raw_in_bin = self.recycle_bin_dir / raw_path.name
                if raw_in_bin.exists():
                    raw_in_bin.rename(raw_path)
                    restored_files.append(raw_path.name)
                    log.info(f"Restored {raw_path.name} from recycle bin")
            
            # Update status
            files_str = ", ".join(restored_files)
            self.update_status_message(f"Restored: {files_str}")
            
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
            # update_prefetch will handle cancelling stale tasks and incrementing generation
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()
            
        except OSError as e:
            self.update_status_message(f"Undo failed: {e}")
            log.error(f"Failed to restore image: {e}")
            # Put it back in history if it failed
            self.delete_history.append((jpg_path, raw_path))

    def shutdown(self):
        log.info("Application shutting down.")
        
        # Check if recycle bin has files and prompt to empty
        if self.recycle_bin_dir.exists():
            files_in_bin = list(self.recycle_bin_dir.glob("*"))
            if files_in_bin:
                reply = QMessageBox.question(
                    None,
                    "Empty Recycle Bin?",
                    f"There are {len(files_in_bin)} files in the recycle bin. Do you want to permanently delete them?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                
                if reply == QMessageBox.Yes:
                    self.empty_recycle_bin()
        
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
            log.info("Emptied recycle bin")
        except OSError as e:
            log.error(f"Failed to empty recycle bin: {e}")

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
            log.info(f"Using RAW file for Photoshop: {raw_path}")
        else:
            current_image_path = image_file.path
            log.info(f"Using JPG file for Photoshop: {current_image_path}")
        
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
            log.error(f"Photoshop executable validation failed: {error_msg}")
            return
        
        # Validate that the file path exists and is a file
        if not current_image_path.exists() or not current_image_path.is_file():
            self.update_status_message(f"Image file not found: {current_image_path.name}")
            log.error(f"Image file not found or not a file: {current_image_path}")
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
                    log.error(f"Invalid photoshop_args format: {e}")
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
            self.update_status_message(f"Opened {current_image_path.name} in Photoshop.")
            log.info(f"Launched Photoshop with: {command}")
        except (OSError, subprocess.SubprocessError) as e:
            self.update_status_message(f"Failed to open in Photoshop: {e}")
            log.exception(f"Error launching Photoshop: {e}")
        except FileNotFoundError as e:
            self.update_status_message(f"Photoshop executable not found: {e}")
            log.exception(f"Photoshop executable not found: {e}")

    @Slot()
    def copy_path_to_clipboard(self):
        if not self.image_files:
            self.update_status_message("No image path to copy.")
            return

        current_image_path = str(self.image_files[self.current_index].path)
        QApplication.clipboard().setText(current_image_path)
        self.update_status_message(f"Copied: {current_image_path}")
        log.info(f"Copied path to clipboard: {current_image_path}")

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

        file_path = self.image_files[self.current_index].path
        if not file_path.exists():
            log.error(f"File does not exist, cannot start drag: {file_path}")
            return

        if self.main_window is None:
            return

        drag = QDrag(self.main_window)
        mime_data = QMimeData()

        # --- Windows file drop payload ---
        if sys.platform.startswith("win"):
            hdrop = make_hdrop([str(file_path)])
            mime_data.setData('application/x-qt-windows-mime;value="FileDrop"', hdrop)
            mime_data.setData('application/x-qt-windows-mime;value="FileNameW"',
                              (str(file_path) + "\0").encode("utf-16le"))
            mime_data.setData('application/x-qt-windows-mime;value="FileName"',
                              (str(file_path) + "\0").encode("mbcs", errors="replace"))
        else:
            mime_data.setUrls([QUrl.fromLocalFile(str(file_path))])

        drag.setMimeData(mime_data)

        # --- thumbnail / drag preview ---
        pix = QPixmap(str(file_path))
        if not pix.isNull():
            # scale it down so it’s not huge
            scaled = pix.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            drag.setPixmap(scaled)
            # hotspot = center of image
            drag.setHotSpot(QPoint(scaled.width() // 2, scaled.height() // 2))

        log.info(f"Starting drag for {file_path}")
        drag.exec(Qt.CopyAction)

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
        log.info(f"_get_stack_info for index {index}: {info}")
        return info

    def get_stack_summary(self) -> str:
        if not self.stacks:
            return "No stacks defined."
        summary = []
        for i, (start, end) in enumerate(self.stacks):
            summary.append(f"Stack {i+1}: {start}-{end}")
        return "; ".join(summary)

    def is_stacked(self) -> bool:
        if not self.image_files:
            return False
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        return meta.stacked

def main(
    image_dir: Optional[Path] = typer.Argument(None, help="Directory of images to view"),
    debug: bool = typer.Option(False, "--debug", help="Enable debug logging and timing information")
):
    """FastStack Application Entry Point"""
    global _debug_mode
    _debug_mode = debug
    
    t0 = time.perf_counter()
    setup_logging(debug)
    if debug:
        log.info(f"Startup: after setup_logging: {time.perf_counter() - t0:.3f}s")
    log.info("Starting FastStack")

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"

    app = QApplication(sys.argv) # Moved here
    if debug:
        log.info(f"Startup: after QApplication: {time.perf_counter() - t0:.3f}s")

    if image_dir is None:
        image_dir_str = config.get('core', 'default_directory')
        if not image_dir_str:
            log.warning("No image directory provided and no default directory set. Opening directory selection dialog.")
            selected_dir = QFileDialog.getExistingDirectory(None, "Select Image Directory")
            if not selected_dir:
                log.error("No image directory selected. Exiting.")
                sys.exit(1)
            image_dir_str = selected_dir
        image_dir = Path(image_dir_str)

    if not image_dir.is_dir():
        log.error(f"Image directory not found: {image_dir}")
        sys.exit(1)
    app.setOrganizationName("FastStack")
    app.setOrganizationDomain("faststack.dev")
    app.setApplicationName("FastStack")

    engine = QQmlApplicationEngine()
    controller = AppController(image_dir, engine)
    if debug:
        log.info(f"Startup: after AppController: {time.perf_counter() - t0:.3f}s")
    image_provider = ImageProvider(controller)
    engine.addImageProvider("provider", image_provider)

    # Expose controller and UI state to QML
    context = engine.rootContext()
    context.setContextProperty("uiState", controller.ui_state)
    context.setContextProperty("controller", controller)

    qml_file = Path(__file__).parent / "qml" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    if debug:
        log.info(f"Startup: after engine.load(QML): {time.perf_counter() - t0:.3f}s")

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
        log.info(f"Startup: after controller.load(): {time.perf_counter() - t0:.3f}s")

    # Graceful shutdown
    app.aboutToQuit.connect(controller.shutdown)

    sys.exit(app.exec())

if __name__ == "__main__":
    typer.run(main)
