
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from pathlib import Path
import sys

# Ensure faststack is in path
from faststack.imaging.editor import ImageEditor
from PIL import Image

class TestDebugError(unittest.TestCase):
    def test_debug_save_image(self):
        editor = ImageEditor()
        editor.float_image = np.zeros((10, 10, 3), dtype=np.float32)
        editor.current_filepath = Path("fake_path.jpg")
        # Need original_image to pass _ensure_float_image checks if called,
        # and for save logic
        editor.original_image = MagicMock()

        # Patch create_backup_file to succeed
        with patch("faststack.imaging.editor.create_backup_file", return_value=Path("backup.jpg")):
            # Patch Image.fromarray at the module level where it's used
            with patch("faststack.imaging.editor.Image.fromarray") as mock_fromarray:
                # Configure the mock object returned by fromarray
                mock_img = MagicMock()
                mock_img.save.side_effect = PermissionError("Mocked save error")
                mock_fromarray.return_value = mock_img

                # Expect RuntimeError because save_image catches exceptions and raises RuntimeError
                with self.assertRaises(RuntimeError):
                    editor.save_image()

    def test_save_image_raises_on_missing_float(self):
        editor = ImageEditor()
        editor.float_image = None
        editor.current_filepath = Path("fake.jpg")
        editor.original_image = MagicMock()
        
        # Simulate race: _ensure_float_image "thinks" it succeeded (or was raced), 
        # but float_image is actually None when we enter the lock.
        # We achieve this by silencing _ensure_float_image so it doesn't populate float_image.
        editor._ensure_float_image = MagicMock()

        # Should raise RuntimeError explicitly now (instead of returning None)
        with self.assertRaisesRegex(RuntimeError, "save_image called with no float_image"):
            editor.save_image()

if __name__ == "__main__":
    unittest.main()
