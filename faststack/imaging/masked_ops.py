# faststack/imaging/masked_ops.py
"""Masked image-processing operations.

Layer 3 of the mask subsystem.  Each function takes an image array and a
soft mask and applies a local adjustment.  Background darkening is the
first consumer; future tools can add ``apply_masked_exposure``,
``apply_masked_saturation``, etc.
"""

import logging

import numpy as np

log = logging.getLogger(__name__)

# Optional dependency -------------------------------------------------------
try:
    import cv2
except ImportError:
    cv2 = None  # type: ignore[assignment]


def apply_masked_darken(
    arr: np.ndarray,
    mask: np.ndarray,
    darken_amount: float,
    edge_protection: float,
) -> np.ndarray:
    """Apply background darkening inside *mask*.

    The algorithm combines two complementary techniques for a natural look:

    1. **Pedestal subtraction** — removes the ambient-light "haze floor"
       that makes backgrounds look grey instead of black.
    2. **Multiplicative darkening** — compresses the remaining tonal range
       in the masked area, preserving relative brightness relationships.
    3. **Edge detail preservation** — optionally adds back local detail
       in darkened areas so texture is not lost.

    Parameters
    ----------
    arr : float32 (H, W, 3)  sRGB image in [0, 1].  Modified **in-place**.
    mask : float32 (H, W)    soft mask in [0, 1], where 1.0 = full background.
    darken_amount : float     overall strength, 0–1.
    edge_protection : float   detail preservation strength, 0–1.

    Returns
    -------
    The same *arr* array (for convenience chaining).
    """
    if darken_amount < 0.001:
        return arr

    mask3 = mask[..., np.newaxis]  # (H, W, 1) for broadcasting

    # 1. Pedestal subtraction — remove the grey floor
    pedestal = darken_amount * 0.15
    arr -= mask3 * pedestal

    # 2. Multiplicative darkening — compress remaining range
    mult_factor = darken_amount * 0.4
    arr *= 1.0 - mask3 * mult_factor

    # 3. Edge detail preservation
    if edge_protection > 0.01:
        # Compute local detail at a fine scale
        try:
            if cv2 is not None and hasattr(cv2, "GaussianBlur"):
                luma = np.ascontiguousarray(
                    arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
                )
                blurred = cv2.GaussianBlur(luma, (5, 5), 1.5)
                if isinstance(blurred, np.ndarray) and blurred.shape == luma.shape:
                    detail = luma - blurred  # high-frequency detail
                    restore = detail[..., np.newaxis] * mask3 * edge_protection * 0.5
                    arr += restore
        except Exception:
            log.debug(
                "Edge detail preservation skipped: arr=%s mask3=%s",
                getattr(arr, "dtype", "?"),
                getattr(mask3, "dtype", "?"),
                exc_info=True,
            )

    # Safety clamp — keep in valid range
    np.clip(arr, 0.0, 1.0, out=arr)

    return arr
