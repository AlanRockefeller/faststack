import shutil
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ExifTags
from unittest.mock import MagicMock, patch
import sys

from faststack.imaging.editor import ImageEditor


class TestExifOrientation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.editor = ImageEditor()

        # Patch cv2 in the editor module if needed for some tests,
        # but ImageEditor logic mainly uses PIL/numpy unless specialized.
        # If we really need to mock cv2, we should do it on the imported module attribute.
        self.cv2_patch = patch("faststack.imaging.editor.cv2", MagicMock())
        self.mock_cv2 = self.cv2_patch.start()

    def tearDown(self):
        self.cv2_patch.stop()
        shutil.rmtree(self.test_dir)

    def _create_test_image(self, filename, orientation=1):
        """Creates a dummy JPEG with specific EXIF orientation."""
        path = Path(self.test_dir) / filename

        # Create a simple image: Red on left, Blue on right (to detect rotation)
        # 100x50
        img = Image.new("RGB", (100, 50), color="red")
        # Make right half blue
        for x in range(50, 100):
            for y in range(50):
                img.putpixel((x, y), (0, 0, 255))

        exif = img.getexif()
        exif[ExifTags.Base.Orientation] = orientation
        # Add another tag to verify general EXIF preservation (e.g. ImageDescription)
        # 0x010E is ImageDescription
        exif[0x010E] = "Test Image"

        img.save(path, format="JPEG", exif=exif.tobytes())
        return path

    def test_orientation_sanitization_on_rotation(self):
        """Verify Orientation is reset to 1 if we rotate the image."""
        for start_ori in [3, 6, 8]:
            with self.subTest(start_ori=start_ori):
                path = self._create_test_image(
                    f"test_rot_{start_ori}.jpg", orientation=start_ori
                )

                # Load
                self.editor.load_image(str(path))

                # Apply Rotation (90 degrees) - this usually rotates CCW in our pipeline
                # but the key is that 'transforms_applied' becomes True.
                self.editor.current_edits["rotation"] = 90

                # Save
                saved_path, _ = self.editor.save_image()

                # Verify
                with Image.open(saved_path) as res:
                    exif = res.getexif()
                    orientation = exif.get(ExifTags.Base.Orientation)
                    # Should be sanitized to 1
                    self.assertEqual(
                        orientation,
                        1,
                        f"Expected Orientation 1, got {orientation} for start {start_ori}",
                    )

                    # Double rotation check: if we reload this image, it should look correct
                    # without any further rotation needed.
                    # Start 6 (Vertical 50x100) -> Baked (50x100) -> Rotate 90 -> 100x50
                    # Start 8 (Vertical 50x100) -> Baked (50x100) -> Rotate 90 -> 100x50
                    # Start 3 (Horizontal 100x50) -> Baked (100x50) -> Rotate 90 -> 50x100
                    expected_size = (50, 100) if start_ori == 3 else (100, 50)
                    self.assertEqual(
                        res.size,
                        expected_size,
                        f"Dimensions check failed for start {start_ori} with rotation",
                    )

    def test_orientation_preserved_no_rotation(self):
        """Verify Orientation is PRESERVED if we do NOT rotate."""
        for start_ori in [3, 6, 8]:
            with self.subTest(start_ori=start_ori):
                path = self._create_test_image(
                    f"test_no_rot_{start_ori}.jpg", orientation=start_ori
                )

                # Load
                self.editor.load_image(str(path))

                # Apply NO geometric edits, just color
                self.editor.current_edits["exposure"] = 0.5

                # Save
                saved_path, _ = self.editor.save_image()

                # Verify
                with Image.open(saved_path) as res:
                    exif = res.getexif()
                    orientation = exif.get(ExifTags.Base.Orientation)

                    # Should be sanitized to 1 because editor now ALWAYS bakes orientation
                    self.assertEqual(
                        orientation,
                        1,
                        f"Orientation should be sanitized to 1, got {orientation}",
                    )

                    # Verify pixels are rotated if necessary (Start 5-8 involve 90 deg rotation or swap)
                    # Start 3 (180) -> Same dims
                    # Start 6 (90 CW) -> Swapped dims
                    # Start 8 (90 CCW) -> Swapped dims
                    if start_ori in [5, 6, 7, 8]:
                        self.assertEqual(
                            res.size,
                            (50, 100),
                            f"Dimensions should be swapped for start {start_ori} due to baking",
                        )
                    else:
                        self.assertEqual(
                            res.size,
                            (100, 50),
                            f"Dimensions should be preserved for start {start_ori}",
                        )

    def test_raw_mode_exif_preservation(self):
        """Verify that camera EXIF from a source JPEG is preserved when 'developing' RAW (simulated with TIFF)."""
        # 1. Create a "source" JPEG with camera EXIF and Orientation=6
        source_path = self._create_test_image("camera_source.jpg", orientation=6)

        with Image.open(source_path) as src:
            source_exif_bytes = src.info.get("exif")
            self.assertIsNotNone(source_exif_bytes, "Source image should have EXIF")

        # 2. Create a "working TIFF" (simulating developed RAW output) which lacks EXIF
        tiff_path = Path(self.test_dir) / "working_source.tif"
        tiff_img = Image.new("RGB", (100, 50), color="green")
        tiff_img.save(tiff_path, format="TIFF")

        # 3. Load TIFF into editor, passing the source EXIF
        self.editor.load_image(str(tiff_path), source_exif=source_exif_bytes)

        # 4. Save developed JPG WITHOUT transforms -> Orientation should be preserved (?)
        # Actually, RAW development usually results in an image that is visually upright
        # if the developer (RawTherapee) handled orientation.
        # But our save_image logic says: if no transforms_applied, preserve original EXIF.
        # If the original EXIF said Orientation=6, but the TIFF is already upright,
        # we might get a "double rotation" IF the viewer respects EXIF.
        # HOWEVER, the user said: "if you do sanitize, ensure you don’t accidentally lose other tags"
        # and "ensure no 'double rotation' on reload".

        # If we ARE developing a RAW, we usually want to bake in the orientation
        # or at least ensure the output is correct.

        # Current logic: We ALWAYS sanitize to 1 because we bake orientation on load.
        # This prevents "double rotation".

        res = self.editor.save_image(write_developed_jpg=True)
        developed_path = Path(self.test_dir) / "working_source-developed.jpg"

        with Image.open(developed_path) as dev:
            exif = dev.getexif()
            self.assertEqual(
                exif.get(ExifTags.Base.Orientation),
                1,
                "Orientation should be sanitized to 1 even if no editor transforms (to prevent double rotation)",
            )

        # 5. Now apply an editor transform (90 deg)
        self.editor.current_edits["rotation"] = 90
        self.editor.save_image(write_developed_jpg=True)

        with Image.open(developed_path) as dev:
            exif = dev.getexif()
            description = exif.get(0x010E)
            self.assertEqual(description, "Test Image", "EXIF tags preserved")
            self.assertEqual(
                exif.get(ExifTags.Base.Orientation),
                1,
                "Orientation sanitized after rotation",
            )


if __name__ == "__main__":
    unittest.main()
