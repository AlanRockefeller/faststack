import unittest
import numpy as np

# Adjust import path if necessary, but faststack is likely installed or in pythonpath
import sys
import os
from unittest.mock import MagicMock

# Mock cv2 before importing faststack modules that depend on it
sys.modules["cv2"] = MagicMock()
sys.modules["turbojpeg"] = MagicMock()
sys.modules["PyTurboJPEG"] = MagicMock()

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from faststack.imaging.editor import ImageEditor, _apply_headroom_shoulder


class TestHighlightsV2(unittest.TestCase):
    def test_shoulder_asymptote(self):
        """Verify the new shoulder asymptotes to 1.0 + max_overshoot."""
        x = np.array([1.0, 2.0, 10.0, 100.0], dtype=np.float32)
        max_overshoot = 0.05
        out = _apply_headroom_shoulder(x, max_overshoot=max_overshoot)

        # At 1.0, should be 1.0
        self.assertAlmostEqual(out[0], 1.0, places=5)

        # Above 1.0, should be < 1.0 + max_overshoot
        self.assertTrue(np.all(out[1:] < 1.0 + max_overshoot))

        # Monotonicity
        self.assertTrue(out[1] > out[0])
        self.assertTrue(out[2] > out[1])

        # Asymptote check: at very large x, should be close to 1.05
        self.assertAlmostEqual(out[-1], 1.0 + max_overshoot, delta=0.001)

    def test_analysis_decoupling(self):
        """Verify analysis runs before adjustments and is cached via preview path."""
        editor = ImageEditor()
        # Create a linear image with some headroom
        linear = np.ones((100, 100, 3), dtype=np.float32) * 1.2
        # sRGB mock indicating some clipping (e.g. 255)
        srgb = np.ones((100, 100, 3), dtype=np.uint8) * 255

        # Setup editor state to simulate the image being loaded
        # We need this because _apply_edits works on self.float_image/preview logic usually,
        # but one can pass arr.
        # But _apply_edits updates _last_highlight_state.

        # Run _apply_edits flow
        edits = editor._initial_edits()
        edits["highlights"] = -0.5

        # _apply_edits expects global self.float_image for some contexts?
        # No, it takes img_arr arg.

        editor._apply_edits(linear, edits=edits, for_export=False)

        # Check cache
        self.assertIsNotNone(editor._last_highlight_state)
        # Note: update logic might use striding so check rough values
        self.assertGreater(editor._last_highlight_state["headroom_pct"], 0.9)

    def test_robust_ceiling(self):
        """Verify headroom ceiling handles hot pixels."""
        try:
            editor = ImageEditor()
            linear = np.ones((100, 100, 3), dtype=np.float32) * 1.1  # Moderate headroom
            # Add a single hot pixel
            linear[50, 50, :] = 1000.0

            # Use highlights recovery, ensuring we pass srgb_u8 if needed by analysis
            # (Though robust ceiling logic is in the adjustment phase, analysis happens first)
            editor._apply_highlights_shadows(linear, highlights=-1.0, shadows=0.0)

            # Check that we didn't explode or crash
            # The result is returned by _apply_highlights_shadows, but editor doesn't store it in place of input?
            # Wait, editor method returns new array.
            out = editor._apply_highlights_shadows(linear, highlights=-1.0, shadows=0.0)

            self.assertTrue(np.isfinite(out).all())
            # The hot pixel should be compressed but not NaN
            self.assertLess(out[50, 50, 0], 1000.0)
        except Exception:
            import traceback
            import sys

            traceback.print_exc(file=sys.__stderr__)
            raise


if __name__ == "__main__":
    unittest.main()
