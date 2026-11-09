# faststack/tests/conftest.py
import atexit
import faulthandler
import os
import shutil
import signal
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# Point the app data dir (config INI, session records, logs) at a throwaway
# directory before any faststack module is imported, so the suite never reads
# or overwrites the developer's real ~/.faststack or %APPDATA%\faststack.
# get_app_data_dir() skips candidates that don't exist yet, so create it.
_TEST_APP_DATA = tempfile.mkdtemp(prefix="faststack-test-appdata-")
os.makedirs(os.path.join(_TEST_APP_DATA, "faststack"), exist_ok=True)
os.environ["APPDATA"] = _TEST_APP_DATA
os.environ.pop("LOCALAPPDATA", None)
# Remove the directory when the interpreter exits; otherwise every run leaves
# one behind in the system temp folder. atexit callbacks run after the
# non-daemon save/delete executor threads have been joined, so a late
# session-record or sidecar write cannot recreate the tree. Best effort: a
# daemon thread racing the removal only costs a leftover directory.
atexit.register(shutil.rmtree, _TEST_APP_DATA, ignore_errors=True)


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


def make_config_mock() -> MagicMock:
    """A stand-in for ``faststack.config`` that behaves like the real getters.

    The real config returns the caller's ``fallback`` when a key is missing, so
    production code can do arithmetic on the result. A bare ``MagicMock``
    returns a mock instead, which blows up on the first comparison.
    """

    def _fallback(section, key, fallback=None):
        return fallback

    cfg = MagicMock()
    cfg.get.side_effect = _fallback
    cfg.getint.side_effect = _fallback
    cfg.getfloat.side_effect = _fallback
    cfg.getboolean.side_effect = _fallback
    return cfg


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
        patch("faststack.app.config", new_callable=make_config_mock),
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
        # Tests drive delete completion by calling _on_delete_finished()
        # directly. The real background delete job is still submitted, and
        # Future.add_done_callback() runs inline on the calling thread when the
        # job has already finished, so on a fast machine the real completion
        # could settle a placeholder before the test got to it. Disconnect the
        # bridge so only explicit calls advance delete state.
        controller._deleteFinished.disconnect(controller._on_delete_finished)
        yield controller
