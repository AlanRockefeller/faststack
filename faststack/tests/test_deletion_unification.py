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
    _ = qapp  # Keep QApplication active for UI-touching code
    engine = Mock()
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

        # Mock signals and methods for verification
        controller.dataChanged = Mock()
        controller.sync_ui_state = Mock()
        controller.update_status_message = Mock()
        controller.refresh_image_list = Mock()
        controller.image_cache = Mock()
        controller.prefetcher = Mock()
        controller._thumbnail_model = Mock()
        controller._thumbnail_model.rowCount.return_value = 0

        return controller


def test_delete_batch_images_success(mock_controller):
    """Test deleting a batch of images to recycle bin."""
    # Setup state
    img1 = ImageFile(Path("test1.jpg"))
    img2 = ImageFile(Path("test2.jpg"))
    img3 = ImageFile(Path("test3.jpg"))
    mock_controller.image_files = [img1, img2, img3]
    mock_controller.batches = [[0, 1]]  # Delete test1 and test2
    mock_controller.undo_history = []

    # Mock _move_to_recycle
    mock_controller._move_to_recycle = Mock(
        side_effect=lambda p: Path("recycle") / p.name
    )

    with patch("faststack.app.log.info") as mock_log:
        mock_controller.delete_batch_images()

        # Verify standardized action used
        found_log = any(
            "type='batch'" in call.args[0]
            for call in mock_log.call_args_list
            if "Deletion complete" in call.args[0]
        )
        assert found_log

    # Verifications
    assert mock_controller._move_to_recycle.call_count == 2
    # Note: refresh_image_list is now deferred via QTimer.singleShot for faster UI
    # We verify sync_ui_state was called (immediate UI update) instead
    mock_controller.sync_ui_state.assert_called_once()
    assert mock_controller.batches == []
    mock_controller.update_status_message.assert_called_with("Deleted 2 images")


def test_grid_delete_selection(mock_controller):
    """Test deleting images selected in grid view."""
    # Setup state
    img1 = ImageFile(Path("test1.jpg"))
    img2 = ImageFile(Path("test2.jpg"))
    mock_controller.image_files = [img1, img2]
    mock_controller._path_to_index = {img1.path.resolve(): 0, img2.path.resolve(): 1}

    # Mock selection in thumbnail model
    mock_controller._thumbnail_model.get_selected_paths.return_value = [img1.path]
    mock_controller._move_to_recycle = Mock(return_value=Path("recycle/test1.jpg"))

    with patch("faststack.app.log.info") as mock_log:
        mock_controller.grid_delete_at_cursor(0)
        found_log = any(
            "type='grid_selection'" in call.args[0]
            for call in mock_log.call_args_list
            if "Deletion complete" in call.args[0]
        )
        assert found_log

    mock_controller._thumbnail_model.clear_selection.assert_called_once()
    mock_controller.update_status_message.assert_called_with(
        "Image moved to recycle bin"
    )


def test_grid_cursor_correct_mapping(mock_controller):
    """CRITICAL: Test that grid delete at cursor uses path mapping, NOT raw index."""
    # Setup: Application order is 0:A, 1:B
    # Grid order is 0:B, 1:A (reversed sort)
    imgA = ImageFile(Path("A.jpg"))
    imgB = ImageFile(Path("B.jpg"))
    mock_controller.image_files = [imgA, imgB]
    mock_controller._path_to_index = {imgA.path.resolve(): 0, imgB.path.resolve(): 1}

    # User clicks 'Delete' on Grid Index 0 (which is image B)
    mock_controller._thumbnail_model.get_selected_paths.return_value = []
    # Mock entry at index 0 returns path B
    mock_entry = Mock()
    mock_entry.path = imgB.path
    mock_entry.is_folder = False
    mock_controller._thumbnail_model.get_entry.return_value = mock_entry

    mock_controller._move_to_recycle = Mock(return_value=Path("recycle/B.jpg"))

    # Call delete at grid index 0
    mock_controller.grid_delete_at_cursor(0)

    # VERIFY: Image B (app index 1) was sent to deletion engine
    # We check _move_to_recycle was called with B's path
    mock_controller._move_to_recycle.assert_called_once_with(imgB.path)


def test_partial_recycle_feedback(mock_controller):
    """Test behavior when JPG recycles but RAW fails and undo also fails.

    With atomic pair behavior, if RAW exists and fails to move, we try to undo
    the JPG move. If undo also fails (common in tests), the image is marked as
    deleted to prevent UI resurrection of a missing file.
    """
    img = ImageFile(Path("test.jpg"))
    img.raw_pair = Path("test.DNG")
    mock_controller.image_files = [img]

    # Mock RAW exists but fails to recycle
    with patch("faststack.models.Path.exists", return_value=True):
        mock_controller._move_to_recycle = Mock(
            side_effect=[Path("recycle/test.jpg"), None]
        )

        mock_controller.delete_current_image()

        # Undo failed (paths don't exist in test), so:
        # - Image is marked as deleted (jpg_moved=True)
        # - No fallback dialog (can't act on it)
        # - Image removed from list (not resurrected)
        assert len(mock_controller.image_files) == 0
        # Warning message shown to user
        mock_controller.update_status_message.assert_called()


def test_permanent_delete_fallback_cancelled(mock_controller):
    """Test that batches are NOT cleared if user cancels permanent delete fallback."""
    img1 = ImageFile(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller.batches = [[0, 0]]

    mock_controller._move_to_recycle = Mock(return_value=None)

    with patch("faststack.app.confirm_permanent_delete", return_value=False):
        mock_controller.delete_batch_images()

        assert mock_controller.batches == [[0, 0]]
        mock_controller.update_status_message.assert_called_with("Deletion cancelled")


def test_delete_current_image_triggers_batch_dialog(mock_controller):
    """Test that delete_current_image triggers the multi-image dialog if a batch exists."""
    img1 = ImageFile(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller.current_index = 0

    # Mock a batch containing the current image
    mock_controller.get_batch_count_for_current_image = Mock(return_value=5)
    mock_controller.main_window = Mock()
    mock_controller._delete_indices = Mock()

    mock_controller.delete_current_image()

    # Verify dialog was opened instead of immediate deletion
    mock_controller.main_window.show_delete_batch_dialog.assert_called_once_with(5)
    # Ensure _delete_indices was NOT called (deletion is deferred to dialog)
    assert mock_controller._delete_indices.call_count == 0


def test_grid_cursor_not_found_feedback(mock_controller):
    """Test standardized feedback for grid cursor delete when image not found."""
    mock_controller._thumbnail_model.get_selected_paths.return_value = []
    mock_entry = Mock()
    mock_entry.path = Path("missing.jpg")
    mock_entry.is_folder = False
    mock_controller._thumbnail_model.get_entry.return_value = mock_entry

    mock_controller._path_to_index = {}  # Image not in list

    mock_controller.grid_delete_at_cursor(0)

    mock_controller.update_status_message.assert_called_with(
        "Image not found in current list."
    )


def test_delete_indices_summary_return(mock_controller):
    """Test that _delete_indices returns the expected summary dictionary."""
    img1 = ImageFile(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller._move_to_recycle = Mock(return_value=Path("recycle/test1.jpg"))

    result = mock_controller._delete_indices([0], "test")

    assert result["total_deleted"] == 1
    assert result["recycled"] == 1
    assert result["permanent"] == 0
    assert result["cancelled"] is False


def test_grid_cursor_mapping_regression(mock_controller):
    """Locked-in regression test: Ensure grid delete at index 0 maps to correct app index.

    Setup:
    - App internal list: [B, A]  (A is at index 1)
    - Grid view (sorted): [A, B] (A is at index 0)

    User presses Delete on Grid index 0. We must delete A (app index 1).
    """
    imgA = ImageFile(Path("A.jpg"))
    imgB = ImageFile(Path("B.jpg"))
    mock_controller.image_files = [imgB, imgA]
    mock_controller._path_to_index = {imgB.path.resolve(): 0, imgA.path.resolve(): 1}

    # User on Grid Index 0 (A.jpg)
    mock_controller._thumbnail_model.get_selected_paths.return_value = []
    mock_entry = Mock()
    mock_entry.path = imgA.path
    mock_entry.is_folder = False
    mock_controller._thumbnail_model.get_entry.return_value = mock_entry

    mock_controller._move_to_recycle = Mock(return_value=Path("recycle/A.jpg"))

    # EXECUTE: Delete at grid index 0
    mock_controller.grid_delete_at_cursor(0)

    # VERIFY: Image A (application index 1) was deleted
    mock_controller._move_to_recycle.assert_called_once_with(imgA.path)


def test_grid_delete_folder_feedback(mock_controller):
    """Test feedback when attempting to delete a folder in grid."""
    mock_controller._thumbnail_model.get_selected_paths.return_value = []
    mock_entry = Mock()
    mock_entry.is_folder = True
    mock_controller._thumbnail_model.get_entry.return_value = mock_entry

    mock_controller.grid_delete_at_cursor(0)

    mock_controller.update_status_message.assert_called_with(
        "Cannot delete folders in grid view."
    )


def test_delete_updates_path_resolver(mock_controller):
    """Test that deletion schedules a path resolver update via deferred refresh.

    Note: The actual path resolver update happens in a deferred QTimer callback,
    so we verify the _refresh_scheduled flag is set (scheduling happened).
    """

    img1 = ImageFile(Path("test1.jpg"))
    mock_controller.image_files = [img1]
    mock_controller._move_to_recycle = Mock(return_value=Path("recycle/test1.jpg"))
    mock_controller._path_resolver = Mock()
    mock_controller._refresh_scheduled = False  # Initialize the flag

    # Configure shared mock for the model in both calls
    mock_controller._thumbnail_model.rowCount.return_value = 1
    mock_controller._thumbnail_model.get_entry.return_value = Mock(
        path=img1.path, is_folder=False
    )

    # 1. Selection path
    mock_controller._thumbnail_model.get_selected_paths.return_value = [img1.path]
    mock_controller.grid_delete_at_cursor(0)

    # Verify deferred refresh was scheduled (path resolver update happens there)
    assert mock_controller._refresh_scheduled is True


def test_partial_delete_cancel_preserves_batch(mock_controller):
    """Test that if some images in a batch fail to delete and user cancels, batch is NOT cleared."""
    img1 = ImageFile(Path("test1.jpg"))
    img2 = ImageFile(Path("test2.jpg"))
    mock_controller.image_files = [img1, img2]
    mock_controller.batches = [[0, 1]]

    # img1 recycles successfully, img2 fails
    def mock_recycle(p):
        if p == img1.path:
            return Path("recycle/test1.jpg")
        raise PermissionError("Fail img2")

    mock_controller._move_to_recycle = Mock(side_effect=mock_recycle)

    # User cancels permanent delete for img2
    with patch("faststack.app.confirm_permanent_delete", return_value=False):
        # We need to mock rowCount for the resolver update that happens during refresh
        mock_controller._thumbnail_model.rowCount.return_value = 1
        mock_controller.delete_batch_images()

    # Verify:
    # 1. batches were NOT cleared because all_deleted was False
    assert len(mock_controller.batches) == 1
    assert mock_controller.batches == [[0, 1]]
