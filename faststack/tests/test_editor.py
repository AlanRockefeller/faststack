import os
import unittest
from PIL import Image
try:
    from pytest import approx
except ImportError:
    # Minimal approximation helper
    def approx(val, rel=None, abs=None):
        class Approx:
            def __init__(self, expected):
                self.expected = expected
            def __eq__(self, other):
                return abs_val(self.expected - other) <= (abs or 1e-6)
        return Approx(val)
        
    def abs_val(x):
        return x if x >= 0 else -x

from faststack.imaging.editor import ImageEditor

class TestEditor(unittest.TestCase):

    def test_save_image_preserves_mtime(self):
        import tempfile
        from pathlib import Path
        import shutil
        
        tmp_dir = tempfile.mkdtemp()
        try:
            tmp_path = Path(tmp_dir)
            
            img_path = tmp_path / "sample.jpg"
            Image.new("RGB", (4, 4), color=(10, 20, 30)).save(img_path)

            preserved_time = 1_600_000_000  # stable integer timestamp
            os.utime(img_path, (preserved_time, preserved_time))

            editor = ImageEditor()
            self.assertTrue(editor.load_image(str(img_path)))
            editor.set_edit_param('brightness', 0.1)

            saved = editor.save_image()
            self.assertIsNotNone(saved)
            saved_path, backup_path = saved

            self.assertEqual(str(saved_path), str(img_path))
            self.assertTrue(backup_path.exists())

            # Check within 2 seconds
            st = img_path.stat()
            self.assertTrue(abs(st.st_mtime - preserved_time) < 2)
        finally:
            shutil.rmtree(tmp_dir)

    def test_texture_edit(self):
        editor = ImageEditor()
        import tempfile
        from pathlib import Path
        import shutil
        try:
            import cv2
        except ImportError:
            cv2 = None
        
        if cv2 is None:
            self.skipTest("OpenCV not installed, skipping texture test")

        import numpy as np
        
        tmp_dir = tempfile.mkdtemp()
        try:
            tmp_path = Path(tmp_dir)
            img_path = tmp_path / "texture_test.jpg"
            # Create image with some detail (checkerboard)
            arr = np.zeros((20, 20, 3), dtype=np.uint8)
            arr[::2, ::2] = 255
            Image.fromarray(arr).save(img_path)
            
            self.assertTrue(editor.load_image(str(img_path)))
            
            # Baseline
            orig_arr = editor.float_image.copy()
            preview_orig = editor._apply_edits(orig_arr.copy())
            
            # Apply Texture
            editor.set_edit_param('texture', 0.5)
            preview_tex = editor._apply_edits(orig_arr.copy())
            
            # Should be different
            # Depending on how texture works, mean might shift slightly or just variance.
            # But the arrays should not be identical.
            self.assertFalse(np.allclose(preview_orig, preview_tex))
            
        finally:
            shutil.rmtree(tmp_dir)
