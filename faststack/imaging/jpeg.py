"""High-performance JPEG decoding using PyTurboJPEG with a Pillow fallback."""

import logging
from io import BytesIO
from typing import Optional, Tuple

import numpy as np
from PIL import Image

from faststack.imaging.turbo import TJPF_RGB, create_turbojpeg

log = logging.getLogger(__name__)

JPEG_DECODER, TURBO_AVAILABLE = create_turbojpeg()


def decode_jpeg_rgb(jpeg_bytes: bytes, fast_dct: bool = False) -> Optional[np.ndarray]:
    """Decodes JPEG bytes into an RGB numpy array."""
    if TURBO_AVAILABLE and JPEG_DECODER:
        try:
            # Decode with proper color space handling (no TJFLAG_FASTDCT).
            flags = 0
            if fast_dct:
                # TJFLAG_FASTDCT = 2048
                flags |= 2048
            return JPEG_DECODER.decode(jpeg_bytes, pixel_format=TJPF_RGB, flags=flags)
        except Exception as e:
            log.exception("PyTurboJPEG failed to decode image: %s. Trying Pillow.", e)

    try:
        img = Image.open(BytesIO(jpeg_bytes)).convert("RGB")
        return np.array(img)
    except Exception as e:
        log.exception("Pillow also failed to decode image: %s", e)
        return None


def decode_jpeg_thumb_rgb(
    jpeg_bytes: bytes, max_dim: int = 256
) -> Optional[np.ndarray]:
    """Decodes a JPEG into a thumbnail-sized RGB numpy array."""
    if TURBO_AVAILABLE and JPEG_DECODER:
        try:
            width, height, _, _ = JPEG_DECODER.decode_header(jpeg_bytes)
            scaling_factor = _get_turbojpeg_scaling_factor(width, height, max_dim)

            decoded = JPEG_DECODER.decode(
                jpeg_bytes,
                scaling_factor=scaling_factor,
                pixel_format=TJPF_RGB,
                flags=0,
            )
            if decoded.shape[0] > max_dim or decoded.shape[1] > max_dim:
                img = Image.fromarray(decoded)
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                return np.array(img)
            return decoded
        except Exception as e:
            log.exception(
                "PyTurboJPEG failed to decode thumbnail: %s. Trying Pillow.", e
            )

    try:
        img = Image.open(BytesIO(jpeg_bytes))
        img.thumbnail((max_dim, max_dim))
        return np.array(img.convert("RGB"))
    except Exception as e:
        log.exception("Pillow also failed to decode thumbnail: %s", e)
        return None


def _get_turbojpeg_scaling_factor(
    width: int, height: int, max_dim: int
) -> Optional[Tuple[int, int]]:
    """Finds the best libjpeg-turbo scaling factor to get a thumbnail <= max_dim."""
    if not TURBO_AVAILABLE or not JPEG_DECODER:
        return None

    supported_factors = sorted(
        JPEG_DECODER.scaling_factors,
        key=lambda x: x[0] / x[1],
        reverse=True,
    )

    for num, den in supported_factors:
        if (width * num / den) <= max_dim and (height * num / den) <= max_dim:
            return (num, den)

    return supported_factors[-1] if supported_factors else None


def decode_jpeg_resized(
    jpeg_bytes: bytes, width: int, height: int, fast_dct: bool = False
) -> Optional[np.ndarray]:
    """Decodes and resizes a JPEG to fit within the given dimensions."""
    if width <= 0 or height <= 0:
        return decode_jpeg_rgb(jpeg_bytes, fast_dct=fast_dct)

    if TURBO_AVAILABLE and JPEG_DECODER:
        try:
            img_width, img_height, _, _ = JPEG_DECODER.decode_header(jpeg_bytes)

            if img_width * height > img_height * width:
                max_dim = width
            else:
                max_dim = height

            scale_factor = _get_turbojpeg_scaling_factor(img_width, img_height, max_dim)

            if scale_factor:
                flags = 0
                if fast_dct:
                    flags |= 2048

                decoded = JPEG_DECODER.decode(
                    jpeg_bytes,
                    scaling_factor=scale_factor,
                    pixel_format=TJPF_RGB,
                    flags=flags,
                )

                if decoded.shape[0] > height or decoded.shape[1] > width:
                    img = Image.fromarray(decoded)
                    img.thumbnail((width, height), Image.Resampling.BILINEAR)
                    return np.array(img)
                return decoded
        except Exception as e:
            log.exception("PyTurboJPEG failed: %s", e)

    try:
        img = Image.open(BytesIO(jpeg_bytes))

        if width <= 0 or height <= 0:
            return np.array(img.convert("RGB"))

        scale_factor_ratio = min(img.width / width, img.height / height)

        if scale_factor_ratio > 4:
            resampling = Image.Resampling.BILINEAR
        else:
            resampling = Image.Resampling.LANCZOS

        img.thumbnail((width, height), resampling)
        return np.array(img.convert("RGB"))
    except Exception as e:
        log.exception("Pillow failed to decode and resize image: %s", e)
        return None
