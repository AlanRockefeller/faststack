"""Filesystem watcher to detect changes in the image directory."""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

# Matches FastStack backup filenames: name-backup.jpg, name-backup2.jpg, etc.
_BACKUP_RE = re.compile(r"-backup\d*\.jpe?g$")


def _is_ignored_path(path: str) -> bool:
    """Return True for paths the watcher should silently ignore."""
    # Normalize separators to forward slashes for consistent checking
    p = path.lower().replace(os.sep, "/").replace("\\", "/")
    return (
        p.endswith(".tmp")
        or p.endswith("faststack.json")
        or ".__faststack_tmp__" in p
        or _BACKUP_RE.search(p) is not None
        or "image recycle bin" in p.split("/")
    )


class ImageDirectoryEventHandler(FileSystemEventHandler):
    """Handles filesystem events for the image directory.

    Events are forwarded to the callback immediately.  The callback is
    expected to handle debouncing (e.g. via QTimer on the UI thread).
    """

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def on_created(self, event):
        if _is_ignored_path(event.src_path):
            return
        log.info("Detected file creation: %s. Requesting refresh.", event.src_path)
        self.callback(event.src_path)

    def on_deleted(self, event):
        if _is_ignored_path(event.src_path):
            return
        log.info("Detected file deletion: %s. Requesting refresh.", event.src_path)
        self.callback(event.src_path)

    def on_moved(self, event):
        if _is_ignored_path(event.src_path) or _is_ignored_path(event.dest_path):
            return
        log.info(
            "Detected file move: %s -> %s. Requesting refresh.",
            event.src_path,
            event.dest_path,
        )
        self.callback(event.src_path)
        self.callback(event.dest_path)

    def on_modified(self, event):
        # This is a no-op to prevent spurious refreshes from file modifications
        # that don't change the content (e.g., antivirus scans).
        pass


class Watcher:
    """Manages the filesystem observer."""

    def __init__(self, directory: Path, callback):
        self.observer: Optional[Observer] = None  # Initialize to None
        self.event_handler = ImageDirectoryEventHandler(callback)
        self.directory = directory
        self.callback = callback

    def start(self):
        """Starts watching the directory."""
        if not self.directory.is_dir():
            log.warning(f"Cannot watch non-existent directory: {self.directory}")
            return

        if self.observer and self.observer.is_alive():
            return  # Already running

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
            self.observer = None  # Clear instance after stopping

    def is_alive(self) -> bool:
        """Checks if the watcher thread is alive."""
        return bool(self.observer and self.observer.is_alive())
