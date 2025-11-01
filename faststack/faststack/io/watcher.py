"""Filesystem watcher to detect changes in the image directory."""

import logging
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

class ImageDirectoryEventHandler(FileSystemEventHandler):
    """Handles filesystem events for the image directory."""
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def on_any_event(self, event):
        # In a real implementation, we would filter events and be more specific.
        # For now, any change triggers a refresh.
        log.info(f"Detected filesystem change: {event}. Triggering refresh.")
        self.callback()

class Watcher:
    """Manages the filesystem observer."""
    def __init__(self, directory: Path, callback):
        self.observer = Observer()
        self.event_handler = ImageDirectoryEventHandler(callback)
        self.directory = directory

    def start(self):
        """Starts watching the directory."""
        if not self.directory.is_dir():
            log.warning(f"Cannot watch non-existent directory: {self.directory}")
            return
        self.observer.schedule(self.event_handler, str(self.directory), recursive=False)
        self.observer.start()
        log.info(f"Started watching directory: {self.directory}")

    def stop(self):
        """Stops watching the directory."""
        if self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            log.info("Stopped watching directory.")
