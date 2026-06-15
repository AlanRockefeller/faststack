"""Non-destructive image editor: crop, rotate, exposure, contrast, WB, sharpness."""

import io
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import ExifTags, Image, ImageFilter, ImageOps

from faststack.imaging.jpeg import TURBO_AVAILABLE, decode_jpeg_rgb

# Mask subsystem (lazy imports avoided — lightweight dataclasses)
from faststack.imaging.mask import MaskData
from faststack.imaging.mask_engine import MaskRasterCache
from faststack.imaging.math_utils import (
    _analyze_highlight_state,
    _apply_headroom_shoulder,
    _highlight_boost_linear,
    _highlight_recover_linear,
    _lerp,
    _linear_to_srgb,
    _linear_to_srgb_fast,
    _smoothstep01,
    _srgb_to_linear,
    _srgb_to_linear_fast,
)
from faststack.imaging.orientation import apply_orientation_to_np, get_exif_orientation
from faststack.imaging.prefetch import apply_loupe_color_correction
from faststack.models import DecodedImage

try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None

from faststack.imaging.optional_deps import cv2

log = logging.getLogger(__name__)

_REPLACE_RETRY_DELAY = 0.3
_REPLACE_MAX_RETRIES = 3
_AUTO_VIBRANCE_MAX = 0.18
_AUTO_VIBRANCE_MIN = 0.03
_AUTO_VIBRANCE_TARGET_SAT = 0.22
_AUTO_VIBRANCE_SAT_CEILING = 0.18
_AUTO_VIBRANCE_MIN_COLOR_DELTA = 0.015
_AUTO_VIBRANCE_CLIP_TOLERANCE = 0.0001
_AUTO_LEVELS_ANALYSIS_MAX_EDGE = 1920
# Colorful-subject guard: median saturation can be low while a small subject
# is already vivid. Scale the boost down as the 90th-percentile saturation
# approaches full saturation so that subject is not pushed garish.
_AUTO_VIBRANCE_P90_SOFT = 0.55
_AUTO_VIBRANCE_P90_HARD = 0.85
# Skin protection: halve the boost when a meaningful share of the analyzed
# pixels falls in the skin-tone hue/saturation envelope.
_AUTO_VIBRANCE_SKIN_FRACTION = 0.04
_AUTO_VIBRANCE_SKIN_FACTOR = 0.5

# Levels soft knee/toe: instead of hard-clipping the linear blacks/whites
# ramp, values beyond the shoulder (or toe) are compressed smoothly so
# stretched highlights keep tonal separation and hue instead of slamming each
# channel to pure white/black independently. Spans are output-range fractions.
_LEVELS_SHOULDER_SPAN = 0.05
_LEVELS_TOE_SPAN = 0.02
# Output values that correspond to a pre-soft-clip value of exactly 1.0 / 0.0:
# shoulder(1.0) = knee + span*(1 - e^-1) = 1 - span*e^-1, toe(0.0) = toe*e^-1.
# Used to count "effectively clipped" pixels whether or not the knee is active.
_SOFT_CLIP_HI_MARK = 1.0 - _LEVELS_SHOULDER_SPAN * math.exp(-1.0)
_SOFT_CLIP_LO_MARK = _LEVELS_TOE_SPAN * math.exp(-1.0)

# Export dither: TPDF noise hides the 8-bit banding that appears when a
# levels stretch amplifies the source's quantization steps. Only applied at
# or above this stretch gain.
_EXPORT_DITHER_MIN_GAIN = 1.2
# Cap on the longest edge of the buffer analyze_auto_vibrance renders. The
# recommendation only depends on aggregate saturation/clipping statistics, which
# are stable under uniform downsampling, so bounding the resolution keeps the
# several _apply_edits passes cheap on the UI thread (Shift+L is synchronous).
_AUTO_VIBRANCE_ANALYSIS_MAX_EDGE = 640


_REC601_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _rec601_gray(arr: np.ndarray) -> np.ndarray:
    """Rec.601 luma of an (H, W, 3) float32 array, staying in float32.

    The naive ``arr.dot([0.299, 0.587, 0.114])`` promotes through float64
    (Python-list coefficients), which silently doubles the memory traffic of
    every downstream blend; cv2.transform is also multithreaded.
    """
    if cv2 is not None and arr.flags["C_CONTIGUOUS"]:
        return cv2.transform(arr, _REC601_LUMA.reshape(1, 3))
    return arr @ _REC601_LUMA


def _float01_to_u8(arr: np.ndarray) -> np.ndarray:
    """Convert [0,1]-range float RGB to uint8 for encoding.

    cv2.convertScaleAbs fuses scale+round+saturate into one multithreaded
    pass (the numpy fallback truncates instead of rounding — a <=1 LSB
    difference well below JPEG encoding noise).
    """
    clipped = np.clip(arr, 0.0, 1.0)
    if cv2 is not None:
        return cv2.convertScaleAbs(clipped, alpha=255.0)
    return (clipped * 255).astype(np.uint8)


def _apply_levels_soft_clip(arr: np.ndarray) -> np.ndarray:
    """Soft shoulder/toe for the levels ramp (mutates ``arr`` in place).

    The linear blacks/whites ramp sends out-of-range values past [0, 1] where
    they would later hard-clip per channel — hue shifts in bright saturated
    areas and abrupt steps in crushed shadows. Compress everything beyond the
    shoulder/toe with an exponential rolloff that is C1-continuous at the
    junction and asymptotes to the range limits, so a value of exactly 1.0
    lands at ``_SOFT_CLIP_HI_MARK`` and strong overshoot still approaches 1.0.

    Callers must pass an array they own (the levels ramp always allocates).
    """
    knee = 1.0 - _LEVELS_SHOULDER_SPAN
    hi = arr > knee
    if np.any(hi):
        v = arr[hi]
        # minimum() guards float32 rounding (knee + span can sum past 1.0)
        arr[hi] = np.minimum(
            knee
            + _LEVELS_SHOULDER_SPAN
            * (1.0 - np.exp((knee - v) / _LEVELS_SHOULDER_SPAN)),
            1.0,
        )
    toe = _LEVELS_TOE_SPAN
    lo = arr < toe
    if np.any(lo):
        v = arr[lo]
        arr[lo] = np.maximum(toe * np.exp((v - toe) / toe), 0.0)
    return arr


def _normalized_wb_gains(by: float, mg: float) -> Tuple[float, float, float]:
    """Linear-space WB channel gains, normalized to preserve luminance.

    ``by``/``mg`` are the slider values already scaled by 0.5. The gains are
    divided by their Rec.709-weighted sum so a neutral gray keeps its linear
    luminance: white balance shifts hue only instead of also brightening or
    darkening the image. Channel ratios (what the AWB estimator solves for)
    are unaffected by the normalization.
    """
    r_gain = 1.0 + by
    b_gain = 1.0 - by
    g_gain = 1.0 - mg
    luma_gain = 0.2126 * r_gain + 0.7152 * g_gain + 0.0722 * b_gain
    if luma_gain > 1e-6:
        r_gain /= luma_gain
        g_gain /= luma_gain
        b_gain /= luma_gain
    return r_gain, g_gain, b_gain


def _safe_replace(tmp_path: Path, target_path: Path) -> None:
    """Atomically replace target with tmp, retrying on Windows file-lock errors."""
    for attempt in range(_REPLACE_MAX_RETRIES):
        try:
            os.replace(str(tmp_path), str(target_path))
            return
        except OSError:
            if attempt < _REPLACE_MAX_RETRIES - 1:
                time.sleep(_REPLACE_RETRY_DELAY)
            else:
                raise


# Aspect Ratios for cropping
INSTAGRAM_RATIOS = {
    "Freeform": None,
    "1:1 (Square)": (1, 1),
    "4:5 (Portrait)": (4, 5),
    "1.91:1 (Landscape)": (191, 100),
    "9:16 (Story)": (9, 16),
}


def sanitize_exif_orientation(exif_bytes: bytes | None) -> bytes | None:
    """
    Parses EXIF bytes and resets Orientation to 1 (Normal).
    Returns cleaned bytes or None if parsing/sanitizing fails.
    """
    if not exif_bytes:
        return None
    try:
        exif = Image.Exif()
        exif.load(exif_bytes)
        # Pillow 9.1.0+ has ExifTags.Base.Orientation, fallback to 0x0112 if needed
        orientation_tag = getattr(ExifTags.Base, "Orientation", 0x0112)
        exif[orientation_tag] = 1
        return exif.tobytes()
    except Exception:
        # If we can't parse/sanitize, safest is to drop EXIF to avoid rotation bugs
        return None


def create_backup_file(original_path: Path) -> Optional[Path]:
    """
    Creates a backup of the original file with naming pattern:
    filename-backup.jpg, filename-backup2.jpg, etc.

    Returns:
        Path to the backup file on success, None on failure.
    """
    if not original_path.exists():
        return None

    # Extract base name without any existing -backup suffix
    stem = original_path.stem
    # Remove any existing -backup, -backup2, -backup-1, etc. (handles both old and new formats)
    base_stem = re.sub(r"-backup(-?\d+)?$", "", stem)

    # Try filename-backup.jpg first
    backup_path = original_path.parent / f"{base_stem}-backup{original_path.suffix}"

    # If that exists, try filename-backup2.jpg, filename-backup3.jpg, etc.
    i = 2
    while backup_path.exists():
        backup_path = (
            original_path.parent / f"{base_stem}-backup{i}{original_path.suffix}"
        )
        i += 1

    try:
        # Perform the backup
        shutil.copy2(original_path, backup_path)
        return backup_path
    except OSError as e:
        log.exception("Failed to create backup: %s", e)
        return None


# ----------------------------
# sRGB ↔ Linear Conversion Helpers
# ----------------------------


# Constants for Highlight Recovery

# Highlight Compression Curve
HEADROOM_COMPRESSION_STEEPNESS = 2.0

# Adaptive Parameters (tuned by image content analysis)
# Pivot: Linear-light brightness where recovery starts to take hold. The effect
# tapers in smoothly (smoothstep) from zero at the pivot to full at display
# white, so a low pivot widens the affected band across all bright pixels
# (Photoshop-style) while still leaving midtones near the pivot essentially
# untouched. linear 0.30/0.50 ≈ sRGB 0.58/0.74.
ADAPTIVE_PIVOT_MIN = 0.30
ADAPTIVE_PIVOT_MAX = 0.50

# K Factor: Steepness of the over-white compression shoulder
ADAPTIVE_K_BASE = 8.0
ADAPTIVE_K_SCALING = 4.0
ADAPTIVE_K_HEADROOM_BASE = 8.0
ADAPTIVE_K_HEADROOM_SCALING = 4.0

# Chroma Rolloff: Desaturation in extreme highlights
ADAPTIVE_ROLLOFF_MIN = 0.02
ADAPTIVE_ROLLOFF_MAX = 0.12

# Analysis Safety
HEADROOM_MAX_BRIGHTNESS_PERCENTILE = 99.5


def _gaussian_blur_float(arr: np.ndarray, radius: float) -> np.ndarray:
    """Apply Gaussian Blur to a float32 array using OpenCV.

    Preserves values outside [0, 1] range.
    """
    if radius <= 0:
        return arr

    if cv2 is None:
        # Fallback: Use Pillow's GaussianBlur in 'F' mode (float32) per channel
        # This preserves values > 1.0 (headroom) which is critical for highlight recovery.
        try:
            c = arr.shape[2]
            blurred_channels = []

            # Process each channel independently
            for i in range(c):
                ch_data = arr[:, :, i]
                # Scale float range to uint8 to allow Pillow filters (they don't support 'F' mode)
                # We scale the actual range [min, max] (but at least [0, 1]) to [0, 255]
                mx = max(1.0, float(ch_data.max()))
                mn = min(0.0, float(ch_data.min()))
                scale = mx - mn

                if scale > 0:
                    ch_u8 = ((ch_data - mn) / scale * 255).astype(np.uint8)
                    ch_img = Image.fromarray(ch_u8, mode="L")
                    # Pillow's GaussianBlur radius is roughly comparable to OpenCV sigma
                    blurred_ch_img = ch_img.filter(
                        ImageFilter.GaussianBlur(radius=radius)
                    )
                    # Scale back to original float range
                    blurred_ch = (
                        np.array(blurred_ch_img).astype(np.float32) / 255.0 * scale + mn
                    )
                    blurred_channels.append(blurred_ch)
                else:
                    blurred_channels.append(ch_data.copy())

            # Stack back into (H, W, C)
            return np.stack(blurred_channels, axis=-1)

        except Exception as e:
            log.warning("Fallback blur failed: %s", e)
            return arr

    # Sigma calculation matching Pillow's radius-to-sigma
    # Radius in Pillow is the radius of the kernel, sigma is approx radius / 2
    # OpenCV's GaussianBlur takes sigma.
    sigma = radius / 2.0

    # We use (0, 0) for ksize to let OpenCV calculate it based on sigma
    return cv2.GaussianBlur(
        arr, (0, 0), sigmaX=sigma, sigmaY=sigma, borderType=cv2.BORDER_REFLECT
    )


# ----------------------------
# Rotate + Autocrop helper
# ----------------------------


def _rotated_rect_with_max_area(w: int, h: int, angle_rad: float) -> tuple[int, int]:
    """
    Largest axis-aligned rectangle within a w x h rectangle rotated by angle_rad.
    Returns (crop_w, crop_h) in pixels.
    """
    if w <= 0 or h <= 0:
        return 0, 0

    # fold angle into [0, pi/2)
    angle_rad = abs(angle_rad) % (math.pi / 2)
    if angle_rad > math.pi / 4:
        angle_rad = (math.pi / 2) - angle_rad

    sin_a = abs(math.sin(angle_rad))
    cos_a = abs(math.cos(angle_rad))

    # if basically unrotated
    if sin_a < 1e-12:
        return w, h

    width_is_longer = w >= h
    side_long = w if width_is_longer else h
    side_short = h if width_is_longer else w

    # "half constrained" case
    if side_short <= 2.0 * sin_a * cos_a * side_long or abs(sin_a - cos_a) < 1e-12:
        x = 0.5 * side_short
        if width_is_longer:
            wr = x / sin_a
            hr = x / cos_a
        else:
            wr = x / cos_a
            hr = x / sin_a
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a

    cw = math.floor(abs(wr))
    ch = math.floor(abs(hr))
    cw = max(1, min(w, cw))
    ch = max(1, min(h, ch))
    return cw, ch


def _expanded_canvas_size(
    src_w: int, src_h: int, straighten_angle: float
) -> tuple[int, int]:
    """Canvas size produced by PIL ``rotate(expand=True)`` for a src_w x src_h image.

    Mirrors PIL's expand computation (corner extents with ceil/floor) so
    geometry can be reasoned about without rotating pixels first.
    """
    # PIL special-cases right angles to exact transposes; the corner-extent
    # math below would inflate them by 1-2px of float epsilon.
    remainder = abs(straighten_angle) % 90.0
    if remainder < 0.01 or remainder > 89.99:
        if round(straighten_angle / 90.0) % 2:
            return src_h, src_w
        return src_w, src_h
    angle_rad = math.radians(straighten_angle)
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    xs, ys = [], []
    for px, py in ((0, 0), (src_w, 0), (src_w, src_h), (0, src_h)):
        dx, dy = px - src_w / 2.0, py - src_h / 2.0
        xs.append(dx * cos_a - dy * sin_a)
        ys.append(dx * sin_a + dy * cos_a)
    return (
        int(math.ceil(max(xs)) - math.floor(min(xs))),
        int(math.ceil(max(ys)) - math.floor(min(ys))),
    )


def _rotated_content_point(
    px: float,
    py: float,
    src_w: float,
    src_h: float,
    canvas_w: float,
    canvas_h: float,
    cos_a: float,
    sin_a: float,
) -> tuple[float, float]:
    """Where a source pixel lands on the expanded canvas after straightening.

    The image is rotated with PIL ``rotate(-straighten_angle, expand=True)``;
    a content point at offset d from the source center maps to
    ``R(+straighten_angle) @ d`` in this y-down formula (verified against a
    rotated-marker ground truth). ``cos_a``/``sin_a`` are of
    ``+straighten_angle``.
    """
    dx, dy = px - src_w / 2.0, py - src_h / 2.0
    return (
        dx * cos_a - dy * sin_a + canvas_w / 2.0,
        dx * sin_a + dy * cos_a + canvas_h / 2.0,
    )


def _source_footprint_halfplanes(
    src_w: float,
    src_h: float,
    canvas_w: float,
    canvas_h: float,
    cos_a: float,
    sin_a: float,
) -> list[tuple[float, float, float]]:
    """Half-planes (unit outward normal nx, ny, offset c) bounding the rotated
    source rectangle on the expanded canvas. A point p is valid (real pixels,
    not rotation fill) iff ``nx*px + ny*py <= c`` for all four planes."""
    corners = [
        _rotated_content_point(px, py, src_w, src_h, canvas_w, canvas_h, cos_a, sin_a)
        for px, py in ((0.0, 0.0), (src_w, 0.0), (src_w, src_h), (0.0, src_h))
    ]
    cx = sum(p[0] for p in corners) / 4.0
    cy = sum(p[1] for p in corners) / 4.0
    planes = []
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        ex, ey = x2 - x1, y2 - y1
        norm = math.hypot(ex, ey)
        if norm <= 1e-9:
            continue
        nx, ny = ey / norm, -ex / norm
        if nx * (cx - x1) + ny * (cy - y1) > 0:
            nx, ny = -nx, -ny
        planes.append((nx, ny, nx * x1 + ny * y1))
    return planes


def _trim_rect_to_halfplanes(
    left: float,
    top: float,
    right: float,
    bottom: float,
    planes: list[tuple[float, float, float]],
    inset: float = 0.0,
) -> tuple[float, float, float, float]:
    """Shrink an axis-aligned rect until it lies inside every half-plane.

    Each violated plane is resolved by moving its extreme corner along the
    plane normal (the minimal cut). Shrinking a side never increases any
    plane's extreme-corner value, so one pass converges; extra passes guard
    against float noise.
    """
    for _ in range(3):
        dirty = False
        for nx, ny, c in planes:
            qx = right if nx > 0 else left
            qy = bottom if ny > 0 else top
            d = nx * qx + ny * qy - (c - inset)
            if d <= 1e-6:
                continue
            dirty = True
            if nx > 0:
                right -= nx * d
            else:
                left -= nx * d
            if ny > 0:
                bottom -= ny * d
            else:
                top -= ny * d
        if not dirty:
            break
    return left, top, right, bottom


def _autocrop_canvas_rect(
    cw: int, ch: int, canvas_w: int, canvas_h: int, straighten_angle: float
) -> tuple[int, int, int, int]:
    """Center the max-area autocrop rect on the expanded canvas, applying the
    legacy 2px inset (skipped for exact 90-degree angles) and clamps."""
    cx, cy = canvas_w / 2.0, canvas_h / 2.0
    left = round(cx - cw / 2.0)
    top = round(cy - ch / 2.0)
    right = left + cw
    bottom = top + ch

    # Apply inset (2px) to match legacy behavior and avoid edge artifacts.
    # Skip for exact 90-degree increments to preserve full dimensions.
    is_exact_90 = abs(straighten_angle % 90.0) < 0.01
    inset = 0 if is_exact_90 else 2
    if (right - left) > 2 * inset and (bottom - top) > 2 * inset:
        left += inset
        top += inset
        right -= inset
        bottom -= inset

    left = max(0, min(canvas_w - 1, left))
    top = max(0, min(canvas_h - 1, top))
    right = max(left + 1, min(canvas_w, right))
    bottom = max(top + 1, min(canvas_h, bottom))
    return left, top, right, bottom


def _crop_box_canvas_rect(
    crop_box: tuple[float, float, float, float],
    src_w: int,
    src_h: int,
    straighten_angle: float,
    canvas_w: int,
    canvas_h: int,
    inset: float = 2.0,
) -> tuple[int, int, int, int]:
    """Map a 0-1000 source-space crop box onto the expanded rotated canvas.

    Returns the int canvas rect to slice: an upright rect with the drawn
    box's own dimensions (swapped when the rotation lands at an odd
    90-degree multiple), centered where the framed content lands after
    rotation, then shrunk just enough that it contains no rotation fill.
    Using the bounding box of the rotated rectangle instead would deliver an
    image larger than the drawn box by ``w*|cos| + h*|sin|`` per axis.
    """
    angle_rad = math.radians(straighten_angle)
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)

    c_left = crop_box[0] * src_w / 1000.0
    c_top = crop_box[1] * src_h / 1000.0
    c_right = crop_box[2] * src_w / 1000.0
    c_bottom = crop_box[3] * src_h / 1000.0

    ccx, ccy = _rotated_content_point(
        (c_left + c_right) / 2.0,
        (c_top + c_bottom) / 2.0,
        src_w,
        src_h,
        canvas_w,
        canvas_h,
        cos_a,
        sin_a,
    )
    box_w = c_right - c_left
    box_h = c_bottom - c_top
    if round(straighten_angle / 90.0) % 2:
        box_w, box_h = box_h, box_w
    left = ccx - box_w / 2.0
    right = ccx + box_w / 2.0
    top = ccy - box_h / 2.0
    bottom = ccy + box_h / 2.0

    planes = _source_footprint_halfplanes(
        src_w, src_h, canvas_w, canvas_h, cos_a, sin_a
    )
    left, top, right, bottom = _trim_rect_to_halfplanes(
        left, top, right, bottom, planes, inset=inset
    )

    # Round inward so the slice never reintroduces fill at the borders.
    left_i = max(0, int(math.ceil(left)))
    top_i = max(0, int(math.ceil(top)))
    right_i = min(canvas_w, int(math.floor(right)))
    bottom_i = min(canvas_h, int(math.floor(bottom)))
    left_i = min(left_i, canvas_w - 1)
    top_i = min(top_i, canvas_h - 1)
    right_i = max(right_i, left_i + 1)
    bottom_i = max(bottom_i, top_i + 1)
    return left_i, top_i, right_i, bottom_i


def rotate_autocrop_rgb(
    img: Image.Image, angle_deg: float, inset: int = 2
) -> Image.Image:
    """
    Rotate by any angle and then crop to the largest axis-aligned rectangle that contains
    ONLY valid pixels (no wedges). Works for large angles.
    """
    if abs(angle_deg) < 0.01:
        return img.convert("RGB")

    img = img.convert("RGB")
    w, h = img.size

    # Reduce angle for rectangle math (rotation by 120° has same inscribed rect as 60°)
    a = abs(angle_deg) % 180.0
    if a > 90.0:
        a = 180.0 - a
    angle_rad = math.radians(a)

    # Largest rectangle inside the rotated original (in original pixel coordinates)
    crop_w, crop_h = _rotated_rect_with_max_area(w, h, angle_rad)
    crop_w = max(1, min(w, crop_w))
    crop_h = max(1, min(h, crop_h))

    # Rotate with expand so content is preserved
    rot = img.rotate(
        -angle_deg,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(0, 0, 0),
    )

    # Center-crop to the inscribed rectangle
    cx = rot.width / 2.0
    cy = rot.height / 2.0
    left = math.floor(cx - crop_w / 2.0)
    top = math.floor(cy - crop_h / 2.0)
    right = left + crop_w
    bottom = top + crop_h

    # Small inset to remove any bicubic edge contamination
    # We skip this for exact 90-degree increments as there is no edge contamination.
    is_exact_90 = abs(angle_deg % 90.0) < 0.01
    actual_inset = 0 if is_exact_90 else inset

    if (
        actual_inset > 0
        and (right - left) > 2 * actual_inset
        and (bottom - top) > 2 * actual_inset
    ):
        left += actual_inset
        top += actual_inset
        right -= actual_inset
        bottom -= actual_inset

    # Clamp defensively
    left = max(0, min(rot.width - 1, left))
    top = max(0, min(rot.height - 1, top))
    right = max(left + 1, min(rot.width, right))
    bottom = max(top + 1, min(rot.height, bottom))

    out = rot.crop((left, top, right, bottom)).convert("RGB")
    return out


class ImageEditor:
    """Handles core image manipulation using PIL."""

    def __init__(self):
        # Stores the currently loaded PIL Image object (original)
        self.original_image: Optional[Image.Image] = None
        # Float32 normalized master image (H, W, 3) range 0.0-1.0
        self.float_image: Optional[np.ndarray] = None
        # Float32 normalized preview image
        self.float_preview: Optional[np.ndarray] = None

        # Stores the currently applied edits (used for preview)
        self.current_edits: Dict[str, Any] = self._initial_edits()
        self.current_filepath: Optional[Path] = None
        self.source_filepath: Optional[Path] = None
        self.session_id: Optional[str] = None

        # Caching support for smooth updates
        self._lock = threading.RLock()
        self._edits_rev = 0
        self._cached_rev = -1
        self._cached_preview = None

        # Bit depth of the loaded image (8 or 16)
        self.bit_depth: int = 8

        # Cached EXIF bytes from original source (e.g., paired JPEG for RAW mode)
        # Used to preserve camera metadata when saving developed JPGs
        self._source_exif_bytes: Optional[bytes] = None

        # Last computed highlight state for UI display (thread-safe read via property)
        self._last_highlight_state: Optional[Dict[str, float]] = None

        # Timestamp of the currently loaded file (for cache invalidation)
        self.current_mtime: float = 0.0

        # Caching for expensive percentile calculation in highlight recovery
        # Stores: {'rev': int, 'max_brightness': float}
        # We rely on _edits_rev to invalidate, but strictly we also need to check if
        # edits that affect 'upstream' data (exposure, wb, crop) have changed vs just 'highlights' slider.
        # For simplicity/robustness, we just cache per full edit revision + a check on upstream params?
        # Actually, simpler: just cache the result for a given (image_id/path) + (upstream_params_hash).
        # But wait, self._edits_rev increments on ANY edit.
        # If I change "highlights" slider, _edits_rev increments.
        # But input to _apply_highlights_shadows depends on Exposure, WB, etc.
        # So if I only change Highlights, the input ARR is largely same (ignoring previous stages being re-run).
        # We need to cache the 'max_brightness' of 'arr' entering the function.
        self._cached_max_brightness_state: Optional[Dict[str, Any]] = None
        self._cached_highlight_analysis: Optional[Dict[str, Any]] = None

        # Cache for luma detail bands (pyramid blur decomposition)
        # Stores: {'hash': int, 'Y20': ndarray, 'Y3': ndarray, 'Y1': ndarray}
        self._cached_detail_bands: Optional[Dict[str, Any]] = None

        # Cached 768-entry LUT list for save_image_uint8_levels (R+G+B tables),
        # keyed on (round(blacks, 3), round(whites, 3), soft_knee).
        self._cached_u8_lut: Optional[Tuple[Tuple[float, float, bool], List[int]]] = (
            None
        )
        self._cached_u8_wb_lut: Optional[Tuple[Tuple[float, float], List[int]]] = None

        # Mask subsystem — generic mask assets keyed by tool id
        self._mask_assets: Dict[str, MaskData] = {}
        self._mask_raster_cache = MaskRasterCache()

        # Rendering preferences pushed in from AppController config:
        # soft shoulder/toe on the levels ramp, and TPDF dither on 8-bit
        # export when a strong levels stretch would band the source.
        self.levels_soft_knee: bool = True
        self.export_dither: bool = True

        # Statistics from the most recent analyze_auto_levels() call
        # (median luma etc.), read by the auto-adjust midtone recommendation.
        self.last_auto_levels_stats: Dict[str, float] = {}

    def clear(self):
        """Clear all editor state so the next edit starts from a clean slate."""
        with self._lock:
            self.original_image = None
            self.current_filepath = None
            self.source_filepath = None
            self.session_id = None
            self.float_image = None
            self.float_preview = None
            self._edits_rev += 1
            self._cached_preview = None
            self._cached_rev = -1
            self.bit_depth = 8
            self._source_exif_bytes = None
            self._last_highlight_state = None  # Explicit reset
            self._cached_highlight_analysis = None
            self._cached_detail_bands = None
            self._cached_u8_lut = None
            self._cached_u8_wb_lut = None
            self._mask_assets.clear()
            self._mask_raster_cache.clear()
        # Optionally also reset edits if that matches your mental model:
        # self.current_edits = self._initial_edits()

    def set_source_exif(self, exif_bytes: Optional[bytes]):
        """Store EXIF bytes from the original source (e.g., paired JPEG).

        Call this when switching to RAW mode to preserve camera metadata
        in the developed JPG output.
        """
        self._source_exif_bytes = exif_bytes

    def reset_edits(self):
        """Reset edits to initial values and bump revision."""
        with self._lock:
            self.current_edits = self._initial_edits()
            self._edits_rev += 1

    def _initial_edits(self) -> Dict[str, Any]:
        return {
            "brightness": 0.0,
            "contrast": 0.0,
            "saturation": 0.0,
            "white_balance_by": 0.0,  # Blue/Yellow (Cool/Warm)
            "white_balance_mg": 0.0,  # Magenta/Green (Tint)
            "crop_box": None,  # Normalized box after 90-degree rotation.
            "sharpness": 0.0,
            "rotation": 0,
            "exposure": 0.0,
            "highlights": 0.0,
            "shadows": 0.0,
            "vibrance": 0.0,
            "vignette": 0.0,
            "blacks": 0.0,
            "whites": 0.0,
            "clarity": 0.0,
            "texture": 0.0,
            "straighten_angle": 0.0,
            "darken_settings": None,  # DarkenSettings or None
        }

    @staticmethod
    def _rotate_point_90_normalized(
        x: float, y: float, steps_ccw: int
    ) -> Tuple[float, float]:
        """Rotate a normalized 0-1000 point around image center in 90-degree steps."""
        steps = steps_ccw % 4
        if steps == 1:
            return y, 1000.0 - x
        if steps == 2:
            return 1000.0 - x, 1000.0 - y
        if steps == 3:
            return 1000.0 - y, x
        return x, y

    @classmethod
    def _rotate_crop_box_for_rotation_change(
        cls,
        crop_box: Any,
        old_rotation: int,
        new_rotation: int,
    ) -> Any:
        """Keep an existing crop visually stable when 90-degree rotation changes."""
        if crop_box is None:
            return crop_box

        try:
            if len(crop_box) != 4:
                return crop_box
            left, top, right, bottom = (float(v) for v in crop_box)
        except (TypeError, ValueError):
            return crop_box

        delta_steps = ((int(new_rotation) - int(old_rotation)) // 90) % 4
        if delta_steps == 0:
            return crop_box

        corners = (
            (left, top),
            (right, top),
            (right, bottom),
            (left, bottom),
        )
        rotated = (
            cls._rotate_point_90_normalized(x, y, delta_steps) for x, y in corners
        )
        xs, ys = zip(*rotated)

        new_left = max(0, min(1000, int(round(min(xs)))))
        new_top = max(0, min(1000, int(round(min(ys)))))
        new_right = max(0, min(1000, int(round(max(xs)))))
        new_bottom = max(0, min(1000, int(round(max(ys)))))

        if new_right <= new_left or new_bottom <= new_top:
            return crop_box
        return new_left, new_top, new_right, new_bottom

    @staticmethod
    def _edits_skip_linear(edits: Dict[str, Any]) -> bool:
        """True when no linear-space edits are active (WB, exposure, highlights,
        shadows, clarity, texture, sharpness).  When True the sRGB→Linear→sRGB
        round-trip in ``_apply_edits`` is a mathematical no-op and can be skipped."""

        def _get_f(key: str) -> float:
            try:
                return float(edits.get(key, 0.0))
            except (ValueError, TypeError):
                return 1.0  # Safe default: treat as "active" to skip optimization

        return (
            abs(_get_f("white_balance_by")) <= 0.001
            and abs(_get_f("white_balance_mg")) <= 0.001
            and abs(_get_f("exposure")) <= 0.001
            and abs(_get_f("highlights")) <= 0.001
            and abs(_get_f("shadows")) <= 0.001
            and abs(_get_f("clarity")) <= 0.001
            and abs(_get_f("texture")) <= 0.001
            and abs(_get_f("sharpness")) <= 0.001
        )

    @staticmethod
    def _edits_can_share_input(edits: Dict[str, Any]) -> bool:
        """True when ``_apply_edits(for_export=True)`` will not mutate the input
        array, meaning the caller can pass ``self.float_image`` directly without
        ``.copy()``.

        Requirements (all must hold):
        - No linear-space edits (``_edits_skip_linear``).
        - No vignette (uses in-place ``arr *=``).
        - No geometry ops — rotation, straighten, crop create views/slices; later
          in-place ops on those views would mutate the backing array.

        All remaining sRGB-space ops (brightness, contrast, saturation, vibrance,
        levels) use reassignment (``arr = arr * factor``), which is safe.
        """

        def _get_f(key: str) -> float:
            try:
                return float(edits.get(key, 0.0))
            except (ValueError, TypeError):
                return 1.0  # Safe default: treat as "active" to skip optimization

        darken = edits.get("darken_settings")
        darken_active = darken is not None and getattr(darken, "enabled", False)

        return (
            ImageEditor._edits_skip_linear(edits)
            and abs(_get_f("vignette")) <= 0.001
            and edits.get("rotation", 0) == 0
            and abs(_get_f("straighten_angle")) <= 0.001
            and not edits.get("crop_box")
            and not darken_active
        )

    def load_image(
        self,
        filepath: str,
        cached_preview: Optional[DecodedImage] = None,
        source_exif: Optional[bytes] = None,
        preview_only: bool = False,
    ):
        """Load a new image for editing.

        Args:
            filepath: Path to the image file
            cached_preview: Optional byte-buffer for faster initial display
            source_exif: Optional EXIF bytes from original source (preserve camera metadata)
            preview_only: If True and image is 8-bit, skip cv2 and float32 conversion.
                          Loads only PIL image + float_preview for histogram analysis.
                          float_image stays None.  Ignored for 16-bit (TIFF) files.
        """
        if not filepath or not Path(filepath).exists():
            with self._lock:
                self.original_image = None
                self.float_image = None
                self.float_preview = None
                self.current_filepath = None
                self.source_filepath = None
                self.session_id = None
                self._source_exif_bytes = None
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1
                # Clear mask state from previous image
                self._mask_assets.clear()
                self._mask_raster_cache.clear()
            log.error("Image file not found: %s", filepath)
            return False

        load_filepath = Path(filepath)
        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()
        try:
            new_mtime = load_filepath.stat().st_mtime
        except OSError:
            new_mtime = 0.0

        with self._lock:
            # Clear previous cached EXIF and set new one if provided
            self.current_mtime = new_mtime
            self._source_exif_bytes = source_exif
            # Clear mask state from previous image
            self._mask_assets.clear()
            self._mask_raster_cache.clear()

        _is_tiff = load_filepath.suffix.lower() in (".tif", ".tiff")
        _is_jpeg = load_filepath.suffix.lower() in (".jpg", ".jpeg")

        try:
            # --- JPEG fast path: decode pixels with TurboJPEG ---
            # JPEGs are always 8-bit, so the OpenCV 16-bit probe is pointless
            # and Pillow's full decode (~150ms at 20MP) can be replaced by
            # TurboJPEG (~60ms; decode_jpeg_rgb falls back to Pillow itself).
            # A lazy BytesIO-backed PIL handle supplies EXIF/ICC metadata
            # without decoding pixels.
            jpeg_arr = None
            jpeg_meta_info: Optional[dict] = None
            jpeg_meta_exif = None
            if _is_jpeg:
                try:
                    file_bytes = load_filepath.read_bytes()
                    meta_image = Image.open(io.BytesIO(file_bytes))
                    jpeg_meta_info = dict(meta_image.info)
                    jpeg_meta_exif = meta_image.getexif()
                    jpeg_arr = decode_jpeg_rgb(
                        file_bytes, source_path=str(load_filepath)
                    )
                except Exception as e:
                    log.warning(
                        "JPEG fast decode failed for %s (%s); using standard path",
                        load_filepath,
                        e,
                    )
                    jpeg_arr = None
                if jpeg_arr is not None and (
                    jpeg_arr.ndim != 3
                    or jpeg_arr.shape[2] != 3
                    or jpeg_arr.dtype != np.uint8
                ):
                    jpeg_arr = None

            if jpeg_arr is None:
                # We must load and close the original file handle immediately
                with Image.open(load_filepath) as im:
                    # Keep original PIL for EXIF/Format preservation
                    loaded_original = im.copy()
            if _debug:
                t_pil = time.perf_counter()

            loaded_bit_depth = 8
            loaded_float_image = None
            float_image_orientation_applied = False

            if jpeg_arr is not None:
                orientation = get_exif_orientation(load_filepath, exif=jpeg_meta_exif)
                if orientation > 1:
                    # apply_orientation_to_np may return a non-contiguous view
                    jpeg_arr = np.ascontiguousarray(
                        apply_orientation_to_np(jpeg_arr, orientation)
                    )
                    float_image_orientation_applied = True
                loaded_original = Image.fromarray(jpeg_arr)
                if jpeg_meta_info:
                    # Carry EXIF/ICC over so getexif() and info["icc_profile"]
                    # behave exactly like a Pillow-decoded image.
                    loaded_original.info.update(jpeg_meta_info)
                if not preview_only:
                    loaded_float_image = jpeg_arr.astype(np.float32)
                    loaded_float_image *= np.float32(1.0 / 255.0)
                log.info(
                    "Loaded 8-bit JPEG via %s: %s",
                    "TurboJPEG" if TURBO_AVAILABLE else "Pillow (turbo unavailable)",
                    load_filepath,
                )
            else:
                # --- Convert to Float32 (standard path) ---
                # Use OpenCV for reliable 16-bit loading as Pillow often
                # downsamples to 8-bit RGB
                if preview_only and not _is_tiff:
                    cv_img = None
                elif cv2 is None:
                    log.warning(
                        "OpenCV not installed, falling back to Pillow (may lose 16-bit depth)"
                    )
                    cv_img = None
                else:
                    # Use IMREAD_UNCHANGED to preserve bit depth
                    # Note: OpenCV loads as BGR by default
                    cv_img = cv2.imread(str(load_filepath), cv2.IMREAD_UNCHANGED)

                # Robust validation: cv2.imread can return None or an empty/invalid array
                cv_img_valid = (
                    cv_img is not None
                    and isinstance(cv_img, np.ndarray)
                    and cv_img.size > 0
                )

                # Read EXIF orientation early (before float conversion) so we can
                # apply it to the PIL image on the 8-bit path — rotating uint8 is
                # ~5x faster than rotating float32.
                orientation = get_exif_orientation(
                    load_filepath, exif=loaded_original.getexif()
                )

                if cv_img_valid and cv_img.dtype == np.uint16:
                    loaded_bit_depth = 16
                    # Normalize 0-65535 -> 0.0-1.0
                    arr = cv_img.astype(np.float32) / 65535.0

                    # Handle channels
                    if len(arr.shape) == 2:
                        # Grayscale -> RGB
                        arr = np.stack((arr,) * 3, axis=-1)
                    elif len(arr.shape) == 3 and arr.shape[2] == 3:
                        # BGR -> RGB (OpenCV default)
                        # Note: If IMREAD_UNCHANGED loads a TIFF, it *might* be RGB depending on backend (libtiff).
                        # But consistently OpenCV uses BGR layout for 3-channel images.
                        # Let's verify by assuming BGR and swapping.
                        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
                    else:
                        # Invalid channel count, fall back to Pillow
                        cv_img_valid = False
                        loaded_bit_depth = 8
                        # For fallback 8-bit from bad CV2, orient PIL first then convert
                        if orientation > 1:
                            loaded_original = ImageOps.exif_transpose(loaded_original)
                        rgb = loaded_original.convert("RGB")
                        arr = np.array(rgb).astype(np.float32) / 255.0
                        float_image_orientation_applied = orientation > 1
                        log.warning(
                            "OpenCV loaded unexpected channel count, falling back to Pillow: %s",
                            load_filepath,
                        )

                    loaded_float_image = arr
                    if loaded_bit_depth == 16:
                        log.info("Loaded 16-bit image via OpenCV: %s", load_filepath)
                    else:
                        log.info(
                            "Loaded 8-bit image via Pillow (OpenCV fallback): %s",
                            load_filepath,
                        )
                else:
                    # Fallback to Pillow logic for 8-bit or if OpenCV failed/returned 8-bit
                    loaded_bit_depth = 8
                    # Apply EXIF orientation on PIL image BEFORE float conversion.
                    # Rotating uint8 PIL is ~5x faster than rotating float32 numpy.
                    if orientation > 1:
                        loaded_original = ImageOps.exif_transpose(loaded_original)
                        float_image_orientation_applied = True
                    if not preview_only:
                        rgb = loaded_original.convert("RGB")
                        # In-place multiply avoids a second full-size float
                        # allocation (~40% faster than astype + divide at 20MP).
                        loaded_float_image = np.asarray(rgb).astype(np.float32)
                        loaded_float_image *= np.float32(1.0 / 255.0)
                    log.info("Loaded 8-bit image via Pillow: %s", load_filepath)
            if _debug:
                t_float = time.perf_counter()

            # --- Apply EXIF Orientation ---
            # For 16-bit CV2 path, orientation was not applied during float
            # conversion, so apply it to the numpy array now.
            # For 8-bit PIL path, float_image is already oriented.
            if orientation > 1:
                if float_image_orientation_applied:
                    log.debug(
                        "EXIF orientation %d already applied during PIL load: %s",
                        orientation,
                        load_filepath,
                    )
                else:
                    log.info(
                        "Applying EXIF orientation %d to float buffer (CV2 path): %s",
                        orientation,
                        load_filepath,
                    )
                    loaded_original = ImageOps.exif_transpose(loaded_original)
                    if loaded_float_image is not None:
                        loaded_float_image = apply_orientation_to_np(
                            loaded_float_image, orientation
                        )
            if _debug:
                t_orient = time.perf_counter()

            # --- Create Float Preview ---
            # Use the cached, display-sized preview if available to speed up
            if cached_preview:
                # cached_preview.buffer is uint8
                preview_arr = np.frombuffer(
                    cached_preview.buffer, dtype=np.uint8
                ).reshape((cached_preview.height, cached_preview.width, 3))

                # IMPORTANT: The cached_preview coming from the Prefetcher already has
                # EXIF orientation applied (in prefetch.py's "Unified EXIF Orientation Application").
                # Do NOT apply orientation again here - that would cause double rotation!
                # The cached_preview is also "cooked" (has Color Management / Saturation applied).
                # We use it for the VERY FIRST frame for fast display, then immediately
                # re-render from the master float_image in the background.
                log.debug(
                    "Using cached preview (assumed orientation-correct from prefetcher)"
                )

                loaded_float_preview = preview_arr.astype(np.float32) / 255.0
            else:
                # Downscale to preview size. The JPEG fast path already has the
                # oriented pixels as a numpy array; cv2.resize is ~4x faster
                # than the PIL thumbnail round-trip at 20MP.
                if jpeg_arr is not None and cv2 is not None:
                    h, w = jpeg_arr.shape[:2]
                    scale = min(1920.0 / w, 1080.0 / h, 1.0)
                    if scale < 1.0:
                        preview_u8 = cv2.resize(
                            jpeg_arr,
                            (max(1, int(w * scale)), max(1, int(h * scale))),
                            interpolation=cv2.INTER_AREA,
                        )
                    else:
                        preview_u8 = jpeg_arr
                else:
                    thumb = loaded_original.copy()
                    thumb.thumbnail((1920, 1080))
                    preview_u8 = np.asarray(thumb.convert("RGB"), dtype=np.uint8)

                # float_preview is display-space by contract: cached previews
                # from the prefetcher arrive already "cooked" (ICC/saturation
                # applied), and preview-sized renders are shown WITHOUT any
                # further color correction. A preview built from raw source
                # pixels must get the same treatment, or ICC mode displays it
                # badly oversaturated on wide-gamut monitors.
                preview_u8 = apply_loupe_color_correction(
                    preview_u8,
                    icc_bytes=loaded_original.info.get("icc_profile"),
                )
                loaded_float_preview = preview_u8.astype(np.float32)
                loaded_float_preview *= np.float32(1.0 / 255.0)

                # Preview is derived from oriented pixels (exif_transpose /
                # apply_orientation_to_np already ran), so orientation is correct.

            if _debug:
                t_preview = time.perf_counter()

            # Assign all state atomically under lock to prevent race with preview worker
            with self._lock:
                self.current_filepath = load_filepath
                self.source_filepath = load_filepath
                self.session_id = uuid.uuid4().hex
                self.original_image = loaded_original
                self.float_image = loaded_float_image
                self.float_preview = loaded_float_preview
                self.bit_depth = loaded_bit_depth
                # Reset edits
                self.current_edits = self._initial_edits()
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1

            if _debug:
                t_end = time.perf_counter()
                log.debug(
                    "[LOAD_IMAGE] pil_open=%dms float_convert=%dms exif_orient=%dms preview=%dms total=%dms  %s",
                    int((t_pil - t0) * 1000),
                    int((t_float - t_pil) * 1000),
                    int((t_orient - t_float) * 1000),
                    int((t_preview - t_orient) * 1000),
                    int((t_end - t0) * 1000),
                    load_filepath.name,
                )
            return True
        except Exception as e:
            # We catch specific errors during the process if needed, but for general failure
            # we should cleanup and then RETURN FALSE so the caller (UI) knows what happened.
            # This matches the legacy contract (exceptions for programmer errors, False for runtime/IO failure)
            log.warning("Error loading image for editing: %s", e)
            with self._lock:
                self.original_image = None
                self.float_image = None
                self.float_preview = None
                self.current_filepath = None
                self.source_filepath = None
                self.session_id = None
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1
                self._mask_assets.clear()
                self._mask_raster_cache.clear()
            return False

    def _rotate_float_image(
        self, img_arr: np.ndarray, angle_deg: float, expand: bool = False
    ) -> np.ndarray:
        """Rotates a float32 RGB image using PIL 'F' mode per channel to preserve precision."""
        if abs(angle_deg) < 0.01:
            return img_arr

        c = img_arr.shape[2]
        channels = []
        for i in range(c):
            # Convert channel to PIL Float image
            im_c = Image.fromarray(img_arr[:, :, i], mode="F")
            # Rotate
            rot_c = im_c.rotate(
                angle_deg,
                resample=Image.Resampling.BICUBIC,
                expand=expand,
                fillcolor=0.0,
            )
            channels.append(rot_c)

        # Merge back (all channels rotated to same size)
        return np.stack([np.array(ch) for ch in channels], axis=-1)

    def _apply_edits(
        self,
        img_arr: np.ndarray,
        edits: Optional[Dict[str, Any]] = None,
        *,
        for_export: bool = False,
        mask_assets_override: Optional[Dict[str, "MaskData"]] = None,
        cache_override: Optional["MaskRasterCache"] = None,
        cache_context: Optional[dict] = None,
        update_highlight_state: bool = True,
        downscale_long_edge: Optional[int] = None,
        protect_input: bool = False,
    ) -> np.ndarray:
        """Applies all current edits to the provided float32 numpy array.
        Returns float32 array (H, W, 3).

        ``update_highlight_state`` controls whether non-export renders publish
        highlight telemetry for the live clipping indicator. Analysis callers
        should disable it when rendering downsampled scratch buffers.

        ``downscale_long_edge`` resizes the array down to the given long edge
        AFTER geometry (rotation/crop) but BEFORE tonal edits. Display-only
        renders use it so a full-resolution master is not processed at 20MP
        when the screen can only show a fraction of that.

        ``protect_input`` lets callers pass a shared buffer (e.g.
        ``self.float_image``) without copying it first: after geometry and
        downscale, the working array is copied only if it still shares memory
        with ``img_arr``. Cropping then copies just the cropped region instead
        of the whole master, and a downscale already produced fresh memory.
        """
        if edits is None:
            edits = self.current_edits

        debug_enabled = log.isEnabledFor(logging.DEBUG)
        debug_t0 = time.perf_counter() if debug_enabled else None
        debug_stage_marks: list[tuple[str, float]] | None = (
            [] if debug_enabled else None
        )

        def _mark(stage: str) -> None:
            if debug_stage_marks is not None:
                debug_stage_marks.append((stage, time.perf_counter()))

        # Alias
        arr = img_arr

        # ENSURE we are working with a float32 numpy array
        if isinstance(arr, Image.Image):
            arr = np.array(arr.convert("RGB")).astype(np.float32) / 255.0
        elif not isinstance(arr, np.ndarray):
            arr = np.array(arr)
            if arr.dtype == np.uint8:
                arr = arr.astype(np.float32) / 255.0
            elif arr.dtype == np.uint16:
                arr = arr.astype(np.float32) / 65535.0
            else:
                arr = arr.astype(np.float32)
                # Heuristic: only scan for max if necessary, or use a sample for speed
                # If the first few thousand pixels are > 1.0, it's likely 8-bit data.
                if arr.size > 0:
                    sample = arr.reshape(-1)[:2000]
                    s_max = sample.max()
                    if 1.0 < s_max <= 255.0:
                        arr /= 255.0
                    elif s_max <= 1.0:
                        # Double check full array only if sample was small or ambiguous
                        # but typically 0.0-1.0 images stay 0.0-1.0.
                        pass

        # NOTE: For UI analysis, we want to capture the state AFTER White Balance and Exposure
        # but BEFORE Highlights/Shadows/ToneMapping, so the indicators reflect the
        # "available headroom" and "current clipping" accurately for the recovery tools.

        # 1. Rotation (90 degree steps)
        # np.rot90 rotates 90 degrees CCW k times.
        rotation = edits.get("rotation", 0)
        k = (rotation // 90) % 4
        if k > 0:
            # np.rot90 rotates first two axes by default (rows, cols)
            arr = np.rot90(arr, k=k)

        # 2. Straighten (Free Rotation)
        straighten_angle = float(edits.get("straighten_angle", 0.0))
        has_crop_box = "crop_box" in edits and edits.get("crop_box", 0.0)

        # Effective crop selection in source space (post-90, pre-straighten).
        # A full-frame box selects everything, so it gets the same fill-free
        # autocrop geometry as "no crop" — rotate-only commits must not keep
        # the whole expanded canvas with its four black wedges.
        crop_box_vals: Optional[tuple] = None
        if has_crop_box:
            crop_box_edit = edits.get("crop_box")
            try:
                if len(crop_box_edit) == 4:
                    crop_box_vals = tuple(float(v) for v in crop_box_edit)
            except (TypeError, ValueError):
                crop_box_vals = None
            if crop_box_vals == (0.0, 0.0, 1000.0, 1000.0):
                crop_box_vals = None

        # Apply rotation if significant
        # During preview (for_export=False), we might skip this if QML handles visuals,
        # BUT current QML implementation likely expects the buffer to be pre-transformed?
        # Actually `editor.py` says "During preview (for_export=False), QML handles the visual rotation."
        # If so, we skip free rotation here for speed?
        # But if we crop, we MUST rotate first.
        # Let's preserve logic: if only straightening and not exporting, maybe skip?
        # The previous code skipped it if NOT for_export?
        # "Only apply rotation if... and we are exporting" was the comment. implies preview logic handles it.
        # However, for accurate cropping, we need to rotate.

        apply_rotation = abs(straighten_angle) > 0.001 and (for_export or has_crop_box)

        # Capture dimensions after 90-degree rotation and before free rotation.
        orig_h, orig_w = arr.shape[:2]

        if apply_rotation:
            # Use the float rotation helper
            # Note: rotate_autocrop_rgb logic was complex.
            # If we have crop box, we manually crop later.
            # If no crop box, we might auto-crop (remove wedges).
            # For floating point, standard 'expand' rotation + manual crop is best.

            # Calculate auto-crop parameters BEFORE rotation if needed
            crop_rect = None
            if crop_box_vals is None:
                h, w = arr.shape[:2]
                # Normalize angle for helper (helper expects radians, handles quadrants but ensuring positive can help)
                angle_rad = math.radians(straighten_angle)
                # Helper logic for crop size
                cw, ch = _rotated_rect_with_max_area(w, h, angle_rad)
                crop_rect = (cw, ch)

            # Perform rotation (Expanded)
            arr = self._rotate_float_image(arr, -straighten_angle, expand=True)

            # Apply Auto-Crop if calculated
            if crop_rect:
                cw, ch = crop_rect
                rh, rw = arr.shape[:2]
                left, top, right, bottom = _autocrop_canvas_rect(
                    cw, ch, rw, rh, straighten_angle
                )
                arr = arr[top:bottom, left:right, :]

        # 3. Crop
        if crop_box_vals is not None:
            # The crop_box is in 0-1000 normalized coordinates relative to the
            # image after 90-degree rotation, but before free straighten. If
            # straighten uses expand=True, transform that box onto the expanded
            # canvas before slicing.
            if apply_rotation and abs(straighten_angle) > 0.001:
                new_h, new_w = arr.shape[:2]
                left, t, r, b = _crop_box_canvas_rect(
                    crop_box_vals, orig_w, orig_h, straighten_angle, new_w, new_h
                )
            else:
                # No rotation - use current dimensions directly
                h, w = arr.shape[:2]
                left = int(crop_box_vals[0] * w / 1000)
                t = int(crop_box_vals[1] * h / 1000)
                r = int(crop_box_vals[2] * w / 1000)
                b = int(crop_box_vals[3] * h / 1000)

                left = max(0, left)
                t = max(0, t)
                r = min(w, r)
                b = min(h, b)

            if r > left and b > t:
                arr = arr[t:b, left:r, :]

        if debug_enabled:
            log.debug(
                "geometry: src=%dx%d crop_box=%s angle=%.3f for_export=%s out=%dx%d",
                orig_w,
                orig_h,
                crop_box_vals,
                straighten_angle,
                for_export,
                arr.shape[1],
                arr.shape[0],
            )

        _mark("geometry")

        # 3.5. Display-size downscale (display-only renders)
        # Tonal edits below are per-pixel, so applying them to an INTER_AREA
        # downscale of the cropped master is visually equivalent to rendering
        # at full resolution and letting the GPU scale it down — and several
        # times cheaper.
        if downscale_long_edge and cv2 is not None:
            h, w = arr.shape[:2]
            long_edge = max(h, w)
            if long_edge > downscale_long_edge:
                scale = downscale_long_edge / long_edge
                new_w = max(1, round(w * scale))
                new_h = max(1, round(h * scale))
                arr = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_AREA)

        _mark("downscale")

        # Detach from a shared input buffer before any tonal op can touch it.
        # Everything below either reassigns or mutates `arr` in place (vignette,
        # the caller's final in-place clip), so from here on the array must be
        # private memory when the caller didn't pass a copy. may_share_memory
        # is a cheap bounds check; a false positive just costs the copy the
        # caller would otherwise have made up front.
        if protect_input and np.may_share_memory(arr, img_arr):
            arr = arr.copy()

        # 4. Conversion to Linear Light
        # Cache sRGB u8 BEFORE linearization for accurate JPEG clipping detection.
        # JPEG clipping happens in sRGB after gamma/quantization, so we need the
        # original sRGB values to detect flat-top clipping correctly.
        # MOVED to after WB/Exposure so indicators reflect current pipeline state.

        # --- Skip linear round-trip optimization ---
        # When only sRGB-space edits are active (levels, brightness, contrast,
        # saturation, vibrance, vignette), the sRGB→Linear→sRGB conversion is a
        # no-op that costs ~3.5s on large images (and ~120ms per preview
        # render). Skip it entirely. Previews still need the highlight
        # telemetry for the live clipping indicators, which the skip branch
        # computes from a 4x-strided view below.
        _skip_linear = self._edits_skip_linear(edits)

        if for_export:
            log.debug("_apply_edits for_export: skip_linear=%s", _skip_linear)

        if _skip_linear and not for_export:
            upstream_hash = self._get_upstream_edits_hash(edits)
            analysis_state = None
            with self._lock:
                cached_dict = (
                    cache_context.get("highlight_analysis")
                    if cache_context is not None
                    else self._cached_highlight_analysis
                )
                if cached_dict and cached_dict["hash"] == upstream_hash:
                    analysis_state = cached_dict["state"]

            if analysis_state is None:
                arr_stride = arr[::4, ::4, :]
                if cv2 is not None:
                    srgb_u8_stride = cv2.convertScaleAbs(arr_stride, alpha=255.0)
                else:
                    srgb_u8_stride = (np.clip(arr_stride, 0.0, 1.0) * 255).astype(
                        np.uint8
                    )
                # With no WB/exposure active (guaranteed by _edits_skip_linear)
                # the pre-exposure and current linear states are identical.
                linear_stride = _srgb_to_linear_fast(arr_stride)
                analysis_state = _analyze_highlight_state(
                    linear_stride,
                    srgb_u8=srgb_u8_stride,
                    pre_exposure_linear=linear_stride,
                )
                with self._lock:
                    entry = {
                        "hash": upstream_hash,
                        "state": analysis_state,
                    }
                    if cache_context is not None:
                        cache_context["highlight_analysis"] = entry
                    else:
                        self._cached_highlight_analysis = entry

            if update_highlight_state:
                with self._lock:
                    self._last_highlight_state = analysis_state

            _mark("skip_linear")

        if not _skip_linear:
            # Capture strided view for analysis ONLY if needed
            # We need analysis if:
            # 1. We are in preview (not for_export) -> To show UI indicators.
            # 2. OR if we have highlights/shadows active -> To drive adaptive params.

            highlights = float(edits.get("highlights", 0.0))
            shadows = float(edits.get("shadows", 0.0))
            should_analyze = (not for_export) or (
                abs(highlights) > 0.001 or abs(shadows) > 0.001
            )

            arr_stride = None
            srgb_u8_stride = None
            analysis_state = None

            if should_analyze:
                # Capture strided view for analysis
                arr_stride = arr[::4, ::4, :]
                if cv2 is not None:
                    # cv2.convertScaleAbs is very fast for saturation casting [0,1]*255 to uint8
                    srgb_u8_stride = cv2.convertScaleAbs(arr_stride, alpha=255.0)
                else:
                    srgb_u8_stride = (np.clip(arr_stride, 0.0, 1.0) * 255).astype(
                        np.uint8
                    )

            # Base image data is always in [0, 1], so the clamped LUT version
            # is safe here; headroom (>1.0) only appears later, in linear space.
            arr = _srgb_to_linear_fast(arr)
            _mark("linear_convert")

            # 5. White Balance (Multipliers in Linear Space)
            by = edits.get("white_balance_by", 0.0) * 0.5
            mg = edits.get("white_balance_mg", 0.0) * 0.5
            if abs(by) > 0.001 or abs(mg) > 0.001:
                r_gain, g_gain, b_gain = _normalized_wb_gains(by, mg)
                arr[:, :, 0] *= r_gain
                arr[:, :, 1] *= g_gain
                arr[:, :, 2] *= b_gain

            # --- Analyzed Highlight State (Post-WB, Pre-Exposure) ---
            # Capture pre-exposure linear state for "True Headroom" calculation
            pre_exposure_linear_stride = None
            if should_analyze:
                pre_exposure_linear_stride = arr[::4, ::4, :]

            # 6. Exposure (Linear Gain for True Headroom)
            exposure = edits.get("exposure", 0.0)
            if abs(exposure) > 0.001:
                # EV units: 2^exposure
                gain = 2.0**exposure
                arr = arr * gain

            # --- Analyzed Highlight State (Post-Exposure, Pre-Recovery) ---
            # We do this UNCONDITIONALLY for display so UI indicators are live.
            # We use the current linear array 'arr' which now includes WB and Exposure.
            # We pass srgb_u8=None to force using linear thresholds on the current data (or pre-exposure data if passed).

            if should_analyze:
                # Check cache for analysis state to avoid expensive re-computation on downstream edits
                upstream_hash = self._get_upstream_edits_hash(edits)

                cached_analysis = None
                with self._lock:
                    cached_dict = (
                        cache_context.get("highlight_analysis")
                        if cache_context is not None
                        else self._cached_highlight_analysis
                    )
                    if cached_dict and cached_dict["hash"] == upstream_hash:
                        cached_analysis = cached_dict["state"]

                if cached_analysis:
                    analysis_state = cached_analysis
                else:
                    # Use strided views for speed (re-stride linear if it changed, but usually we just want current)
                    arr_linear_stride = arr[::4, ::4, :]
                    # Pass the srgb_u8_stride captured BEFORE linearization for true JPEG clipping detection
                    # Pass pre_exposure_linear_stride to measure "True Headroom" before exposure boost
                    # arr_linear_stride is "Current State" (Post-WB, Post-Exposure)
                    analysis_state = _analyze_highlight_state(
                        arr_linear_stride,
                        srgb_u8=srgb_u8_stride,  # Source (Pre-Edit) State
                        pre_exposure_linear=pre_exposure_linear_stride,
                    )

                    with self._lock:
                        entry = {
                            "hash": upstream_hash,
                            "state": analysis_state,
                        }
                        if cache_context is not None:
                            cache_context["highlight_analysis"] = entry
                        else:
                            self._cached_highlight_analysis = entry

            if not for_export and update_highlight_state:
                with self._lock:
                    self._last_highlight_state = analysis_state

            # 7. Highlights/Shadows - Using linear light and brightness-based processing
            if abs(highlights) > 0.001 or abs(shadows) > 0.001:
                arr = self._apply_highlights_shadows(
                    arr,
                    highlights,
                    shadows,
                    srgb_u8_stride=srgb_u8_stride,  # Pass if we need to recompute analysis
                    analysis_state=analysis_state,
                    edits=edits,
                    cache_context=cache_context,
                )

            _mark("linear_tone")

            # 8-10. Clarity / Texture / Sharpness (Unified Pyramid Detail Bands)
            #
            # Uses a hierarchical luma-only pyramid decomposition to avoid:
            # - Triple-amplifying the same edges (halo stacking)
            # - Chroma artifacts from RGB high-pass
            # - Incorrect midtone mask on HDR/linear values >1.0
            #
            # Bands:
            #   D_clarity = Y - Y20    (coarse local contrast)
            #   D_texture = Y3 - Y20   (mid-frequency detail)
            #   D_sharp   = Y1 - Y3    (fine detail)
            #
            clarity = edits.get("clarity", 0.0)
            texture = edits.get("texture", 0.0)
            sharpness = edits.get("sharpness", 0.0)

            if abs(clarity) > 0.001 or abs(texture) > 0.001 or abs(sharpness) > 0.001:
                # Ensure float32 to avoid memory bloat from float64 upcast
                arr = arr.astype(np.float32, copy=False)

                # Current exposure gain (for scaling cached blurs)
                current_exp_gain = 2.0 ** edits.get("exposure", 0.0)

                # Compute linear luminance (Rec.709 coefficients)
                Y = arr @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

                # Determine which blurs we need based on active sliders
                need_Y20 = abs(clarity) > 0.001 or abs(texture) > 0.001
                need_Y3 = abs(texture) > 0.001 or abs(sharpness) > 0.001
                need_Y1 = abs(sharpness) > 0.001

                # Check cache for detail bands (hash + frozen tuple verification)
                detail_hash, detail_frozen = self._get_detail_upstream_hash(edits)
                Y20_cached = Y3_cached = Y1_cached = None
                cache_hit = False
                cached_exp_gain = 1.0

                with self._lock:
                    cached = (
                        cache_context.get("detail_bands")
                        if cache_context is not None
                        else self._cached_detail_bands
                    )
                    # Verify both hash AND frozen values to avoid collisions
                    if (
                        cached
                        and cached.get("hash") == detail_hash
                        and cached.get("frozen") == detail_frozen
                    ):
                        Y20_cached = cached.get("Y20")
                        Y3_cached = cached.get("Y3")
                        Y1_cached = cached.get("Y1")
                        cached_exp_gain = cached.get("exp_gain", 1.0)
                        cache_hit = True

                        # Validate cached array shapes match current Y dimensions
                        # This prevents reusing preview-resolution blurs during export
                        y_shape = Y.shape
                        for cached_arr in (Y20_cached, Y3_cached, Y1_cached):
                            if cached_arr is not None and cached_arr.shape != y_shape:
                                # Shape mismatch - invalidate cache
                                Y20_cached = Y3_cached = Y1_cached = None
                                cache_hit = False
                                break

                # Compute exposure scale factor for reusing cached blurs
                # blur(k*Y) = k*blur(Y) is exact only if Y scales linearly with exposure.
                # Since highlights/shadows recovery (step 7) is non-linear and sits between
                # exposure and detail bands, this scaling is APPROXIMATE when h/s is active.
                # The approximation is good enough for smooth 60fps dragging; exact render
                # happens when upstream params (WB/crop/rotate) change and cache invalidates.
                exp_scale = (
                    current_exp_gain / cached_exp_gain
                    if cache_hit and abs(cached_exp_gain) > 1e-9
                    else 1.0
                )

                # Safe extraction: use [..., 0] if 3D, else keep as-is (avoids squeeze() collapsing H/W)
                def _extract_2d(blur_result):
                    return blur_result[..., 0] if blur_result.ndim == 3 else blur_result

                # Get or compute each blur, tracking what we freshly computed
                Y_3d = Y[..., None]  # (H, W, 1) for blur function
                Y20 = Y3 = Y1 = None
                newly_computed = {"Y20": None, "Y3": None, "Y1": None}

                if need_Y20:
                    if Y20_cached is not None:
                        Y20 = Y20_cached * exp_scale
                    else:
                        Y20 = _extract_2d(_gaussian_blur_float(Y_3d, radius=20.0))
                        newly_computed["Y20"] = Y20

                if need_Y3:
                    if Y3_cached is not None:
                        Y3 = Y3_cached * exp_scale
                    else:
                        Y3 = _extract_2d(_gaussian_blur_float(Y_3d, radius=3.0))
                        newly_computed["Y3"] = Y3

                if need_Y1:
                    if Y1_cached is not None:
                        Y1 = Y1_cached * exp_scale
                    else:
                        Y1 = _extract_2d(_gaussian_blur_float(Y_3d, radius=1.0))
                        newly_computed["Y1"] = Y1

                # Update cache if we computed any new blurs
                # Merge newly computed blurs with existing cached blurs (unscaled)
                if any(v is not None for v in newly_computed.values()):
                    with self._lock:
                        # Start with existing cached values (unscaled) or empty
                        if cache_hit:
                            new_cache = {
                                "hash": detail_hash,
                                "frozen": detail_frozen,
                                "exp_gain": cached_exp_gain,  # Keep original exp_gain for existing blurs
                                "Y20": Y20_cached,
                                "Y3": Y3_cached,
                                "Y1": Y1_cached,
                            }
                            # Add newly computed blurs (they're at current_exp_gain, need to rescale to cached_exp_gain)
                            rescale_to_cached = (
                                cached_exp_gain / current_exp_gain
                                if abs(current_exp_gain) > 1e-9
                                else 1.0
                            )
                            for key, val in newly_computed.items():
                                if val is not None:
                                    new_cache[key] = val * rescale_to_cached
                        else:
                            # Fresh cache at current exposure
                            new_cache = {
                                "hash": detail_hash,
                                "frozen": detail_frozen,
                                "exp_gain": current_exp_gain,
                                "Y20": newly_computed["Y20"],
                                "Y3": newly_computed["Y3"],
                                "Y1": newly_computed["Y1"],
                            }
                        if cache_context is not None:
                            cache_context["detail_bands"] = new_cache
                        else:
                            self._cached_detail_bands = new_cache

                # Build hierarchical pyramid bands (non-overlapping frequency ranges)
                detail = np.zeros_like(Y)

                if abs(clarity) > 0.001:
                    # D_clarity = Y - Y20 (coarse local contrast)
                    D_clarity = Y - Y20
                    detail += clarity * D_clarity

                if abs(texture) > 0.001:
                    # D_texture = Y3 - Y20 (mid-frequency detail)
                    # Y3 has more high-frequency than Y20, so this isolates mid-band
                    D_texture = Y3 - Y20
                    detail += texture * D_texture

                if abs(sharpness) > 0.001:
                    # D_sharp = Y1 - Y3 (fine detail)
                    # Scale factor to match perceived strength of old Y - Y1 unsharp mask
                    k_sharp = 2.0
                    D_sharp = Y1 - Y3
                    detail += sharpness * k_sharp * D_sharp

                # Compute bounded midtone mask from perceptual luminance
                # Use sqrt for perceptual curve (approximates gamma)
                Y_mask = np.clip(Y, 0.0, 1.0)
                Y_mask = np.sqrt(Y_mask)
                midtone_mask = np.clip(1.0 - np.abs(Y_mask - 0.5) * 2.0, 0.0, 1.0)

                # Apply detail via luma-ratio gain (preserves hue/saturation)
                # Only apply ratio where Y > eps; leave gain at 1.0 for dark/negative regions
                eps = 1e-7
                valid_mask = Y > eps
                den = np.where(valid_mask, Y, 1.0)
                gain = 1.0 + midtone_mask * detail / den
                gain = np.where(valid_mask, gain, 1.0)
                # Soft clamp to prevent extreme values (hard clamp for v1, can soften later)
                gain = np.clip(gain, 0.5, 2.0)
                arr *= gain[..., None]

            _mark("detail_bands")

            # 11. Global Headroom Shoulder (safety net for values > 1.0)
            # This ONLY affects values above 1.0, compressing headroom smoothly.
            # It does NOT interfere with normal highlight slider work below 1.0.
            # Applied here in linear space before gamma conversion.
            # Use small max_overshoot (0.05) to keep values very close to 1.0
            arr = _apply_headroom_shoulder(arr, max_overshoot=0.05)

            # --- Conversion back to sRGB ---
            # The headroom shoulder above caps values at 1.05, inside the LUT
            # domain, so the fast version is exact here (within quantization).
            arr = _linear_to_srgb_fast(arr)
            _mark("linear_exit")

        # --- sRGB Space Operations ---
        # NOTE: All operations below must be non-mutating (use reassignment) when
        # _skip_linear=True and for_export=True to avoid corrupting self.float_image.
        # Vignette is excluded from the no-copy path because it uses in-place math.

        # 11. Brightness / Contrast (sRGB Space)
        # 7. Brightness
        b_val = edits.get("brightness", 0.0)
        if abs(b_val) > 0.001:
            factor = 1.0 + b_val
            arr = arr * factor

        # 8. Contrast
        c_val = edits.get("contrast", 0.0)
        if abs(c_val) > 0.001:
            # Scale effect to reduce sensitivity (0.4x)
            factor = 1.0 + c_val * 0.4
            arr = (arr - 0.5) * factor + 0.5

        # 12. Saturation / Vibrance (sRGB Space)
        # 10. Saturation
        sat_val = edits.get("saturation", 0.0)
        if abs(sat_val) > 0.001:
            # Scale effect to reduce sensitivity (0.5x)
            factor = 1.0 + sat_val * 0.5
            gray = _rec601_gray(arr)[..., None]
            arr = gray + (arr - gray) * factor

        # 12. Vibrance (Smart Saturation)
        vibrance = edits.get("vibrance", 0.0)
        if abs(vibrance) > 0.001:
            if cv2 is not None:
                # ~3x faster than numpy axis reductions at full resolution
                cmax = cv2.max(cv2.max(arr[:, :, 0], arr[:, :, 1]), arr[:, :, 2])
                cmin = cv2.min(cv2.min(arr[:, :, 0], arr[:, :, 1]), arr[:, :, 2])
            else:
                cmax = arr.max(axis=2)
                cmin = arr.min(axis=2)
            delta = cmax - cmin
            sat = np.zeros_like(cmax)
            np.divide(delta, cmax, out=sat, where=cmax > 0.0001)

            sat_mask = np.clip(1.0 - sat, 0.0, 1.0)
            factor = 1.0 + vibrance * sat_mask

            gray = _rec601_gray(arr)[..., None]
            arr = gray + (arr - gray) * np.expand_dims(factor, axis=2)

        # 13. Levels (Blacks/Whites)
        blacks = edits.get("blacks", 0.0)
        whites = edits.get("whites", 0.0)
        if abs(blacks) > 0.001 or abs(whites) > 0.001:
            bp = -blacks * 0.15
            wp = 1.0 - (whites * 0.15)
            if abs(wp - bp) < 0.0001:
                wp = bp + 0.0001
            arr = (arr - bp) / (wp - bp)
            if self.levels_soft_knee:
                # The ramp above allocates, so in-place soft clip is safe.
                arr = _apply_levels_soft_clip(arr)

        # 13.5. Background Darkening (masked, after levels, before vignette)
        darken = edits.get("darken_settings")
        if darken is not None and getattr(darken, "enabled", False):
            # Use override assets/cache if provided (export snapshot), else live state
            _assets = (
                mask_assets_override
                if mask_assets_override is not None
                else self._mask_assets
            )
            _cache = (
                cache_override
                if cache_override is not None
                else self._mask_raster_cache
            )
            mask_data = _assets.get(darken.mask_id)
            if mask_data is not None and mask_data.has_strokes():
                from faststack.imaging.mask_engine import resolve_mask
                from faststack.imaging.masked_ops import apply_masked_darken

                resolved = resolve_mask(
                    mask_data,
                    darken,
                    arr,
                    arr.shape[:2],
                    edits,
                    cache=_cache,
                )
                arr = apply_masked_darken(
                    arr,
                    resolved,
                    darken_amount=darken.darken_amount,
                    edge_protection=darken.edge_protection,
                )

        _mark("srgb_ops")

        # 14. Vignette
        vignette = edits.get("vignette", 0.0)
        if abs(vignette) > 0.001:
            h, w = arr.shape[:2]
            y, x = np.ogrid[:h, :w]
            cx = (x - w / 2) / (w / 2)
            cy = (y - h / 2) / (h / 2)
            dist_sq = cx**2 + cy**2

            if vignette > 0:
                gain = 1.0 - np.clip(dist_sq * vignette, 0.0, 1.0)
                arr *= np.expand_dims(gain, axis=2)
            else:
                gain = 1.0 + dist_sq * (-vignette)
                arr *= np.expand_dims(gain, axis=2)

        _mark("vignette")

        # Export contract: return in [0,1] sRGB when skip_linear (no tone mapping
        # was applied, just sRGB-space ops). save_image also clips, but this
        # ensures callers always get valid data. Non-export callers need the
        # unclipped overshoot (e.g. analyze_auto_vibrance measures clipping).
        if _skip_linear and for_export:
            arr = np.clip(arr, 0.0, 1.0)

        if debug_enabled and debug_t0 is not None and debug_stage_marks is not None:
            total_ms = (time.perf_counter() - debug_t0) * 1000.0
            if total_ms >= 500.0:
                prev_time = debug_t0
                breakdown = []
                for name, mark_time in debug_stage_marks:
                    breakdown.append(
                        f"{name}={((mark_time - prev_time) * 1000.0):.0f}ms"
                    )
                    prev_time = mark_time
                breakdown.append(
                    f"final={((time.perf_counter() - prev_time) * 1000.0):.0f}ms"
                )
                log.debug(
                    "[APPLY_EDITS_SLOW] total=%.0fms export=%s skip_linear=%s size=%dx%d stages=%s",
                    total_ms,
                    for_export,
                    _skip_linear,
                    arr.shape[1],
                    arr.shape[0],
                    ", ".join(breakdown),
                )

        return (
            arr  # May exceed 1.0 in preview/non-export; clipped for skip_linear export.
        )

    def auto_levels(
        self, threshold_percent: float = 0.1, channel_budget: float = 3.0
    ) -> Tuple[float, float, float, float]:
        """
        Returns (blacks, whites, p_low, p_high).
        p_low/p_high are luma-driven with a per-channel clip budget so a single
        saturated channel cannot veto the stretch (see analyze_auto_levels).
        """
        blacks, whites, p_low, p_high = self.analyze_auto_levels(
            threshold_percent,
            reset_levels=True,
            channel_budget=channel_budget,
        )

        with self._lock:
            self.current_edits["blacks"] = blacks
            self.current_edits["whites"] = whites
            self._edits_rev += 1
        return blacks, whites, float(p_low), float(p_high)

    def analyze_auto_levels(
        self,
        threshold_percent: float = 0.1,
        *,
        edits: Optional[Dict[str, Any]] = None,
        reset_levels: bool = True,
        channel_budget: float = 3.0,
    ) -> Tuple[float, float, float, float]:
        """Analyze auto-levels on the current edited baseline without mutating edits."""
        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()

        threshold_percent = max(0.0, min(10.0, threshold_percent))

        with self._lock:
            # Auto-levels is an aggregate percentile estimate. If the full
            # master is already warm, sample it down before copying so analysis
            # follows source pixels without rendering a 20MP+ buffer. If not,
            # fall back to the preview so the first quick auto-adjust keypress
            # remains preview-only. Final saves still apply the scalar edits to
            # the full-resolution master.
            source_arr = None
            source_label = "none"
            source_is_full = False
            if self.float_image is not None:
                source_arr = self.float_image
                source_label = "full"
                source_is_full = True
            elif self.float_preview is not None:
                source_arr = self.float_preview
                source_label = "preview"
            edits_snapshot = dict(self.current_edits) if edits is None else dict(edits)

        if source_arr is not None:
            if source_is_full:
                longest_edge = max(source_arr.shape[0], source_arr.shape[1])
                stride = max(
                    1,
                    math.ceil(longest_edge / _AUTO_LEVELS_ANALYSIS_MAX_EDGE),
                )
                if stride > 1:
                    source_arr = source_arr[::stride, ::stride, :]
                    source_label = f"full/{stride}x"
            img_arr = np.array(source_arr, dtype=np.float32, copy=True, order="C")
        else:
            img_arr = None

        if img_arr is None:
            # Fallback for tests or cases where float data isn't initialized yet
            if self.original_image is not None:
                img_arr = (
                    np.array(self.original_image.convert("RGB")).astype(np.float32)
                    / 255.0
                )
                source_label = "pil"
            else:
                return 0.0, 0.0, 0.0, 255.0

        if reset_levels:
            edits_snapshot["blacks"] = 0.0
            edits_snapshot["whites"] = 0.0

        # Render the current edited baseline first so auto-levels sees any
        # already-active adjustments such as WB, crop, rotation, or tone edits.
        if _debug:
            t_arr = time.perf_counter()
        edited_arr = self._apply_edits(img_arr, edits=edits_snapshot, for_export=False)

        # Quantize the float render to 10-bit bins for percentile analysis.
        # 1024 bins resolve the endpoints ~4x finer than the legacy uint8
        # histogram, which keeps black/white placement stable between the
        # preview-sized analysis and the full-resolution export.
        nbins = 1024
        scaled = np.clip(edited_arr, 0.0, 1.0)
        quantized = (scaled * (nbins - 1)).astype(np.uint16)
        if _debug:
            t_u8 = time.perf_counter()

        bin_to_255 = 255.0 / (nbins - 1)

        # Per-channel clip budget: the luma percentiles drive the stretch,
        # while each individual channel is allowed to clip up to
        # channel_budget x threshold. A budget of 1.0 reproduces the old
        # conservative min/max-channel anchors; larger budgets stop a single
        # saturated channel (blue sky, red flower) from vetoing the whole
        # stretch.
        channel_budget = max(1.0, min(10.0, float(channel_budget)))
        chan_t = min(50.0, threshold_percent * channel_budget)

        # No explicit pre-clip pinning is needed: percentile ranks already
        # count clipped pixels, so stretching to the q-th percentile keeps the
        # *total* clipped fraction (pre-existing plus new) within the
        # threshold rather than disabling the stretch outright the moment a
        # few specular pixels sit at 255.
        chan_lows = []
        chan_highs = []
        for c in range(3):
            hist = np.bincount(quantized[:, :, c].reshape(-1), minlength=nbins)
            chan_lows.append(self._percentile_from_hist(hist, chan_t, method="lower"))
            chan_highs.append(
                self._percentile_from_hist(hist, 100.0 - chan_t, method="higher")
            )

        luma_q = (_rec601_gray(scaled) * (nbins - 1)).astype(np.uint16)
        luma_hist = np.bincount(luma_q.reshape(-1), minlength=nbins)
        luma_low = self._percentile_from_hist(
            luma_hist, threshold_percent, method="lower"
        )
        luma_high = self._percentile_from_hist(
            luma_hist, 100.0 - threshold_percent, method="higher"
        )
        median_luma = self._percentile_from_hist(luma_hist, 50.0, method="lower") / (
            nbins - 1
        )

        # Black point: luma-driven target, capped by the per-channel budgets.
        p_low = min(luma_low, min(chan_lows)) * bin_to_255
        # White point: luma-driven target, floored by the per-channel budgets.
        p_high = max(luma_high, max(chan_highs)) * bin_to_255

        # Safety
        p_low = max(0.0, min(255.0, p_low))
        p_high = max(0.0, min(255.0, p_high))

        # Check for degenerate range (e.g. flat image) to prevent extreme stretching
        if (p_high - p_low) < 1.0:
            blacks = 0.0
            whites = 0.0
        else:
            blacks = -p_low / 40.0
            whites = (255.0 - p_high) / 40.0

        with self._lock:
            self.last_auto_levels_stats = {
                "median_luma": float(median_luma),
                "p_low": float(p_low),
                "p_high": float(p_high),
            }

        if _debug:
            t_end = time.perf_counter()
            h, w = scaled.shape[:2]
            log.debug(
                "[AUTO_LEVEL] get_array=%dms render=%dms hist+clip=%dms total=%dms  "
                "(%dx%d, %s, median_luma=%.3f)",
                int((t_arr - t0) * 1000),
                int((t_u8 - t_arr) * 1000),
                int((t_end - t_u8) * 1000),
                int((t_end - t0) * 1000),
                w,
                h,
                source_label,
                median_luma,
            )

        return blacks, whites, float(p_low), float(p_high)

    def analyze_auto_vibrance(
        self,
        *,
        blacks: float,
        whites: float,
    ) -> float:
        """Recommend a conservative vibrance boost for low-color auto-adjusts."""
        with self._lock:
            # Prefer the unedited master buffer so this analysis cannot double-apply
            # edits if preview rendering semantics change.  float_preview is only a
            # fallback for preview-only loads where no master buffer exists.
            source_arr = (
                self.float_image if self.float_image is not None else self.float_preview
            )
            edits_snapshot = dict(self.current_edits)
            fallback_original = (
                self.original_image.copy()
                if source_arr is None and self.original_image is not None
                else None
            )

        if source_arr is None:
            if fallback_original is None:
                return 0.0
            source_arr = (
                np.array(fallback_original.convert("RGB")).astype(np.float32) / 255.0
            )

        # Downsample the source before copying so full-resolution masters stay
        # cheap while _apply_edits still receives a private mutable array.
        longest_edge = max(source_arr.shape[0], source_arr.shape[1])
        stride = max(1, longest_edge // _AUTO_VIBRANCE_ANALYSIS_MAX_EDGE)
        analysis_source = (
            source_arr[::stride, ::stride, :] if stride > 1 else source_arr
        )
        img_arr = np.array(analysis_source, dtype=np.float32, copy=True, order="C")

        try:
            current_vibrance = float(edits_snapshot.get("vibrance", 0.0))
            current_saturation = float(edits_snapshot.get("saturation", 0.0))
        except (TypeError, ValueError):
            return 0.0
        if abs(current_vibrance) > 0.001 or abs(current_saturation) > 0.001:
            return 0.0

        baseline_edits = dict(edits_snapshot)
        baseline_edits["blacks"] = float(blacks)
        baseline_edits["whites"] = float(whites)
        baseline_edits["vibrance"] = current_vibrance

        # The baseline and every candidate share identical edits upstream of
        # vibrance, so an isolated cache lets _apply_edits reuse the highlight
        # analysis instead of recomputing it per pass (and avoids touching the
        # live preview cache or live clipping telemetry).
        analysis_cache: dict = {}

        baseline = self._apply_edits(
            img_arr.copy(),
            edits=baseline_edits,
            for_export=False,
            cache_context=analysis_cache,
            update_highlight_state=False,
        )
        rgb = np.clip(baseline, 0.0, 1.0)
        cmax = rgb.max(axis=2)
        cmin = rgb.min(axis=2)
        delta = cmax - cmin
        luma = _rec601_gray(rgb)
        useful = (luma > 0.08) & (luma < 0.92) & (cmax > 0.04)
        if int(np.count_nonzero(useful)) < 100:
            return 0.0

        sat = np.zeros_like(cmax)
        np.divide(delta, cmax, out=sat, where=cmax > 0.0001)
        useful_sat = sat[useful]
        useful_delta = delta[useful]
        median_sat = float(np.percentile(useful_sat, 50))
        high_color_delta = float(np.percentile(useful_delta, 95))
        if (
            median_sat >= _AUTO_VIBRANCE_SAT_CEILING
            or high_color_delta < _AUTO_VIBRANCE_MIN_COLOR_DELTA
        ):
            return 0.0

        recommended = min(
            _AUTO_VIBRANCE_MAX,
            (_AUTO_VIBRANCE_TARGET_SAT - median_sat) * 0.9,
        )

        # Colorful-subject guard: a gray-dominant scene with one vivid subject
        # has a low *median* saturation, but boosting it pushes that subject
        # toward garish. Fade the boost as the 90th-percentile saturation
        # approaches full saturation.
        p90_sat = float(np.percentile(useful_sat, 90))
        if p90_sat > _AUTO_VIBRANCE_P90_SOFT:
            guard = (_AUTO_VIBRANCE_P90_HARD - p90_sat) / (
                _AUTO_VIBRANCE_P90_HARD - _AUTO_VIBRANCE_P90_SOFT
            )
            recommended *= max(0.0, min(1.0, guard))

        # Skin protection: vibrance is hue-blind, and skin tolerates extra
        # saturation poorly. When a meaningful share of the analyzed pixels
        # sits in the skin-tone envelope (orange hue band, moderate
        # saturation, mid luma), halve the boost.
        r = rgb[:, :, 0]
        g = rgb[:, :, 1]
        b = rgb[:, :, 2]
        hue_ratio = (g - b) / np.maximum(delta, 1e-6)
        skin = (
            useful
            & (r >= g)
            & (g > b)
            & (delta > 0.02)
            & (hue_ratio > 0.15)
            & (hue_ratio < 0.8)
            & (sat > 0.1)
            & (sat < 0.65)
        )
        useful_count = max(1, int(np.count_nonzero(useful)))
        skin_fraction = float(np.count_nonzero(skin)) / useful_count
        if skin_fraction > _AUTO_VIBRANCE_SKIN_FRACTION:
            recommended *= _AUTO_VIBRANCE_SKIN_FACTOR

        if recommended < _AUTO_VIBRANCE_MIN:
            return 0.0

        baseline_clip = self._channel_overshoot_fraction(baseline)
        for candidate in (
            recommended,
            recommended * 0.75,
            recommended * 0.5,
            recommended * 0.25,
        ):
            candidate_edits = dict(baseline_edits)
            candidate_edits["vibrance"] = current_vibrance + candidate
            candidate_arr = self._apply_edits(
                img_arr.copy(),
                edits=candidate_edits,
                for_export=False,
                cache_context=analysis_cache,
                update_highlight_state=False,
            )
            candidate_clip = self._channel_overshoot_fraction(candidate_arr)
            if candidate_clip <= baseline_clip + _AUTO_VIBRANCE_CLIP_TOLERANCE:
                return float(candidate)

        return 0.0

    @staticmethod
    def _channel_overshoot_fraction(arr: np.ndarray) -> float:
        """Return fraction of pixels with any channel effectively clipped.

        With the levels soft knee active, values that would have hard-clipped
        are compressed to just inside [0, 1]; the marks correspond to a
        pre-soft-clip value of exactly 0.0 / 1.0, so this measures "would have
        clipped" in both soft and hard modes. Comparisons using this are
        differential (candidate vs. baseline), so legitimately near-black or
        near-white content cancels out.
        """
        if arr.size == 0:
            return 0.0
        overshoot = np.any(
            (arr < _SOFT_CLIP_LO_MARK) | (arr > _SOFT_CLIP_HI_MARK), axis=2
        )
        total = arr.shape[0] * arr.shape[1]
        return float(np.count_nonzero(overshoot)) / float(total)

    @staticmethod
    def _percentile_from_hist(
        hist: np.ndarray, percentile: float, method: str = "lower"
    ) -> float:
        """Return a discrete percentile (bin index) from histogram counts."""
        total = int(hist.sum())
        if total <= 0:
            return 0.0

        q = max(0.0, min(100.0, float(percentile))) / 100.0
        rank = (total - 1) * q
        if method == "higher":
            target_index = math.ceil(rank)
        else:
            target_index = math.floor(rank)

        cdf = np.cumsum(hist)
        value = int(np.searchsorted(cdf, target_index + 1, side="left"))
        return float(max(0, min(len(hist) - 1, value)))

    def _crop_view_for_analysis(self, img_arr: np.ndarray) -> np.ndarray:
        """Return a view of ``img_arr`` restricted to the active crop box.

        Color statistics should describe the pixels the user is keeping, so
        cropped-away borders/backgrounds cannot skew the estimate. The crop
        box is defined after 90-degree rotation (a cheap numpy view that does
        not change the pixel population); straighten is ignored because the
        corner wedges are negligible for aggregate statistics.
        """
        with self._lock:
            edits = dict(self.current_edits)
        crop_box = edits.get("crop_box")
        if not crop_box:
            return img_arr
        try:
            if len(crop_box) != 4:
                return img_arr
            left_n, top_n, right_n, bottom_n = (float(v) for v in crop_box)
        except (TypeError, ValueError):
            return img_arr
        if (left_n, top_n, right_n, bottom_n) == (0.0, 0.0, 1000.0, 1000.0):
            return img_arr

        try:
            k = (int(edits.get("rotation", 0) or 0) % 360) // 90
        except (TypeError, ValueError):
            k = 0
        view = np.rot90(img_arr, k=k) if k else img_arr
        h, w = view.shape[:2]
        left = max(0, min(w, int(left_n * w / 1000.0)))
        top = max(0, min(h, int(top_n * h / 1000.0)))
        right = max(0, min(w, int(right_n * w / 1000.0)))
        bottom = max(0, min(h, int(bottom_n * h / 1000.0)))
        if right - left < 32 or bottom - top < 32:
            return img_arr
        return view[top:bottom, left:right]

    def estimate_auto_white_balance(
        self,
        *,
        strength: float = 0.7,
        warm_bias: int = 6,
        tint_bias: int = 0,
        tint_damp: float = 0.6,
        luma_lower_bound: int = 30,
        luma_upper_bound: int = 220,
        rgb_lower_bound: int = 5,
        rgb_upper_bound: int = 250,
        target_pixels: int = 600_000,
    ) -> Optional[Dict[str, float]]:
        """Estimate white-balance sliders from a robust preview-sized sample.

        Combines a neutral-pixel weighted gray-world estimate with a
        Shades-of-Gray (Minkowski) estimate, scales the applied strength by
        confidence (neutral sample size and estimator agreement), and damps
        the magenta/green axis since real illuminants vary mostly along
        blue/yellow.
        """
        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()

        img_arr = (
            self.float_image if self.float_image is not None else self.float_preview
        )
        if img_arr is None:
            if self.original_image is None:
                return None
            img_arr = (
                np.asarray(self.original_image.convert("RGB"), dtype=np.float32) / 255.0
            )

        img_arr = self._crop_view_for_analysis(img_arr)
        h, w = img_arr.shape[:2]
        total_pixels = max(1, h * w)
        stride = max(1, int(math.sqrt(total_pixels / max(1, target_pixels))))
        srgb = np.ascontiguousarray(np.clip(img_arr[::stride, ::stride], 0.0, 1.0))

        rgb_low = max(0.0, min(255.0, float(rgb_lower_bound))) / 255.0
        rgb_high = max(0.0, min(255.0, float(rgb_upper_bound))) / 255.0
        luma_low = max(0.0, min(255.0, float(luma_lower_bound))) / 255.0
        luma_high = max(0.0, min(255.0, float(luma_upper_bound))) / 255.0

        mask = np.all(srgb > rgb_low, axis=2) & np.all(srgb < rgb_high, axis=2)
        luma = 0.2126 * srgb[:, :, 0] + 0.7152 * srgb[:, :, 1] + 0.0722 * srgb[:, :, 2]
        mask &= (luma > luma_low) & (luma < luma_high)

        if not np.any(mask):
            return None

        # Exposure-valid population before neutral narrowing; used by the
        # secondary Shades-of-Gray estimator below.
        broad_mask = mask

        spread = np.max(srgb, axis=2) - np.min(srgb, axis=2)
        chroma_ratio = spread / np.maximum(luma, 1.0 / 255.0)
        valid_ratio = chroma_ratio[mask]

        neutrality_limit = 0.18
        if valid_ratio.size >= 128:
            try:
                neutrality_limit = float(
                    np.percentile(valid_ratio, 35.0, method="linear")
                )
            except TypeError:
                neutrality_limit = float(
                    np.percentile(valid_ratio, 35.0, interpolation="linear")
                )
            neutrality_limit = float(np.clip(neutrality_limit, 0.03, 0.18))

            neutral_mask = mask & (chroma_ratio <= neutrality_limit)
            if np.count_nonzero(neutral_mask) >= 128:
                mask = neutral_mask

        midtone_weight = 1.0 - np.abs(luma - 0.5) / 0.5
        midtone_weight = np.clip(midtone_weight, 0.1, 1.0)
        weights = midtone_weight / np.maximum(chroma_ratio + 0.02, 0.02)
        weights = np.clip(weights, 0.0, 20.0)

        selected = mask & np.isfinite(weights)
        selected_count = int(np.count_nonzero(selected))
        if selected_count == 0:
            return None

        sample_srgb = srgb[selected]
        sample_weights = weights[selected].astype(np.float32, copy=False)
        sample_linear = _srgb_to_linear(sample_srgb).astype(np.float32, copy=False)
        if not np.isfinite(sample_linear).all():
            return None

        r_mean = float(np.average(sample_linear[:, 0], weights=sample_weights))
        g_mean = float(np.average(sample_linear[:, 1], weights=sample_weights))
        b_mean = float(np.average(sample_linear[:, 2], weights=sample_weights))

        eps = 1e-6
        ratio_rb = b_mean / max(r_mean, eps)
        by_raw = 2.0 * (ratio_rb - 1.0) / max(ratio_rb + 1.0, eps)

        rb_target = 2.0 * r_mean * b_mean / max(r_mean + b_mean, eps)
        g_gain_target = rb_target / max(g_mean, eps)
        mg_raw = 2.0 * (1.0 - g_gain_target)

        # Secondary estimator: Shades-of-Gray (Minkowski p=6 mean) over the
        # broad exposure-valid population. Its failure modes differ from the
        # neutral-pixel estimate (it weights bright regions more), so
        # agreement between the two is a confidence signal and blending
        # tempers each one's biases.
        by_sog: Optional[float] = None
        mg_sog: Optional[float] = None
        broad_linear = _srgb_to_linear(srgb[broad_mask]).astype(np.float32, copy=False)
        if broad_linear.shape[0] >= 128 and np.isfinite(broad_linear).all():
            p_norm = 6.0
            sog = np.power(
                np.mean(np.power(broad_linear, p_norm), axis=0), 1.0 / p_norm
            )
            sog_r, sog_g, sog_b = (float(v) for v in sog)
            if min(sog_r, sog_g, sog_b) > eps:
                ratio_rb_sog = sog_b / sog_r
                by_sog = 2.0 * (ratio_rb_sog - 1.0) / max(ratio_rb_sog + 1.0, eps)
                rb_target_sog = 2.0 * sog_r * sog_b / max(sog_r + sog_b, eps)
                mg_sog = 2.0 * (1.0 - rb_target_sog / sog_g)

        confidence = 1.0
        if by_sog is not None and mg_sog is not None:
            disagreement = max(abs(by_raw - by_sog), abs(mg_raw - mg_sog))
            confidence = float(np.clip(1.25 - 2.5 * disagreement, 0.4, 1.0))
            by_raw = 0.7 * by_raw + 0.3 * by_sog
            mg_raw = 0.7 * mg_raw + 0.3 * mg_sog

        # Few usable neutral pixels means an unreliable estimate; fade the
        # correction toward identity instead of failing or over-correcting.
        confidence *= float(np.clip((selected_count - 64) / 2000.0, 0.0, 1.0))
        if confidence <= 0.0:
            return None

        # Real illuminants vary mostly along the blue/yellow (Planckian)
        # axis; a large magenta/green component is more often subject color
        # than color cast, so damp the tint axis.
        mg_raw *= float(np.clip(tint_damp, 0.0, 1.0))

        effective_strength = float(strength) * confidence
        by_value = (by_raw + (float(warm_bias) / 128.0)) * effective_strength
        mg_value = (mg_raw + (float(tint_bias) / 128.0)) * effective_strength

        by_value = float(np.clip(by_value, -1.0, 1.0))
        mg_value = float(np.clip(mg_value, -1.0, 1.0))

        if _debug:
            t_end = time.perf_counter()
            log.debug(
                "[AUTO_WB_EST] total=%dms sample=%dx%d stride=%d selected=%d "
                "neutral<=%.3f means=(%.4f, %.4f, %.4f) sog=(%s, %s) "
                "confidence=%.2f wb=(%.4f, %.4f)",
                int((t_end - t0) * 1000),
                srgb.shape[1],
                srgb.shape[0],
                stride,
                selected_count,
                neutrality_limit,
                r_mean,
                g_mean,
                b_mean,
                f"{by_sog:.4f}" if by_sog is not None else "n/a",
                f"{mg_sog:.4f}" if mg_sog is not None else "n/a",
                confidence,
                by_value,
                mg_value,
            )

        return {
            "by_value": by_value,
            "mg_value": mg_value,
            "r_mean": r_mean,
            "g_mean": g_mean,
            "b_mean": b_mean,
            "selected_pixels": float(selected_count),
            "stride": float(stride),
            "neutrality_limit": neutrality_limit,
            "confidence": confidence,
        }

    def _get_upstream_edits_hash(self, edits: Dict[str, Any]) -> int:
        """Returns a hash of edit parameters that affect the input to highlight recovery."""
        # Parameters that affect the image BEFORE highlight recovery:
        # bit_depth (implicit), crop_box, rotation, straighten_angle,
        # white_balance_by, white_balance_mg, exposure.
        # Note: 'highlights' and 'shadows' are applied IN this step, so they don't affect input.
        keys = [
            "crop_box",
            "rotation",
            "straighten_angle",
            "white_balance_by",
            "white_balance_mg",
            "exposure",
        ]

        def _freeze(v):
            if isinstance(v, list):
                return tuple(v)
            if isinstance(v, dict):
                return tuple(sorted(v.items()))
            if isinstance(v, np.ndarray):
                return v.tobytes()
            return v

        values = [_freeze(edits.get(k)) for k in keys]
        # Also include file path to distinguish different images
        values.append(str(self.current_filepath))
        # Include float_image ID to catch reload-in-place or content changes (e.g. forced reload)
        values.append(self.current_mtime)
        return hash(tuple(values))

    def _get_detail_upstream_hash(self, edits: Dict[str, Any]) -> tuple:
        """Returns a frozen tuple of edit parameters that affect the input to detail bands.

        NOTE: We intentionally EXCLUDE exposure, highlights, and shadows from this hash.

        Rationale for exclusions (performance vs accuracy tradeoff):
        - Exposure: We scale cached blurs by exp_gain ratio. This is exact only when
          highlights/shadows recovery is inactive (step 7 is non-linear).
        - Highlights/Shadows: Non-linear, so cached blurs are approximate after changes.

        The approximation is acceptable for smooth 60fps dragging. Exact blurs are
        recomputed when geometry (crop/rotate) or WB changes, which invalidates cache.

        Returns a tuple (hash, frozen_values) for collision-safe verification.
        """
        keys = [
            "crop_box",
            "rotation",
            "straighten_angle",
            "white_balance_by",
            "white_balance_mg",
        ]

        def _freeze(v):
            # Recursively freeze and quantize floats
            if isinstance(v, (list, tuple)):
                return tuple(_freeze(x) for x in v)
            if isinstance(v, dict):
                return tuple(sorted((_freeze(k), _freeze(val)) for k, val in v.items()))
            if isinstance(v, np.ndarray):
                return v.tobytes()
            # Quantize floats to avoid hash churn from tiny slider noise
            if isinstance(v, float):
                return round(v, 4)
            return v

        frozen = tuple(_freeze(edits.get(k)) for k in keys)
        frozen += (str(self.current_filepath), self.current_mtime)
        return (hash(frozen), frozen)

    def get_preview_data_cached(
        self,
        allow_compute: bool = True,
        edits_override: Optional[Dict[str, Any]] = None,
    ) -> Optional[DecodedImage]:
        """Return cached preview if available, otherwise compute and cache.

        Args:
            allow_compute: If False, returns None immediately if cache is stale (avoids blocking).
        """
        with self._lock:
            # Check cache validity
            if (
                edits_override is None
                and self._cached_preview is not None
                and self._cached_rev == self._edits_rev
            ):
                return self._cached_preview

            if not allow_compute:
                return None

            # Prepare for computation - snapshot data under lock. float_preview
            # is only ever reassigned, never mutated in place, so the render
            # can share it; protect_input copies only the post-crop region.
            base = self.float_preview
            edits = (
                dict(self.current_edits)
                if edits_override is None
                else dict(edits_override)
            )
            icc_bytes = (
                self.original_image.info.get("icc_profile")
                if self.original_image is not None
                else None
            )
            rev = self._edits_rev

        if base is None:
            return None

        decoded = self._render_decoded_from_float(
            base,
            edits=edits,
            for_export=False,
            icc_bytes=icc_bytes,
            protect_input=True,
        )

        with self._lock:
            # Only cache if revision hasn't changed during computation
            if edits_override is None and self._edits_rev == rev:
                self._cached_preview = decoded
                self._cached_rev = rev

        return decoded

    def _render_decoded_from_float(
        self,
        base: np.ndarray,
        *,
        edits: Dict[str, Any],
        for_export: bool,
        apply_loupe_color: bool = False,
        icc_bytes: Optional[bytes] = None,
        cache_context: Optional[dict] = None,
        downscale_long_edge: Optional[int] = None,
        protect_input: bool = False,
    ) -> DecodedImage:
        """Render edits against a float RGB array and package it for Qt display."""
        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()
        arr = self._apply_edits(
            base,
            edits=edits,
            for_export=for_export,
            cache_context=cache_context,
            downscale_long_edge=downscale_long_edge,
            protect_input=protect_input,
        )
        if _debug:
            t_apply = time.perf_counter()
        # _apply_edits returns either a fresh array or a view of `base`; every
        # caller passes a private copy or sets protect_input=True, so clipping
        # in place cannot corrupt editor state. cv2.convertScaleAbs fuses
        # scale+round+saturate into one multithreaded pass (~5x faster than
        # clip + mul + astype).
        if cv2 is not None:
            np.clip(arr, 0.0, 1.0, out=arr)
            arr_u8 = cv2.convertScaleAbs(arr, alpha=255.0)
        else:
            arr = np.clip(arr, 0.0, 1.0)
            arr_u8 = (arr * 255).astype(np.uint8)
        if _debug:
            t_u8 = time.perf_counter()
        if apply_loupe_color:
            arr_u8 = apply_loupe_color_correction(arr_u8, icc_bytes=icc_bytes)
        if _debug:
            t_color = time.perf_counter()
            log.debug(
                "[RENDER_DECODED] apply=%dms u8=%dms color=%dms total=%dms  (%dx%d, %s)",
                int((t_apply - t0) * 1000),
                int((t_u8 - t_apply) * 1000),
                int((t_color - t_u8) * 1000),
                int((t_color - t0) * 1000),
                arr_u8.shape[1],
                arr_u8.shape[0],
                "export" if for_export else "preview",
            )

        if QImage is None:
            raise ImportError(
                "PySide6.QtGui.QImage is required for rendering decoded image data"
            )

        # tobytes() always serializes in C-contiguous (row-major) order, so
        # bytes_per_line must reflect that layout. Operations like np.rot90
        # (90-degree rotation) leave arr_u8 as a non-contiguous view whose
        # strides[0] is NOT width*channels; force contiguity so the stride and
        # the serialized buffer agree, otherwise QImage decodes to a null image.
        arr_u8 = np.ascontiguousarray(arr_u8)
        img_buffer = arr_u8.tobytes()
        return DecodedImage(
            buffer=memoryview(img_buffer),
            width=arr_u8.shape[1],
            height=arr_u8.shape[0],
            bytes_per_line=arr_u8.strides[0],
            format=QImage.Format.Format_RGB888,
        )

    def get_full_resolution_preview_data(
        self,
        max_long_edge: Optional[int] = None,
        edits_override: Optional[Dict[str, Any]] = None,
    ) -> Optional[DecodedImage]:
        """Apply current edits to the full-resolution master for live display.

        ``max_long_edge`` caps the rendered output (applied after crop, before
        tonal edits) so display-only renders do not process a 20MP master when
        the screen can only show a fraction of it. Pass None for true full
        resolution.
        """
        try:
            self._ensure_float_image()
        except RuntimeError:
            return None

        with self._lock:
            if self.float_image is None:
                return None
            # Share the master instead of copying ~3 bytes/pixel of float32 up
            # front; protect_input defers the copy until after crop/downscale,
            # so only the (much smaller) displayed region is ever duplicated.
            # float_image is only ever reassigned, never mutated in place, so
            # rendering from the shared reference outside the lock is safe.
            base = self.float_image
            edits = (
                dict(self.current_edits)
                if edits_override is None
                else dict(edits_override)
            )
            icc_bytes = (
                self.original_image.info.get("icc_profile")
                if self.original_image is not None
                else None
            )

        return self._render_decoded_from_float(
            base,
            edits=edits,
            for_export=True,
            apply_loupe_color=True,
            icc_bytes=icc_bytes,
            cache_context={},
            downscale_long_edge=max_long_edge,
            protect_input=True,
        )

    def _crop_only_edits(self, edits: Dict[str, Any]) -> Dict[str, Any]:
        """Return edits for before/after comparison while preserving crop framing."""
        crop_only = self._initial_edits()
        for key in ("crop_box", "rotation", "straighten_angle"):
            crop_only[key] = edits.get(key, crop_only[key])
        return crop_only

    def _float_preview_from_master(self) -> Optional[np.ndarray]:
        """Build a display-sized float preview from the unedited source buffer.

        The result is display-space ("cooked"), matching the float_preview
        contract — preview-sized renders are shown without further color
        correction.
        """
        with self._lock:
            if self.float_preview is not None:
                return self.float_preview.copy()
            source = self.float_image.copy() if self.float_image is not None else None
            original = (
                self.original_image.copy() if self.original_image is not None else None
            )
            icc_bytes = (
                self.original_image.info.get("icc_profile")
                if self.original_image is not None
                else None
            )

        if source is not None:
            arr_u8 = (np.clip(source, 0.0, 1.0) * 255).astype(np.uint8)
            thumb = Image.fromarray(arr_u8, mode="RGB")
        elif original is not None:
            thumb = original.convert("RGB")
        else:
            return None

        thumb.thumbnail((1920, 1080))
        preview_u8 = apply_loupe_color_correction(
            np.asarray(thumb.convert("RGB"), dtype=np.uint8),
            icc_bytes=icc_bytes,
        )
        preview = preview_u8.astype(np.float32)
        preview *= np.float32(1.0 / 255.0)
        return preview

    def get_original_compare_preview_data(
        self, *, full_resolution: bool = False
    ) -> Optional[DecodedImage]:
        """Render the source image with only crop framing applied."""
        if full_resolution:
            try:
                self._ensure_float_image()
            except RuntimeError:
                return None

        with self._lock:
            if full_resolution and self.float_image is None:
                return None
            # Share the master (never mutated in place) and let protect_input
            # copy only the cropped region; the preview branch already returns
            # a private array, so it needs no protection.
            base = (
                self.float_image
                if full_resolution
                else self._float_preview_from_master()
            )
            edits = self._crop_only_edits(dict(self.current_edits))
            icc_bytes = (
                self.original_image.info.get("icc_profile")
                if self.original_image is not None
                else None
            )

        if base is None:
            return None

        return self._render_decoded_from_float(
            base,
            edits=edits,
            for_export=full_resolution,
            apply_loupe_color=full_resolution,
            icc_bytes=icc_bytes,
            cache_context={},
            protect_input=full_resolution,
        )

    def get_preview_data(self) -> Optional[DecodedImage]:
        """Apply current edits and return the data as a DecodedImage."""
        return self.get_preview_data_cached()

    def get_edit_value(self, key: str, default: Any = None) -> Any:
        """Thread-safe retrieval of an edit parameter."""
        with self._lock:
            return self.current_edits.get(key, default)

    def set_edit_param(self, key: str, value: Any) -> bool:
        """Update a single edit parameter."""
        with self._lock:
            if key == "rotation":
                # Guard against arbitrary angles in 'rotation'. It expects 90-degree steps.
                # For arbitrary rotation (drag to rotate), use 'straighten_angle'.
                try:
                    current_val = int(self.current_edits.get(key, 0)) % 360
                    # Round to nearest 90 degrees
                    val_deg = float(value)
                    rounded_deg = round(val_deg / 90.0) * 90
                    final_val = int(rounded_deg) % 360

                    if abs(val_deg - rounded_deg) > 1.0:
                        log.warning(
                            "'rotation' received %s. Rounding to %d. Use 'straighten_angle' for free rotation.",
                            value,
                            final_val,
                        )

                    crop_box = self.current_edits.get("crop_box")
                    self.current_edits["crop_box"] = (
                        self._rotate_crop_box_for_rotation_change(
                            crop_box,
                            current_val,
                            final_val,
                        )
                    )
                    self.current_edits[key] = final_val
                    self._edits_rev += 1
                    return True
                except (ValueError, TypeError) as e:
                    log.warning("Invalid value for rotation %r: %s", value, e)
                    return False

            if key in self.current_edits and key != "crop_box":
                # Check for floating point equality to prevent cache thrashing
                new_val = value
                current_val = self.current_edits.get(key)

                # Try to compare as floats if possible
                try:
                    vf = float(new_val)
                    cf = float(current_val)
                    if math.isclose(vf, cf, rel_tol=1e-5, abs_tol=1e-7):
                        return False
                except (ValueError, TypeError):
                    # Fallback to direct equality
                    if current_val == new_val:
                        return False

                self.current_edits[key] = value
                self._edits_rev += 1
                return True
            return False

    def _apply_highlights_shadows(
        self,
        linear: np.ndarray,
        highlights: float,
        shadows: float,
        *,
        srgb_u8_stride: Optional[np.ndarray] = None,
        srgb_u8: Optional[np.ndarray] = None,  # Planned future alias for srgb_u8_stride
        analysis_state: Optional[Dict[str, float]] = None,
        edits: Optional[Dict[str, Any]] = None,
        cache_context: Optional[dict] = None,
    ) -> np.ndarray:
        """Apply highlights and shadows adjustments using brightness-based processing in linear light.

        Highlights slider semantics:
        - Negative (e.g., -100): Compress bright regions, recover detail if headroom exists.
          Uses brightness-based rescaling to preserve hue/chroma.
        - Positive (e.g., +100): Lift highlights (brighten bright areas).

        For JPEG (no headroom): Applies perceptual rolloff + optional desaturation
        (artistic fallback) to simulate recovery of micro-contrast.

        Args:
            linear: Float32 RGB array (H, W, 3) in linear light, may have values > 1.0
            highlights: -1.0 to 1.0, negative recovers highlights, positive boosts
            shadows: -1.0 to 1.0, positive lifts shadows, negative crushes
            srgb_u8_stride: Optional uint8 sRGB array (strided) for accurate JPEG clipping detection
                     (should be the image BEFORE linearization)
            srgb_u8: Keyword-only alias for srgb_u8_stride (not yet active in call site).
            analysis_state: Optional pre-computed analysis state to avoid re-work.

        Returns:
            Adjusted float32 RGB array (linear)
        """
        arr = linear
        effective_srgb_u8 = srgb_u8 if srgb_u8 is not None else srgb_u8_stride

        # Analyze highlight state if needed
        # If caller passed analysis_state, usage that.
        state = analysis_state
        if state is None:
            # Re-compute locally if not provided
            # We assume effective_srgb_u8 is ALREADY STRIDED if passed
            arr_stride = arr[::4, ::4, :]
            # If effective_srgb_u8 was passed, use it directly (it's already small).
            # If it wasn't passed, we can't easily recreate the source state here without the original source buffer.
            # But the caller (_apply_edits) usually provides it.
            state = _analyze_highlight_state(arr_stride, srgb_u8=effective_srgb_u8)

        # Ensure we have edits dict to check upstream hash
        if edits is None:
            # Fallback to current_edits if not provided (cached preview path passes it)
            # But access under lock to be safe from modifications
            with self._lock:
                edits = self.current_edits.copy()

        # We DO NOT update self._last_highlight_state here to avoid race/staleness during export.
        # The preview path in _apply_edits handles the UI state update.

        # --- Shadows Adjustment (unchanged approach) ---
        if abs(shadows) > 0.001:
            # Compute luminance for shadow mask
            lum = arr[:, :, 0] * 0.2126 + arr[:, :, 1] * 0.7152 + arr[:, :, 2] * 0.0722
            lum = np.clip(lum, 1e-10, None)

            pivot = 0.18  # Mid-gray in linear
            shadow_mask = _smoothstep01(1.0 - lum / pivot)

            shadow_adj = shadows * 0.5
            shadow_factor = 1.0 + shadow_adj * shadow_mask
            shadow_factor = np.expand_dims(shadow_factor, axis=2)
            arr = arr * shadow_factor

        # --- Highlights Adjustment (new brightness-based approach) ---
        if abs(highlights) > 0.001:
            headroom_pct = state["headroom_pct"]

            # Use specific keys from new analysis logic
            # source_clipped_pct: True JPEG flat-top clipping
            # current_nearwhite_pct: Current brightness distribution near 1.0
            clipped_pct = state.get("source_clipped_pct", state.get("clipped_pct", 0.0))
            near_white_pct = state.get(
                "current_nearwhite_pct", state.get("near_white_pct", 0.0)
            )

            if highlights < 0:
                # Negative: compress/recover highlights
                amount = -highlights  # 0 to 1

                # Adaptive parameters based on headroom and clipping
                # More clipping (source) → later pivot (only affect very top end)
                pivot = _lerp(ADAPTIVE_PIVOT_MIN, ADAPTIVE_PIVOT_MAX, clipped_pct)

                # k increases with near_white_pct (recoverable micro-contrast)
                # BUT also increase k when headroom exists for stronger compression
                k = ADAPTIVE_K_BASE + ADAPTIVE_K_SCALING * near_white_pct
                if headroom_pct > 0.01:
                    k = max(
                        k,
                        ADAPTIVE_K_HEADROOM_BASE
                        + ADAPTIVE_K_HEADROOM_SCALING * headroom_pct,
                    )  # Stronger k for headroom

                # More clipping → more chroma rolloff
                chroma_rolloff = _lerp(
                    ADAPTIVE_ROLLOFF_MIN, ADAPTIVE_ROLLOFF_MAX, clipped_pct
                )

                # Headroom ceiling: preserve some tonal separation above 1.0
                # Robustify: use percentile to ignore hot pixels, clamped to valid range
                if headroom_pct > 0.01:
                    # Check cache for max_brightness
                    max_brightness = 1.0

                    # Compute hash of upstream params
                    current_hash = self._get_upstream_edits_hash(edits)

                    max_brightness = 1.0
                    hit = False
                    with self._lock:
                        cached = (
                            cache_context.get("max_brightness_state")
                            if cache_context is not None
                            else self._cached_max_brightness_state
                        )
                        if cached and cached.get("hash") == current_hash:
                            max_brightness = cached["value"]
                            hit = True

                    if not hit:
                        # Use 99.5th percentile of max-channel brightness to avoid hot pixels
                        max_rgb = arr.max(axis=2)
                        if max_rgb.size > 0:
                            # Optimize: Use much coarser stride and np.partition for speed
                            # We only need an estimate for headroom, so we don't need high precision
                            # Stride ::10 reduces data by 100x vs full, 6x faster than ::4
                            view = max_rgb[::10, ::10]
                            if view.size > 0:
                                # np.partition is O(N) vs np.percentile O(N log N)
                                # We want 99.5th percentile roughly.
                                # Index for 99.5% = size * 0.995 => size - (size * 0.005)
                                k_index = int(view.size * 0.995)
                                # Clamp to valid range
                                k_index = min(max(0, k_index), view.size - 1)

                                partitioned = np.partition(view.flatten(), k_index)
                                max_brightness = float(partitioned[k_index])
                            else:
                                max_brightness = 1.0
                        else:
                            max_brightness = 1.0

                        with self._lock:
                            entry = {
                                "hash": current_hash,
                                "value": max_brightness,
                            }
                            if cache_context is not None:
                                cache_context["max_brightness_state"] = entry
                            else:
                                self._cached_max_brightness_state = entry

                    # Clamp to avoid crazy values from single hot pixels or artifacts
                    max_brightness = min(max_brightness, 100.0)

                    if max_brightness > 1.0:
                        # Preserve some headroom detail, reduced by amount
                        headroom_ceiling = 1.0 + (max_brightness - 1.0) * 0.3 * (
                            1.0 - amount * 0.7
                        )
                        pivot = min(pivot, 0.5 + 0.25 * (1.0 - headroom_pct))
                    else:
                        headroom_ceiling = 1.0
                else:
                    headroom_ceiling = 1.0

                # JPEG fallback: when near_white is high but clipping is low,
                # nudge pivot earlier to expose micro-contrast (Photoshop-like feel)
                if headroom_pct < 0.01:
                    if near_white_pct > 0.05 and clipped_pct < 0.05:
                        # Lots of recoverable near-white, not much flat clipping:
                        # nudge the pivot earlier so the broad highlight band is
                        # engaged, floored low enough to keep covering ordinary
                        # bright pixels rather than just the clipped top end.
                        pivot = max(0.28, pivot - 0.12 * near_white_pct)
                    if clipped_pct > 0.02:
                        # Increase chroma rolloff for flat-clipped JPEGs
                        chroma_rolloff = max(chroma_rolloff, 0.14)

                arr = _highlight_recover_linear(
                    arr,
                    amount,
                    pivot=pivot,
                    k=k,
                    chroma_rolloff=chroma_rolloff,
                    headroom_ceiling=headroom_ceiling,
                )
            else:
                # Positive: boost highlights (hue-preserving)
                amount = highlights  # 0 to 1
                arr = _highlight_boost_linear(arr, amount, pivot=0.5)

        return arr

    def set_crop_box(self, crop_box: Tuple[int, int, int, int]):
        """Set the normalized crop box (left, top, right, bottom) from 0-1000."""
        with self._lock:
            if self.current_edits.get("crop_box") == crop_box:
                return False
            self.current_edits["crop_box"] = crop_box
            self._edits_rev += 1
            return True

    def map_crop_draft_to_source(
        self,
        draft_box: Tuple[int, int, int, int],
        base_crop_box: Optional[Tuple[int, int, int, int]],
        base_straighten_angle: float,
    ) -> Optional[Tuple[int, int, int, int]]:
        """Map a crop box drawn on the displayed render back into source space.

        ``draft_box`` is normalized 0-1000 against the image shown when crop
        mode was entered: the render produced by ``base_crop_box`` +
        ``base_straighten_angle``. The result is normalized to source space
        (post 90-degree rotation, pre-straighten) — the space
        ``current_edits["crop_box"]`` uses. With a committed straighten the
        display is a rotated, fill-trimmed window onto the source, so a
        linear composition into the committed box selects the wrong region;
        this replicates the render geometry and inverts it.
        """
        if draft_box is None:
            return None
        try:
            draft = tuple(float(v) for v in draft_box)
        except (TypeError, ValueError):
            return None
        if len(draft) != 4:
            return None
        if base_crop_box is not None and tuple(base_crop_box) == (0, 0, 1000, 1000):
            base_crop_box = None

        def _finalize(values: tuple) -> Tuple[int, int, int, int]:
            left, top, right, bottom = (int(round(v)) for v in values)
            left, right = min(left, right), max(left, right)
            top, bottom = min(top, bottom), max(top, bottom)
            left = max(0, min(1000, left))
            top = max(0, min(1000, top))
            right = max(0, min(1000, right))
            bottom = max(0, min(1000, bottom))
            if right <= left:
                right = min(1000, left + 1)
            if bottom <= top:
                bottom = min(1000, top + 1)
            return left, top, right, bottom

        with self._lock:
            original = self.original_image
            try:
                rotation = int(self.current_edits.get("rotation", 0) or 0) % 360
            except (TypeError, ValueError):
                rotation = 0

        if abs(base_straighten_angle) <= 0.001 or original is None:
            # The display is an unrotated window: plain linear composition.
            if base_crop_box is None:
                return _finalize(draft)
            base_left, base_top, base_right, base_bottom = (
                float(v) for v in base_crop_box
            )
            base_w = base_right - base_left
            base_h = base_bottom - base_top
            if base_w <= 0 or base_h <= 0:
                return _finalize(draft)
            return _finalize(
                (
                    base_left + base_w * draft[0] / 1000.0,
                    base_top + base_h * draft[1] / 1000.0,
                    base_left + base_w * draft[2] / 1000.0,
                    base_top + base_h * draft[3] / 1000.0,
                )
            )

        src_w, src_h = original.size
        if rotation in (90, 270):
            src_w, src_h = src_h, src_w
        if src_w <= 0 or src_h <= 0:
            return _finalize(draft)

        canvas_w, canvas_h = _expanded_canvas_size(src_w, src_h, base_straighten_angle)

        # The rect of the expanded canvas that was displayed (same geometry
        # the renderer uses in _apply_edits).
        if base_crop_box is None:
            angle_rad = math.radians(base_straighten_angle)
            cw, ch = _rotated_rect_with_max_area(src_w, src_h, angle_rad)
            d_left, d_top, d_right, d_bottom = _autocrop_canvas_rect(
                cw, ch, canvas_w, canvas_h, base_straighten_angle
            )
        else:
            d_left, d_top, d_right, d_bottom = _crop_box_canvas_rect(
                tuple(float(v) for v in base_crop_box),
                src_w,
                src_h,
                base_straighten_angle,
                canvas_w,
                canvas_h,
            )

        disp_w = d_right - d_left
        disp_h = d_bottom - d_top
        if disp_w <= 0 or disp_h <= 0:
            return _finalize(draft)

        # Draft rect in canvas pixels.
        c_left = d_left + disp_w * draft[0] / 1000.0
        c_top = d_top + disp_h * draft[1] / 1000.0
        c_right = d_left + disp_w * draft[2] / 1000.0
        c_bottom = d_top + disp_h * draft[3] / 1000.0

        angle_rad = math.radians(base_straighten_angle)

        # Exact inverse of the renderer's geometry: a source box renders as
        # an upright rect of the box's dimensions (swapped at odd 90-degree
        # multiples) centered where the box center lands. So the source box
        # takes the drawn rect's dimensions (swapped back) and is centered at
        # the drawn rect's center inverse-rotated into source space — which
        # makes repeated crop sessions compose exactly.
        target_w = c_right - c_left
        target_h = c_bottom - c_top
        if round(base_straighten_angle / 90.0) % 2:
            target_w, target_h = target_h, target_w

        dx = (c_left + c_right) / 2.0 - canvas_w / 2.0
        dy = (c_top + c_bottom) / 2.0 - canvas_h / 2.0
        inv_cos, inv_sin = math.cos(-angle_rad), math.sin(-angle_rad)
        scx = dx * inv_cos - dy * inv_sin + src_w / 2.0
        scy = dx * inv_sin + dy * inv_cos + src_h / 2.0
        left = max(0.0, scx - target_w / 2.0)
        top = max(0.0, scy - target_h / 2.0)
        right = min(float(src_w), scx + target_w / 2.0)
        bottom = min(float(src_h), scy + target_h / 2.0)

        return _finalize(
            (
                left * 1000.0 / src_w,
                top * 1000.0 / src_h,
                right * 1000.0 / src_w,
                bottom * 1000.0 / src_h,
            )
        )

    def _write_tiff_16bit(self, path: Path, arr_float: np.ndarray):
        """
        Writes a float32 (0-1) numpy array as an uncompressed 16-bit RGB TIFF using OpenCV.
        arr_float shape: (H, W, 3)
        """
        if cv2 is None:
            raise RuntimeError("Saving 16-bit TIFF requires OpenCV")

        # Convert to 16-bit
        # Clip to safe range before scaling
        arr = (np.clip(arr_float, 0.0, 1.0) * 65535).astype(np.uint16)

        # OpenCv expects BGR for imwrite
        if len(arr.shape) == 3 and arr.shape[2] == 3:
            arr_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            success = cv2.imwrite(str(path), arr_bgr)
            if not success:
                raise IOError(f"Failed to write TIFF -> {path}")
        else:
            raise ValueError("Only RGB supported for TIFF writer")

    def _get_sanitized_exif_bytes(self) -> Optional[bytes]:
        """
        Returns EXIF bytes with Orientation reset to 1 (Normal).
        Used when we've baked rotation/straightening into the pixels.

        Prefers cached source EXIF (from paired JPEG) if available,
        otherwise falls back to the current original_image's EXIF.

        If sanitization or serialization fails, returns None (drops EXIF)
        to prevent incorrect "double rotation" in viewers.

        Returns:
            bytes object of EXIF data, or None if sanitization/serialization failed.
        """
        try:
            exif = None

            # 1. Try to build an Exif object from raw bytes (best: preserves all tags)
            if self._source_exif_bytes and hasattr(Image, "Exif"):
                try:
                    ex = Image.Exif()
                    if hasattr(ex, "load"):
                        ex.load(self._source_exif_bytes)
                        exif = ex
                except Exception:
                    exif = None

            # 2. Fallback: pull EXIF from the loaded image (may be partial, but usually ok)
            if exif is None and self.original_image is not None:
                try:
                    exif = self.original_image.getexif()
                except Exception:
                    exif = None

            if exif is None:
                return None

            # 3. Orientation tag (0x0112)
            orientation_tag = 0x0112
            try:
                # Pillow 9.1.0+ has ExifTags.Base.Orientation
                orientation_tag = ExifTags.Base.Orientation
            except Exception:
                pass

            # 4. Reset Orientation to 1 (Normal)
            exif[orientation_tag] = 1

            # 5. Guard for tobytes()
            if not hasattr(exif, "tobytes"):
                log.warning(
                    "EXIF object has no tobytes() method, dropping EXIF to prevent rotation issues."
                )
                return None

            try:
                return exif.tobytes()
            except Exception as e:
                log.warning(
                    "Failed to serialize sanitized EXIF: %s. Dropping EXIF to prevent rotation issues.",
                    e,
                )
                return None
        except Exception as e:
            log.warning("Failed to sanitize EXIF orientation: %s. Dropping EXIF.", e)
            return None

    def _ensure_float_image(self) -> None:
        """Ensure self.float_image exists. Needed when load_image(preview_only=True)."""
        # 1. Quick check under lock
        with self._lock:
            if self.float_image is not None:
                return
            if self.original_image is None:
                raise RuntimeError("No image loaded")
            # Snapshot original image to convert outside lock
            original_ref = self.original_image

        # 2. Expensive conversion outside lock
        rgb = original_ref.convert("RGB")
        float_arr = np.asarray(rgb).astype(np.float32)
        float_arr *= np.float32(1.0 / 255.0)

        # 3. Store result under lock (checking if someone beat us to it, or if image changed)
        with self._lock:
            # Only assign if original_image hasn't changed
            if self.original_image is original_ref:
                if self.float_image is None:
                    self.float_image = float_arr

    def snapshot_for_export(
        self,
        write_developed_jpg: bool = False,
        developed_path: Optional[Path] = None,
        save_target_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Capture an immutable export snapshot on the calling thread.

        Must be called on the main thread BEFORE submitting to a background
        executor.  The returned dict contains everything needed to produce the
        final output — no live ``ImageEditor`` state is required afterwards.

        Returns:
            A dict with all export-critical data.

        Raises:
            RuntimeError: If preconditions are not met (no path, no image).
        """
        if self.current_filepath is None:
            raise RuntimeError("No file path set")
        if self.original_image is None:
            raise RuntimeError("No image loaded")

        # Ensure float master exists (preview_only loads may not have it)
        self._ensure_float_image()

        with self._lock:
            if self.float_image is None:
                raise RuntimeError("snapshot_for_export called with no float_image")

            # --- Source image ---
            _safe_no_copy = self._edits_can_share_input(self.current_edits)
            if _safe_no_copy:
                source_arr = self.float_image
                log.debug(
                    "snapshot_for_export: skipping float_image.copy() (safe no-copy path)"
                )
            else:
                source_arr = self.float_image.copy()

            source_shape = self.float_image.shape[:2]  # for debug logging

            # --- Edits (shallow dict copy) ---
            edits_snapshot = self.current_edits.copy()

            # --- Deep-snapshot mutable darken state ---
            # Always deepcopy DarkenSettings when present so the background
            # thread never reads the live object (which the main thread can
            # mutate, e.g. enabling/disabling or changing params).
            import copy

            ds = edits_snapshot.get("darken_settings")
            if ds is not None:
                edits_snapshot["darken_settings"] = copy.deepcopy(ds)
                if getattr(ds, "enabled", False):
                    live_mask = self._mask_assets.get(ds.mask_id)
                    mask_snapshot = (
                        copy.deepcopy(live_mask) if live_mask is not None else None
                    )
                    export_cache = MaskRasterCache()
                else:
                    # Darken disabled — record the absence explicitly so
                    # save_from_snapshot does not fall back to live assets.
                    mask_snapshot = None
                    export_cache = None
            else:
                mask_snapshot = None
                export_cache = None

            mask_assets_snapshot = {
                key: copy.deepcopy(mask)
                for key, mask in self._mask_assets.items()
                if mask is not None
            }

            # --- Paths ---
            filepath_snapshot = self.current_filepath
            source_filepath_snapshot = self.source_filepath or self.current_filepath

            # --- EXIF (may read original_image and _source_exif_bytes) ---
            main_exif = self._get_sanitized_exif_bytes()
            source_exif = self._source_exif_bytes

        # Build mask override dict.  When darken is present but disabled (or
        # has no mask), provide an empty dict so _apply_edits uses it instead
        # of falling back to the live self._mask_assets.
        ds_snap = edits_snapshot.get("darken_settings")
        if ds_snap is not None:
            mask_override = (
                {ds_snap.mask_id: mask_snapshot} if mask_snapshot is not None else {}
            )
        else:
            mask_override = None

        original_path = save_target_path if save_target_path else filepath_snapshot

        return {
            "source_arr": source_arr,
            "source_shape": source_shape,
            "edits": edits_snapshot,
            "mask_override": mask_override,
            "export_cache": export_cache,
            "original_path": original_path,
            "filepath_snapshot": filepath_snapshot,
            "source_filepath": source_filepath_snapshot,
            "current_mtime": self.current_mtime,
            "bit_depth": self.bit_depth,
            "main_exif": main_exif,
            "source_exif": source_exif,
            "source_icc_bytes": self.original_image.info.get("icc_profile"),
            "write_developed_jpg": write_developed_jpg,
            "developed_path": developed_path,
            "mask_assets": mask_assets_snapshot,
        }

    def _dither_for_export(
        self, final_float: np.ndarray, edits: Dict[str, Any]
    ) -> np.ndarray:
        """Add ±1 LSB TPDF dither before 8-bit quantization when warranted.

        A strong blacks/whites stretch amplifies the source's 8-bit
        quantization steps into visible banding (skies, gradients). Triangular
        noise decorrelates the quantization error and hides the bands. The
        noise plane is shared across channels (luminance-only, no chroma
        speckle) and the RNG is seeded so identical edits export identical
        bytes. Returns a new array; the input snapshot buffer is not mutated.
        """
        if not self.export_dither:
            return final_float
        try:
            blacks = float(edits.get("blacks", 0.0))
            whites = float(edits.get("whites", 0.0))
        except (TypeError, ValueError):
            return final_float
        bp = -blacks * 0.15
        wp = 1.0 - (whites * 0.15)
        gain = 1.0 / max(wp - bp, 1e-4)
        if gain < _EXPORT_DITHER_MIN_GAIN:
            return final_float

        h, w = final_float.shape[:2]
        rng = np.random.default_rng(0x5EED)
        noise = rng.random((h, w), dtype=np.float32)
        noise -= rng.random((h, w), dtype=np.float32)
        noise *= 1.0 / 255.0
        log.debug(
            "[EXPORT_DITHER] applied TPDF dither (levels gain %.2f)",
            gain,
        )
        return final_float + noise[..., None]

    def save_from_snapshot(
        self, snapshot: Dict[str, Any]
    ) -> Optional[Tuple[Path, Path]]:
        """Run the full-resolution export from a pre-captured snapshot.

        This method is safe to call from a background thread — it does NOT
        read any live ``ImageEditor`` state for export-critical data.
        All mutable state comes from the *snapshot* dict produced by
        ``snapshot_for_export()``.

        Returns:
            A tuple of (saved_path, backup_path) on success, otherwise None.

        Raises:
            RuntimeError: If saving fails.
        """
        source_arr = snapshot["source_arr"]
        edits_snapshot = snapshot["edits"]
        mask_override = snapshot["mask_override"]
        export_cache = snapshot["export_cache"]
        original_path = snapshot["original_path"]
        main_exif = snapshot["main_exif"]
        source_exif = snapshot["source_exif"]
        write_developed_jpg = snapshot["write_developed_jpg"]
        developed_path = snapshot["developed_path"]
        source_shape = snapshot["source_shape"]

        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()

        # 1. Apply edits to full resolution — uses only snapshot data
        # Use isolated snapshot context so background export doesn't pollute self._cached_*
        export_cache_context = {}
        final_float = self._apply_edits(
            source_arr,
            edits=edits_snapshot,
            for_export=True,
            mask_assets_override=mask_override,
            cache_override=export_cache,
            cache_context=export_cache_context,
        )  # (H,W,3) float32

        if _debug:
            t_edits = time.perf_counter()

        try:
            original_stat = original_path.stat()
        except OSError as e:
            log.warning("Unable to read timestamps for %s: %s", original_path, e)
            original_stat = None

        # 2. Backup
        backup_path = create_backup_file(original_path)
        if backup_path is None:
            return None
        if _debug:
            t_backup = time.perf_counter()

        try:
            # 3. Save Main File
            is_tiff = original_path.suffix.lower() in [".tif", ".tiff"]

            # 8-bit outputs (main JPEG and/or developed JPG) share one
            # dithered buffer; the 16-bit TIFF path stays undithered.
            if not is_tiff or write_developed_jpg:
                dithered_float = self._dither_for_export(final_float, edits_snapshot)
            else:
                dithered_float = final_float

            if is_tiff:
                tmp_path = original_path.with_name(
                    f".{original_path.stem}_{uuid.uuid4().hex[:8]}{original_path.suffix}"
                )
                try:
                    self._write_tiff_16bit(tmp_path, final_float)
                    _safe_replace(tmp_path, original_path)
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise
            else:
                arr_u8 = _float01_to_u8(dithered_float)
                img_u8 = Image.fromarray(arr_u8, mode="RGB")

                save_kwargs = {"quality": 95}
                if main_exif:
                    save_kwargs["exif"] = main_exif

                tmp_path = original_path.with_name(
                    f".{original_path.stem}_{uuid.uuid4().hex[:8]}{original_path.suffix}"
                )
                try:
                    try:
                        img_u8.save(tmp_path, **save_kwargs)
                    except Exception:
                        img_u8.save(tmp_path)
                    _safe_replace(tmp_path, original_path)
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise

            if original_stat is not None:
                self._restore_file_times(original_path, original_stat)

            # 4. Save Sidecar JPG (-developed.jpg) — only when explicitly requested
            if write_developed_jpg:
                if developed_path is None:
                    stem = original_path.stem
                    if stem.lower().endswith("-working"):
                        stem = stem[:-8]
                    developed_path = original_path.with_name(f"{stem}-developed.jpg")

                rotation = edits_snapshot.get("rotation", 0)
                straighten_angle = float(edits_snapshot.get("straighten_angle", 0.0))
                transforms_applied = (rotation != 0) or (abs(straighten_angle) > 0.001)

                exif_bytes = None
                if transforms_applied:
                    exif_bytes = main_exif
                elif source_exif:
                    exif_bytes = sanitize_exif_orientation(source_exif)

                arr_u8 = _float01_to_u8(dithered_float)
                img_u8 = Image.fromarray(arr_u8)

                dev_kwargs = {"quality": 90}
                if exif_bytes:
                    dev_kwargs["exif"] = exif_bytes

                tmp_dev = developed_path.with_name(
                    f".{developed_path.stem}_{uuid.uuid4().hex[:8]}{developed_path.suffix}"
                )
                try:
                    try:
                        img_u8.save(tmp_dev, **dev_kwargs)
                    except Exception:
                        img_u8.save(tmp_dev)
                    _safe_replace(tmp_dev, developed_path)
                except BaseException:
                    tmp_dev.unlink(missing_ok=True)
                    raise

            if _debug:
                t_write = time.perf_counter()
                h, w = source_shape
                log.debug(
                    "[SAVE_IMAGE] apply_edits=%dms backup=%dms write=%dms total=%dms  (%dx%d, %s)",
                    int((t_edits - t0) * 1000),
                    int((t_backup - t_edits) * 1000),
                    int((t_write - t_backup) * 1000),
                    int((t_write - t0) * 1000),
                    w,
                    h,
                    original_path.name,
                )
            return original_path, backup_path

        except Exception as e:
            log.exception("Failed to save %s: %s", original_path, e)
            raise RuntimeError(f"Save failed: {e}") from e

    def save_image(
        self,
        write_developed_jpg: bool = False,
        developed_path: Optional[Path] = None,
        save_target_path: Optional[Path] = None,
    ) -> Optional[Tuple[Path, Path]]:
        """Saves the edited image, backing up the original.

        Convenience wrapper that calls ``snapshot_for_export()`` then
        ``save_from_snapshot()`` in sequence.  Kept for backward compatibility
        and direct (non-background) save paths.

        Args:
            write_developed_jpg: If True, also create a `-developed.jpg` sidecar file.
            developed_path: Optional explicit path for the developed JPG.
            save_target_path: Optional override for the output path.

        Returns:
            A tuple of (saved_path, backup_path) on success, otherwise None.

        Raises:
            RuntimeError: If preconditions are not met or saving fails.
        """
        snapshot = self.snapshot_for_export(
            write_developed_jpg=write_developed_jpg,
            developed_path=developed_path,
            save_target_path=save_target_path,
        )
        return self.save_from_snapshot(snapshot)

    def _save_u8_pil_image(
        self, img_u8: Image.Image, original_path: Path, log_prefix: str
    ) -> Optional[Tuple[Path, Path]]:
        """Save a prepared uint8 RGB image with backup, EXIF, and best-effort atomic replace."""
        try:
            original_stat = original_path.stat()
        except OSError:
            original_stat = None

        backup_path = create_backup_file(original_path)
        if backup_path is None:
            return None

        exif_bytes = self._get_sanitized_exif_bytes()
        save_kwargs = {"quality": 95}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes

        tmp_path = original_path.with_name(
            f"{original_path.stem}.__faststack_tmp__{uuid.uuid4().hex}{original_path.suffix}"
        )
        try:
            try:
                img_u8.save(tmp_path, **save_kwargs)
            except Exception:
                img_u8.save(tmp_path, quality=95)
            try:
                os.replace(tmp_path, original_path)
            except OSError as e:
                log.warning(
                    "Atomic replace failed (%s); falling back to direct save", e
                )
                try:
                    img_u8.save(original_path, **save_kwargs)
                except Exception:
                    img_u8.save(original_path, quality=95)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

        if original_stat is not None:
            self._restore_file_times(original_path, original_stat)

        log.debug("%s saved via uint8 fast path: %s", log_prefix, original_path.name)
        return original_path, backup_path

    def save_image_uint8_levels(
        self,
        save_target_path: Optional[Path] = None,
    ) -> Optional[Tuple[Path, Path]]:
        """Fast-path save using a uint8 LUT for levels-only edits.

        Instead of float_convert -> _apply_edits -> uint8, builds a 256-entry
        lookup table from the blacks/whites levels formula and applies it
        directly to the original uint8 PIL image data.

        Args:
            save_target_path: Optional override for the output path (variant save).

        Returns:
            (saved_path, backup_path) on success, None if the fast path is not
            applicable (TIFF, missing image, non-levels edits active).
        """
        if self.original_image is None or self.current_filepath is None:
            return None

        original_path = save_target_path if save_target_path else self.current_filepath

        # TIFF needs 16-bit pipeline
        if original_path.suffix.lower() in (".tif", ".tiff"):
            return None

        # Only applicable when blacks/whites are the sole active edits
        with self._lock:
            edits = self.current_edits.copy()

        for key, default in self._initial_edits().items():
            if key in ("blacks", "whites"):
                continue
            val = edits.get(key, default)
            if isinstance(default, float):
                try:
                    if abs(float(val) - float(default)) > 0.001:
                        return None
                except (TypeError, ValueError):
                    return None
            elif val != default:
                return None

        try:
            blacks = float(edits.get("blacks", 0.0))
            whites = float(edits.get("whites", 0.0))
        except (TypeError, ValueError):
            return None

        # Nothing to apply
        if abs(blacks) <= 0.001 and abs(whites) <= 0.001:
            return None

        bp = -blacks * 0.15
        wp = 1.0 - (whites * 0.15)
        if abs(wp - bp) < 0.0001:
            wp = bp + 0.0001

        # A LUT cannot dither (it is pointwise), so when the stretch is strong
        # enough to band 8-bit sources, decline the fast path and let the
        # float pipeline add TPDF dither during quantization.
        if self.export_dither and 1.0 / max(wp - bp, 1e-4) >= _EXPORT_DITHER_MIN_GAIN:
            return None

        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()

        # Build 768-entry LUT matching _apply_edits step 13 (cached by rounded key)
        cache_key = (round(blacks, 3), round(whites, 3), bool(self.levels_soft_knee))
        with self._lock:
            cached = self._cached_u8_lut
            if cached is not None and cached[0] == cache_key:
                lut_rgb = cached[1]
            else:
                lut_rgb = None

        if lut_rgb is None:
            lut = np.arange(256, dtype=np.float32) / 255.0
            lut = (lut - bp) / (wp - bp)
            if self.levels_soft_knee:
                lut = _apply_levels_soft_clip(lut)
            lut = np.clip(lut, 0.0, 1.0)
            lut_rgb = (lut * 255.0).astype(np.uint8).tolist() * 3  # 768 entries
            with self._lock:
                self._cached_u8_lut = (cache_key, lut_rgb)

        # Apply LUT via Pillow .point() — single C call, no large NumPy allocation
        rgb_img = self.original_image
        if rgb_img.mode != "RGB":
            rgb_img = rgb_img.convert("RGB")
        img_u8 = rgb_img.point(lut_rgb)

        if _debug:
            t_lut = time.perf_counter()

        save_result = self._save_u8_pil_image(
            img_u8, original_path, log_prefix="[SAVE_IMAGE_U8_LEVELS]"
        )
        if save_result is None:
            return None

        if _debug:
            t_write = time.perf_counter()
            w, h = img_u8.size
            log.debug(
                "[SAVE_IMAGE_U8] lut+apply=%dms save+write=%dms total=%dms  (%dx%d, %s)",
                int((t_lut - t0) * 1000),
                int((t_write - t_lut) * 1000),
                int((t_write - t0) * 1000),
                w,
                h,
                original_path.name,
            )
        return save_result

    def save_image_uint8_white_balance(
        self,
        save_target_path: Optional[Path] = None,
    ) -> Optional[Tuple[Path, Path]]:
        """Fast-path save using per-channel LUTs for white-balance-only edits."""
        if self.original_image is None or self.current_filepath is None:
            return None

        original_path = save_target_path if save_target_path else self.current_filepath
        if original_path.suffix.lower() in (".tif", ".tiff"):
            return None

        with self._lock:
            edits = self.current_edits.copy()

        for key, default in self._initial_edits().items():
            if key in ("white_balance_by", "white_balance_mg"):
                continue
            val = edits.get(key, default)
            if isinstance(default, float):
                try:
                    if abs(float(val) - float(default)) > 0.001:
                        return None
                except (TypeError, ValueError):
                    return None
            elif val != default:
                return None

        try:
            by = float(edits.get("white_balance_by", 0.0))
            mg = float(edits.get("white_balance_mg", 0.0))
        except (TypeError, ValueError):
            return None

        if abs(by) <= 0.001 and abs(mg) <= 0.001:
            return None

        _debug = log.isEnabledFor(logging.DEBUG)
        if _debug:
            t0 = time.perf_counter()

        cache_key = (round(by, 3), round(mg, 3))
        with self._lock:
            cached = self._cached_u8_wb_lut
            if cached is not None and cached[0] == cache_key:
                lut_rgb = cached[1]
            else:
                lut_rgb = None

        if lut_rgb is None:
            # Must match _apply_edits step 5 (luma-preserving gains).
            r_gain, g_gain, b_gain = _normalized_wb_gains(by * 0.5, mg * 0.5)
            r_gain = max(0.0, r_gain)
            g_gain = max(0.0, g_gain)
            b_gain = max(0.0, b_gain)

            lut = np.arange(256, dtype=np.float32) / 255.0
            lut_linear = _srgb_to_linear(lut)
            lut_r = np.clip(_linear_to_srgb(lut_linear * r_gain), 0.0, 1.0)
            lut_g = np.clip(_linear_to_srgb(lut_linear * g_gain), 0.0, 1.0)
            lut_b = np.clip(_linear_to_srgb(lut_linear * b_gain), 0.0, 1.0)
            lut_rgb = (
                np.rint(lut_r * 255.0).astype(np.uint8).tolist()
                + np.rint(lut_g * 255.0).astype(np.uint8).tolist()
                + np.rint(lut_b * 255.0).astype(np.uint8).tolist()
            )
            with self._lock:
                self._cached_u8_wb_lut = (cache_key, lut_rgb)

        rgb_img = self.original_image
        if rgb_img.mode != "RGB":
            rgb_img = rgb_img.convert("RGB")
        img_u8 = rgb_img.point(lut_rgb)

        if _debug:
            t_lut = time.perf_counter()

        save_result = self._save_u8_pil_image(
            img_u8, original_path, log_prefix="[SAVE_IMAGE_U8_WB]"
        )
        if save_result is None:
            return None

        if _debug:
            t_write = time.perf_counter()
            w, h = img_u8.size
            log.debug(
                "[SAVE_IMAGE_U8_WB] lut+apply=%dms write=%dms total=%dms  (%dx%d, %s)",
                int((t_lut - t0) * 1000),
                int((t_write - t_lut) * 1000),
                int((t_write - t0) * 1000),
                w,
                h,
                original_path.name,
            )
        return save_result

    def _restore_file_times(self, path: Path, original_stat: os.stat_result) -> None:
        """Best-effort restoration of access/modify timestamps after saving."""
        try:
            os.utime(path, (original_stat.st_atime, original_stat.st_mtime))
        except OSError as e:
            log.warning("Unable to restore timestamps for %s: %s", path, e)

    def rotate_image_cw(self):
        """Decreases the rotation edit parameter by 90° modulo 360 (clockwise)."""
        with self._lock:
            current = int(self.current_edits.get("rotation", 0)) % 360
            new_rotation = (current - 90) % 360
            self.current_edits["crop_box"] = self._rotate_crop_box_for_rotation_change(
                self.current_edits.get("crop_box"),
                current,
                new_rotation,
            )
            self.current_edits["rotation"] = new_rotation
            self._edits_rev += 1

    def rotate_image_ccw(self):
        """Increases the rotation edit parameter by 90° modulo 360 (counter-clockwise)."""
        with self._lock:
            current = int(self.current_edits.get("rotation", 0)) % 360
            new_rotation = (current + 90) % 360
            self.current_edits["crop_box"] = self._rotate_crop_box_for_rotation_change(
                self.current_edits.get("crop_box"),
                current,
                new_rotation,
            )
            self.current_edits["rotation"] = new_rotation
            self._edits_rev += 1


# Dictionary of ratios for QML dropdown
ASPECT_RATIOS = [
    {"name": name, "ratio": ratio} for name, ratio in INSTAGRAM_RATIOS.items()
]
