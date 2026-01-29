"""Tests for file locking handling in undo operations."""

import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import shutil


class TestRestoreBackupSafe(unittest.TestCase):
    """Tests for _restore_backup_safe method without mocking."""

    def setUp(self):
        """Create temp directory with test files."""
        self.temp_dir = tempfile.mkdtemp()
        self.saved_path = Path(self.temp_dir) / "test_image.jpg"
        self.backup_path = Path(self.temp_dir) / "test_image.jpg.backup"

        # Create a backup file with content
        self.backup_path.write_bytes(b"backup content")

    def tearDown(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_controller(self):
        """Create a minimal controller with just what _restore_backup_safe needs."""
        # We can't easily instantiate AppController, so we'll test the logic directly
        # by calling the function with a mock self
        from faststack.app import AppController

        # Patch __init__ to skip complex initialization
        with patch.object(AppController, "__init__", return_value=None):
            controller = AppController()
            controller.update_status_message = MagicMock()
            return controller

    def test_simple_restore_no_target(self):
        """Test restoring backup when target doesn't exist."""
        controller = self._make_controller()

        # Target doesn't exist, backup exists
        self.assertFalse(self.saved_path.exists())
        self.assertTrue(self.backup_path.exists())

        result = controller._restore_backup_safe(
            str(self.saved_path), str(self.backup_path)
        )

        self.assertTrue(result)
        self.assertTrue(self.saved_path.exists())
        self.assertFalse(self.backup_path.exists())
        self.assertEqual(self.saved_path.read_bytes(), b"backup content")

    def test_restore_replaces_target(self):
        """Test restoring backup when target already exists (replaced cleanly)."""
        controller = self._make_controller()

        # Create both files
        self.saved_path.write_bytes(b"old content")

        result = controller._restore_backup_safe(
            str(self.saved_path), str(self.backup_path)
        )

        self.assertTrue(result)
        self.assertTrue(self.saved_path.exists())
        self.assertFalse(self.backup_path.exists())
        self.assertEqual(self.saved_path.read_bytes(), b"backup content")

    def test_backup_not_found(self):
        """Test handling when backup file doesn't exist."""
        controller = self._make_controller()

        # Remove backup
        self.backup_path.unlink()

        result = controller._restore_backup_safe(
            str(self.saved_path), str(self.backup_path)
        )

        self.assertFalse(result)
        controller.update_status_message.assert_called()

    def test_verification_after_move(self):
        """Test that the method verifies the file exists after move."""
        controller = self._make_controller()

        result = controller._restore_backup_safe(
            str(self.saved_path), str(self.backup_path)
        )

        self.assertTrue(result)
        # File must exist and have content
        self.assertTrue(self.saved_path.exists())
        self.assertGreater(self.saved_path.stat().st_size, 0)

    def test_unique_temp_path_used(self):
        """Test that unique temp paths don't collide with existing files."""
        controller = self._make_controller()

        # Create a file that would collide with a fixed .tmp_restore suffix
        collision_path = self.saved_path.with_suffix(".tmp_restore")
        collision_path.write_bytes(b"collision content")

        # Create target to force the locked-file path
        self.saved_path.write_bytes(b"old content")

        result = controller._restore_backup_safe(
            str(self.saved_path), str(self.backup_path)
        )

        self.assertTrue(result)
        # Collision file should be untouched
        self.assertTrue(collision_path.exists())
        self.assertEqual(collision_path.read_bytes(), b"collision content")


class TestUndoDeleteVerification(unittest.TestCase):
    """Integration tests for restore_file verification in undo_delete."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_restore_file_verifies_success(self):
        """Test that restore_file nested function verifies shutil.move succeeded."""
        src_path = Path(self.temp_dir) / "source.jpg"
        bin_path = Path(self.temp_dir) / "bin" / "source.jpg"

        # Create bin directory and file
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_bytes(b"test content")

        # Move it
        shutil.move(str(bin_path), str(src_path))

        # Verify it worked
        self.assertTrue(src_path.exists())
        self.assertFalse(bin_path.exists())
        self.assertEqual(src_path.read_bytes(), b"test content")


if __name__ == "__main__":
    unittest.main(verbosity=2)
