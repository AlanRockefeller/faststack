import pytest
from unittest.mock import MagicMock, patch
import shutil


# Create a dummy fixture for AppController that uses the real class but mocks dependencies
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
        # Mock UI state
        controller.ui_state = MagicMock()
        controller.ui_state.isHistogramVisible = False

        return controller


class TestRestoreFromRecycleBinSafe:
    def test_restore_success(self, app_controller, tmp_path):
        """Test successful restore."""
        bin_file = tmp_path / "bin.jpg"
        bin_file.write_text("data")
        dest_file = tmp_path / "restored.jpg"

        success, reason = app_controller._restore_from_recycle_bin_safe(
            dest_file, bin_file
        )

        assert success is True
        assert reason == "ok"
        assert dest_file.exists()
        assert not bin_file.exists()

    def test_missing_bin_file(self, app_controller, tmp_path):
        """Test restore fails if bin file is missing."""
        bin_file = tmp_path / "missing.jpg"
        dest_file = tmp_path / "restored.jpg"

        success, reason = app_controller._restore_from_recycle_bin_safe(
            dest_file, bin_file
        )

        assert success is False
        assert reason == "missing_in_bin"
        assert not dest_file.exists()

    def test_dest_exists(self, app_controller, tmp_path):
        """Test restore fails if destination already exists."""
        bin_file = tmp_path / "bin.jpg"
        bin_file.write_text("bin data")
        dest_file = tmp_path / "existing.jpg"
        dest_file.write_text("existing data")

        success, reason = app_controller._restore_from_recycle_bin_safe(
            dest_file, bin_file
        )

        assert success is False
        assert reason == "dest_exists"
        assert bin_file.exists()  # Should not touch bin file

    def test_permission_error(self, app_controller, tmp_path):
        """Test handling of OSError during move."""
        bin_file = tmp_path / "bin.jpg"
        bin_file.write_text("data")
        dest_file = tmp_path / "restored.jpg"

        with patch("shutil.move", side_effect=OSError("Permission denied")):
            success, reason = app_controller._restore_from_recycle_bin_safe(
                dest_file, bin_file
            )

        assert success is False
        assert reason == "move_failed"
        assert bin_file.exists()


class TestUndoDeleteAtomicity:
    def test_undo_delete_success(self, app_controller, tmp_path):
        """Test undo delete successfully restores both JPG and RAW."""
        # Setup paths
        jpg_src = tmp_path / "img.jpg"
        jpg_bin = tmp_path / "bin_img.jpg"
        raw_src = tmp_path / "img.orf"
        raw_bin = tmp_path / "bin_img.orf"

        jpg_bin.write_text("jpg")
        raw_bin.write_text("raw")

        # Setup history
        action_data = ((jpg_src, jpg_bin), (raw_src, raw_bin))
        app_controller.undo_history.append(("delete", action_data, 12345))
        app_controller.delete_history.append(action_data)

        # Mock refresh methods to avoid complex logic
        app_controller._post_undo_refresh_and_select = MagicMock()

        # Execute
        app_controller.undo_delete()

        # Verify
        assert jpg_src.exists()
        assert raw_src.exists()
        assert not jpg_bin.exists()
        assert not raw_bin.exists()
        # History should be empty
        assert len(app_controller.delete_history) == 0
        assert len(app_controller.undo_history) == 0

    def test_undo_delete_raw_exists_strategy(self, app_controller, tmp_path):
        """Test that if RAW exists, JPG is kept (no rollback) and user is warned."""
        jpg_src = tmp_path / "img.jpg"
        jpg_bin = tmp_path / "bin_img.jpg"
        raw_src = tmp_path / "img.orf"
        raw_bin = tmp_path / "bin_img.orf"

        jpg_bin.write_text("jpg")
        raw_bin.write_text("raw")
        raw_src.write_text("existing raw")  # RAW already exists

        action_data = ((jpg_src, jpg_bin), (raw_src, raw_bin))
        app_controller.undo_history.append(("delete", action_data, 12345))
        app_controller.delete_history.append(action_data)

        app_controller._post_undo_refresh_and_select = MagicMock()

        app_controller.undo_delete()

        # Verify JPG restored
        assert jpg_src.exists()
        # Verify RAW still exists (wasn't overwritten, wasn't moved from bin)
        assert raw_src.read_text() == "existing raw"
        assert raw_bin.exists()  # Bin copy remains

        # History cleared because it was considered a partial success
        assert len(app_controller.delete_history) == 0
        assert len(app_controller.undo_history) == 0

    def test_undo_delete_raw_move_fails_rollback(self, app_controller, tmp_path):
        """Test that if RAW move fails (OSError), JPG is rolled back."""
        jpg_src = tmp_path / "img.jpg"
        jpg_bin = tmp_path / "bin_img.jpg"
        raw_src = tmp_path / "img.orf"
        raw_bin = tmp_path / "bin_img.orf"

        jpg_bin.write_text("jpg")
        raw_bin.write_text("raw")

        action_data = ((jpg_src, jpg_bin), (raw_src, raw_bin))
        app_controller.undo_history.append(("delete", action_data, 12345))
        app_controller.delete_history.append(action_data)

        # Mock shutil.move to fail ONLY for raw
        original_move = shutil.move

        def side_effect(src, dst):
            if str(src) == str(raw_bin):
                raise OSError("Simulated failure")
            return original_move(src, dst)

        with patch("shutil.move", side_effect=side_effect):
            app_controller.undo_delete()

        # Verify JPG rolled back (exists in bin, not src)
        assert not jpg_src.exists()
        assert jpg_bin.exists()

        # Verify RAW failed
        assert raw_bin.exists()
        assert not raw_src.exists()

        # Verify history restored (nothing popped permanently)
        assert len(app_controller.delete_history) == 1
        assert len(app_controller.undo_history) == 1


class TestHistoryConsistency:
    def test_malformed_history_ignored(self, app_controller):
        """Test that malformed history actions don't crash the app."""
        # Action data with missing tuple elements
        malformed_data = ("just one thing",)
        app_controller.undo_history.append(("delete", malformed_data, 12345))

        app_controller.undo_delete()

        # Should handle exception and return
        assert len(app_controller.undo_history) == 0  # Popped but failed
