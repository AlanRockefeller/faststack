import numpy as np
from typing import Optional

# ----------------------------
# sRGB ↔ Linear Conversion Helpers
# ----------------------------


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """Convert sRGB values to linear light.

    Preserves headroom (values > 1.0) for highlight recovery.
    Clamps negatives to 0 since the power function requires non-negative input.
    """
    # Clamp negatives to 0, but preserve values > 1.0 for headroom
    x = np.clip(x, 0.0, None)
    a = 0.055

    # Apply the standard sRGB transfer function - works for values > 1.0 too
    return np.where(x <= 0.04045, x / 12.92, ((x + a) / (1.0 + a)) ** 2.4)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """Convert linear light values to sRGB (0-1)."""
    x = np.clip(x, 0.0, None)
    a = 0.055
    return np.where(x <= 0.0031308, 12.92 * x, (1.0 + a) * (x ** (1.0 / 2.4)) - a)


def _smoothstep01(x: np.ndarray) -> np.ndarray:
    """Hermite smoothstep: 0 at x<=0, 1 at x>=1, smooth S-curve between."""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _apply_headroom_shoulder(x: np.ndarray, max_overshoot: float = 0.05) -> np.ndarray:
    """Compress values above 1.0 smoothly into a very small headroom.

    Maps headroom (x > 1.0) into [1.0, 1.0 + max_overshoot).
    Asymptotes to 1.0 + max_overshoot as x -> inf.
    Maintains continuity and monotonicity at 1.0.

    Args:
        x: Float32 array in linear light, may have values > 1.0
        max_overshoot: Maximum amount to overshoot 1.0 (e.g. 0.05 means max 1.05)
    """
    mask = x > 1.0
    if not np.any(mask):
        return x

    out = x.copy()
    excess = x[mask] - 1.0
    # Rational compression targeting asymptote of 'max_overshoot'
    # y = saturation * x / (saturation + x)  -> asymptotes to saturation
    # Here x=excess, saturation=max_overshoot
    compressed_excess = max_overshoot * excess / (max_overshoot + excess)
    out[mask] = 1.0 + compressed_excess
    return out


# Constants for chroma rolloff
_CHROMA_ROLLOFF_START = 0.85
_CHROMA_ROLLOFF_WIDTH = 0.15


# Precomputed thresholds for JPEG clipping detection in linear space
# These correspond to sRGB u8 values 250, 253, 254 converted to linear
_LINEAR_THRESHOLD_250 = ((250.0 / 255.0 + 0.055) / 1.055) ** 2.4  # ~0.871
_LINEAR_THRESHOLD_253 = ((253.0 / 255.0 + 0.055) / 1.055) ** 2.4  # ~0.948
_LINEAR_THRESHOLD_254 = ((254.0 / 255.0 + 0.055) / 1.055) ** 2.4  # ~0.972


def _analyze_highlight_state(
    rgb_linear: np.ndarray,
    srgb_u8: Optional[np.ndarray] = None,
    pre_exposure_linear: Optional[np.ndarray] = None,
) -> dict:
    """Analyze image for headroom and clipping to tune recovery parameters.

    Args:
        rgb_linear: Float32 RGB array in linear light (post-exposure/WB)
        srgb_u8: Optional uint8 sRGB array (source image) for accurate JPEG clipping detection.
                 MUST have same H×W dimensions as rgb_linear (or be stride-compatible).

    Returns:
        Dict with:
        - headroom_pct: Fraction of pixels with max(rgb) > 1.0 (current state recoverable data)
        - clipped_pct: Fraction of pixels with true source clipping (flat-top JPEG clip at 254+ if srgb_u8 provided)
        - source_clipped_pct: Alias for clipped_pct (true source clipping)
        - near_white_pct: Alias for current_nearwhite_pct (for UI display)
        - current_nearwhite_pct: Fraction of pixels currently near white [250, 253] equivalent in processed linear space.
    """
    total_pixels = rgb_linear.shape[0] * rgb_linear.shape[1]
    if total_pixels == 0:
        return {
            "headroom_pct": 0.0,
            "clipped_pct": 0.0,
            "source_clipped_pct": 0.0,
            "near_white_pct": 0.0,
            "current_nearwhite_pct": 0.0,
        }

    # Headroom detection: Use pre-exposure buffer if available for "True Headroom"
    if pre_exposure_linear is not None:
        max_source = pre_exposure_linear.max(axis=2)
        headroom_pct = float(np.count_nonzero(max_source > 1.0)) / total_pixels
    else:
        max_rgb = rgb_linear.max(axis=2)
        headroom_pct = float(np.count_nonzero(max_rgb > 1.0)) / total_pixels

    # 1. Source Clipping Statistics (True JPEG Clipping)
    # If srgb_u8 is provided, use it. Otherwise approximate from linear (less accurate if exposure shifted).
    if srgb_u8 is not None and srgb_u8.shape[:2] == rgb_linear.shape[:2]:
        max_u8 = srgb_u8.max(axis=2)
        source_clipped_pct = float(np.count_nonzero(max_u8 >= 254)) / total_pixels
        # Note: We don't necessarily use srgb_u8 for 'near_white' pivoting logic if user wants "current" state logic,
        # but checking source near-white is useful for "is this image naturally bright".
    else:
        # Fallback: estimate "source clipping" from pre-exposure linear if available, else current
        if pre_exposure_linear is not None:
            max_to_check = pre_exposure_linear.max(axis=2)
        else:
            max_to_check = rgb_linear.max(axis=2)

        source_clipped_pct = (
            float(np.count_nonzero(max_to_check >= _LINEAR_THRESHOLD_254))
            / total_pixels
        )

    # 2. Current Near-White Statistics (for Pivot Nudging)
    # This drives the "micro-contrast feel" based on how bright the image IS NOW.
    # Calculate max_rgb if we didn't do it earlier (when pre_exposure_linear was provided)
    if pre_exposure_linear is not None:
        max_rgb = rgb_linear.max(axis=2)

    current_nearwhite_pct = (
        float(
            np.count_nonzero(
                (max_rgb >= _LINEAR_THRESHOLD_250) & (max_rgb < _LINEAR_THRESHOLD_254)
            )
        )
        / total_pixels
    )

    # Legacy compat: near_white_pct usually referred to current state in previous logic?
    # Actually previous logic tried to use srgb_u8 if available for 'near_white_pct', which implies source.
    # But for pivot nudging, we might want current?
    # The user said: "drive 'pivot nudging' off current_nearwhite_pct" and "drive 'JPEG fallback' off source_clipped_pct".
    # So we provide both.

    return {
        "headroom_pct": headroom_pct,
        "source_clipped_pct": source_clipped_pct,
        "current_nearwhite_pct": current_nearwhite_pct,
    }


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation between a and b by t (clamped 0-1)."""
    t_clamped = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return a + (b - a) * t_clamped


def _highlight_recover_linear(
    rgb_linear: np.ndarray,
    amount: float,
    *,
    pivot: float = 0.7,
    k: float = 8.0,
    chroma_rolloff: float = 0.15,
    headroom_ceiling: float = 1.0,
) -> np.ndarray:
    """Apply highlight recovery using brightness-based rescaling to preserve hue.

    Why brightness-based rescale?
    - Per-channel compression causes hue/chroma shifts (e.g., bright red becomes pink).
    - By computing a single brightness metric and rescaling all channels equally,
      we preserve the original RGB color ratios (hue and relative saturation).

    For 16-bit sources with headroom (values > 1.0), the curve compresses into
    [pivot, headroom_ceiling] rather than [pivot, 1.0], preserving subtle tonal
    separation above 1.0 that represents real recovered detail.

    Args:
        rgb_linear: Float32 RGB array (H, W, 3) in linear light, may have values > 1.0
        amount: Recovery strength 0.0-1.0 (mapped from slider -100 to 0)
        pivot: Brightness threshold below which no recovery occurs
        k: Compression factor (adaptive). Higher k = stronger shoulder.
        chroma_rolloff: Desaturation amount in extreme highlights (0-1)
        headroom_ceiling: Maximum output brightness (> 1.0 preserves headroom detail)

    Returns:
        Recovered float32 RGB array (linear)
    """
    if amount < 0.001:
        return rgb_linear

    eps = 1e-7

    # Use max-channel as brightness metric - handles saturated highlights better than luminance
    brightness = rgb_linear.max(axis=2)

    # Build smooth highlight mask: 0 below pivot, 1 in highlights
    # Use headroom_ceiling instead of 1.0 for the normalization range
    mask = _smoothstep01((brightness - pivot) / (headroom_ceiling - pivot + eps))

    # Highlights recovery: we want to pull down highlights to reveal detail.
    # Rational compression formula: y = x / (1 + kx).
    # We apply this relative to the pivot.
    # normalization: brightness is already linear.
    x_norm = (brightness - pivot) / (headroom_ceiling - pivot + eps)
    x_norm = np.clip(x_norm, 0.0, None)

    # Compressed value (normalized context)
    # At amount=1, we use full rational compression.
    # At amount=0, we use identity.
    compressed_norm = x_norm / (1.0 + k * amount * x_norm)

    # Map back to brightness scale
    target_brightness = pivot + compressed_norm * (headroom_ceiling - pivot)
    # Clamp to headroom_ceiling to satisfy docstring contract (small amount can cause overshoot)
    target_brightness = np.minimum(target_brightness, headroom_ceiling)

    # If brightness was below pivot, keep it as is
    target_brightness = np.where(brightness > pivot, target_brightness, brightness)

    # Rescale RGB to preserve hue/chroma
    # Protect against div-by-zero or huge scale factors for near-black pixels
    scale = np.clip(target_brightness / (brightness + eps), 0.0, 2.0)
    scale = np.expand_dims(scale, axis=2)
    recovered = rgb_linear * scale

    # Optional chroma rolloff in extreme highlights to reduce "neon" colors
    if chroma_rolloff > 0.001:
        # Use target_brightness (post-compression) for the mask to maintain monotonicity
        # Normalize against headroom_ceiling for consistent behavior
        extreme_mask = _smoothstep01(
            (target_brightness - _CHROMA_ROLLOFF_START * headroom_ceiling)
            / (_CHROMA_ROLLOFF_WIDTH * headroom_ceiling)
        )
        extreme_mask = np.expand_dims(extreme_mask, axis=2)

        # Compute grayscale (luminance) of recovered image
        gray = (
            recovered[:, :, 0:1] * 0.2126
            + recovered[:, :, 1:2] * 0.7152
            + recovered[:, :, 2:3] * 0.0722
        )

        # Desaturate in extreme highlights
        # Note: This preserves monotonicity because both recovered and gray are
        # monotonic with respect to input brightness, and we blend between them.
        desat_amount = chroma_rolloff * amount * extreme_mask
        recovered = recovered * (1.0 - desat_amount) + gray * desat_amount

    return recovered


def _highlight_boost_linear(
    rgb_linear: np.ndarray,
    amount: float,
    *,
    pivot: float = 0.5,
) -> np.ndarray:
    """Apply highlight boost using brightness-based rescaling to preserve hue.

    Uses same hue-preserving approach as recovery for symmetry.

    Args:
        rgb_linear: Float32 RGB array (H, W, 3) in linear light
        amount: Boost strength 0.0-1.0 (mapped from slider 0 to 100)
        pivot: Brightness threshold below which minimal boost occurs

    Returns:
        Boosted float32 RGB array (linear)
    """
    if amount < 0.001:
        return rgb_linear

    eps = 1e-7

    brightness = rgb_linear.max(axis=2)

    # Build mask for highlights
    mask = _smoothstep01((brightness - pivot) / (1.0 - pivot + eps))

    # Target brightness: lift with curve
    target_brightness = brightness * (1.0 + amount * 1.5 * mask)

    # Rescale RGB to preserve hue, cap scale at 1.5x to prevent blowout
    scale = np.clip(target_brightness / (brightness + eps), 0.0, 2.0)
    scale = np.minimum(scale, 1.5)  # Direct cap on scale
    scale = np.expand_dims(scale, axis=2)

    return rgb_linear * scale
