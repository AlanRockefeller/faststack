"""High-performance JPEG decoding using PyTurboJPEG with a Pillow fallback."""

import logging
import time
import warnings
from io import BytesIO
from typing import Any, Literal, Optional, Tuple

import numpy as np
from PIL import Image

from faststack.imaging.optional_deps import get_cv2
from faststack.imaging.turbo import TJPF_RGB, create_turbojpeg

log = logging.getLogger(__name__)

JPEG_DECODER, TURBO_AVAILABLE = create_turbojpeg()

_PREMATURE_EOF_RETRY_DELAY = 0.15


def _decode_with_retry(
    jpeg_bytes: bytes,
    *,
    source_path: Optional[str] = None,
    decoder: Any = None,
    **decode_kwargs: Any,
) -> Optional[np.ndarray]:
    """Call decoder.decode() with a single retry on 'Premature end of JPEG file'.

    TurboJPEG emits this as a Python warning (not an exception) when the
    file is truncated.  We treat it as a soft/retryable condition — the
    file may still be written by another process — and retry once after
    a short delay.
    """
    dec = decoder or JPEG_DECODER
    for attempt in range(2):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = dec.decode(jpeg_bytes, **decode_kwargs)

        premature = any("Premature end of JPEG file" in str(w.message) for w in caught)

        if not premature:
            return result

        if attempt == 0:
            time.sleep(_PREMATURE_EOF_RETRY_DELAY)
            continue

        label = source_path or "<unknown>"
        log.warning(
            "TurboJPEG: 'Premature end of JPEG file' for %s "
            "(retry also warned — file may be truncated)",
            label,
        )
        return result


def decode_jpeg_rgb(
    jpeg_bytes: bytes,
    fast_dct: bool = False,
    source_path: Optional[str] = None,
    stats: Optional[dict] = None,
    log_errors: bool = True,
) -> Optional[np.ndarray]:
    """Decodes JPEG bytes into an RGB numpy array."""
    if TURBO_AVAILABLE and JPEG_DECODER:
        try:
            flags = 0
            if fast_dct:
                flags |= 2048
            result = _decode_with_retry(
                jpeg_bytes,
                source_path=source_path,
                pixel_format=TJPF_RGB,
                flags=flags,
            )
            if stats is not None and result is not None:
                stats["decoder"] = "turbojpeg"
                stats["dct"] = (1, 1)
            return result
        except Exception as e:
            log.log(
                logging.ERROR if log_errors else logging.DEBUG,
                "PyTurboJPEG failed to decode %s: %s. Trying Pillow.",
                source_path or "<unknown>",
                e,
                exc_info=log_errors,
            )

    # Fallback to Pillow
    try:
        img = Image.open(BytesIO(jpeg_bytes)).convert("RGB")
        if stats is not None:
            stats["decoder"] = "pillow"
            stats["dct"] = (1, 1)
        return np.array(img)
    except Exception as e:
        log.log(
            logging.ERROR if log_errors else logging.DEBUG,
            "Pillow also failed to decode %s: %s",
            source_path or "<unknown>",
            e,
            exc_info=log_errors,
        )
        return None


def decode_jpeg_thumb_rgb(
    jpeg_bytes: bytes,
    max_dim: int = 256,
    source_path: Optional[str] = None,
) -> Optional[np.ndarray]:
    """Decodes a JPEG into a thumbnail-sized RGB numpy array."""
    if TURBO_AVAILABLE and JPEG_DECODER:
        try:
            width, height, _, _ = JPEG_DECODER.decode_header(jpeg_bytes)
            scaling_factor = _get_turbojpeg_scaling_factor(width, height, max_dim)

            decoded = _decode_with_retry(
                jpeg_bytes,
                source_path=source_path,
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

    # Fallback to Pillow
    try:
        img = Image.open(BytesIO(jpeg_bytes))
        img.thumbnail((max_dim, max_dim))
        return np.array(img.convert("RGB"))
    except Exception as e:
        log.exception("Pillow also failed to decode thumbnail: %s", e)
        return None


def _get_turbojpeg_scaling_factor(
    width: int,
    height: int,
    max_width: int,
    max_height: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """Find the largest TurboJPEG scale that fits the target rectangle.

    "Fit within" semantics: returns the LARGEST factor whose scaled
    dimensions are within ``max_width`` and ``max_height``, falling back to
    the smallest available factor if nothing fits. Omitting ``max_height``
    retains the original square-thumbnail behavior. For display decodes that
    must never come out undersized, use
    :func:`_get_turbojpeg_covering_factor` instead.
    """
    if not TURBO_AVAILABLE or not JPEG_DECODER:
        return None
    if max_height is None:
        max_height = max_width

    # PyTurboJPEG provides a set of supported scaling factors
    supported_factors = sorted(
        JPEG_DECODER.scaling_factors,
        key=lambda x: x[0] / x[1],
        reverse=True,
    )

    for num, den in supported_factors:
        if (width * num / den) <= max_width and (height * num / den) <= max_height:
            return (num, den)

    # If no suitable factor is found, return the smallest one
    return supported_factors[-1] if supported_factors else None


def _get_turbojpeg_covering_factor(
    constraining_dim: int, target: int
) -> Optional[Tuple[int, int]]:
    """Finds the smallest libjpeg-turbo factor that keeps the decode >= target.

    "Never undersized" semantics for the display path: given the image's
    CONSTRAINING dimension (the one that bounds the aspect-preserving
    fit into the viewport — the caller must determine which dimension
    that is), returns the SMALLEST non-upscaling factor (``num <= den``)
    such that ``constraining_dim * num / den >= target``. A subsequent
    high-quality downscale can then hit the exact target without ever
    handing an undersized image to a caller that would upscale it
    (e.g. nearest-neighbor QML display). Only the constraining dimension
    matters: the other dimension needs fewer pixels than the viewport
    offers, so requiring it to reach the target too would only force a
    needlessly large decode.

    Upscaling factors (num > den) are never selected. If even the
    full-size image (1/1) is smaller than ``target``, returns (1, 1) —
    native size is the best available source and no scaling factor can
    help.
    """
    if not TURBO_AVAILABLE or not JPEG_DECODER:
        return None

    # Only consider non-upscaling factors, smallest-first, so we pick
    # the smallest one that still meets or exceeds the target.
    no_upscale = sorted(
        (f for f in JPEG_DECODER.scaling_factors if f[0] <= f[1]),
        key=lambda x: x[0] / x[1],
    )
    for num, den in no_upscale:
        if (constraining_dim * num / den) >= target:
            return (num, den)
    return (1, 1)


def _fit_dimensions(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[int, int]:
    """Return an aspect-preserving size bounded by the target rectangle."""
    scale = min(target_width / source_width, target_height / source_height, 1.0)
    return (
        max(1, int(round(source_width * scale))),
        max(1, int(round(source_height * scale))),
    )


def decode_jpeg_resized(
    jpeg_bytes: bytes,
    width: int,
    height: int,
    fast_dct: bool = False,
    source_path: Optional[str] = None,
    mode: Literal["fast", "cover"] = "cover",
    stats: Optional[dict] = None,
    log_errors: bool = True,
) -> Optional[np.ndarray]:
    """Decodes and resizes a JPEG to fit within the given dimensions.

    When ``stats`` is provided it is populated in-place with diagnostic fields
    (``source``, ``dct``, ``output``, ``jpeg_ms``, ``resize_ms``) for the
    navigation trace. Purely optional; callers that omit it are unaffected.
    """
    if mode not in ("fast", "cover"):
        raise ValueError(f"Unknown JPEG resize mode: {mode}")

    if width <= 0 or height <= 0:
        return decode_jpeg_rgb(
            jpeg_bytes,
            fast_dct=fast_dct,
            source_path=source_path,
            stats=stats,
            log_errors=log_errors,
        )

    if TURBO_AVAILABLE and JPEG_DECODER:
        try:
            if stats is not None:
                stats["decoder"] = "turbojpeg"
            img_width, img_height, _, _ = JPEG_DECODER.decode_header(jpeg_bytes)
            if stats is not None:
                stats["source"] = (img_width, img_height)

            if mode == "cover":
                # The constraining dimension is the one that bounds the
                # aspect-preserving fit into the viewport; only it needs to
                # reach max_dim after scaling (the other dimension needs
                # fewer pixels than the viewport provides).
                if img_width * height > img_height * width:
                    max_dim = width
                    constraining_dim = img_width
                else:
                    max_dim = height
                    constraining_dim = img_height

                scale_factor = _get_turbojpeg_covering_factor(constraining_dim, max_dim)
            else:
                scale_factor = _get_turbojpeg_scaling_factor(
                    img_width,
                    img_height,
                    width,
                    height,
                )

            if scale_factor:
                if stats is not None:
                    stats["dct"] = scale_factor
                flags = 0
                if fast_dct:
                    flags |= 2048

                _t_decode = time.perf_counter() if stats is not None else None
                decoded = _decode_with_retry(
                    jpeg_bytes,
                    source_path=source_path,
                    scaling_factor=scale_factor,
                    pixel_format=TJPF_RGB,
                    flags=flags,
                )
                if stats is not None and _t_decode is not None:
                    stats["jpeg_ms"] = (time.perf_counter() - _t_decode) * 1000.0

                # Only do a final resize if the selected DCT scale is larger
                # than the exact display target. OpenCV's area downsampler is
                # substantially faster than Pillow LANCZOS for the multi-
                # megapixel settled frame while retaining high downscale quality.
                if decoded.shape[0] > height or decoded.shape[1] > width:
                    _t_resize = time.perf_counter() if stats is not None else None
                    cv2 = get_cv2()
                    output_width, output_height = _fit_dimensions(
                        decoded.shape[1],
                        decoded.shape[0],
                        width,
                        height,
                    )
                    if cv2 is not None:
                        interpolation = (
                            cv2.INTER_LINEAR if mode == "fast" else cv2.INTER_AREA
                        )
                        result = cv2.resize(
                            decoded,
                            (output_width, output_height),
                            interpolation=interpolation,
                        )
                    else:
                        img = Image.fromarray(decoded)
                        resampling = (
                            Image.Resampling.BILINEAR
                            if mode == "fast"
                            else Image.Resampling.LANCZOS
                        )
                        img.thumbnail((width, height), resampling)
                        result = np.array(img)
                    result = np.ascontiguousarray(result)
                    if stats is not None and _t_resize is not None:
                        stats["resize_ms"] = (time.perf_counter() - _t_resize) * 1000.0
                        stats["output"] = (result.shape[1], result.shape[0])
                    return result
                if stats is not None:
                    stats["resize_ms"] = 0.0
                    stats["output"] = (decoded.shape[1], decoded.shape[0])
                return decoded
        except Exception as e:
            log.log(
                logging.ERROR if log_errors else logging.DEBUG,
                "PyTurboJPEG failed for %s: %s",
                source_path or "<unknown>",
                e,
                exc_info=log_errors,
            )

    # Fallback to Pillow (existing code)
    try:
        if stats is not None:
            stats["decoder"] = "pillow"
            stats["dct"] = (1, 1)
        _t_decode = time.perf_counter() if stats is not None else None
        img = Image.open(BytesIO(jpeg_bytes))
        if stats is not None:
            stats["source"] = (img.width, img.height)

        if width <= 0 or height <= 0:
            result = np.array(img.convert("RGB"))
            if stats is not None and _t_decode is not None:
                stats["jpeg_ms"] = (time.perf_counter() - _t_decode) * 1000.0
                stats["output"] = (result.shape[1], result.shape[0])
            return result

        scale_factor_ratio = min(img.width / width, img.height / height)

        # Use faster BILINEAR for large downscales, LANCZOS for smaller
        if scale_factor_ratio > 4:
            resampling = Image.Resampling.BILINEAR  # Much faster
        else:
            resampling = (
                Image.Resampling.LANCZOS
            )  # Higher quality for smaller downscales

        img.thumbnail((width, height), resampling)
        result = np.array(img.convert("RGB"))
        if stats is not None and _t_decode is not None:
            # Pillow decodes and resizes together; attribute the whole cost to
            # jpeg (there is no separable DCT-scale step in this path).
            stats["jpeg_ms"] = (time.perf_counter() - _t_decode) * 1000.0
            stats["resize_ms"] = 0.0
            stats["output"] = (result.shape[1], result.shape[0])
        return result
    except Exception as e:
        log.log(
            logging.ERROR if log_errors else logging.DEBUG,
            "Pillow failed to decode and resize %s: %s",
            source_path or "<unknown>",
            e,
            exc_info=log_errors,
        )
        return None
