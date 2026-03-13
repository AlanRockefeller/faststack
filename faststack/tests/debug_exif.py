import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import ExifTags, Image

from faststack.imaging.editor import ImageEditor, sanitize_exif_orientation


class TestDebugExif(unittest.TestCase):
    def test_sanitize_exif_orientation(self):
        """Verify sanitize_exif_orientation resets the orientation tag to 1."""
        # Create source image with Orientation 6 (Rotated 90 CW)
        img = Image.new("RGB", (100, 50), color="red")
        exif = img.getexif()
        exif[ExifTags.Base.Orientation] = 6
        exif_bytes = exif.tobytes()

        # Test sanitize_exif_orientation directly
        sanitized = sanitize_exif_orientation(exif_bytes)
        self.assertIsNotNone(sanitized)

        chk = Image.Exif()
        chk.load(sanitized)
        self.assertEqual(chk.get(ExifTags.Base.Orientation), 1)

    def test_editor_full_workflow_exif(self):
        """Verify ImageEditor workflow preserves and sanitizes EXIF in real file I/O."""
        tmp_dir = tempfile.mkdtemp()
        try:
            tmp_path = Path(tmp_dir)
            img_path = tmp_path / "test_exif_workflow.jpg"

            # 1. Create source file with EXIF Orientation 6
            img = Image.new("RGB", (100, 50), color="blue")
            exif = img.getexif()
            exif[ExifTags.Base.Orientation] = 6
            img.save(img_path, exif=exif)

            # 2. Load into editor
            editor = ImageEditor()
            self.assertTrue(editor.load_image(str(img_path)))

            # 3. Verify editor state
            self.assertIsNotNone(editor.float_image)
            # ImageEditor.load_image bakes orientation, so original (100x50) [WxH] orient 6 [90 CW]
            # becomes (50x100) [WxH]. In NumPy (H, W, C), this is (100, 50, 3).
            self.assertEqual(editor.float_image.shape[0], 100)  # Height
            self.assertEqual(editor.float_image.shape[1], 50)  # Width

            # 4. Apply edit and save
            editor.set_edit_param("brightness", 0.5)
            # This triggers backup and save
            saved = editor.save_image()
            self.assertIsNotNone(saved)
            saved_path, _ = saved

            # 5. Verify saved file has sterilized orientation
            with Image.open(saved_path) as out_img:
                out_exif = out_img.getexif()
                # Orientation should be 1 because we baked the rotation into the pixels
                self.assertEqual(out_exif.get(ExifTags.Base.Orientation), 1)
        finally:
            shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    unittest.main()
