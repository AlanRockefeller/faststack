import pytest
from unittest.mock import Mock, patch
from pathlib import Path
from faststack.app import AppController
from faststack.models import ImageFile


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication exists for tests that might touch UI elements."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def mock_controller(tmp_path, qapp):
    """Creates an AppController with mocked dependencies."""
    # Mock dependencies
    engine = Mock()

    # Mock internal components heavily to avoid initializing the full app
    with (
        patch("faststack.app.Watcher"),
        patch("faststack.app.SidecarManager"),
        patch("faststack.app.ImageEditor"),
        patch("faststack.app.ByteLRUCache"),
        patch("faststack.app.Prefetcher"),
        patch("faststack.app.ThumbnailCache"),
        patch("faststack.app.ThumbnailPrefetcher"),
        patch("faststack.app.ThumbnailModel"),
        patch("faststack.app.ThumbnailProvider"),
        patch("faststack.app.UIState"),
        patch("faststack.app.QCoreApplication"),
        patch("faststack.app.Keybinder"),
    ):
        controller = AppController(tmp_path, engine)

        # Manually mock signals that might be emitted
        controller.dataChanged = Mock()
        controller.dataChanged.emit = Mock()
        controller.sync_ui_state = Mock()
        controller._do_prefetch = Mock()
        controller.update_status_message = Mock()
        controller._thumbnail_model = Mock()
        controller._thumbnail_model.rowCount.return_value = 0

        return controller


def test_delete_current_image_recycle_success(mock_controller):
    """Test successful deletion to recycle bin."""
    # Setup state
    img1 = ImageFile(Path("test1.jpg"))
    img2 = ImageFile(Path("test2.jpg"))
    mock_controller.image_files = [img1, img2]
    mock_controller.current_index = 0
    mock_controller.undo_history = []
    mock_controller.refresh_image_list = Mock()
    mock_controller.image_cache = Mock()
    mock_controller.prefetcher = Mock()

    # Mock _move_to_recycle to return a path (success)
    mock_controller._move_to_recycle = Mock(return_value=Path("recycle/test1.jpg"))

    # Call delete
    mock_controller.delete_current_image()

    # Verification
    mock_controller._move_to_recycle.assert_called_with(img1.path)
    # Note: refresh_image_list is now deferred via QTimer for faster UI
    mock_controller.sync_ui_state.assert_called_once()

    # Verify undo history
    assert len(mock_controller.undo_history) == 1
    action, record, ts = mock_controller.undo_history[0]
    assert action == "delete"
    assert record[0][0] == img1.path
    assert record[0][1] == Path("recycle/test1.jpg")

    mock_controller.update_status_message.assert_called_with(
        "Image moved to recycle bin"
    )

    # Verify cache/prefetch cleanup
    mock_controller.image_cache.clear.assert_called_once()
    mock_controller.prefetcher.cancel_all.assert_called_once()


def test_delete_current_image_recycle_fail_fallback_success(mock_controller):
    """Test recycle bin failure falling back to permanent delete (confirmed)."""
    # Setup state
    img1 = ImageFile(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller.current_index = 0

    # Mock _move_to_recycle to fail
    mock_controller._move_to_recycle = Mock(
        side_effect=PermissionError("Mock perm error")
    )

    # Mock external deletion module
    with (
        patch(
            "faststack.app.confirm_permanent_delete", return_value=True
        ) as mock_confirm,
        patch(
            "faststack.app.permanently_delete_image_files", return_value=True
        ) as mock_perm_delete,
    ):
        mock_controller.delete_current_image()

        mock_confirm.assert_called_once()
        mock_perm_delete.assert_called_once_with(img1)

        mock_controller.update_status_message.assert_called_with(
            "Permanently deleted 1 image(s)"
        )


def test_delete_current_image_cancel(mock_controller):
    """Test user canceling permanent delete fallback."""
    # Setup state
    img1 = ImageFile(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller.current_index = 0

    # Mock _move_to_recycle to fail
    mock_controller._move_to_recycle = Mock(
        side_effect=PermissionError("Mock perm error")
    )

    # Mock external deletion module - user says NO
    with patch(
        "faststack.app.confirm_permanent_delete", return_value=False
    ) as mock_confirm:
        mock_controller.delete_current_image()

        mock_confirm.assert_called_once()
        # verify no refresh or cache clear occurred
        mock_controller.update_status_message.assert_called_with("Deletion cancelled")
