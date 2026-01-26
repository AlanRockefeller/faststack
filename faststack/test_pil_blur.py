
from PIL import Image, ImageFilter
import numpy as np
import time

def test_blur():
    try:
        # Create a dummy float image
        data = np.random.rand(100, 100).astype(np.float32)
        img = Image.fromarray(data, mode='F')
        
        print("Attempting blur on mode 'F'...")
        start = time.time()
        blurred = img.filter(ImageFilter.GaussianBlur(radius=5))
        print(f"Blur took {time.time() - start:.4f}s")
        
        result = np.array(blurred)
        print(f"Result shape: {result.shape}, dtype: {result.dtype}")
        
        # Check if it actually blurred (simple check: std dev should decrease)
        print(f"Original std: {np.std(data):.4f}")
        print(f"Blurred std: {np.std(result):.4f}")
        
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    test_blur()
