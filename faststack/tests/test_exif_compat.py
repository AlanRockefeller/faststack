import unittest
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path
from PIL import Image, ExifTags
import numpy as np

# Ensure project root is in sys.path
project_root = str(Path(__file__).parents[1])
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Pre-mock modules that might cause issues or aren't needed for this test
sys.modules["cv2"] = MagicMock()
# Mock faststack.models since it's used by editor.py
mock_models = MagicMock()
sys.modules["faststack.models"] = mock_models

from faststack.imaging.editor import ImageEditor, sanitize_exif_orientation


class TestExifCompat(unittest.TestCase):
    def setUp(self):
        self.editor = ImageEditor()
        # Create a dummy image for testing
        self.editor.original_image = Image.new("RGB", (10, 10))
        self.editor._source_exif_bytes = b"dummy exif bytes"

    def test_missing_image_exif_attribute(self):
        """Test fallback when PIL.Image.Exif is missing."""
        # Patching PIL.Image.Exif to raise AttributeError on access simulates it being missing
        with patch("PIL.Image.Exif", side_effect=AttributeError):
            # Also mock getexif to verify it's the fallback
            self.editor.original_image.getexif = MagicMock(return_value=None)
            self.editor._get_sanitized_exif_bytes()
            self.editor.original_image.getexif.assert_called_once()

    def test_missing_load_method(self):
        """Test fallback when Exif object has no load() method."""
        mock_exif_instance = MagicMock()
        del mock_exif_instance.load

        with patch("PIL.Image.Exif", return_value=mock_exif_instance):
            # Should fall back to original_image.getexif()
            self.editor.original_image.getexif = MagicMock(return_value=None)
            self.editor._get_sanitized_exif_bytes()
            self.editor.original_image.getexif.assert_called_once()

    def test_missing_tobytes_method(self):
        """Test graceful failure when Exif object has no tobytes() method."""
        mock_exif_instance = MagicMock()
        if hasattr(mock_exif_instance, "tobytes"):
            del mock_exif_instance.tobytes

        # Mocking getexif to return this broken instance
        self.editor.original_image.getexif = MagicMock(return_value=mock_exif_instance)
        # Set source bytes to verify they are NOT used as fallback (safer policy)
        self.editor._source_exif_bytes = b"fallback bytes"

        res = self.editor._get_sanitized_exif_bytes()
        self.assertIsNone(
            res, "Should return None if tobytes() is missing to prevent rotation issues"
        )

    def test_tobytes_failure_drops_exif(self):
        """Verify that failure in tobytes() now returns None (drops EXIF)."""
        mock_exif = MagicMock()
        mock_exif.tobytes.side_effect = Exception("failed to serialize")

        # Patch Image.Exif to return our mock
        with patch("PIL.Image.Exif", return_value=mock_exif):
            # Set source bytes
            self.editor._source_exif_bytes = b"fallback bytes"

            res = self.editor._get_sanitized_exif_bytes()
            self.assertIsNone(
                res, "Should return None if tobytes() fails to prevent rotation issues"
            )

    def test_missing_exiftags_base(self):
        """Test fallback when ExifTags.Base is missing (older Pillow)."""
        # Patch PIL.ExifTags to be a mock that does NOT have 'Base'
        # This will cause ExifTags.Base to raise AttributeError
        with patch("PIL.ExifTags", spec=[]):
            # Use a mock that doesn't restrict attributes, but has tobytes
            mock_exif = MagicMock()
            mock_exif.tobytes = MagicMock(return_value=b"serialized exif")
            self.editor.original_image.getexif = MagicMock(return_value=mock_exif)
            self.editor._source_exif_bytes = None

            res = self.editor._get_sanitized_exif_bytes()
            # Check if it tried to set 0x0112 (the fallback)
            mock_exif.__setitem__.assert_called_with(0x0112, 1)
            self.assertEqual(res, b"serialized exif")

    def test_sanitize_exif_orientation_helper(self):
        """Test the standalone sanitize_exif_orientation helper."""
        # 1. Valid EXIF with Orientation=6
        img = Image.new("RGB", (10, 10))
        exif = img.getexif()
        # Use fallback if Base is not available in test env (just in case)
        orientation_tag = getattr(ExifTags.Base, "Orientation", 0x0112)
        exif[orientation_tag] = 6
        exif_bytes = exif.tobytes()

        sanitized = sanitize_exif_orientation(exif_bytes)
        self.assertIsNotNone(sanitized)

        # Verify it's now 1
        loaded_exif = Image.Exif()
        loaded_exif.load(sanitized)
        self.assertEqual(loaded_exif[orientation_tag], 1)

        # 2. None input
        self.assertIsNone(sanitize_exif_orientation(None))

        # 3. Invalid bytes
        self.assertIsNone(sanitize_exif_orientation(b"invalid junk"))

    def test_save_uses_sanitizer_for_sidecar(self):
        """Verify save_image calls sanitizer for sidecar when rotation baked in."""
        # Setup: source bytes present, edits imply rotation (or not, since we always bake now)
        self.editor._source_exif_bytes = b"source_bytes"
        self.editor.current_filepath = Path("test.jpg")
        self.editor.float_image = np.zeros((10, 10, 3), dtype=np.float32)

        # Mock dependencies specifically for this test
        with (
            patch(
                "faststack.imaging.editor.sanitize_exif_orientation"
            ) as mock_sanitize,
            patch(
                "faststack.imaging.editor.create_backup_file",
                return_value=Path("test-backup.jpg"),
            ),
            patch("PIL.Image.fromarray") as mock_fromarray,
            patch.object(self.editor, "_write_tiff_16bit") as mock_tiff,
        ):
            mock_img = MagicMock()
            mock_fromarray.return_value = mock_img

            # Action: Save with sidecar
            self.editor.save_image(write_developed_jpg=True)

            # Assert sanitizer was called with source bytes
            mock_sanitize.assert_called_with(b"source_bytes")


if __name__ == "__main__":
    unittest.main()
