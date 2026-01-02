
import numpy as np
from PIL import Image
from faststack.imaging.editor import ImageEditor
import os

def test_white_balance():
    editor = ImageEditor()
    
    # 1. Test Black Preservation
    # Create a purely black image
    black_img = Image.new('RGB', (100, 100), (0, 0, 0))
    black_path = "test_black.jpg"
    black_img.save(black_path)
    
    editor.load_image(black_path)
    
    # Apply strong temperature and tint
    editor.set_edit_param('white_balance_by', 1.0) # Max Warm
    editor.set_edit_param('white_balance_mg', 1.0) # Max Magenta
    
    # Get processed image
    # We need to access the internal method or use save, but let's use _apply_edits directly for testing
    # editor.original_image is loaded.
    processed_img = editor._apply_edits(editor.original_image.copy())
    arr = np.array(processed_img)
    
    # Check max value - should still be 0 or very close to it
    max_val = arr.max()
    print(f"Black Image Max Value after WB: {max_val}")
    
    if max_val > 0:
        print("FAIL: Black level not preserved!")
    else:
        print("PASS: Black level preserved.")

    # 2. Test Grey Shift
    # Create a mid-grey image
    grey_img = Image.new('RGB', (100, 100), (128, 128, 128))
    grey_path = "test_grey.jpg"
    grey_img.save(grey_path)
    
    editor.load_image(grey_path)
    editor.set_edit_param('white_balance_by', 0.5) # Warm
    # r_gain = 1 + 0.25 = 1.25 -> 128 * 1.25 = 160
    # b_gain = 1 - 0.25 = 0.75 -> 128 * 0.75 = 96
    
    processed_img = editor._apply_edits(editor.original_image.copy())
    arr = np.array(processed_img)
    r, g, b = arr[0,0]
    print(f"Grey Image RGB after Warm shift: R={r}, G={g}, B={b}")
    
    if r > 128 and b < 128:
        print("PASS: Grey shifted warm correctly.")
    else:
        print("FAIL: Grey did not shift as expected.")

    # Cleanup
    for path in [black_path, grey_path]:
        try:
            os.remove(path)
        except OSError:
            pass  # File may not exist or be locked

if __name__ == "__main__":
    test_white_balance()
