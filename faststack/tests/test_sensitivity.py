import numpy as np
import sys
import os

# Add parent directory to sys.path to allow importing faststack
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from faststack.imaging.editor import ImageEditor

def test_contrast_saturation_sensitivity():
    print("Testing contrast and saturation sensitivity...")
    editor = ImageEditor()
    # Create a 100x100 dummy image (gray with some color)
    arr = np.zeros((100, 100, 3), dtype=np.float32)
    arr[:, :50, 0] = 0.8  # Red left half
    arr[:, 50:, 1] = 0.8  # Green right half
    editor.float_preview = arr
    
    # Test Contrast at 100 (backend value 1.0)
    print("Testing Contrast at 1.0...")
    edits = editor._initial_edits()
    edits['contrast'] = 1.0
    out = editor._apply_edits(arr.copy(), edits=edits)
    
    # Original contrast factor was 1.0 + 1.0 = 2.0
    # New contrast factor should be 1.0 + 1.0 * 0.4 = 1.4
    # Check a pixel that was 0.8: (0.8 - 0.5) * 1.4 + 0.5 = 0.3 * 1.4 + 0.5 = 0.42 + 0.5 = 0.92
    val = out[0, 0, 0]
    print(f"Contrast 1.0 result: {val}")
    assert np.allclose(val, 0.92, atol=0.01), f"Expected 0.92, got {val}"
    
    # Test Saturation at 100 (backend value 1.0)
    print("Testing Saturation at 1.0...")
    edits = editor._initial_edits()
    edits['saturation'] = 1.0
    out = editor._apply_edits(arr.copy(), edits=edits)
    
    # Original saturation factor was 1.0 + 1.0 = 2.0
    # New saturation factor should be 1.0 + 1.0 * 0.5 = 1.5
    # Pixel (0.8, 0, 0): gray = 0.8 * 0.299 = 0.2392
    # New: 0.2392 + (0.8 - 0.2392) * 1.5 = 0.2392 + 0.5608 * 1.5 = 0.2392 + 0.8412 = 1.0804
    val_sat = out[0, 0, 0]
    print(f"Saturation 1.0 result: {val_sat}")
    assert np.allclose(val_sat, 1.0804, atol=0.01), f"Expected 1.0804, got {val_sat}"
    print("All tests passed!")

if __name__ == "__main__":
    try:
        test_contrast_saturation_sensitivity()
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)
