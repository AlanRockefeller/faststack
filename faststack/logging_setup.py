"""Configures application-wide logging."""

import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


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


def get_app_data_dir() -> Path:
    """Return a writable application data directory, with fallbacks."""
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


def setup_logging(debug: bool = False):
    """Sets up logging to a rotating file in the app data directory.

    Args:
        debug: If True, sets log level to DEBUG. Otherwise, sets to WARNING to reduce noise.
    """
    log_dir = get_app_data_dir() / "logs"
    log_file = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = Path(tempfile.gettempdir()) / "faststack" / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = None

    if log_dir is None:
        sys.stderr.write(
            "WARNING: Could not create log directory; logs will not be persisted.\n"
        )

    if log_dir is not None:
        log_file = log_dir / "app.log"

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Console handler (for seeing logs in terminal)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Set log level based on debug flag
    root_logger.setLevel(logging.DEBUG if debug else logging.WARNING)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)

    if log_file is not None:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Configure logging for key modules
    if debug:
        logging.getLogger("faststack.imaging.cache").setLevel(logging.DEBUG)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.DEBUG)
    else:
        # In non-debug mode, only log errors from these noisy modules
        logging.getLogger("faststack.imaging.cache").setLevel(logging.ERROR)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.ERROR)
    logging.getLogger("PIL").setLevel(logging.INFO)
