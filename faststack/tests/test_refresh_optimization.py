import pytest
from pathlib import Path
from unittest.mock import Mock, patch
from faststack.app import AppController


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def controller(tmp_path, qapp):
    _ = qapp
    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.setup_logging"),
        patch("faststack.app.QQmlApplicationEngine"),
        patch("faststack.app.ThumbnailModel"),
    ):
        ctrl = AppController(tmp_path, Mock())
        ctrl._thumbnail_model = Mock()
        ctrl._path_resolver = Mock()
        return ctrl


def test_do_delete_refresh_updates_resolver(controller):
    """Verify that _do_delete_refresh updates the path resolver without full model rebuild."""
    controller.image_files = [Mock(), Mock()]

    with patch("faststack.app._debug_mode", True):
        controller._do_delete_refresh()

    # Should NOT have called refresh_from_controller (trusts optimistic updates)
    assert controller._thumbnail_model.refresh_from_controller.call_count == 0
    # Should have updated resolver
    assert controller._path_resolver.update_from_model.called
