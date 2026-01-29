import unittest
import numpy as np
from faststack.imaging.editor import ImageEditor


class TestEditorRotation(unittest.TestCase):
    def setUp(self):
        self.editor = ImageEditor()
        self.editor.current_filepath = "dummy.jpg"

    def create_quadrant_image_float(self, w=100, h=100):
        # TL: Red (1, 0, 0)
        # TR: Green (0, 1, 0)
        # BL: Blue (0, 0, 1)
        # BR: White (1, 1, 1)
        arr = np.zeros((h, w, 3), dtype=np.float32)
        cx, cy = w // 2, h // 2

        # TL
        arr[:cy, :cx] = [1, 0, 0]
        # TR
        arr[:cy, cx:] = [0, 1, 0]
        # BL
        arr[cy:, :cx] = [0, 0, 1]
        # BR
        arr[cy:, cx:] = [1, 1, 1]

        return arr

    def test_rotate_cw(self):
        """Test CW rotation (90 deg clockwise)."""
        # Logic: (current - 90). np.rot90 k=1 is CCW.
        # CW = -90 = 270 CCW. k=3.

        arr = self.create_quadrant_image_float()

        # Manually set rotation to 270 (which is -90 CW)
        self.editor.current_edits["rotation"] = 270

        # Apply
        res = self.editor._apply_edits(arr.copy(), for_export=True)

        # Check Quadrants
        # TL (Red) -> TR
        # TR (Green) -> BR
        # BL (Blue) -> TL
        # BR (White) -> BL

        w, h = 100, 100
        qw, qh = 25, 25

        # New TL (was BL Blue)
        np.testing.assert_allclose(res[qh, qw], [0, 0, 1], err_msg="TL should be Blue")
        # New TR (was TL Red)
        np.testing.assert_allclose(
            res[qh, w - qw], [1, 0, 0], err_msg="TR should be Red"
        )
        # New BL (was BR White)
        np.testing.assert_allclose(
            res[h - qh, qw], [1, 1, 1], err_msg="BL should be White"
        )
        # New BR (was TR Green)
        np.testing.assert_allclose(
            res[h - qh, w - qw], [0, 1, 0], err_msg="BR should be Green"
        )

    def test_straighten_angle(self):
        """Test free rotation."""
        arr = np.zeros((100, 100, 3), dtype=np.float32)
        # Draw a horizontal line in middle
        arr[48:52, :, :] = 1.0

        # Rotate 90 degrees via straighten
        self.editor.current_edits["straighten_angle"] = 90.0
        # Should result in vertical line

        # Note: _rotate_float_image uses PIL rotate.
        # PIL rotate(angle) is Counter-Clockwise.
        # straighten_angle=90 -> call rotate(-90) -> Clockwise 90?
        # My implementation: `self._rotate_float_image(arr, -straighten_angle, expand=True)`
        # If straighten_angle is 90, we call rotate(-90).
        # rotate(-90) is Clockwise 90.
        # So horizontal line becomes vertical.

        res = self.editor._apply_edits(arr.copy(), for_export=True)

        # Check shape (expanded)
        # If expanded, and 90 deg, size should swap (but here 100x100 -> 100x100)
        self.assertEqual(res.shape[0], 100)
        self.assertEqual(res.shape[1], 100)

        # Check center column is white-ish (due to bicubic interpolation might be fuzzy)
        # mid x = 50.
        center_col = res[:, 50, 0]
        self.assertTrue(np.mean(center_col) > 0.1)  # Should have signal

        # Check left/right columns are black
        self.assertTrue(np.mean(res[:, 10, 0]) < 0.1)
