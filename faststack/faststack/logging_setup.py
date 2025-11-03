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

def setup_logging():
    """Sets up logging to a rotating file in the app data directory."""
    log_dir = get_app_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5
    )
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)

    # Configure logging for key modules
    logging.getLogger("faststack.imaging.cache").setLevel(logging.DEBUG)
    logging.getLogger("faststack.imaging.prefetch").setLevel(logging.DEBUG)
    logging.getLogger("PIL").setLevel(logging.INFO)
