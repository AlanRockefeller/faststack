import unittest
import numpy as np
from faststack.imaging.editor import ImageEditor, _apply_headroom_shoulder
from faststack.ui.provider import UIState
from faststack.app import AppController
from PySide6.QtCore import QObject, Signal

class MockAppController(QObject):
    def __init__(self):
        super().__init__()
        self.image_editor = ImageEditor()
        self.ui_state = None # Circular ref handle manually

class TestHighlightsV2(unittest.TestCase):
    def test_shoulder_asymptote(self):
        """Verify the new shoulder asymptotes to 1.0 + steepness."""
        x = np.array([1.0, 2.0, 10.0, 100.0], dtype=np.float32)
        steepness = 0.05
        out = _apply_headroom_shoulder(x, steepness=steepness)
        
        # At 1.0, should be 1.0
        self.assertAlmostEqual(out[0], 1.0, places=5)
        
        # Above 1.0, should be < 1.0 + steepness
        self.assertTrue(np.all(out[1:] < 1.0 + steepness))
        
        # Monotonicity
        self.assertTrue(out[1] > out[0])
        self.assertTrue(out[2] > out[1])
        
        # Asymptote check: at very large x, should be close to 1.05
        self.assertAlmostEqual(out[-1], 1.0 + steepness, delta=0.001)

    def test_analysis_decoupling(self):
        """Verify analysis runs before adjustments and is cached."""
        editor = ImageEditor()
        # Create a linear image with some headroom
        linear = np.ones((100, 100, 3), dtype=np.float32) * 1.2
        # sRGB mock indicating some clipping (e.g. 255)
        srgb = np.ones((100, 100, 3), dtype=np.uint8) * 255
        
        # Run with highlights=-0.5
        editor._apply_highlights_shadows(linear, highlights=-0.5, shadows=0.0, srgb_u8=srgb)
        
        # Check cache
        self.assertIsNotNone(editor._last_highlight_state)
        self.assertGreater(editor._last_highlight_state['headroom_pct'], 0.9)
        self.assertGreater(editor._last_highlight_state['clipped_pct'], 0.9)

    def test_robust_ceiling(self):
        """Verify headroom ceiling handles hot pixels."""
        editor = ImageEditor()
        linear = np.ones((100, 100, 3), dtype=np.float32) * 1.1 # Moderate headroom
        # Add a single hot pixel
        linear[50, 50, :] = 1000.0
        
        # Use highlights recovery
        # This triggers the robust percentile logic
        out = editor._apply_highlights_shadows(linear, highlights=-1.0, shadows=0.0)
        
        # Check that we didn't explode or crash
        self.assertTrue(np.isfinite(out).all())
        # The hot pixel should be compressed but not NaN
        self.assertLess(out[50, 50, 0], 1000.0) 

if __name__ == '__main__':
    unittest.main()
