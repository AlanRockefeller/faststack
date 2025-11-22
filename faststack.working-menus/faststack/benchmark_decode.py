import mmap
import time
from pathlib import Path
from faststack.imaging.jpeg import decode_jpeg_resized, TURBO_AVAILABLE

print(f"TurboJPEG available: {TURBO_AVAILABLE}")

test_image = Path(r"C:\Users\alanr\Pictures\Lightroom\2025\2025-11-14\20251114-PB140001-2.JPG")

# Match the real code path with mmap
iterations = 20
start = time.perf_counter()
for _ in range(iterations):
    with open(test_image, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
            jpeg_bytes = mmapped[:]
    decode_jpeg_resized(jpeg_bytes, 1920, 1080)
elapsed = time.perf_counter() - start

print(f"Average time (with mmap): {elapsed/iterations*1000:.1f}ms")
