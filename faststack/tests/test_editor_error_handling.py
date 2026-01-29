import sys
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from pathlib import Path

# We need to mock cv2 before importing editor if it's not already imported,
# but since tests run in the same process, we just rely on patching.


class TestEditorErrorHandling(unittest.TestCase):
    """Test ImageEditor error handling for load and save operations."""

    def test_load_image_returns_false_on_failure(self):
        """Ensure load_image returns False when file opening fails."""
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        # Patch Image.open to raise an exception
        with patch("PIL.Image.open", side_effect=OSError("Mocked file error")):
            # We also need to ensure cv2 doesn't rescue it.
            # If cv2 exists, it might try to load.
            # Let's mock cv2.imread to return None so it falls back to PIL, which fails.

            with patch.dict(sys.modules, {"cv2": MagicMock()}):
                sys.modules["cv2"].imread.return_value = None

                # load_image returns False on failure, not raises
                result = editor.load_image("non_existent_file.jpg")
                self.assertFalse(result)

    def test_save_image_raises_runtime_error_on_failure(self):
        """Ensure save_image raises RuntimeError when saving fails."""
        from faststack.imaging.editor import ImageEditor

        editor = ImageEditor()

        # Setup a fake state so save_image attempts to run
        editor.float_image = np.zeros((10, 10, 3), dtype=np.float32)
        editor.current_filepath = Path("fake_path.jpg")

        # Patch create_backup_file to succeed
        with patch(
            "faststack.imaging.editor.create_backup_file",
            return_value=Path("backup.jpg"),
        ):
            # Patch Image.fromarray to return a mock that fails to save
            mock_img = MagicMock()
            mock_img.save.side_effect = PermissionError("Mocked save error")

            with patch("PIL.Image.fromarray", return_value=mock_img):
                # save_image wraps exceptions in RuntimeError
                with self.assertRaises(RuntimeError) as cm:
                    editor.save_image()

                self.assertIn("Mocked save error", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
