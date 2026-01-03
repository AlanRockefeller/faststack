
import time
import io
import numpy as np
from PIL import Image
from faststack.imaging.jpeg import decode_jpeg_rgb, _get_turbojpeg_scaling_factor, TURBO_AVAILABLE, jpeg_decoder, TJPF_RGB

def decode_jpeg_resized_bilinear(jpeg_bytes: bytes, width: int, height: int):
    """Decodes and resizes a JPEG to fit within the given dimensions using BILINEAR."""
    if width == 0 or height == 0:
        return decode_jpeg_rgb(jpeg_bytes)

    if TURBO_AVAILABLE and jpeg_decoder:
        try:
            # Get image header to determine dimensions
            img_width, img_height, _, _ = jpeg_decoder.decode_header(jpeg_bytes)

            # Determine which dimension is the limiting factor
            if img_width * height > img_height * width:
                max_dim = width
            else:
                max_dim = height

            scale_factor = _get_turbojpeg_scaling_factor(img_width, img_height, max_dim)
         
            if scale_factor:
                decoded = jpeg_decoder.decode(
                    jpeg_bytes, 
                    scaling_factor=scale_factor,
                    pixel_format=TJPF_RGB, 
                    flags=0
                )
                
                # Only use Pillow for final resize if needed
                if decoded.shape[0] > height or decoded.shape[1] > width:
                    img = Image.fromarray(decoded)
                    # CHANGED: Use BILINEAR instead of LANCZOS
                    img.thumbnail((width, height), Image.Resampling.BILINEAR)
                    return np.array(img)
                return decoded
        except Exception as e:
            print(f"PyTurboJPEG failed: {e}")
    
    # Fallback to Pillow
    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        img.thumbnail((width, height), Image.Resampling.BILINEAR)
        return np.array(img.convert("RGB"))
    except Exception as e:
        print(f"Pillow failed: {e}")
        return None

def create_test_jpeg(width=6000, height=4000):
    """Creates a large test JPEG in memory."""
    print(f"Creating test JPEG ({width}x{height})...")
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
    decode_jpeg_resized_bilinear(jpeg_bytes, target_width, target_height)

    iterations = 10
    start = time.perf_counter()
    for _ in range(iterations):
        decode_jpeg_resized_bilinear(jpeg_bytes, target_width, target_height)
    end = time.perf_counter()

    avg_time = (end - start) / iterations
    print(f"Average decode time (BILINEAR): {avg_time:.4f} s")
    print(f"FPS: {1/avg_time:.2f}")

if __name__ == "__main__":
    benchmark()
