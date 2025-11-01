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
        # Ignore temporary files created during atomic saves and the sidecar file itself
        if event.src_path.endswith(".tmp") or event.src_path.endswith("faststack.json"):
            return
        log.info(f"Detected filesystem change: {event}. Triggering refresh.")
        self.callback()

class Watcher:
    """Manages the filesystem observer."""
    def __init__(self, directory: Path, callback):
        self.observer: Optional[Observer] = None # Initialize to None
        self.event_handler = ImageDirectoryEventHandler(callback)
        self.directory = directory
        self.callback = callback # Store callback for new observer

    def start(self):
        """Starts watching the directory."""
        if not self.directory.is_dir():
            log.warning(f"Cannot watch non-existent directory: {self.directory}")
            return

        if self.observer and self.observer.is_alive():
            return # Already running

        # Create a new observer instance every time, as it cannot be restarted
        self.observer = Observer()
        self.observer.schedule(self.event_handler, str(self.directory), recursive=False)
        self.observer.start()
        log.info(f"Started watching directory: {self.directory}")

    def stop(self):
        """Stops watching the directory."""
        if self.observer and self.observer.is_alive():
            self.observer.stop()
            self.observer.join()
            log.info("Stopped watching directory.")
            self.observer = None # Clear instance after stopping

    def is_alive(self) -> bool:
        """Checks if the watcher thread is alive."""
        return self.observer and self.observer.is_alive()
