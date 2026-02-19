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
    ):
        ctrl = AppController(tmp_path, Mock())
        ctrl._thumbnail_model = Mock()
        ctrl._thumbnail_model.rowCount.return_value = 0
        ctrl._path_resolver = Mock()
        return ctrl


def test_startup_only_one_scan(controller):
    """Verify that startup performs exactly one variant scan."""
    with patch(
        "faststack.app.find_images_with_variants", return_value=([], {})
    ) as mock_scan:
        controller.load()

    assert controller._scan_count_variant == 1
    assert controller._grid_refreshes == 1
    assert mock_scan.call_count == 1

    # refresh_from_controller should be used instead of refresh
    assert controller._thumbnail_model.refresh_from_controller.called
    assert not controller._thumbnail_model.refresh.called


def test_toggle_grid_avoids_redundant_refresh(controller):
    """Verify that toggling grid view does not trigger a refresh if the model is already current."""
    # Setup: simulate a state where model is already current
    controller.image_files = [Mock()]
    controller._grid_model_dirty = False
    controller._is_grid_view_active = False
    controller._thumbnail_model.rowCount.return_value = 1
    controller._grid_refreshes = 0

    controller._thumbnail_model.find_image_index.return_value = -1

    # Toggle to grid
    controller._set_grid_view_active(True)

    # Should NOT have refreshed because it wasn't dirty and rowCount > 0
    assert not controller._thumbnail_model.refresh_from_controller.called
    assert not controller._thumbnail_model.refresh.called
    assert controller._grid_refreshes == 0


def test_apply_filter_uses_efficient_refresh(controller):
    """Verify that apply_filter uses refresh_from_controller and only one refresh."""
    controller.image_files = [Mock()]
    controller._thumbnail_model.refresh_from_controller.reset_mock()
    controller._thumbnail_model.refresh.reset_mock()

    controller.apply_filter("test", [])

    assert controller._thumbnail_model.refresh_from_controller.called
    assert not controller._thumbnail_model.refresh.called
    # Check that it called set_filter with refresh=False
    controller._thumbnail_model.set_filter.assert_called_with("test", refresh=False)


def test_loupe_filter_handles_dirty_flag(controller):
    """Verify that filtering in loupe mode doesn't refresh the grid but marks it dirty."""
    controller.image_files = [Mock()]
    controller._is_grid_view_active = False
    controller._grid_model_dirty = False
    controller._grid_refreshes = 0
    controller._thumbnail_model.refresh_from_controller.reset_mock()

    controller.apply_filter("test", [])

    # Grid should NOT have refreshed
    assert (
        not controller._thumbnail_model.refresh_from_controller.called
    ), "refresh_from_controller should NOT have been called"
    assert (
        controller._grid_refreshes == 0
    ), f"grid_refreshes should be 0, got {controller._grid_refreshes}"
    # Dirty flag should be set
    assert (
        controller._grid_model_dirty is True
    ), "grid_model_dirty should be True after filtering in loupe mode"

    # Toggle to grid should now trigger the refresh
    controller._thumbnail_model.find_image_index.return_value = -1
    controller._set_grid_view_active(True)

    assert controller._thumbnail_model.refresh_from_controller.called
    assert controller._grid_refreshes == 1
    assert controller._grid_model_dirty is False
