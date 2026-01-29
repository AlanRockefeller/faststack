import unittest
import numpy as np
from unittest.mock import patch

# Import the functionality to test
from faststack.imaging import editor


class TestFallbackBlur(unittest.TestCase):
    def test_fallback_blur_logic(self):
        """Test that _gaussian_blur_float works even when cv2 is None"""

        # Setup a dummy float image (checkerboard)
        # 0.0 and 1.0 values
        arr = np.zeros((20, 20, 3), dtype=np.float32)
        arr[::2, ::2] = 1.0

        # Calculate expected "unblurred" std dev
        orig_std = np.std(arr)

        # Mock cv2 to be None to force fallback path
        with patch("faststack.imaging.editor.cv2", None):
            # Verify we are hitting the fallback
            self.assertIsNone(editor.cv2)

            # Run the blur function
            blurred = editor._gaussian_blur_float(arr, radius=2.0)

            # Check shape/type preservation
            self.assertEqual(blurred.shape, arr.shape)
            self.assertEqual(blurred.dtype, np.float32)

            # Check that it actually blurred
            # A blurred checkerboard should have lower standard deviation than the original
            new_std = np.std(blurred)
            print(f"Original Std: {orig_std:.4f}, Blurred Std: {new_std:.4f}")

            self.assertLess(
                new_std, orig_std, "Image should be blurred (lower variance)"
            )

            # Additional check: max value should decrease, min value should increase (for 0/1 checkerboard)
            self.assertLess(blurred.max(), 1.0)
            self.assertGreater(blurred.min(), 0.0)


if __name__ == "__main__":
    unittest.main()
