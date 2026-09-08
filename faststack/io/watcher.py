"""Filesystem watcher to detect changes in the image directory."""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from faststack.io.indexer import JPG_EXTENSIONS, RAW_EXTENSIONS

log = logging.getLogger(__name__)

# Matches FastStack backup filenames: name-backup.jpg, name-backup2.jpg, etc.
_BACKUP_RE = re.compile(r"-backup\d*\.jpe?g$")
_TEMP_IMAGE_RE = re.compile(
    r"/\.[^/]+\.(?:jpe?g|jpe|tiff?|cr2|cr3|nef|arw|orf|rw2|raf|dng)$",
    re.IGNORECASE,
)
_WATCHED_IMAGE_EXTENSIONS = frozenset(JPG_EXTENSIONS | RAW_EXTENSIONS)


def _is_ignored_path(path: str) -> bool:
    """Return True for paths the watcher should silently ignore."""
    # Normalize separators to forward slashes for consistent checking
    p = path.lower().replace(os.sep, "/").replace("\\", "/")
    return (
        p.endswith(".tmp")
        or p.endswith("faststack.json")
        or p.endswith("faststack.json.lock")
        or ".__faststack_tmp__" in p
        or _TEMP_IMAGE_RE.search(p) is not None
        or _BACKUP_RE.search(p) is not None
        or "image recycle bin" in p.split("/")
    )


def _is_watched_image_path(path: str) -> bool:
    """Return True when a file can contribute pixels to FastStack's image list."""
    return Path(path).suffix.lower() in _WATCHED_IMAGE_EXTENSIONS


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
        log.info(
            "Detected file move: %s -> %s. Requesting refresh.",
            event.src_path,
            event.dest_path,
        )
        # Treat the endpoints independently. Applications commonly save by moving
        # an ignored temporary file over the real image; the destination still
        # needs to invalidate its cached pixels.
        if not _is_ignored_path(event.src_path):
            self.callback(event.src_path)
        if not _is_ignored_path(event.dest_path):
            self.callback(event.dest_path)

    def on_modified(self, event):
        if (
            event.is_directory
            or _is_ignored_path(event.src_path)
            or not _is_watched_image_path(event.src_path)
        ):
            return
        log.info("Detected image modification: %s. Requesting refresh.", event.src_path)
        self.callback(event.src_path)


class Watcher:
    """Manages the filesystem observer."""

    def __init__(self, directory: Path, callback):
        self.observer: Optional[BaseObserver] = None  # Initialize to None
        self.event_handler = ImageDirectoryEventHandler(callback)
        self.directory = directory
        self.callback = callback

    def start(self) -> bool:
        """Start watching the directory. Returns True when watching is active.

        Filesystem watching is best-effort: the watchdog backend can fail to
        allocate an inotify instance, hit a permission error, or refuse a
        network/FUSE path. That must never abort folder loading, which happens
        after the window is already on screen -- a raised exception there left
        a visible but half-initialised app (FS-P1-005). Any failure is logged,
        the half-built Observer is torn down, and ``self.observer`` is left
        None so ``is_alive()`` never claims a healthy observer.
        """
        if not self.directory.is_dir():
            log.warning(f"Cannot watch non-existent directory: {self.directory}")
            return False

        if self.observer and self.observer.is_alive():
            return True  # Already running

        # Create a new observer instance every time, as it cannot be restarted.
        # Construction is inside the try too: selecting a backend can already
        # fail (e.g. inotify instance limit reached).
        obs = None
        try:
            obs = Observer()
            obs.schedule(self.event_handler, str(self.directory), recursive=False)
            obs.start()
        except Exception as e:
            log.warning(
                "Filesystem watching unavailable for %s: %s. External file "
                "changes will not be detected automatically.",
                self.directory,
                e,
            )
            if obs is not None:
                self._discard_observer(obs)
            self.observer = None
            return False

        self.observer = obs
        log.info(f"Started watching directory: {self.directory}")
        return True

    @staticmethod
    def _discard_observer(obs: BaseObserver) -> None:
        """Tear down an Observer that failed to start. Never raises."""
        try:
            obs.unschedule_all()
        except Exception:
            log.debug("unschedule_all() failed on partial observer", exc_info=True)
        try:
            if obs.is_alive():
                obs.stop()
                obs.join(timeout=2.0)
        except Exception:
            log.debug("stop()/join() failed on partial observer", exc_info=True)

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
