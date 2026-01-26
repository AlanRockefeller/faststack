import unittest
from unittest.mock import MagicMock, patch
import numpy as np
import sys

# We check for modules that might be missing and mock them if needed
# inside the test setup to avoid import errors at module level.

class TestRolloff(unittest.TestCase):
    def setUp(self):
        # Now we use the pure math utils, so no need to mock cv2/gui/models
        # unless math_utils unexpectedly depends on them.
        
        from faststack.imaging.math_utils import _apply_headroom_shoulder
        self._apply_headroom_shoulder = _apply_headroom_shoulder

    def tearDown(self):
        pass

    def test_apply_headroom_shoulder_threshold(self):
        # Test that values <= 1.0 are unchanged
        max_overshoot = 0.05
        x = np.array([0.0, 0.5, 0.9, 1.0])
        out = self._apply_headroom_shoulder(x, max_overshoot=max_overshoot)
        np.testing.assert_allclose(out, x)

    def test_apply_headroom_shoulder_rolloff(self):
        # Test that values > 1.0 are compressed
        max_overshoot = 0.05
        # 1.0 + max_overshoot is the asymptote
        x = np.array([1.01, 1.1, 2.0, 10.0])
        out = self._apply_headroom_shoulder(x, max_overshoot=max_overshoot)
        
        # Check that they are compressed (out < x)
        self.assertTrue(np.all(out < x))
        
        # Check that they stay above 1.0
        self.assertTrue(np.all(out > 1.0))
        
        # Check asymptote (should never exceed 1.0 + max_overshoot)
        self.assertTrue(np.all(out < 1.0 + max_overshoot))

    def test_apply_headroom_shoulder_monotonic(self):
        # Test monotonicity
        max_overshoot = 0.05
        x = np.linspace(0.9, 5.0, 100)
        out = self._apply_headroom_shoulder(x, max_overshoot=max_overshoot)
        
        # Check if strictly increasing
        diffs = np.diff(out)
        self.assertTrue(np.all(diffs > 0), "Output should be monotonic increasing")

    def test_apply_headroom_shoulder_continuity(self):
        # Test continuity at 1.0
        max_overshoot = 0.05
        # Check very close to 1.0 from both sides
        x = np.array([1.0 - 1e-7, 1.0, 1.0 + 1e-7])
        out = self._apply_headroom_shoulder(x, max_overshoot=max_overshoot)
        
        # Difference should be negligible
        diffs = np.diff(out)
        # Should be very small but positive
        self.assertTrue(np.all(np.abs(diffs) < 1e-6))
        
    def test_apply_headroom_shoulder_asymptote_check(self):
        # Verification Plan Step: Feed synthetic array with very high values
        max_overshoot = 0.05
        x = np.array([1.0, 1.0 + max_overshoot/2, 1.0 + 1000.0])
        out = self._apply_headroom_shoulder(x, max_overshoot=max_overshoot)
        
        # f(1.0) == 1.0
        self.assertAlmostEqual(out[0], 1.0)
        
        # f(very_large) should be close to 1.0 + max_overshoot
        self.assertAlmostEqual(out[2], 1.0 + max_overshoot, places=4)
        
        # values <= 1.0 + max_overshoot
        self.assertTrue(np.all(out <= 1.0 + max_overshoot + 1e-9))

if __name__ == "__main__":
    unittest.main()
