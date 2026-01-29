"""Tests for EXIF orientation correction during display."""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ExifTags

# Adjust path to import faststack
sys.path.insert(0, str(Path(__file__).parents[1]))

from faststack.imaging.prefetch import apply_exif_orientation


class TestExifDisplayOrientation(unittest.TestCase):
    """Tests for apply_exif_orientation function."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _create_test_image(self, filename: str, orientation: int) -> Path:
        """Creates a JPEG with a specific EXIF orientation.

        The image is 100x50 with red on left, blue on right.
        This makes it easy to verify rotation by checking pixel colors.
        """
        path = Path(self.test_dir) / filename

        # Create asymmetric image: 100w x 50h
        # Left half (0-49) = red, right half (50-99) = blue
        img = Image.new("RGB", (100, 50), color="red")
        for x in range(50, 100):
            for y in range(50):
                img.putpixel((x, y), (0, 0, 255))

        exif = img.getexif()
        exif[ExifTags.Base.Orientation] = orientation

        img.save(path, format="JPEG", exif=exif.tobytes())
        return path

    def test_orientation_1_no_change(self):
        """Orientation 1 (normal) should return unchanged buffer."""
        path = self._create_test_image("test_ori1.jpg", 1)

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        result = apply_exif_orientation(original.copy(), path)

        self.assertEqual(result.shape, original.shape)
        np.testing.assert_array_equal(result, original)

    def test_orientation_3_rotate_180(self):
        """Orientation 3 should rotate 180 degrees."""
        path = self._create_test_image("test_ori3.jpg", 3)

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        result = apply_exif_orientation(original.copy(), path)

        # Shape unchanged (still 50x100)
        self.assertEqual(result.shape, original.shape)

        # After 180 rotation, top-left should now be blue (was bottom-right)
        # Check that top-left pixel is blue
        self.assertTrue(result[0, 0, 2] > 200)  # Blue channel high
        self.assertTrue(result[0, 0, 0] < 50)  # Red channel low

    def test_orientation_6_rotate_90_cw(self):
        """Orientation 6 should rotate 90 degrees clockwise (270 CCW)."""
        path = self._create_test_image("test_ori6.jpg", 6)

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        result = apply_exif_orientation(original.copy(), path)

        # Dimensions should swap: 100x50 -> 50x100
        self.assertEqual(result.shape, (100, 50, 3))

        # After 90 CW rotation of [red-left, blue-right],
        # top should be red, bottom should be blue
        # Check top-left pixel is red
        self.assertTrue(result[0, 0, 0] > 200)  # Red channel high
        self.assertTrue(result[0, 0, 2] < 50)  # Blue channel low

    def test_orientation_8_rotate_90_ccw(self):
        """Orientation 8 should rotate 90 degrees counter-clockwise."""
        path = self._create_test_image("test_ori8.jpg", 8)

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        result = apply_exif_orientation(original.copy(), path)

        # Dimensions should swap: 100x50 -> 50x100
        self.assertEqual(result.shape, (100, 50, 3))

        # After 90 CCW rotation of [red-left, blue-right],
        # top should be blue, bottom should be red
        # Check top-left pixel is blue
        self.assertTrue(result[0, 0, 2] > 200)  # Blue channel high
        self.assertTrue(result[0, 0, 0] < 50)  # Red channel low

    def test_orientation_2_mirror_horizontal(self):
        """Orientation 2 should mirror horizontally."""
        path = self._create_test_image("test_ori2.jpg", 2)

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        result = apply_exif_orientation(original.copy(), path)

        # Shape unchanged
        self.assertEqual(result.shape, original.shape)

        # After horizontal flip, left becomes blue, right becomes red
        # Check top-left pixel is blue
        self.assertTrue(result[0, 0, 2] > 200)  # Blue channel high
        self.assertTrue(result[0, 0, 0] < 50)  # Red channel low

    def test_no_exif_returns_unchanged(self):
        """Image without EXIF should return unchanged buffer."""
        path = Path(self.test_dir) / "no_exif.jpg"

        # Create image without EXIF
        img = Image.new("RGB", (100, 50), color="green")
        img.save(path, format="JPEG")

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        result = apply_exif_orientation(original.copy(), path)

        np.testing.assert_array_equal(result, original)

    def test_invalid_path_returns_unchanged(self):
        """Non-existent file should return unchanged buffer."""
        path = Path(self.test_dir) / "nonexistent.jpg"

        dummy = np.zeros((50, 100, 3), dtype=np.uint8)
        result = apply_exif_orientation(dummy.copy(), path)

        np.testing.assert_array_equal(result, dummy)

    def test_orientation_contiguity(self):
        """Verify that the result is always C-contiguous after transformations."""
        # Orientation 6 involves rotation which often results in non-contiguous arrays
        path = self._create_test_image("test_contiguity.jpg", 6)

        with Image.open(path) as img:
            original = np.array(img.convert("RGB"))

        # Ensure input is contiguous
        self.assertTrue(original.flags["C_CONTIGUOUS"])

        result = apply_exif_orientation(original, path)

        # Verify the result is C-contiguous
        self.assertTrue(
            result.flags["C_CONTIGUOUS"],
            "Result of apply_exif_orientation should be C-contiguous",
        )


if __name__ == "__main__":
    unittest.main()
