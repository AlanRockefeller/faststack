"""High-performance JPEG decoding using PyTurboJPEG with a Pillow fallback."""

import logging
from typing import Optional, Tuple

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Attempt to import PyTurboJPEG

try:
    from turbojpeg import TurboJPEG, TJPF_RGB
except ImportError:
    jpeg_decoder = None
    TURBO_AVAILABLE = False
    log.warning("PyTurboJPEG not found. Falling back to Pillow for JPEG decoding.")
else:
    try:
        jpeg_decoder = TurboJPEG()
    except Exception:
        jpeg_decoder = None
        TURBO_AVAILABLE = False
        log.exception("PyTurboJPEG initialization failed. Falling back to Pillow.")
    else:
        TURBO_AVAILABLE = True
        log.info("PyTurboJPEG is available. Using it for JPEG decoding.")


def decode_jpeg_rgb(jpeg_bytes: bytes) -> Optional[np.ndarray]:
    """Decodes JPEG bytes into an RGB numpy array."""
    if TURBO_AVAILABLE and jpeg_decoder:
        try:
            # Decode with proper color space handling (no TJFLAG_FASTDCT)
            # This ensures proper YCbCr->RGB conversion with correct gamma
            return jpeg_decoder.decode(jpeg_bytes, pixel_format=TJPF_RGB, flags=0)
        except Exception as e:
            log.exception(f"PyTurboJPEG failed to decode image: {e}. Trying Pillow.")
            # Fall through to Pillow fallback

    # Fallback to Pillow
    try:
        from io import BytesIO
        img = Image.open(BytesIO(jpeg_bytes)).convert("RGB")
        return np.array(img)
    except Exception as e:
        log.exception(f"Pillow also failed to decode image: {e}")
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

            decoded = jpeg_decoder.decode(
                jpeg_bytes,
                scaling_factor=scaling_factor,
                pixel_format=TJPF_RGB,
                flags=0,  # Proper color space handling
            )
            if decoded.shape[0] > max_dim or decoded.shape[1] > max_dim:
                img = Image.fromarray(decoded)
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                return np.array(img)
            return decoded
        except Exception as e:
            log.exception(f"PyTurboJPEG failed to decode thumbnail: {e}. Trying Pillow.")

    # Fallback to Pillow
    try:
        from io import BytesIO
        img = Image.open(BytesIO(jpeg_bytes))
        img.thumbnail((max_dim, max_dim))
        return np.array(img.convert("RGB"))
    except Exception as e:
        log.exception(f"Pillow also failed to decode thumbnail: {e}")
        return None


def _get_turbojpeg_scaling_factor(width: int, height: int, max_dim: int) -> Optional[Tuple[int, int]]:
    """Finds the best libjpeg-turbo scaling factor to get a thumbnail <= max_dim."""
    if not TURBO_AVAILABLE or not jpeg_decoder:
        return None

    # PyTurboJPEG provides a set of supported scaling factors
    supported_factors = sorted(
        jpeg_decoder.scaling_factors,
        key=lambda x: x[0] / x[1],
        reverse=True,
    )

    for num, den in supported_factors:
        if (width * num / den) <= max_dim and (height * num / den) <= max_dim:
            return (num, den)
            
    # If no suitable factor is found, return the smallest one
    return supported_factors[-1] if supported_factors else None


def decode_jpeg_resized(
    jpeg_bytes: bytes, width: int, height: int
) -> Optional[np.ndarray]:
    """Decodes and resizes a JPEG to fit within the given dimensions."""
    if width == 0 or height == 0:
        return decode_jpeg_rgb(jpeg_bytes)

    if TURBO_AVAILABLE and jpeg_decoder:
        try:
            # Get image header to determine dimensions
            img_width, img_height, _, _ = jpeg_decoder.decode_header(jpeg_bytes)

            # Determine which dimension is the limiting factor
            if img_width * height > img_height * width:
                # Image is wider relative to target box; width is the constraint
                max_dim = width
            else:
                # Image is taller relative to target box; height is the constraint
                max_dim = height

            scale_factor = _get_turbojpeg_scaling_factor(img_width, img_height, max_dim)
         
            if scale_factor:
                decoded = jpeg_decoder.decode(
                    jpeg_bytes, 
                    scaling_factor=scale_factor,
                    pixel_format=TJPF_RGB, 
                    flags=0  # Proper color space handling
                )
                
                # Only use Pillow for final resize if needed
                if decoded.shape[0] > height or decoded.shape[1] > width:
                    from io import BytesIO
                    img = Image.fromarray(decoded)
                    img.thumbnail((width, height), Image.Resampling.LANCZOS)
                    return np.array(img)
                return decoded
        except Exception as e:
            log.exception(f"PyTurboJPEG failed: {e}")
    
    # Fallback to Pillow (existing code)
    try:
        from io import BytesIO
        img = Image.open(BytesIO(jpeg_bytes))
        
        scale_factor_ratio = min(img.width / width, img.height / height)

        # Use faster BILINEAR for large downscales, LANCZOS for smaller
        if scale_factor_ratio > 4:
            resampling = Image.Resampling.BILINEAR  # Much faster
        else:
            resampling = Image.Resampling.LANCZOS  # Higher quality

        img.thumbnail((width, height), resampling)
        return np.array(img.convert("RGB"))
    except Exception as e:
        log.exception(f"Pillow failed to decode and resize image: {e}")
        return None
