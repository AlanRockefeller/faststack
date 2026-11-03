from typing import Optional

import numpy as np

from faststack.imaging.optional_deps import get_cv2

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


# LUT-accelerated transfer functions for full-image renders. The exact
# np.where + power versions above cost ~60ms per megapixel-scale call; the
# quantized lookups below are ~3x faster. 65536 entries keeps the worst-case
# round-trip error under 0.03 of one u8 step, far below the 8-bit
# quantization every render goes through afterwards. Keep the exact versions
# for tiny inputs where exactness is free (e.g. building 256-entry save LUTs).
_TRANSFER_LUT_SIZE = 65536
# Headroom shoulder caps linear values at 1.0 + max_overshoot (0.05); leave
# a little margin. Inputs above the domain clamp to the last entry, which is
# equivalent to the exact path once the caller clips display output to [0,1].
_LINEAR_TO_SRGB_DOMAIN = 1.06

_srgb_to_linear_lut: Optional[np.ndarray] = None
_linear_to_srgb_lut: Optional[np.ndarray] = None
_srgb_u8_to_linear_lut: Optional[np.ndarray] = None


def _srgb_to_linear_fast(x: np.ndarray) -> np.ndarray:
    """LUT-based `_srgb_to_linear` for preview/display renders.

    Input is clamped to [0.0, 1.0] — unlike the exact version this does NOT
    preserve headroom above 1.0, so only call it on base image data (which is
    always in [0, 1]) or analysis buffers feeding u8 display output.
    """
    global _srgb_to_linear_lut
    lut = _srgb_to_linear_lut
    if lut is None:
        xs = np.linspace(0.0, 1.0, _TRANSFER_LUT_SIZE, dtype=np.float64)
        lut = _srgb_to_linear(xs).astype(np.float32)
        _srgb_to_linear_lut = lut
    idx = np.clip(
        x * np.float32(_TRANSFER_LUT_SIZE - 1) + np.float32(0.5),
        0,
        _TRANSFER_LUT_SIZE - 1,
    ).astype(np.uint16)
    return lut[idx]


def _linear_to_srgb_fast(x: np.ndarray) -> np.ndarray:
    """LUT-based `_linear_to_srgb` for preview/display renders.

    Covers [0, 1.06]; inputs above that clamp to the last entry, which is
    indistinguishable from the exact version after the caller clips display
    output to [0, 1].
    """
    global _linear_to_srgb_lut
    lut = _linear_to_srgb_lut
    if lut is None:
        xs = np.linspace(
            0.0, _LINEAR_TO_SRGB_DOMAIN, _TRANSFER_LUT_SIZE, dtype=np.float64
        )
        lut = _linear_to_srgb(xs).astype(np.float32)
        _linear_to_srgb_lut = lut
    idx = np.clip(
        x * np.float32((_TRANSFER_LUT_SIZE - 1) / _LINEAR_TO_SRGB_DOMAIN)
        + np.float32(0.5),
        0,
        _TRANSFER_LUT_SIZE - 1,
    ).astype(np.uint16)
    return lut[idx]


def _srgb_u8_exact_to_linear(x: np.ndarray) -> np.ndarray:
    """`_srgb_to_linear` for float data that holds exact 8-bit values (k/255).

    Recovers each u8 code with one rounding pass, then looks it up in a
    256-entry table. Several times faster than `_srgb_to_linear_fast` on
    full-resolution exports, and exact at every code. Only valid when every
    value is k/255 in [0, 1] -- an 8-bit source after lossless geometry
    (90-degree rotation, crop). Interpolated data (straighten, resize) must
    use the general path.
    """
    global _srgb_u8_to_linear_lut
    lut = _srgb_u8_to_linear_lut
    if lut is None:
        codes = np.arange(256, dtype=np.float64) / 255.0
        lut = _srgb_to_linear(codes).astype(np.float32)
        _srgb_u8_to_linear_lut = lut
    cv2 = get_cv2()
    if cv2 is not None and x.dtype == np.float32 and x.ndim in (2, 3):
        # Round-to-nearest with u8 saturation; the input is never negative
        # here, so convertScaleAbs's abs() is a no-op.
        codes_u8 = cv2.convertScaleAbs(x, alpha=255.0)
        return cv2.LUT(codes_u8, lut)
    codes_u8 = np.clip(x * np.float32(255.0) + np.float32(0.5), 0, 255).astype(np.uint8)
    return lut[codes_u8]


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


# ----------------------------
# Highlights: edge-aware local tone mapping
# ----------------------------
#
# A global tone curve cannot recover highlight detail: to darken the bright
# band it must flatten its slope there, which compresses exactly the tonal
# differences that carry the detail (petal veins, cloud texture). Instead the
# brightness is split into a smooth, edge-preserving *base* (the illumination
# of each bright region) and the *detail* riding on it. Only the base is
# compressed, so a bright region gets darker as a whole while the texture
# inside keeps its full amplitude -- plus a mild boost to make it read on
# screen. This is the same base/detail approach as raw converters' Highlights
# slider.
#
# All of it runs on a perceptual brightness ``p = sqrt(v)`` (gamma 2, close to
# how JPEG codes are spaced), where ``v`` mixes luminance and the max channel
# so saturated colours near clipping count as bright. The result goes back to
# RGB as one gain per pixel, which keeps hue and saturation.

# Perceptual base level where the effect begins (about sRGB 0.45). The curve
# deviates quadratically from identity, so the onset is very gentle.
HIGHLIGHT_PIVOT = 0.45
# At -100 a base at display white (p = 1.0) comes down to 1.0 - this.
_HIGHLIGHT_WHITE_DROP = 0.20
# Over-white (exposure headroom) is pulled down until the brightest 0.5% of
# the base lands here; reached by -50 so moderate settings tame the clipping.
_HIGHLIGHT_HEADROOM_TARGET = 0.95
# Detail amplitude gain inside bright regions at -100, and the soft limit on
# how large a deviation counts as "detail" (larger ones pass through unboosted).
_HIGHLIGHT_DETAIL_BOOST = 0.6
_HIGHLIGHT_DETAIL_LIMIT = 0.08
# Positive slider: base expansion strength at +100.
_HIGHLIGHT_BOOST_GAIN = 0.45
# Edge-aware base: guided-filter coefficients are solved on a small copy (so
# preview and export see the same regions) and applied at full resolution.
_HIGHLIGHT_BASE_LONG_EDGE = 768
_HIGHLIGHT_BASE_RADIUS_FRAC = 0.012
_HIGHLIGHT_BASE_EPS = 0.006

_LUMA_709 = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _highlight_brightness(rgb_linear: np.ndarray) -> np.ndarray:
    """Linear highlight brightness: mean of Rec.709 luminance and max channel."""
    cv2 = get_cv2()
    if (
        cv2 is not None
        and rgb_linear.dtype == np.float32
        and rgb_linear.flags.c_contiguous
    ):
        luma = cv2.transform(rgb_linear, _LUMA_709.reshape(1, 3))
        # Channel max as a row reduction over (pixels, 3): several times
        # faster than splitting the channels first.
        peak = cv2.reduce(rgb_linear.reshape(-1, 3), 1, cv2.REDUCE_MAX)
        v = cv2.addWeighted(luma, 0.5, peak.reshape(luma.shape), 0.5, 0.0)
    else:
        r = rgb_linear[..., 0]
        g = rgb_linear[..., 1]
        b = rgb_linear[..., 2]
        # ndarray.max(axis=2) is an order of magnitude slower than this on
        # interleaved float32 images.
        v = np.maximum(np.maximum(r, g), b).astype(np.float32)
        v += r * _LUMA_709[0] + g * _LUMA_709[1] + b * _LUMA_709[2]
        v *= np.float32(0.5)
    np.maximum(v, 0.0, out=v)
    return v


def _box_mean(arr: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2r+1)^2 window with reflected borders."""
    cv2 = get_cv2()
    k = 2 * radius + 1
    if cv2 is not None:
        return cv2.boxFilter(arr, -1, (k, k), borderType=cv2.BORDER_REFLECT)
    padded = np.pad(arr, radius, mode="symmetric").astype(np.float64)
    c = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    h, w = arr.shape
    total = c[k : k + h, k : k + w] - c[:h, k : k + w] - c[k : k + h, :w] + c[:h, :w]
    return (total / (k * k)).astype(np.float32)


def _resize_2d(arr: np.ndarray, size: tuple, *, shrink: bool) -> np.ndarray:
    """Resize a float32 plane to ``size`` = (width, height)."""
    cv2 = get_cv2()
    if cv2 is not None:
        interp = cv2.INTER_AREA if shrink else cv2.INTER_LINEAR
        return cv2.resize(arr, size, interpolation=interp)
    from PIL import Image

    resample = Image.Resampling.BOX if shrink else Image.Resampling.BILINEAR
    img = Image.fromarray(np.ascontiguousarray(arr, dtype=np.float32), mode="F")
    return np.asarray(img.resize(size, resample=resample), dtype=np.float32)


def compute_highlight_base(rgb_linear: np.ndarray) -> tuple:
    """Edge-aware base layer for the Highlights slider.

    Returns ``(p, base, headroom)``: the perceptual brightness ``p``, its
    edge-preserving smooth ``base`` (both H x W float32), and ``headroom``,
    the 99.5th percentile of the base (> 1.0 when exposure pushed whole
    regions past display white).

    The self-guided filter keeps strong edges (a branch against the sky) in
    the base, so compressing the base does not halo, while fine texture is
    left in ``p - base``. Its coefficients are solved at a fixed small size
    and upsampled -- a "fast guided filter" -- which keeps the regions the
    same for the preview and the full-resolution export.
    """
    p = np.sqrt(_highlight_brightness(rgb_linear))
    h, w = p.shape
    scale = max(1.0, max(h, w) / _HIGHLIGHT_BASE_LONG_EDGE)
    small_size = (max(1, round(w / scale)), max(1, round(h / scale)))
    small = _resize_2d(p, small_size, shrink=True) if scale > 1.0 else p
    radius = max(1, round(_HIGHLIGHT_BASE_RADIUS_FRAC * max(small.shape)))
    mean = _box_mean(small, radius)
    var = np.maximum(_box_mean(small * small, radius) - mean * mean, 0.0)
    a = var / (var + np.float32(_HIGHLIGHT_BASE_EPS))
    b = mean - a * mean
    a = _box_mean(a, radius)
    b = _box_mean(b, radius)

    small_base = a * small + b
    flat = small_base.ravel()
    k = min(flat.size - 1, int(flat.size * 0.995))
    headroom = float(np.partition(flat, k)[k]) if flat.size else 1.0

    if scale > 1.0:
        a = _resize_2d(a, (w, h), shrink=False)
        b = _resize_2d(b, (w, h), shrink=False)
    # Reuse the upsampled coefficient buffer for the full-resolution base.
    a *= p
    a += b
    return p, a, headroom


def _highlight_compress_strength(amount, headroom: float):
    """Rational-curve strength ``c`` for a recovery amount in [0, 1]."""
    span = 1.0 - HIGHLIGHT_PIVOT
    c_white = 1.0 / (span - _HIGHLIGHT_WHITE_DROP) - 1.0 / span
    c = amount * c_white
    excess = headroom - HIGHLIGHT_PIVOT
    target = _HIGHLIGHT_HEADROOM_TARGET - HIGHLIGHT_PIVOT
    if excess > target:
        c_head = (excess / target - 1.0) / excess
        c = np.maximum(c, np.minimum(2.0 * amount, 1.0) * c_head)
    return c


def apply_highlight_tone(
    rgb_linear: np.ndarray,
    amount,
    base: np.ndarray,
    headroom: float = 1.0,
    p: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Apply the Highlights slider in linear light.

    Args:
        rgb_linear: (H, W, 3) float32 linear RGB, may exceed 1.0.
        amount: Slider value in [-1, 1] (negative recovers), either a scalar
            or an (H, W) per-pixel map for brushed adjustments.
        base: Edge-aware base from ``compute_highlight_base`` for these
            pixels (a crop of the full-image base is fine).
        headroom: Base headroom level from ``compute_highlight_base``.
        p: Perceptual brightness of ``rgb_linear`` if already computed.

    Returns:
        New float32 linear RGB array.
    """
    scalar = np.ndim(amount) == 0
    if scalar:
        amount = float(np.clip(amount, -1.0, 1.0))
        if abs(amount) <= 0.001:
            return rgb_linear
    else:
        amount = np.clip(np.asarray(amount, dtype=np.float32), -1.0, 1.0)
        amount = np.broadcast_to(amount, base.shape)

    if p is None:
        p = np.sqrt(_highlight_brightness(rgb_linear))

    # Every term below scales with the base's height over the pivot, so
    # only pixels in regions above it change. They are usually a minority:
    # work on just those and leave the rest untouched.
    active = base > np.float32(HIGHLIGHT_PIVOT)
    n_active = int(np.count_nonzero(active))
    if n_active == 0:
        return rgb_linear
    if n_active < active.size // 2:
        idx = np.flatnonzero(active)
        amount_px = amount if scalar else amount.ravel()[idx]
        gain = _highlight_gain(p.ravel()[idx], base.ravel()[idx], amount_px, headroom)
        out = np.array(rgb_linear, dtype=np.float32, order="C")
        flat = out.reshape(-1, 3)
        flat[idx] *= gain[:, None]
        return out
    gain = _highlight_gain(p, base, amount, headroom)
    out = rgb_linear * gain[..., None]
    return out.astype(np.float32, copy=False)


def _highlight_gain(p: np.ndarray, base: np.ndarray, amount, headroom: float):
    """Per-pixel linear gain for ``apply_highlight_tone`` (any matching shapes)."""
    t = np.maximum(base - np.float32(HIGHLIGHT_PIVOT), 0.0)
    amount = np.asarray(amount, dtype=np.float32)
    recover = np.maximum(-amount, 0.0)
    boost = np.maximum(amount, 0.0)
    any_recover = bool(np.any(recover > 0.001))

    # Base tone curve over the pivot, as a shift to add. Recovery uses
    # t / (1 + c t): identity slope at the pivot, smoothly flattening, and
    # bounded so any amount of headroom lands below white. Boost uses
    # t (1 + c t), its mirror image.
    shift = np.zeros_like(t)
    if any_recover:
        c = np.asarray(
            _highlight_compress_strength(recover, headroom), dtype=np.float32
        )
        shift -= c * t * t / (1.0 + c * t)
    if np.any(boost > 0.001):
        shift += np.float32(_HIGHLIGHT_BOOST_GAIN) * boost * t * t

    # Pixels much darker than their region (a twig against the sky) keep
    # most of their level instead of being pushed toward black.
    shift *= _smoothstep01((p - np.float32(0.15)) / np.float32(0.45))

    p_new = p + shift
    if any_recover:
        weight = _smoothstep01(t / np.float32(1.0 - HIGHLIGHT_PIVOT))
        # Soft-limit the deviation (rational, cheaper than tanh) so only
        # texture-scale detail is amplified, not real edges.
        detail = p - base
        detail /= 1.0 + np.abs(detail) * np.float32(1.0 / _HIGHLIGHT_DETAIL_LIMIT)
        p_new += np.float32(_HIGHLIGHT_DETAIL_BOOST) * recover * weight * detail
    np.maximum(p_new, 0.0, out=p_new)

    # Back to linear as a gain on brightness (p is its square root).
    v = p * p
    return np.divide(
        p_new * p_new, v, out=np.ones_like(v), where=v > np.float32(1e-8)
    )
