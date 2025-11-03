"""Main application entry point for FastStack."""

import logging
import sys
from pathlib import Path
from typing import Optional, List, Dict
from datetime import date

import os
import typer
import concurrent.futures
from PySide6.QtCore import QUrl, QTimer, QObject, QEvent, Signal
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtQml import QQmlApplicationEngine

from faststack.config import config
from faststack.logging_setup import setup_logging
from faststack.models import ImageFile, DecodedImage, EntryMetadata
from faststack.io.indexer import find_images
from faststack.io.sidecar import SidecarManager
from faststack.io.watcher import Watcher
from faststack.io.helicon import launch_helicon_focus
from faststack.imaging.cache import ByteLRUCache, get_decoded_image_size
from faststack.imaging.prefetch import Prefetcher
from faststack.ui.provider import ImageProvider, UIState
from faststack.ui.keystrokes import Keybinder

import threading

log = logging.getLogger(__name__)

class AppController(QObject):
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
        self.preload_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="PreloadAll")

        # -- UI State --
        self.ui_state = UIState(self)
        self.keybinder = Keybinder(self)

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stacks: List[List[int]] = []
        self.selected_raws: set[Path] = set()

    def get_display_info(self):
        return self.display_width, self.display_height, self.display_generation

    def get_display_info(self):
        return self.display_width, self.display_height, self.display_generation

    def on_display_size_changed(self, width: int, height: int):
        if self.display_width == width and self.display_height == height:
            return # No change

        log.info(f"Display size changed to: {width}x{height}")
        self.display_width = width
        self.display_height = height
        self.display_generation += 1
        self.image_cache.clear()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state() # To refresh the image

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
        self.watcher.start()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()


    def refresh_image_list(self):
        """Rescans the directory for images."""
        self.image_files = find_images(self.image_dir)
        self.prefetcher.set_image_files(self.image_files)
        self.ui_state.imageCountChanged.emit()

    def get_decoded_image(self, index: int) -> Optional[DecodedImage]:
        """Retrieves a decoded image, from cache or by decoding."""
        if not self.image_files: # Handle empty image list
            log.warning("get_decoded_image called with empty image_files.")
            return None

        _, _, display_gen = self.get_display_info()
        cache_key = f"{index}_{display_gen}"

        if cache_key in self.image_cache:
            return self.image_cache[cache_key]
        
        # If not in cache, this was likely a cache miss. 
        # The prefetcher should have it, but we can do a blocking load if needed.
        log.warning(f"Cache miss for index {index} (gen: {display_gen}). Forcing synchronous load.")
        future = self.prefetcher.submit_task(index, self.prefetcher.generation)
        if future:
            try:
                # Wait for the result and then retrieve from cache
                result = future.result()
                if result:
                    decoded_index, decoded_display_gen = result
                    cache_key = f"{decoded_index}_{decoded_display_gen}"
                    if cache_key in self.image_cache:
                        return self.image_cache[cache_key]
            except concurrent.futures.CancelledError:
                log.warning(f"Prefetch task for index {index} was cancelled. Attempting synchronous load.")
                return None
        return None

    def sync_ui_state(self):
        """Forces the UI to update by emitting all state change signals."""
        self.ui_refresh_generation += 1
        self.ui_state.currentIndexChanged.emit()
        self.ui_state.currentImageSourceChanged.emit()
        self.ui_state.metadataChanged.emit()
        log.debug(f"UI State Synced: Index={self.ui_state.currentIndex}, Count={self.ui_state.imageCount}")
        log.debug(f"Metadata Synced: Filename={self.ui_state.currentFilename}, Flagged={self.ui_state.isFlagged}, Rejected={self.ui_state.isRejected}, StackInfo='{self.ui_state.stackInfoText}'")

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
        
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        
        stack_info = self._get_stack_info(self.current_index)

        return {
            "filename": self.image_files[self.current_index].path.name,
            "flag": meta.flag,
            "reject": meta.reject,
            "stack_info_text": stack_info,
            "stacked": meta.stacked,
            "stacked_date": meta.stacked_date,
        }

    def toggle_current_flag(self):
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        meta.flag = not meta.flag
        self.sidecar.save()
        self.ui_state.metadataChanged.emit()

    def toggle_current_reject(self):
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        meta.reject = not meta.reject
        self.sidecar.save()
        self.ui_state.metadataChanged.emit()

    def begin_new_stack(self):
        self.stack_start_index = self.current_index
        log.info(f"Stack start marked at index {self.stack_start_index}")
        self.ui_state.metadataChanged.emit() # Update UI to show start marker

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
            self.ui_state.metadataChanged.emit()
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
        raw_files_to_process = []
        if self.selected_raws:
            log.info(f"Launching Helicon with {len(self.selected_raws)} selected RAW files.")
            raw_files_to_process.extend(sorted(list(self.selected_raws))) # Sort for consistent order
        elif self.stacks:
            log.info("No selection, launching Helicon with all defined stacks.")
            for start, end in self.stacks:
                for idx in range(start, end + 1):
                    if idx < len(self.image_files) and self.image_files[idx].raw_pair:
                        raw_files_to_process.append(self.image_files[idx].raw_pair)
        else:
            log.warning("No selection or stacks defined to launch Helicon Focus.")
            return

        if raw_files_to_process:
            log.info(f"Launching Helicon Focus with {len(raw_files_to_process)} RAW files.")
            # Remove duplicates that might arise from stacks
            unique_raw_files = sorted(list(set(raw_files_to_process)))
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
                
                # Clear selection after launching
                self.selected_raws.clear()
                self.sync_ui_state()
        else:
            log.warning("No valid RAW files found to launch Helicon.")

    def _delete_temp_file(self, tmp_path: Path):
        if tmp_path.exists():
            try:
                os.remove(tmp_path)
                log.info(f"Deleted temporary file: {tmp_path}")
            except OSError as e:
                log.error(f"Error deleting temporary file {tmp_path}: {e}")

    def clear_all_stacks(self):
        log.info("Clearing all defined stacks.")
        self.stacks = []
        self.sidecar.data.stacks = self.stacks
        self.sidecar.save()
        self.ui_state.metadataChanged.emit() # Refresh UI to show no stacks

    def get_helicon_path(self):
        return config.get('helicon', 'exe')

    def set_helicon_path(self, path):
        config.set('helicon', 'exe', path)
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

    def get_theme(self):
        return 0 if config.get('core', 'theme') == 'dark' else 1

    def set_theme(self, theme_index):
        theme = 'dark' if theme_index == 0 else 'light'
        config.set('core', 'theme', theme)
        config.save()
        self.ui_state.themeChanged.emit()

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

        def _preload_and_report_progress():
            log.info(f"Preloading images.")
            
            futures = []
            for i in range(len(self.image_files)):
                future = self.prefetcher.submit_task(i, self.prefetcher.generation)
                if future:
                    futures.append(future)
            
            num_futures = len(futures)
            if num_futures == 0:
                self.reporter.finished.emit()
                return

            log.info(f"Submitted {num_futures} preloading tasks.")
            completed_count = 0
            lock = threading.Lock()

            def _on_future_done(future):
                nonlocal completed_count
                with lock:
                    completed_count += 1
                    progress = int((completed_count / num_futures) * 100)
                    self.reporter.progress_updated.emit(progress)

                if completed_count == num_futures:
                    self.reporter.finished.emit()

            for future in futures:
                future.add_done_callback(_on_future_done)

        self.preload_executor.submit(_preload_and_report_progress)

    def _update_preload_progress(self, progress: int):
        log.debug(f"Updating preload progress in UI: {progress}%")
        self.ui_state.preloadProgress = progress

    def _finish_preloading(self):
        self.ui_state.isPreloading = False
        self.ui_state.preloadProgress = 0
        log.info("Finished preloading all images.")

    def shutdown(self):
        log.info("Application shutting down.")
        # Clear QML context property to prevent TypeErrors during shutdown
        if self.engine:
            log.info("Clearing uiState context property in QML.")
            del self.engine # Explicitly delete the engine

        self.watcher.stop()
        self.prefetcher.shutdown()
        self.preload_executor.shutdown(wait=False)
        self.sidecar.set_last_index(self.current_index)
        self.sidecar.save()

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

def main(image_dir: Optional[Path] = typer.Argument(None, help="Directory of images to view")):
    """FastStack Application Entry Point"""
    setup_logging()
    log.info("Starting FastStack")

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"

    app = QApplication(sys.argv) # Moved here

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
    image_provider = ImageProvider(controller)
    engine.addImageProvider("provider", image_provider)

    # Expose controller and UI state to QML
    context = engine.rootContext()
    context.setContextProperty("uiState", controller.ui_state)

    qml_file = Path(__file__).parent / "qml" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_file)))

    if not engine.rootObjects():
        log.error("Failed to load QML.")
        sys.exit(-1)

    # Connect key events from the main window
    main_window = engine.rootObjects()[0]
    controller.main_window = main_window
    main_window.installEventFilter(controller)

    # Load data and start services
    controller.load()

    # Graceful shutdown
    app.aboutToQuit.connect(controller.shutdown)

    sys.exit(app.exec())

if __name__ == "__main__":
    typer.run(main)
