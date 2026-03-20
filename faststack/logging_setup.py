"""Configures application-wide logging."""

import logging
import logging.handlers
import os
from pathlib import Path
from tempfile import NamedTemporaryFile, gettempdir


def _is_writable_directory(path: Path) -> bool:
    """Return True when the directory exists and a temp file can be created there."""
    try:
        if not path.exists() or not path.is_dir():
            return False
        with NamedTemporaryFile(dir=path, prefix=".faststack-write-test-", delete=True):
            pass
        return True
    except OSError:
        return False


def _can_create_directory(path: Path) -> bool:
    """Return True when the target directory can be created without creating it yet."""
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return _is_writable_directory(parent)


def get_app_data_dir() -> Path:
    """Return a writable application data directory path."""
    candidates = []

    override = os.getenv("FASTSTACK_APPDATA")
    if override:
        candidates.append(Path(override))

    app_data = os.getenv("APPDATA")
    if app_data:
        candidates.append(Path(app_data) / "faststack")

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "faststack")

    candidates.append(Path.home() / ".faststack")
    candidates.append(Path(gettempdir()) / "faststack")
    candidates.append(Path.cwd() / "var" / "appdata")

    for candidate in candidates:
        if _is_writable_directory(candidate) or _can_create_directory(candidate):
            return candidate

    return Path.cwd() / "var" / "appdata"


def setup_logging(debug: bool = False):
    """Sets up logging to a rotating file in the app data directory.

    Args:
        debug: If True, sets log level to DEBUG. Otherwise, sets to WARNING to reduce noise.
    """
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.WARNING)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)

    try:
        log_dir = get_app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "app.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError as exc:
        root_logger.warning("File logging disabled: %s", exc)

    if debug:
        logging.getLogger("faststack.imaging.cache").setLevel(logging.DEBUG)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.DEBUG)
    else:
        logging.getLogger("faststack.imaging.cache").setLevel(logging.ERROR)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.ERROR)
    logging.getLogger("PIL").setLevel(logging.INFO)
