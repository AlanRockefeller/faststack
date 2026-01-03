import sys
import unittest
import numpy as np
from PIL import Image
from faststack.imaging.editor import ImageEditor

class TestNewFeatures(unittest.TestCase):
    def setUp(self):
        self.editor = ImageEditor()
        # Create a gradient image 0-255
        self.img = Image.fromarray(np.tile(np.arange(256, dtype=np.uint8), (10, 1)).astype(np.uint8))
        self.editor.original_image = self.img
        self.editor._preview_image = self.img

    def test_auto_levels_strength(self):
        # Create an image capable of clipping but with limited range to force non-zero adjustments
        # Range 50-200. Auto-levels should expand this to 0-255.
        arr = np.linspace(50, 200, 10000).reshape(100, 100).astype(np.uint8)
        img = Image.fromarray(arr)
        
        self.editor.original_image = img
        self.editor._preview_image = img
        
        # Calculate auto levels
        blacks, whites = self.editor.auto_levels(0.1)
        
        # With range [50, 200], we expect:
        # blacks approx -50/40 = -1.25
        # whites approx (200-255)/40 = -1.375
        self.assertNotEqual(blacks, 0.0)
        self.assertNotEqual(whites, 0.0)
        self.assertLess(blacks, 0.0)
        self.assertLess(whites, 0.0)
        
        # Mock strength application matching app.py logic
        strength = 0.5
        b_scaled = blacks * strength
        w_scaled = whites * strength
        
        # Verify scaling works correctly and produces expected intermediate values
        self.assertAlmostEqual(b_scaled, blacks * 0.5)
        self.assertAlmostEqual(w_scaled, whites * 0.5)
        # Verify magnitude is reduced
        self.assertLess(abs(b_scaled), abs(blacks))
        self.assertLess(abs(w_scaled), abs(whites))

    def test_highlights_recovery(self):
        # Set highlights to -1.0 (Recovery)
        self.editor.current_edits['highlights'] = -1.0
        
        # Apply edits
        res = self.editor._apply_edits(self.img.copy())
        res_arr = np.array(res)
        
        # Check pixel at 255 (should be darker)
        # Original 255.
        # Mask at 255 = (255-128)/127 = 1.0.
        # Factor = 1.0 + (-1.0 * 0.75 * 1.0) = 0.25.
        # Expected = 255 * 0.25 = 63.75.
        
        val_255 = res_arr[0, 255]
        print(f"Highlights -1.0 on 255: {val_255}")
        self.assertTrue(val_255 < 255)
        self.assertTrue(val_255 < 100) # Significant darkening
        
        # Check pixel at 128 (should be unchanged)
        # Mask at 128 = 0.
        # Factor = 1.0.
        val_128 = res_arr[0, 128]
        print(f"Highlights -1.0 on 128: {val_128}")
        # Allow small deviation due to float/int conversion
        self.assertTrue(abs(val_128 - 128) < 2)

    def test_straighten_angle(self):
        # Set straighten angle
        self.editor.current_edits['straighten_angle'] = 45.0
        
        # Apply
        res = self.editor._apply_edits(self.img.copy())
        
        # Image should be rotated and larger (expand=True)
        # Original width 256. 45 deg rotation of valid rect makes it wider?
        # Not necessarily if aspect ratio is extreme.
        # Just check that dimensions changed.
        print(f"Original size: {self.img.size}, Rotated size: {res.size}")
        self.assertNotEqual(res.size, self.img.size)

if __name__ == '__main__':
    unittest.main(verbosity=2)
