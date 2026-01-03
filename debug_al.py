
import numpy as np
from PIL import Image
from faststack.imaging.editor import ImageEditor

def debug_run():
    editor = ImageEditor()
    w, h = 200, 200
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 200
    arr[0, 0, 0] = 255
    
    img = Image.fromarray(arr, 'RGB')
    editor.original_image = img
    editor._preview_image = img
    
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.1)
    print(f"RESULT: p_high={p_high}")

if __name__ == "__main__":
    debug_run()
