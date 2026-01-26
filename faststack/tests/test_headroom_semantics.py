import unittest
import numpy as np
import sys
import os
from unittest.mock import MagicMock

# Mock cv2/turbojpeg
sys.modules['cv2'] = MagicMock()
sys.modules['turbojpeg'] = MagicMock()
sys.modules['PyTurboJPEG'] = MagicMock()

# Ensure faststack is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from faststack.imaging.editor import ImageEditor, _analyze_highlight_state, _srgb_to_linear

class TestHeadroomSemantics(unittest.TestCase):
    def test_headroom_exposure_independence(self):
        """Verify headroom calculation ignores exposure gain."""
        # 1. Create a synthetic image with max=1.0 (No headroom)
        # Linear space: 1.0
        img = np.ones((100, 100, 3), dtype=np.float32) * 1.0
        
        # 2. Analyze state with NO exposure change
        # Pre-exposure is same as input
        state = _analyze_highlight_state(img, pre_exposure_linear=img)
        self.assertEqual(state['headroom_pct'], 0.0)

        # 3. Simulate High Exposure (+1 EV -> 2x gain)
        # Current linear becomes 2.0
        exposed_img = img * 2.0
        
        # Analyze: pass exposed as 'rgb_linear' but original as 'pre_exposure_linear'
        state_exposed = _analyze_highlight_state(exposed_img, pre_exposure_linear=img)
        
        # Headroom should STILL be 0.0 because pre-exposure < 1.0
        self.assertEqual(state_exposed['headroom_pct'], 0.0, "Headroom should not be triggering just because of exposure")
        
        # 4. Reference: If we didn't pass pre-exposure, it WOULD show headroom
        state_naive = _analyze_highlight_state(exposed_img, pre_exposure_linear=None)
        self.assertGreater(state_naive['headroom_pct'], 0.99, "Naive analysis should show headroom (sanity check)")

    def test_true_headroom_detection(self):
        """Verify actual headroom is detected regardless of exposure."""
        # 1. Image with real headroom (max=1.5)
        img = np.ones((100, 100, 3), dtype=np.float32) * 1.5
        
        # 2. Even if we darken it (-1 EV -> 0.75), we should know it HAD headroom?
        # Typically "headroom" implies "recoverable data".
        # If I underexpose a RAW, I still want to know it has headroom.
        # Actually, if I underexpose, the values become < 1.0. 
        # But the SOURCE has values > 1.0.
        # So yes, headroom_pct should ideally reflect source capability.
        
        darkened_img = img * 0.5
        
        state = _analyze_highlight_state(darkened_img, pre_exposure_linear=img)
        self.assertGreater(state['headroom_pct'], 0.99)

if __name__ == '__main__':
    unittest.main()
