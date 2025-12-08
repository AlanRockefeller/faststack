
import time
import io
import numpy as np
from PIL import Image
from faststack.imaging.jpeg import decode_jpeg_resized, TURBO_AVAILABLE

def create_test_jpeg(width=6000, height=4000):
    """Creates a large test JPEG in memory."""
    print(f"Creating test JPEG ({width}x{height})...")
    # Create a random image
    arr = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()

def benchmark():
    jpeg_bytes = create_test_jpeg()
    print(f"JPEG size: {len(jpeg_bytes) / 1024 / 1024:.2f} MB")
    print(f"TurboJPEG available: {TURBO_AVAILABLE}")

    target_width = 1920
    target_height = 1080

    # Warmup
    decode_jpeg_resized(jpeg_bytes, target_width, target_height)

    iterations = 10
    start = time.perf_counter()
    for _ in range(iterations):
        decode_jpeg_resized(jpeg_bytes, target_width, target_height)
    end = time.perf_counter()

    avg_time = (end - start) / iterations
    print(f"Average decode time (Current Implementation): {avg_time:.4f} s")
    print(f"FPS: {1/avg_time:.2f}")

if __name__ == "__main__":
    benchmark()
