import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from PySide6.QtWidgets import QApplication

# We mock AppController dependencies here
# Assuming these tests are run in an environment where faststack is importable


@pytest.fixture(scope="session", autouse=True)
def qapplication():
    if not QApplication.instance():
        app = QApplication(sys.argv)
    yield QApplication.instance()


@patch("faststack.app.UIState")
@patch("faststack.app.Keybinder")
@patch("faststack.app.ImageEditor")
@patch("faststack.app.ThumbnailProvider")
@patch("faststack.app.ThumbnailCache")
@patch("faststack.app.ThumbnailPrefetcher")
@patch("faststack.app.Prefetcher")
@patch("faststack.app.ByteLRUCache")
@patch("faststack.app.Watcher")
@patch("faststack.app.find_images_with_variants")
@patch("faststack.app.SidecarManager")
@patch("faststack.app.ThumbnailModel")
@patch("faststack.app.config")
def test_startup_optimization(
    MockConfig,
    MockThumbnailModel,
    MockSidecarManager,
    mock_find_images,
    MockWatcher,
    MockByteLRUCache,
    MockPrefetcher,
    MockThumbnailPrefetcher,
    MockThumbnailCache,
    MockThumbnailProvider,
    MockImageEditor,
    MockKeybinder,
    MockUIState,
):
    """Verify that startup only triggers one disk scan and one model refresh."""

    # Delayed import to ensure patches are active if AppController is imported at top level
    # (though typically patching modules works fine)
    from faststack.app import AppController

    # Setup mocks
    mock_find_images.return_value = ([], {})  # Empty list of images

    mock_model_instance = MockThumbnailModel.return_value
    mock_model_instance.rowCount.return_value = 0  # Empty model initially

    mock_engine = MagicMock()

    controller = AppController(Path("."), mock_engine)

    # Simulate load()
    controller.load()

    # Assertions
    # 1. Exactly one scan variant (from refresh_image_list called by load)
    assert controller._scan_count_variant == 1

    # 2. Exactly one grid refresh (from refresh_image_list calling refresh_from_controller)
    assert controller._grid_refreshes == 1

    # 3. Check mock calls
    mock_model_instance.refresh_from_controller.assert_called_once()
    mock_model_instance.refresh.assert_not_called()


@patch("faststack.app.UIState")
@patch("faststack.app.Keybinder")
@patch("faststack.app.ImageEditor")
@patch("faststack.app.ThumbnailProvider")
@patch("faststack.app.ThumbnailCache")
@patch("faststack.app.ThumbnailPrefetcher")
@patch("faststack.app.Prefetcher")
@patch("faststack.app.ByteLRUCache")
@patch("faststack.app.Watcher")
@patch("faststack.app.find_images_with_variants")
@patch("faststack.app.SidecarManager")
@patch("faststack.app.ThumbnailModel")
@patch("faststack.app.config")
def test_filter_optimization(
    MockConfig,
    MockThumbnailModel,
    MockSidecarManager,
    mock_find_images,
    MockWatcher,
    MockByteLRUCache,
    MockPrefetcher,
    MockThumbnailPrefetcher,
    MockThumbnailCache,
    MockThumbnailProvider,
    MockImageEditor,
    MockKeybinder,
    MockUIState,
):
    """Verify that filtering uses optimized refresh logic."""
    from faststack.app import AppController

    # Setup mocks
    mock_find_images.return_value = ([], {})

    mock_model_instance = MockThumbnailModel.return_value
    mock_model_instance.rowCount.return_value = 0

    mock_engine = MagicMock()
    controller = AppController(Path("."), mock_engine)

    # Initial load
    controller.load()

    # Reset
    mock_model_instance.reset_mock()
    controller._grid_refreshes = 0
    controller._scan_count_variant = 0

    # Apply filter
    controller.apply_filter("test", [])

    # Verify behavior
    mock_model_instance.set_filter.assert_called_with("test", refresh=False)
    mock_model_instance.set_filter_flags.assert_called_with([], refresh=False)

    # refresh_from_controller called (grid active by default)
    mock_model_instance.refresh_from_controller.assert_called_once()
    mock_model_instance.refresh.assert_not_called()
    assert controller._grid_refreshes == 1

    # Test inactive grid view
    controller._is_grid_view_active = False
    mock_model_instance.reset_mock()
    controller._grid_refreshes = 0
    controller._filter_enabled = True

    controller.clear_filter()

    mock_model_instance.refresh_from_controller.assert_not_called()
    assert controller._grid_model_dirty is True
