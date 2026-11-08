"""Filesystem watcher to detect changes in the image directory."""

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from faststack.io.indexer import JPG_EXTENSIONS, RAW_EXTENSIONS

log = logging.getLogger(__name__)

_STOP_JOIN_TIMEOUT_SECONDS = 5.0

# Matches FastStack backup filenames: name-backup.jpg, name-backup2.jpg, etc.
_BACKUP_RE = re.compile(r"-backup\d*\.jpe?g$")
_TEMP_IMAGE_RE = re.compile(
    r"/\.[^/]+\.(?:jpe?g|jpe|tiff?|cr2|cr3|nef|arw|orf|rw2|raf|dng)$",
    re.IGNORECASE,
)
_WATCHED_IMAGE_EXTENSIONS = frozenset(JPG_EXTENSIONS | RAW_EXTENSIONS)
# Files whose appearance or disappearance changes what the UI shows: images,
# plus the TIFF outputs of RAW development (`<stem>-working.tif`), which drive
# the Develop RAW / Load RAW button state.
_RELEVANT_FILE_EXTENSIONS = _WATCHED_IMAGE_EXTENSIONS | {".tif", ".tiff"}

# Only the event types ImageDirectoryEventHandler reacts to. On inotify this
# also narrows the kernel subscription, so FastStack's own reads (every
# prefetch decode is an IN_OPEN) no longer wake the watcher thread.
# FileModifiedEvent still covers IN_ATTRIB, so in-place external writes and
# timestamp-only changes keep arriving.
_WATCHED_EVENT_TYPES = [
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
    FileModifiedEvent,
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
]


def _is_ignored_path(path: str) -> bool:
    """Return True for paths the watcher should silently ignore."""
    # Normalize separators to forward slashes for consistent checking
    p = path.lower().replace(os.sep, "/").replace("\\", "/")
    return (
        p.endswith(".tmp")
        or p.endswith("faststack.json")
        or p.endswith("faststack.json.lock")
        or ".__faststack_tmp__" in p
        or "__faststack_rollback__" in p
        or "__faststack_restore__" in p
        or _TEMP_IMAGE_RE.search(p) is not None
        or _BACKUP_RE.search(p) is not None
        or any(
            part == "image recycle bin" or part.startswith(".faststack-delete-")
            for part in p.split("/")
        )
    )


def _discard_event(_path: str) -> None:
    """Callback for an abandoned observer: its events are no longer wanted."""


def _is_watched_image_path(path: str) -> bool:
    """Return True when a file can contribute pixels to FastStack's image list."""
    return Path(path).suffix.lower() in _WATCHED_IMAGE_EXTENSIONS


def _is_relevant_path(path: str, is_directory: bool) -> bool:
    """Return True when a create/delete/move at ``path`` can change the view.

    Directories matter to the thumbnail grid (folder tiles); files matter only
    when they are images or RAW-development TIFFs. Stray sidecars, lock files,
    text notes and the like used to trigger a full folder rescan on every
    creation or deletion.
    """
    if _is_ignored_path(path):
        return False
    return is_directory or Path(path).suffix.lower() in _RELEVANT_FILE_EXTENSIONS


class ImageDirectoryEventHandler(FileSystemEventHandler):
    """Handles filesystem events for the image directory.

    Events are forwarded to the callback immediately.  The callback is
    expected to handle debouncing (e.g. via QTimer on the UI thread).
    """

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def on_created(self, event):
        if not _is_relevant_path(event.src_path, event.is_directory):
            return
        log.info("Detected file creation: %s. Requesting refresh.", event.src_path)
        self.callback(event.src_path)

    def on_deleted(self, event):
        if not _is_relevant_path(event.src_path, event.is_directory):
            return
        log.info("Detected file deletion: %s. Requesting refresh.", event.src_path)
        self.callback(event.src_path)

    def on_moved(self, event):
        # Treat the endpoints independently. Applications commonly save by moving
        # an ignored temporary file over the real image; the destination still
        # needs to invalidate its cached pixels.
        src_relevant = _is_relevant_path(event.src_path, event.is_directory)
        dest_relevant = _is_relevant_path(event.dest_path, event.is_directory)
        if not (src_relevant or dest_relevant):
            # e.g. our own sidecar write: faststack.<uuid>.tmp -> faststack.json
            return
        log.info(
            "Detected file move: %s -> %s. Requesting refresh.",
            event.src_path,
            event.dest_path,
        )
        if src_relevant:
            self.callback(event.src_path)
        if dest_relevant:
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
            obs.schedule(
                self.event_handler,
                str(self.directory),
                recursive=False,
                event_filter=_WATCHED_EVENT_TYPES,
            )
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

    def stop(self) -> bool:
        """Stop watching. Returns False when the observer had to be abandoned."""
        observer = self.observer
        stopped = True
        if observer and observer.is_alive():
            # Bounded: stop() runs on the UI thread (e.g. around sidecar
            # writes). watchdog's observer.stop() itself joins its emitter
            # threads without a timeout, so the whole stop-and-join runs on a
            # helper thread; an observer stuck on a hung network share is
            # abandoned (watchdog's threads are daemons) instead of freezing
            # the app.
            def _stop_observer() -> None:
                try:
                    observer.stop()
                    observer.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
                except Exception:  # noqa: BLE001 - best-effort shutdown
                    log.debug("Watcher stop raised", exc_info=True)

            stopper = threading.Thread(
                target=_stop_observer, name="watcher-stop", daemon=True
            )
            stopper.start()
            stopper.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)
            if stopper.is_alive() or observer.is_alive():
                log.warning(
                    "Directory watcher did not stop within %.0fs; abandoning it",
                    _STOP_JOIN_TIMEOUT_SECONDS,
                )
                # The stuck observer may still deliver events for the old
                # directory. Cut it off from the shared callback, and give
                # any later start() a handler of its own.
                self.event_handler.callback = _discard_event
                self.event_handler = ImageDirectoryEventHandler(self.callback)
                stopped = False
            else:
                log.info("Stopped watching directory.")
            self.observer = None  # Clear instance after stopping
        return stopped

    def is_alive(self) -> bool:
        """Checks if the watcher thread is alive."""
        return bool(self.observer and self.observer.is_alive())
