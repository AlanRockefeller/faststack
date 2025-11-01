"""High-performance JPEG decoding using PyTurboJPEG with a Pillow fallback."""

import logging
from typing import Optional, Tuple

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Attempt to import PyTurboJPEG
try:
    from turbojpeg import TurboJPEG, TJFLAG_FASTDCT, TJPF_RGB
    jpeg_decoder = TurboJPEG()
    TURBO_AVAILABLE = True
    log.info("PyTurboJPEG is available. Using for JPEG decoding.")
except ImportError:
    jpeg_decoder = None
    TURBO_AVAILABLE = False
    log.warning("PyTurboJPEG not found. Falling back to Pillow for JPEG decoding.")

def decode_jpeg_rgb(jpeg_bytes: bytes) -> Optional[np.ndarray]:
    """Decodes JPEG bytes into an RGB numpy array."""
    if TURBO_AVAILABLE and jpeg_decoder:
        try:
            # The flags prevent upsampling of chroma channels, which is faster.
            return jpeg_decoder.decode(jpeg_bytes, pixel_format=TJPF_RGB, flags=TJFLAG_FASTDCT)
        except Exception as e:
            log.error(f"PyTurboJPEG failed to decode image: {e}. Trying Pillow.")
            # Fall through to Pillow fallback
    
    # Fallback to Pillow
    try:
        from io import BytesIO
        img = Image.open(BytesIO(jpeg_bytes)).convert("RGB")
        return np.array(img)
    except Exception as e:
        log.error(f"Pillow also failed to decode image: {e}")
        return None

def decode_jpeg_thumb_rgb(
    jpeg_bytes: bytes, 
    max_dim: int = 256
) -> Optional[np.ndarray]:
    """Decodes a JPEG into a thumbnail-sized RGB numpy array."""
    if TURBO_AVAILABLE and jpeg_decoder:
        try:
            # Get image header to determine dimensions
            width, height, _, _ = jpeg_decoder.decode_header(jpeg_bytes)
            
            # Find the best scaling factor
            scaling_factor = _get_turbojpeg_scaling_factor(width, height, max_dim)
            
            return jpeg_decoder.decode(jpeg_bytes, scaling_factor=scaling_factor, pixel_format=TJPF_RGB, flags=TJFLAG_FASTDCT)
        except Exception as e:
            log.error(f"PyTurboJPEG failed to decode thumbnail: {e}. Trying Pillow.")

    # Fallback to Pillow
    try:
        from io import BytesIO
        img = Image.open(BytesIO(jpeg_bytes))
        img.thumbnail((max_dim, max_dim))
        return np.array(img.convert("RGB"))
    except Exception as e:
        log.error(f"Pillow also failed to decode thumbnail: {e}")
        return None

def _get_turbojpeg_scaling_factor(width: int, height: int, max_dim: int) -> Optional[Tuple[int, int]]:
    """Finds the best libjpeg-turbo scaling factor to get a thumbnail <= max_dim."""
    # libjpeg-turbo supports scaling factors of N/8 for N in [1, 16]
    for n in range(8, 0, -1):
        if (width * n / 8) <= max_dim and (height * n / 8) <= max_dim:
            return (n, 8)
    return None # Should not happen if max_dim is reasonable
