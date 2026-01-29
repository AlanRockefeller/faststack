import pytest
from unittest.mock import MagicMock, patch


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
    ):
        controller = AppController(image_dir, mock_engine, debug_cache=False)
        return controller


def test_reactive_delete_fallback(app_controller, tmp_path):
    """Test that delete logic prompts for permanent delete when recycle fails."""
    # Setup
    img_path = app_controller.image_dir / "test.jpg"
    img_path.write_text("content")

    img_file = MagicMock()
    img_file.path = img_path
    img_file.raw_pair = None

    app_controller.image_files = [img_file]

    # Mock _move_to_recycle to raise OSError
    with patch.object(
        app_controller, "_move_to_recycle", side_effect=OSError("Permission denied")
    ):
        # Mock confirmation dialogs
        # First one is "Recycle bin partial failure..." -> say YES
        with patch.object(
            app_controller, "_confirm_batch_permanent_delete", return_value=True
        ) as mock_confirm:
            # Mock permanent delete execution
            with patch.object(
                app_controller, "_permanently_delete_image_files", return_value=True
            ) as mock_perm_delete:
                app_controller._delete_grid_selected_images([img_path])

                # Verify fallback triggered
                mock_confirm.assert_called_once()
                assert (
                    "Recycle bin partial failure" in mock_confirm.call_args[1]["reason"]
                )

                # Verify permanent delete called
                mock_perm_delete.assert_called_with(img_file)


def test_reactive_delete_fallback_cancelled(app_controller, tmp_path):
    """Test that user can cancel the fallback permanent delete."""
    img_path = app_controller.image_dir / "test.jpg"
    img_path.write_text("content")

    img_file = MagicMock()
    img_file.path = img_path
    img_file.raw_pair = None

    app_controller.image_files = [img_file]

    with patch.object(
        app_controller, "_move_to_recycle", side_effect=OSError("Permission denied")
    ):
        # User says NO to permanent delete
        with patch.object(
            app_controller, "_confirm_batch_permanent_delete", return_value=False
        ) as mock_confirm:
            with patch.object(
                app_controller, "_permanently_delete_image_files"
            ) as mock_perm_delete:
                app_controller._delete_grid_selected_images([img_path])

                mock_confirm.assert_called_once()
                mock_perm_delete.assert_not_called()
