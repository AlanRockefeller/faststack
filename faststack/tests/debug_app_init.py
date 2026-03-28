from unittest.mock import MagicMock, patch
from pathlib import Path
from faststack.app import AppController
from PySide6.QtWidgets import QApplication
import sys

# Ensure QApplication exists before AppController is imported/used
if not QApplication.instance():
    _qapp = QApplication(sys.argv)


def test_app_init_only():
    """Verify AppController can be instantiated with mocks."""
    with (
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.PathResolver"),
        patch("faststack.app.Watcher"),
        patch("faststack.app.uuid"),
        patch("faststack.app.QTimer"),
        patch("faststack.app.concurrent.futures.ThreadPoolExecutor"),
    ):

        # Create QApplication instance
        from PySide6.QtWidgets import QApplication
        import sys

        if not QApplication.instance():
            _ = QApplication(sys.argv)
        else:
            _ = QApplication.instance()

        mock_engine = MagicMock()
        try:
            _app = AppController(Path("."), mock_engine)
            print("AppController instantiated successfully")
        except Exception as e:
            print(f"AppController instantiation failed: {e}")
            raise
