"""
Tests for hardened image loading logic in ImageEditor.
Specifically tests cv2.imread returning None, empty arrays, or invalid objects.

Note: cv2 is imported INSIDE the load_image() function, so we need to
patch sys.modules['cv2'] before the import happens.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
import tempfile
import os


class TestImageLoadingFallback(unittest.TestCase):
    """Test that ImageEditor gracefully falls back to PIL when cv2.imread fails."""

    def setUp(self):
        """Set up a fresh ImageEditor for each test."""
        self.temp_files = []

    def tearDown(self):
        """Clean up temp files."""
        for f in self.temp_files:
            try:
                os.unlink(f)
            except (OSError, PermissionError):
                pass

    def _create_temp_image(self, color="red"):
        """Create a temporary image file and return its path."""
        from PIL import Image

        fd, temp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)  # Close the file descriptor so PIL can write to it
        img = Image.new("RGB", (10, 10), color=color)
        img.save(temp_path)
        self.temp_files.append(temp_path)
        return temp_path

    def _run_with_mocked_cv2(self, imread_return_value, temp_path):
        """Run load_image with a mocked cv2 module."""
        # Create a mock cv2 module
        mock_cv2 = MagicMock()
        mock_cv2.imread.return_value = imread_return_value
        mock_cv2.IMREAD_UNCHANGED = -1

        # Patch cv2 in sys.modules before importing editor
        with patch.dict(sys.modules, {"cv2": mock_cv2}):
            # Force reimport of editor to pick up the mocked cv2
            if "faststack.imaging.editor" in sys.modules:
                del sys.modules["faststack.imaging.editor"]
            from faststack.imaging.editor import ImageEditor

            editor = ImageEditor()
            result = editor.load_image(temp_path)
            return editor, result

    def test_imread_returns_none(self):
        """cv2.imread returning None should fall back to PIL."""
        temp_path = self._create_temp_image("red")
        editor, result = self._run_with_mocked_cv2(None, temp_path)

        self.assertTrue(result, "load_image should succeed with PIL fallback")
        self.assertEqual(editor.bit_depth, 8, "Should fall back to 8-bit")
        self.assertIsNotNone(editor.float_image, "float_image should be set")

    def test_imread_returns_empty_array(self):
        """cv2.imread returning an empty array should fall back to PIL."""
        temp_path = self._create_temp_image("blue")
        editor, result = self._run_with_mocked_cv2(np.array([]), temp_path)

        self.assertTrue(result, "load_image should succeed with PIL fallback")
        self.assertEqual(editor.bit_depth, 8, "Should fall back to 8-bit")

    def test_imread_returns_non_array(self):
        """cv2.imread returning a non-array object should fall back to PIL."""
        temp_path = self._create_temp_image("green")
        editor, result = self._run_with_mocked_cv2("not an array", temp_path)

        self.assertTrue(result, "load_image should succeed with PIL fallback")
        self.assertEqual(editor.bit_depth, 8, "Should fall back to 8-bit")


if __name__ == "__main__":
    unittest.main()
