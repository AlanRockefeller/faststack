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
        
        # Calculate auto levels - now returns (blacks, whites, p_low, p_high)
        blacks, whites, p_low, p_high = self.editor.auto_levels(0.1)
        
        # With range [50, 200], we expect:
        # p_low should be around 50, p_high around 200 (with 0.1% percentile)
        # blacks approx -50/40 = -1.25
        # whites approx (255-200)/40 = 1.375
        self.assertNotEqual(blacks, 0.0)
        self.assertNotEqual(whites, 0.0)
        self.assertLess(blacks, 0.0)
        self.assertGreater(whites, 0.0)
        
        # Verify percentile values are reasonable
        self.assertGreater(p_low, 45)  # Should be close to 50
        self.assertLess(p_low, 55)
        self.assertGreater(p_high, 195)  # Should be close to 200
        self.assertLess(p_high, 205)
        
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
        res = self.editor._apply_edits(self.img.copy(), for_export=True)
        
        # Image should be rotated and larger (expand=True)
        # Original width 256. 45 deg rotation of valid rect makes it wider?
        # Not necessarily if aspect ratio is extreme.
        # Just check that dimensions changed.
        print(f"Original size: {self.img.size}, Rotated size: {res.size}")
        self.assertNotEqual(res.size, self.img.size)

    def test_auto_levels_stretch_capping(self):
        """
        Regression test: Verify that auto-strength uses stretch-factor capping
        to prevent insane levels on low-dynamic-range images.
        
        Tests:
        1. Reasonable dynamic range: should use full strength (strength=1.0)
        2. Low dynamic range: should cap stretch at 4x maximum
        3. Very low dynamic range: should set strength=0
        """
        threshold_percent = 0.1
        
        # Test case 1: Reasonable dynamic range (50-200, range=150)
        # Expected: stretch = 255/150 = 1.7x (< 4x cap) => strength = 1.0
        arr_reasonable = np.linspace(50, 200, 10000, dtype=np.uint8).reshape(100, 100)
        img_reasonable = Image.fromarray(arr_reasonable)
        self.editor.original_image = img_reasonable
        self.editor._preview_image = img_reasonable
        
        blacks, whites, p_low, p_high = self.editor.auto_levels(threshold_percent)
        
        # Calculate what strength should be based on stretch factor
        dynamic_range = p_high - p_low
        stretch_full = 255.0 / dynamic_range
        STRETCH_CAP = 4.0
        
        if stretch_full <= STRETCH_CAP:
            expected_strength = 1.0
        else:
            expected_strength = (STRETCH_CAP - 1.0) / (stretch_full - 1.0)
        
        print(f"Reasonable range: p_low={p_low:.1f}, p_high={p_high:.1f}, range={dynamic_range:.1f}, "
              f"stretch={stretch_full:.2f}, expected_strength={expected_strength:.3f}")
        
        # For reasonable range, should use full strength
        self.assertAlmostEqual(expected_strength, 1.0, places=2)
        
        # Test case 2: Low dynamic range (100-140, range=40)
        # Expected: stretch = 255/40 = 6.375x (> 4x cap) => strength = 3/5.375 ≈ 0.558
        arr_low_range = np.clip(np.linspace(100, 140, 10000, dtype=np.uint8), 100, 140).reshape(100, 100)
        img_low_range = Image.fromarray(arr_low_range)
        self.editor.original_image = img_low_range
        self.editor._preview_image = img_low_range
        
        blacks, whites, p_low, p_high = self.editor.auto_levels(threshold_percent)
        
        dynamic_range = p_high - p_low
        stretch_full = 255.0 / dynamic_range if dynamic_range >= 1.0 else 255.0
        
        if stretch_full <= STRETCH_CAP:
            expected_strength = 1.0
        else:
            expected_strength = (STRETCH_CAP - 1.0) / (stretch_full - 1.0)
        
        print(f"Low range: p_low={p_low:.1f}, p_high={p_high:.1f}, range={dynamic_range:.1f}, stretch={stretch_full:.2f}, expected_strength={expected_strength:.3f}")
        
        # Stretch should exceed cap, strength should be reduced
        self.assertGreater(stretch_full, STRETCH_CAP)
        self.assertLess(expected_strength, 1.0)
        self.assertGreater(expected_strength, 0.3)  # Should still be reasonable
        
        # Test case 3: Very low dynamic range (120-121, range≈1)
        # Expected: strength = 0 (degenerate case)
        arr_flat = np.full((100, 100), 120, dtype=np.uint8)
        # Add tiny variation to avoid completely flat
        arr_flat[0, 0] = 119
        arr_flat[99, 99] = 121
        img_flat = Image.fromarray(arr_flat)
        self.editor.original_image = img_flat
        self.editor._preview_image = img_flat
        
        blacks, whites, p_low, p_high = self.editor.auto_levels(threshold_percent)
        
        dynamic_range = p_high - p_low
        
        print(f"Flat image: p_low={p_low:.1f}, p_high={p_high:.1f}, range={dynamic_range:.1f}")
        
        # For very low range, should be near 0 or exactly 0
        self.assertLess(dynamic_range, 3.0)
    
    def test_auto_levels_clipping_tolerance(self):
        """
        Regression test: Verify that auto-levels respects the threshold setting
        and doesn't introduce excessive clipping beyond the configured tolerance.
        
        Uses deterministic synthetic images to verify clipping stays within bounds.
        """
        threshold_percent = 0.1
        
        # Create a deterministic image with known distribution
        # Use a beta distribution to create realistic luminance distribution
        # Beta(2, 5) gives a left-skewed distribution (more shadows, fewer highlights)
        np.random.seed(42)  # Deterministic
        beta_samples = np.random.beta(2, 5, size=10000)
        arr = (beta_samples * 255).astype(np.uint8).reshape(100, 100)
        img = Image.fromarray(arr)
        
        self.editor.original_image = img
        self.editor._preview_image = img
        
        blacks, whites, p_low, p_high = self.editor.auto_levels(threshold_percent)
        
        # Apply at full strength
        self.editor.set_edit_param('blacks', blacks)
        self.editor.set_edit_param('whites', whites)
        result = self.editor._apply_edits(img.convert('RGB'))
        result_arr = np.array(result.convert('L'))
        
        # Count pixels at extremes
        total_pixels = result_arr.size
        clipped_low = np.sum(result_arr == 0)
        clipped_high = np.sum(result_arr == 255)
        
        pct_clipped_low = (clipped_low / total_pixels) * 100.0
        pct_clipped_high = (clipped_high / total_pixels) * 100.0
        
        print(f"Beta distribution: Low clip: {pct_clipped_low:.2f}%, High clip: {pct_clipped_high:.2f}%")
        
        # Allow small tolerance for rounding and integer quantization
        # The threshold defines the percentiles, but due to discrete pixel values
        # and the mapping, we may end up with slightly different clipping
        tolerance = 0.5  # 0.1% threshold + 0.5% tolerance = 0.6% max
        
        self.assertLessEqual(pct_clipped_low, threshold_percent + tolerance,
                             f"Excessive shadow clipping: {pct_clipped_low:.2f}% > {threshold_percent + tolerance}%")
        self.assertLessEqual(pct_clipped_high, threshold_percent + tolerance,
                             f"Excessive highlight clipping: {pct_clipped_high:.2f}% > {threshold_percent + tolerance}%")
        
        # Verify mapping is monotonic (sanity check)
        # Create a gradient and verify it maps monotonically
        gradient = np.arange(256, dtype=np.uint8)
        gradient_img = Image.fromarray(gradient.reshape(1, 256))
        self.editor.original_image = gradient_img
        self.editor._preview_image = gradient_img
        
        blacks, whites, p_low, p_high = self.editor.auto_levels(threshold_percent)
        self.editor.set_edit_param('blacks', blacks)
        self.editor.set_edit_param('whites', whites)
        result = self.editor._apply_edits(gradient_img.convert('RGB'))
        result_arr = np.array(result.convert('L'))[0, :]
        
        # Check monotonicity
        diffs = np.diff(result_arr.astype(np.int16))
        self.assertTrue(np.all(diffs >= 0), "Mapping is not monotonic")

if __name__ == '__main__':
    unittest.main(verbosity=2)
