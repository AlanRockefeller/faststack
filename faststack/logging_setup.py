"""Configures application-wide logging."""

import atexit
import logging
import logging.handlers
import os
import queue
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_queue_listener: logging.handlers.QueueListener | None = None
_destination_handler: logging.Handler | None = None
_atexit_registered = False


def _stop_queue_listener() -> None:
    """Flush and stop the process-wide asynchronous logging listener."""
    global _destination_handler, _queue_listener
    listener = _queue_listener
    handler = _destination_handler
    _queue_listener = None
    _destination_handler = None
    if listener is not None:
        listener.stop()
    if handler is not None:
        handler.close()


def _is_writable_dir(path: Path) -> bool:
    """Return True when an existing directory accepts file writes."""
    if not path.is_dir():
        return False

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path, prefix="faststack-write-", delete=True
        ) as f:
            f.write("ok")
        return True
    except OSError:
        return False


def _can_create_dir(path: Path) -> bool:
    """Return True when the nearest existing parent is writable."""
    parent = path
    while not parent.exists():
        next_parent = parent.parent
        if next_parent == parent:
            return False
        parent = next_parent

    return _is_writable_dir(parent)


_app_data_dir: Path | None = None


def get_app_data_dir() -> Path:
    """Return a writable application data directory, with fallbacks.

    The choice is made once per process. Every caller (config, session
    records, logs) must agree on one directory, and the writability probe
    creates and deletes a temp file, which must not run on every call --
    the session registry asks for this directory after each navigation.
    """
    global _app_data_dir
    if _app_data_dir is None:
        _app_data_dir = _choose_app_data_dir()
    return _app_data_dir


def reset_app_data_dir_cache() -> None:
    """Forget the cached app-data directory (for tests that change HOME/APPDATA)."""
    global _app_data_dir
    _app_data_dir = None


def _choose_app_data_dir() -> Path:
    """Pick the first writable (or creatable) app-data candidate, else a temp dir."""
    candidates = []

    app_data = os.getenv("APPDATA")
    if app_data:
        candidates.append(Path(app_data) / "faststack")

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "faststack")

    candidates.append(Path.home() / ".faststack")
    candidates.append(Path.cwd() / "var" / "appdata")

    for candidate in candidates:
        if _is_writable_dir(candidate):
            return candidate

    for candidate in candidates:
        if _can_create_dir(candidate):
            return candidate

    # Final fallback: system temp is the most reliable writable location.
    fallback = Path(tempfile.gettempdir()) / "faststack"
    log.warning(
        "No writable app-data directory found; falling back to temp directory %s. "
        "Configuration and logs may not persist across restarts.",
        fallback,
    )
    return fallback


def setup_logging(debug: bool = False, *, diagnostic: bool = False) -> Path | None:
    """Set up asynchronous console logging, or file logging when headless.

    Args:
        debug: Emit verbose DEBUG records.
        diagnostic: Emit concise INFO diagnostics without enabling general
            DEBUG noise. Ignored when ``debug`` is true.

    Returns:
        The log file path, or None when file logging is unavailable.
    """
    global _atexit_registered, _destination_handler, _queue_listener

    _stop_queue_listener()

    # Console and file logging are deliberately mutually exclusive. A console
    # run already has a durable destination chosen by the caller (the terminal,
    # or an explicit shell redirect); windowed builds have no stderr and need
    # the rotating file instead.
    console_available = sys.stderr is not None and not getattr(
        sys.stderr, "closed", False
    )

    log_dir = None if console_available else get_app_data_dir() / "logs"
    log_file = None
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = Path(tempfile.gettempdir()) / "faststack" / "logs"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                log_dir = None

    if log_dir is not None:
        log_file = log_dir / "app.log"

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    root_logger = logging.getLogger()
    # Base cache diagnostics should not turn every module's INFO stream back
    # on. Keep the root at WARNING and selectively admit AppController's
    # consolidated NAVTRACE/startup records; explicit trace/debug modes remain
    # fully verbose.
    root_logger.setLevel(logging.DEBUG if debug else logging.WARNING)
    root_logger.handlers.clear()

    # faststack.controller holds the AppController mixins that were split out
    # of faststack.app; they keep the same verbosity.
    for logger_name in ("faststack.app", "faststack.controller"):
        app_logger = logging.getLogger(logger_name)
        if debug:
            app_logger.setLevel(logging.DEBUG)
        elif diagnostic:
            app_logger.setLevel(logging.INFO)
        else:
            app_logger.setLevel(logging.NOTSET)

    # A base --debugcache run keeps the root logger at WARNING, but scan timing
    # is a concise startup diagnostic rather than general INFO noise. Admit the
    # indexer's start/result records explicitly; trace/debug modes inherit the
    # root DEBUG level, and ordinary runs continue to inherit root WARNING.
    indexer_logger = logging.getLogger("faststack.io.indexer")
    indexer_logger.setLevel(
        logging.INFO if diagnostic and not debug else logging.NOTSET
    )

    destination_handler: logging.Handler | None = None
    if console_available:
        destination_handler = logging.StreamHandler(sys.stderr)
    elif log_file is not None:
        try:
            destination_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5
            )
        except OSError as exc:
            # The file itself can be unopenable even when the directory exists
            # (locked by another process, a directory named app.log, ACLs).
            # Return None so main() can tell the user no log file is available.
            log.warning("Could not open log file %s: %s", log_file, exc)
            log_file = None

    if destination_handler is not None:
        destination_handler.setFormatter(formatter)
        log_queue: queue.SimpleQueue = queue.SimpleQueue()
        root_logger.addHandler(logging.handlers.QueueHandler(log_queue))
        _queue_listener = logging.handlers.QueueListener(
            log_queue,
            destination_handler,
            respect_handler_level=True,
        )
        _destination_handler = destination_handler
        _queue_listener.start()
        if not _atexit_registered:
            atexit.register(_stop_queue_listener)
            _atexit_registered = True

    # Configure logging for key modules
    if debug:
        logging.getLogger("faststack.imaging.cache").setLevel(logging.DEBUG)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.DEBUG)
    elif diagnostic:
        logging.getLogger("faststack.imaging.cache").setLevel(logging.WARNING)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.WARNING)
    else:
        # In non-debug mode, only log errors from these noisy modules
        logging.getLogger("faststack.imaging.cache").setLevel(logging.ERROR)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.ERROR)
    logging.getLogger("PIL").setLevel(logging.INFO if debug else logging.WARNING)
    # watchdog logs every raw inotify event at DEBUG ("in-event ..."), which
    # buried --debug output; FastStack's own watcher logs what it acts on.
    logging.getLogger("watchdog").setLevel(logging.INFO if debug else logging.WARNING)
    # Lines relayed from external tools (faststack/io/child_process.py) stand
    # in for what those tools used to write straight to the console, so they
    # are shown at every verbosity, not only in --debug runs.
    logging.getLogger("faststack.external").setLevel(logging.INFO)
    return log_file
