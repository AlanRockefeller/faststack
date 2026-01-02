"""Configures application-wide logging."""

import logging
import logging.handlers
import os
from pathlib import Path

def get_app_data_dir() -> Path:
    """Returns the application data directory."""
    app_data = os.getenv("APPDATA")
    if app_data:
        return Path(app_data) / "faststack"
    return Path.home() / ".faststack"

def setup_logging(debug: bool = False):
    """Sets up logging to a rotating file in the app data directory.
    
    Args:
        debug: If True, sets log level to DEBUG. Otherwise, sets to INFO to reduce noise.
    """
    log_dir = get_app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    # File handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5
    )
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(formatter)

    # Console handler (for seeing logs in terminal)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Set log level based on debug flag
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Configure logging for key modules
    if debug:
        logging.getLogger("faststack.imaging.cache").setLevel(logging.DEBUG)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.DEBUG)
    else:
        # In non-debug mode, only log errors from these noisy modules
        logging.getLogger("faststack.imaging.cache").setLevel(logging.ERROR)
        logging.getLogger("faststack.imaging.prefetch").setLevel(logging.ERROR)
    logging.getLogger("PIL").setLevel(logging.INFO)
