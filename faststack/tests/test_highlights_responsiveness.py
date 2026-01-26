import unittest
import numpy as np
import sys
import os
from unittest.mock import MagicMock

# Mock dependencies
sys.modules['cv2'] = MagicMock()
sys.modules['turbojpeg'] = MagicMock()
sys.modules['PyTurboJPEG'] = MagicMock()

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from faststack.imaging.editor import ImageEditor

class TestHighlightsResponsiveness(unittest.TestCase):
    def test_highlights_at_various_levels(self):
        """Test how much highlights recovery affects various brightness levels."""
        editor = ImageEditor()
        
        # Create a gradient from 0.0 to 1.0 (linear)
        # 0.5 linear is about 186/255 in sRGB
        # 0.25 linear is about 137/255 in sRGB
        steps = 11
        vals = np.linspace(0.0, 1.0, steps, dtype=np.float32)
        linear = np.stack([vals]*3, axis=-1).reshape(1, steps, 3)
        
        # Apply edits with highlights at -1.0 (max recovery)
        edits = editor._initial_edits()
        edits['highlights'] = -1.0
        
        out = editor._apply_edits(linear.copy(), edits=edits, for_export=True)
        
        print("\nBrightness Levels (Linear 0.0 -> 1.0):")
        print("Input  ->  Output  (Diff)")
        for i in range(steps):
            inp = vals[i]
            outp = out[0, i, 0]
            diff = inp - outp
            print(f"{inp:0.2f}   ->  {outp:0.4f}  ({diff:0.4f})")
            
        # The goal is to see significant changes (diff > 0.01) starting from lower levels
        # Currently, with pivot 0.75, values below 0.75 should be unchanged (diff=0)
        
if __name__ == '__main__':
    unittest.main()
