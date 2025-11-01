"""Main application entry point for FastStack."""

import logging
import sys
from pathlib import Path
from typing import Optional, List, Dict

import typer
from PySide6.QtCore import QUrl, QTimer, QObject, QEvent
from PySide6.QtGui import QGuiApplication
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

log = logging.getLogger(__name__)

class AppController(QObject):
    def __init__(self, image_dir: Path):
        super().__init__()
        self.image_dir = image_dir
        self.image_files: List[ImageFile] = []
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None

        # -- Backend Components --
        self.sidecar = SidecarManager(self.image_dir)
        self.watcher = Watcher(self.image_dir, self.refresh_image_list)
        
        # -- Caching & Prefetching --
        cache_size_bytes = config.getint('core', 'cache_bytes', int(1.5 * 1024**3))
        self.image_cache = ByteLRUCache(max_bytes=cache_size_bytes, size_of=get_decoded_image_size)
        self.prefetcher = Prefetcher(
            image_files=self.image_files,
            cache_put=self.image_cache.__setitem__,
            prefetch_radius=config.getint('core', 'prefetch_radius', 4)
        )

        # -- UI State --
        self.ui_state = UIState(self)
        self.keybinder = Keybinder(self)

        # -- Stacking State --
        self.selection_set = set()
        self.current_stack_id = 1

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched == self.main_window and event.type() == QEvent.Type.KeyPress:
            self.keybinder.handle_key_press(event)
            return True
        return super().eventFilter(watched, event)

    def load(self):
        """Loads images, sidecar data, and starts services."""
        self.refresh_image_list()
        self.current_index = self.sidecar.data.last_index
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
        if index in self.image_cache:
            return self.image_cache[index]
        
        # If not in cache, this was likely a cache miss. 
        # The prefetcher should have it, but we can do a blocking load if needed.
        log.warning(f"Cache miss for index {index}. Forcing synchronous load.")
        future = self.prefetcher.submit_task(index, self.prefetcher.generation)
        if future:
            # Wait for the result and then retrieve from cache
            decoded_index = future.result()
            if decoded_index is not None and decoded_index in self.image_cache:
                return self.image_cache[decoded_index]
        return None

    def sync_ui_state(self):
        """Forces the UI to update by emitting all state change signals."""
        self.ui_refresh_generation += 1
        self.ui_state.currentIndexChanged.emit()
        self.ui_state.currentImageSourceChanged.emit()
        self.ui_state.metadataChanged.emit()

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
            return {}
        stem = self.image_files[self.current_index].path.stem
        meta = self.sidecar.get_metadata(stem)
        return {
            "filename": self.image_files[self.current_index].path.name,
            "flag": meta.flag,
            "reject": meta.reject,
            "stack_id": meta.stack_id or -1,
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

    def toggle_selection(self):
        raw_path = self.image_files[self.current_index].raw_pair
        if raw_path:
            if raw_path in self.selection_set:
                self.selection_set.remove(raw_path)
                log.info(f"Removed {raw_path.name} from selection.")
            else:
                self.selection_set.add(raw_path)
                log.info(f"Added {raw_path.name} to selection.")

    def begin_new_stack(self):
        # Find the max stack ID and increment
        max_id = 0
        for meta in self.sidecar.data.entries.values():
            if meta.stack_id and meta.stack_id > max_id:
                max_id = meta.stack_id
        self.current_stack_id = max_id + 1
        log.info(f"Started new stack with ID: {self.current_stack_id}")

    def end_current_stack(self):
        # This is mostly a conceptual action in this model
        log.info(f"Ended stack {self.current_stack_id}")

    def launch_helicon(self):
        if not self.selection_set:
            log.warning("No images selected for Helicon Focus.")
            return
        
        sorted_files = sorted(list(self.selection_set))
        launch_helicon_focus(sorted_files)

    def shutdown(self):
        log.info("Application shutting down.")
        self.watcher.stop()
        self.prefetcher.shutdown()
        self.sidecar.set_last_index(self.current_index)
        self.sidecar.save()

def main(image_dir: Path = typer.Argument(..., help="Directory of images to view")):
    """FastStack Application Entry Point"""
    setup_logging()
    log.info("Starting FastStack")

    if not image_dir.is_dir():
        log.error(f"Image directory not found: {image_dir}")
        sys.exit(1)

    app = QGuiApplication(sys.argv)
    app.setOrganizationName("FastStack")
    app.setOrganizationDomain("faststack.dev")
    app.setApplicationName("FastStack")

    controller = AppController(image_dir)
    
    engine = QQmlApplicationEngine()
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
