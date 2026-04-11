# faststack/tests/conftest.py
import faulthandler
import os
import signal
import sys
from unittest.mock import MagicMock, patch

import pytest


def _dump_usr2(signum, frame):
    sys.stderr.write(f"\n\n=== SIGUSR2: pid={os.getpid()} ===\n")
    sys.stderr.flush()
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    sys.stderr.write("=== end SIGUSR2 dump ===\n\n")
    sys.stderr.flush()


def pytest_configure(config):
    # Enable faulthandler for crashes too
    faulthandler.enable(all_threads=True)

    # Install a *non-terminating* handler if signal available (Unix only)
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, _dump_usr2)


@pytest.fixture
def app_controller(tmp_path):
    """Shared fixture: real AppController with all heavy dependencies mocked."""
    from PySide6.QtCore import QCoreApplication

    from faststack.app import AppController

    app = QCoreApplication.instance()
    if not app:
        app = QCoreApplication([])

    image_dir = tmp_path / "images"
    image_dir.mkdir()

    mock_engine = MagicMock()

    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.config"),
        patch("faststack.app.ThumbnailProvider"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.ThumbnailPrefetcher"),
        patch("faststack.app.ThumbnailCache"),
        patch("faststack.app.Keybinder"),
        patch("faststack.app.UIState"),
    ):
        controller = AppController(image_dir, mock_engine, debug_cache=False)
        controller.refresh_image_list = MagicMock()
        controller.update_status_message = MagicMock()
        controller.sync_ui_state = MagicMock()
        controller.image_cache = MagicMock()
        controller.prefetcher = MagicMock()
        controller._thumbnail_model = MagicMock()
        controller._thumbnail_model.rowCount.return_value = 0
        controller._thumbnail_prefetcher = MagicMock()
        controller._path_resolver = MagicMock()
        controller.dataChanged = MagicMock()
        controller.ui_state = MagicMock()
        yield controller
