import pytest
from unittest.mock import MagicMock, patch
from faststack.models import ImageFile


@pytest.fixture
def app_controller(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from faststack.app import AppController

    # Ensure QCoreApplication exists
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
        # Mock depth
        controller.refresh_image_list = MagicMock()
        controller.update_status_message = MagicMock()
        controller.sync_ui_state = MagicMock()
        controller.image_cache = MagicMock()
        controller.prefetcher = MagicMock()
        controller._thumbnail_model = MagicMock()
        controller._thumbnail_model.rowCount.return_value = 0

        return controller


def test_reactive_delete_fallback(app_controller, tmp_path):
    """Test that delete logic prompts for permanent delete when recycle fails."""
    # Setup
    img_path = app_controller.image_dir / "test.jpg"
    img_path.write_text("content")

    img_file = ImageFile(img_path)
    app_controller.image_files = [img_file]
    app_controller.current_index = 0

    # Mock _move_to_recycle to raise OSError
    with patch.object(
        app_controller, "_move_to_recycle", side_effect=OSError("Permission denied")
    ):
        # Mock confirmation dialogs in app (where they are patched by tests normally)
        with patch(
            "faststack.app.confirm_permanent_delete", return_value=True
        ) as mock_confirm:
            # Mock permanent delete execution
            with patch(
                "faststack.app.permanently_delete_image_files", return_value=True
            ) as mock_perm_delete:
                app_controller.delete_current_image()

                # Verify fallback triggered
                mock_confirm.assert_called_once()
                mock_perm_delete.assert_called_with(img_file)

                # Verify standard Refreshes/Cleanup
                # With optimistic deletion, cache is cleared immediately before file I/O
                app_controller.image_cache.clear.assert_called_once()
                app_controller.prefetcher.cancel_all.assert_called_once()
                # Note: refresh_image_list is now deferred via QTimer
                app_controller.sync_ui_state.assert_called_once()


def test_reactive_delete_fallback_cancelled(app_controller, tmp_path):
    """Test that user can cancel the fallback permanent delete and UI rolls back."""
    img_path = app_controller.image_dir / "test.jpg"
    img_path.write_text("content")

    img_file = ImageFile(img_path)
    app_controller.image_files = [img_file]
    app_controller.current_index = 0

    with patch.object(
        app_controller, "_move_to_recycle", side_effect=OSError("Permission denied")
    ):
        # User says NO to permanent delete
        with patch(
            "faststack.app.confirm_permanent_delete", return_value=False
        ) as mock_confirm:
            with patch(
                "faststack.app.permanently_delete_image_files"
            ) as mock_perm_delete:
                app_controller.delete_current_image()

                mock_confirm.assert_called_once()
                mock_perm_delete.assert_not_called()

                # With rollback on cancelled deletion:
                # 1. sync_ui_state called for optimistic UI update
                # 2. sync_ui_state called again after rollback restores the list
                assert app_controller.sync_ui_state.call_count == 2

                # Verify the image was restored (rollback worked)
                assert len(app_controller.image_files) == 1
                assert app_controller.image_files[0] == img_file
