import logging
import os
import shutil
import re
import math
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import numpy as np
from PIL import Image, ImageFilter, ImageOps, ExifTags


from faststack.models import DecodedImage
from faststack.imaging.math_utils import (
    _srgb_to_linear,
    _linear_to_srgb,
    _smoothstep01,
    _apply_headroom_shoulder,
    _analyze_highlight_state,
    _lerp,
    _highlight_recover_linear,
    _highlight_boost_linear,
)
from faststack.imaging.orientation import get_exif_orientation, apply_orientation_to_np

try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None

from faststack.imaging.optional_deps import cv2

import threading


log = logging.getLogger(__name__)

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
        log.exception(f"Failed to create backup: {e}")
        return None


# ----------------------------
# sRGB ↔ Linear Conversion Helpers
# ----------------------------


# Constants for Highlight Recovery

# Highlight Compression Curve
HEADROOM_COMPRESSION_STEEPNESS = 2.0

# Adaptive Parameters (tuned by image content analysis)
# Pivot: Brightness threshold where recovery starts
ADAPTIVE_PIVOT_MIN = 0.45
ADAPTIVE_PIVOT_MAX = 0.65

# K Factor: Steepness of the compression shoulder
ADAPTIVE_K_BASE = 8.0
ADAPTIVE_K_SCALING = 6.0
ADAPTIVE_K_HEADROOM_BASE = 6.0
ADAPTIVE_K_HEADROOM_SCALING = 8.0

# Chroma Rolloff: Desaturation in extreme highlights
ADAPTIVE_ROLLOFF_MIN = 0.10
ADAPTIVE_ROLLOFF_MAX = 0.30

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
            h, w, c = arr.shape
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
            log.warning(f"Fallback blur failed: {e}")
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

    cw = round(abs(wr))
    ch = round(abs(hr))
    cw = max(1, min(w, cw))
    ch = max(1, min(h, ch))
    return cw, ch


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
    left = round(cx - crop_w / 2.0)
    top = round(cy - crop_h / 2.0)
    right = left + crop_w
    bottom = top + crop_h

    # Small inset to remove any bicubic edge contamination
    if inset > 0 and (right - left) > 2 * inset and (bottom - top) > 2 * inset:
        left += inset
        top += inset
        right -= inset
        bottom -= inset

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

    def clear(self):
        """Clear all editor state so the next edit starts from a clean slate."""
        with self._lock:
            self.original_image = None
            self.current_filepath = None
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
            "crop_box": None,  # (left, top, right, bottom) normalized to 0-1000
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
        }

    def load_image(
        self,
        filepath: str,
        cached_preview: Optional[DecodedImage] = None,
        source_exif: Optional[bytes] = None,
    ):
        """Load a new image for editing.

        Args:
            filepath: Path to the image file
            cached_preview: Optional byte-buffer for faster initial display
            source_exif: Optional EXIF bytes from original source (preserve camera metadata)
        """
        if not filepath or not Path(filepath).exists():
            with self._lock:
                self.original_image = None
                self.float_image = None
                self.float_preview = None
                self.current_filepath = None
                self._source_exif_bytes = None
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1
            log.error(f"Image file not found: {filepath}")
            return False

        load_filepath = Path(filepath)
        try:
            new_mtime = load_filepath.stat().st_mtime
        except OSError:
            new_mtime = 0.0

        with self._lock:
            # Clear previous cached EXIF and set new one if provided
            self.current_mtime = new_mtime
            self._source_exif_bytes = source_exif

        try:
            # We must load and close the original file handle immediately
            with Image.open(load_filepath) as im:
                # Keep original PIL for EXIF/Format preservation
                loaded_original = im.copy()

            # --- Convert to Float32 ---
            # Use OpenCV for reliable 16-bit loading as Pillow often downsamples to 8-bit RGB
            if cv2 is None:
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

            loaded_bit_depth = 8
            loaded_float_image = None

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
                    rgb = loaded_original.convert("RGB")
                    arr = np.array(rgb).astype(np.float32) / 255.0
                    log.warning(
                        f"OpenCV loaded unexpected channel count, falling back to Pillow: {load_filepath}"
                    )

                loaded_float_image = arr
                log.info(f"Loaded 16-bit image via OpenCV: {load_filepath}")
            else:
                # Fallback to Pillow logic for 8-bit or if OpenCV failed/returned 8-bit
                loaded_bit_depth = 8
                rgb = loaded_original.convert("RGB")
                loaded_float_image = np.array(rgb).astype(np.float32) / 255.0
                log.info(f"Loaded 8-bit image via Pillow: {load_filepath}")

            # --- Apply EXIF Orientation ---
            # Read orientation from the loaded original image's EXIF
            orientation = get_exif_orientation(
                load_filepath, exif=loaded_original.getexif()
            )
            if orientation > 1:
                log.info(f"Applying EXIF orientation {orientation} to {load_filepath}")
                # 1. Correct the Pillow original (used for metadata/export fallback)
                loaded_original = ImageOps.exif_transpose(loaded_original)

                # 2. Correct the float master buffer
                if loaded_float_image is not None:
                    loaded_float_image = apply_orientation_to_np(
                        loaded_float_image, orientation
                    )

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
                # Downscale from float_image (which now has orientation applied)
                thumb = loaded_original.copy()
                thumb.thumbnail((1920, 1080))
                thumb_rgb = thumb.convert("RGB")
                loaded_float_preview = np.array(thumb_rgb).astype(np.float32) / 255.0

                # Thumbnail is derived from loaded_original AFTER exif_transpose,
                # so orientation is already correct.

            # Assign all state atomically under lock to prevent race with preview worker
            with self._lock:
                self.current_filepath = load_filepath
                self.original_image = loaded_original
                self.float_image = loaded_float_image
                self.float_preview = loaded_float_preview
                self.bit_depth = loaded_bit_depth
                # Reset edits
                self.current_edits = self._initial_edits()
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1

            return True
        except Exception as e:
            # We catch specific errors during the process if needed, but for general failure
            # we should cleanup and then RETURN FALSE so the caller (UI) knows what happened.
            # This matches the legacy contract (exceptions for programmer errors, False for runtime/IO failure)
            log.warning(f"Error loading image for editing: {e}")
            with self._lock:
                self.original_image = None
                self.float_image = None
                self.float_preview = None
                self.current_filepath = None
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1
            return False

    def _rotate_float_image(
        self, img_arr: np.ndarray, angle_deg: float, expand: bool = False
    ) -> np.ndarray:
        """Rotates a float32 RGB image using PIL 'F' mode per channel to preserve precision."""
        if abs(angle_deg) < 0.01:
            return img_arr

        h, w, c = img_arr.shape
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

        # Merge back
        # Assume all channels rotated to same size
        nw, nh = channels[0].size
        new_arr = np.stack([np.array(ch) for ch in channels], axis=-1)
        return new_arr

    def _apply_edits(
        self,
        img_arr: np.ndarray,
        edits: Optional[Dict[str, Any]] = None,
        *,
        for_export: bool = False,
    ) -> np.ndarray:
        """Applies all current edits to the provided float32 numpy array.
        Returns float32 array (H, W, 3).
        """
        if edits is None:
            edits = self.current_edits

        is_export = for_export
        # Alias
        arr = img_arr

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

        if apply_rotation:
            # Use the float rotation helper
            # Note: rotate_autocrop_rgb logic was complex.
            # If we have crop box, we manually crop later.
            # If no crop box, we might auto-crop (remove wedges).
            # For floating point, standard 'expand' rotation + manual crop is best.

            # Calculate auto-crop parameters BEFORE rotation if needed
            crop_rect = None
            if not has_crop_box:
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
                # Center crop on the new expanded image
                rh, rw = arr.shape[:2]
                cx, cy = rw / 2.0, rh / 2.0

                left = round(cx - cw / 2.0)
                top = round(cy - ch / 2.0)
                right = left + cw
                bottom = top + ch

                # Apply inset (2px) to match legacy behavior and avoid edge artifacts
                inset = 2
                if (right - left) > 2 * inset and (bottom - top) > 2 * inset:
                    left += inset
                    top += inset
                    right -= inset
                    bottom -= inset

                # Clamp
                left = max(0, min(rw - 1, left))
                top = max(0, min(rh - 1, top))
                right = max(left + 1, min(rw, right))
                bottom = max(top + 1, min(rh, bottom))

                arr = arr[top:bottom, left:right, :]

        # 3. Crop
        if has_crop_box:
            crop_box = edits.get("crop_box", 0.0)
            if len(crop_box) == 4:
                # 0-1000 relative to current size
                h, w = arr.shape[:2]
                left = int(crop_box[0] * w / 1000)
                t = int(crop_box[1] * h / 1000)
                r = int(crop_box[2] * w / 1000)
                b = int(crop_box[3] * h / 1000)

                left = max(0, left)
                t = max(0, t)
                r = min(w, r)
                b = min(h, b)

                if r > left and b > t:
                    arr = arr[t:b, left:r, :]

        # 4. Conversion to Linear Light
        # Cache sRGB u8 BEFORE linearization for accurate JPEG clipping detection.
        # JPEG clipping happens in sRGB after gamma/quantization, so we need the
        # original sRGB values to detect flat-top clipping correctly.
        # MOVED to after WB/Exposure so indicators reflect current pipeline state.

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
                srgb_u8_stride = (np.clip(arr_stride, 0.0, 1.0) * 255).astype(np.uint8)

        arr = _srgb_to_linear(arr)

        # 5. White Balance (Multipliers in Linear Space)
        by = edits.get("white_balance_by", 0.0) * 0.5
        mg = edits.get("white_balance_mg", 0.0) * 0.5
        if abs(by) > 0.001 or abs(mg) > 0.001:
            r_gain = 1.0 + by
            b_gain = 1.0 - by
            g_gain = 1.0 - mg
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
                if (
                    self._cached_highlight_analysis
                    and self._cached_highlight_analysis["hash"] == upstream_hash
                ):
                    cached_analysis = self._cached_highlight_analysis["state"]

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
                    self._cached_highlight_analysis = {
                        "hash": upstream_hash,
                        "state": analysis_state,
                    }

        if not for_export:
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
            )

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
                cached = self._cached_detail_bands
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

        # 11. Global Headroom Shoulder (safety net for values > 1.0)
        # This ONLY affects values above 1.0, compressing headroom smoothly.
        # It does NOT interfere with normal highlight slider work below 1.0.
        # Applied here in linear space before gamma conversion.
        # Use small max_overshoot (0.05) to keep values very close to 1.0
        arr = _apply_headroom_shoulder(arr, max_overshoot=0.05)

        # --- Conversion back to sRGB ---
        arr = _linear_to_srgb(arr)

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
            gray = arr.dot([0.299, 0.587, 0.114])
            gray = np.expand_dims(gray, axis=2)
            arr = gray + (arr - gray) * factor

        # 12. Vibrance (Smart Saturation)
        vibrance = edits.get("vibrance", 0.0)
        if abs(vibrance) > 0.001:
            cmax = arr.max(axis=2)
            cmin = arr.min(axis=2)
            delta = cmax - cmin
            sat = np.zeros_like(cmax)
            mask = cmax > 0.0001
            sat[mask] = delta[mask] / cmax[mask]

            sat_mask = np.clip(1.0 - sat, 0.0, 1.0)
            factor = 1.0 + vibrance * sat_mask

            gray = arr.dot([0.299, 0.587, 0.114])
            gray = np.expand_dims(gray, axis=2)
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

        return arr  # Potentially > 1.0 if not clipped elsewhere

    def auto_levels(
        self, threshold_percent: float = 0.1
    ) -> Tuple[float, float, float, float]:
        """
        Returns (blacks, whites, p_low, p_high).
        p_low/p_high are computed conservatively from RGB to avoid introducing new channel clipping.
        """
        threshold_percent = max(0.0, min(10.0, threshold_percent))
        # Use preview for speed
        img_arr = (
            self.float_preview if self.float_preview is not None else self.float_image
        )

        if img_arr is None:
            # Fallback for tests or cases where float data isn't initialized yet
            if hasattr(self, "_preview_image") and self._preview_image is not None:
                img_arr = (
                    np.array(self._preview_image.convert("RGB")).astype(np.float32)
                    / 255.0
                )
            elif self.original_image is not None:
                img_arr = (
                    np.array(self.original_image.convert("RGB")).astype(np.float32)
                    / 255.0
                )
            else:
                return 0.0, 0.0, 0.0, 255.0

        # Convert to unit8 (0-255) for histogram analysis
        # This preserves the logic of the original algorithm which was tuned for 0-255 bins
        rgb = (np.clip(img_arr, 0.0, 1.0) * 255).astype(np.uint8)
        # rgb shape: (H, W, 3)

        low_p = threshold_percent
        high_p = 100.0 - threshold_percent

        # --- Detect pre-clipping (per-channel) ---
        # If *any* channel already has clipped pixels, do not push that end further.
        # eps_pct strategy: "Practical" - ignore tiny hot pixels (0.01%) but pin
        # if there is any meaningful pre-clipping, even if below the full threshold.
        eps_pct = min(threshold_percent, 0.01)

        total = rgb.shape[0] * rgb.shape[1]
        clipped_low_pct = []
        clipped_high_pct = []
        p_lows = []
        p_highs = []

        for c in range(3):
            chan = rgb[:, :, c]
            # Treat near-white/near-black as clipped (JPEG artifacts often land on 254/1)
            clipped_low_pct.append(
                100.0 * float(np.count_nonzero(chan <= 1)) / float(total)
            )
            clipped_high_pct.append(
                100.0 * float(np.count_nonzero(chan >= 254)) / float(total)
            )

            # Use discrete selection methods to avoid interpolation surprises on uint8.
            # Fallback for older numpy (<1.22) that doesn't support method=.
            try:
                p_lows.append(float(np.percentile(chan, low_p, method="lower")))
                p_highs.append(float(np.percentile(chan, high_p, method="higher")))
            except TypeError:
                p_lows.append(float(np.percentile(chan, low_p, interpolation="lower")))
                p_highs.append(
                    float(np.percentile(chan, high_p, interpolation="higher"))
                )

        # Conservative anchors to avoid new channel clipping
        p_low = min(p_lows)
        p_high = max(p_highs)

        # NOTE: applying this stretch uniformly to RGB can clip individual channels
        # more than luminance predicts. That's usually acceptable, but if we
        # ever see weird color clipping, that might be why.

        # Pin ends if pre-clipping exists (prevents making it worse)
        if max(clipped_high_pct) > eps_pct:
            p_high = 255.0
        if max(clipped_low_pct) > eps_pct:
            p_low = 0.0

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
            self.current_edits["blacks"] = blacks
            self.current_edits["whites"] = whites
            self._edits_rev += 1

        return blacks, whites, float(p_low), float(p_high)

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
        self, allow_compute: bool = True
    ) -> Optional[DecodedImage]:
        """Return cached preview if available, otherwise compute and cache.

        Args:
            allow_compute: If False, returns None immediately if cache is stale (avoids blocking).
        """
        with self._lock:
            # Check cache validity
            if self._cached_preview is not None and self._cached_rev == self._edits_rev:
                return self._cached_preview

            if not allow_compute:
                return None

            # Prepare for computation - snapshot data under lock
            base = self.float_preview.copy() if self.float_preview is not None else None
            edits = dict(self.current_edits)
            rev = self._edits_rev

        if base is None:
            return None

        # Heavy computation outside lock using snapshot
        # base is float32 (H, W, 3) 0-1
        arr = self._apply_edits(base, edits=edits, for_export=False)

        # Convert to 8-bit for display
        # Global shoulder is now applied in linear space within _apply_edits()
        # Just clip to 0-1 as safety clamp
        arr = np.clip(arr, 0.0, 1.0)
        # Map to 0-255
        arr_u8 = (arr * 255).astype(np.uint8)

        if QImage is None:
            raise ImportError(
                "PySide6.QtGui.QImage is required for get_preview_data_cached"
            )

        # Create QImage from buffer
        img_buffer = arr_u8.tobytes()
        decoded = DecodedImage(
            buffer=memoryview(img_buffer),
            width=arr_u8.shape[1],
            height=arr_u8.shape[0],
            bytes_per_line=arr_u8.shape[1] * 3,
            format=QImage.Format.Format_RGB888,
        )

        with self._lock:
            # Only cache if revision hasn't changed during computation
            if self._edits_rev == rev:
                self._cached_preview = decoded
                self._cached_rev = rev

        return decoded

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
                    # Round to nearest 90 degrees
                    val_deg = float(value)
                    rounded_deg = round(val_deg / 90.0) * 90
                    final_val = int(rounded_deg) % 360

                    if abs(val_deg - rounded_deg) > 1.0:
                        log.warning(
                            f"'rotation' received {value}. Rounding to {final_val}. Use 'straighten_angle' for free rotation."
                        )

                    self.current_edits[key] = final_val
                    self._edits_rev += 1
                    return True
                except (ValueError, TypeError) as e:
                    log.warning(f"Invalid value for rotation {value!r}: {e}")
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
        analysis_state: Optional[Dict[str, float]] = None,
        edits: Optional[Dict[str, Any]] = None,
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
            analysis_state: Optional pre-computed analysis state to avoid re-work.

        Returns:
            Adjusted float32 RGB array (linear)
        """
        arr = linear

        # Analyze highlight state if needed
        # If caller passed analysis_state, usage that.
        state = analysis_state
        if state is None:
            # Re-compute locally if not provided
            # We assume srgb_u8_stride is ALREADY STRIDED if passed (based on the name change)
            arr_stride = arr[::4, ::4, :]
            # If srgb_u8_stride was passed, use it directly (it's already small).
            # If it wasn't passed, we can't easily recreate the source state here without the original source buffer.
            # But the caller (_apply_edits) usually provides it.
            state = _analyze_highlight_state(arr_stride, srgb_u8=srgb_u8_stride)

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
                        cached = self._cached_max_brightness_state
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
                            self._cached_max_brightness_state = {
                                "hash": current_hash,
                                "value": max_brightness,
                            }

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
                        # Lots of recoverable near-white, not much flat clipping
                        pivot = max(0.60, pivot - 0.12 * near_white_pct)
                    if clipped_pct > 0.02:
                        # Increase chroma rolloff for flat-clipped JPEGs
                        chroma_rolloff = max(chroma_rolloff, 0.25)

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
            self.current_edits["crop_box"] = crop_box
            self._edits_rev += 1

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
            from PIL import Image, ExifTags

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
                    f"Failed to serialize sanitized EXIF: {e}. Dropping EXIF to prevent rotation issues."
                )
                return None
        except Exception as e:
            log.warning(f"Failed to sanitize EXIF orientation: {e}. Dropping EXIF.")
            return None

    def save_image(
        self, write_developed_jpg: bool = False, developed_path: Optional[Path] = None
    ) -> Optional[Tuple[Path, Path]]:
        """Saves the edited image, backing up the original.

        Args:
            write_developed_jpg: If True, also create a `-developed.jpg` sidecar file.
                                 This should be True only when editing RAW files.
            developed_path: Optional explicit path for the developed JPG.
                            If not provided, it's derived from current_filepath.

        Returns:
            A tuple of (saved_path, backup_path) on success, otherwise None.
        """
        if self.float_image is None or self.current_filepath is None:
            return None

        # 1. Apply Edits to Full Resolution
        final_float = self._apply_edits(
            self.float_image.copy(), for_export=True
        )  # (H,W,3) float32

        original_path = self.current_filepath
        try:
            original_stat = original_path.stat()
        except OSError as e:
            log.warning(f"Unable to read timestamps for {original_path}: {e}")
            original_stat = None

        # 2. Backup
        backup_path = create_backup_file(original_path)
        if backup_path is None:
            return None

        try:
            # 3. Save Main File
            is_tiff = original_path.suffix.lower() in [".tif", ".tiff"]

            if is_tiff:
                # Save as 16-bit TIFF using custom writer
                self._write_tiff_16bit(original_path, final_float)
            else:
                # Check for geometric transforms
                rotation = self.current_edits.get("rotation", 0)
                straighten_angle = float(
                    self.current_edits.get("straighten_angle", 0.0)
                )
                transforms_applied = (rotation != 0) or (abs(straighten_angle) > 0.001)

                # Determine EXIF bytes to write
                exif_bytes = None
                if self.original_image:
                    # We NO LONGER check transforms_applied here because we ALWAYS
                    # bake orientation into the pixel buffer on load for consistency.
                    # Thus, we ALWAYS sanitize the Orientation tag to 1 to prevent "double rotation".
                    exif_bytes = self._get_sanitized_exif_bytes()

                # Save as standard format (Likely JPG) using Pillow
                # Convert to uint8
                # Legacy soft shoulder moved to linear space (_apply_headroom_shoulder)
                # converted via _linear_to_srgb in _apply_edits, so final_float is already sRGB.
                # Just clip to valid range.
                arr_u8 = (np.clip(final_float, 0.0, 1.0) * 255).astype(np.uint8)
                img_u8 = Image.fromarray(arr_u8, mode="RGB")

                save_kwargs = {"quality": 95}
                if exif_bytes:
                    save_kwargs["exif"] = exif_bytes

                try:
                    img_u8.save(original_path, **save_kwargs)
                except Exception:
                    # Fallback without EXIF
                    img_u8.save(original_path)

            if original_stat is not None:
                self._restore_file_times(original_path, original_stat)

            # 4. Save Sidecar JPG (-developed.jpg) - only when explicitly requested
            if write_developed_jpg:
                if developed_path is None:
                    stem = original_path.stem
                    if stem.lower().endswith("-working"):
                        stem = stem[:-8]
                    developed_path = original_path.with_name(f"{stem}-developed.jpg")

                # Check for geometric transforms (re-check not strictly needed but for clarity)
                rotation = self.current_edits.get("rotation", 0)
                straighten_angle = float(
                    self.current_edits.get("straighten_angle", 0.0)
                )
                transforms_applied = (rotation != 0) or (abs(straighten_angle) > 0.001)

                # Determine EXIF for sidecar - prefer source EXIF (from paired JPEG)
                exif_bytes = None
                if transforms_applied:
                    # Use sanitized EXIF (orientation reset to 1)
                    exif_bytes = self._get_sanitized_exif_bytes()
                elif self._source_exif_bytes:
                    # Use cached source EXIF from paired JPEG
                    # Must sanitize orientation because we baked it on load!
                    exif_bytes = sanitize_exif_orientation(self._source_exif_bytes)
                elif self.original_image:
                    # Fallback to current image's EXIF (may be empty for TIFFs)
                    # Must sanitize orientation because we baked it on load!
                    exif_bytes = sanitize_exif_orientation(
                        self.original_image.info.get("exif")
                    )

                # Use the same uint8 data
                # Legacy soft shoulder moved to linear space
                arr_u8 = (np.clip(final_float, 0.0, 1.0) * 255).astype(np.uint8)
                img_u8 = Image.fromarray(arr_u8)

                dev_kwargs = {"quality": 90}
                if exif_bytes:
                    dev_kwargs["exif"] = exif_bytes

                try:
                    img_u8.save(developed_path, **dev_kwargs)
                except Exception:
                    img_u8.save(developed_path)

            return original_path, backup_path

        except Exception as e:
            log.exception(f"Failed to save {self.current_filepath}: {e}")
            raise RuntimeError(f"Save failed: {str(e)}") from e

    def _restore_file_times(self, path: Path, original_stat: os.stat_result) -> None:
        """Best-effort restoration of access/modify timestamps after saving."""
        try:
            os.utime(path, (original_stat.st_atime, original_stat.st_mtime))
        except OSError as e:
            log.warning(f"Unable to restore timestamps for {path}: {e}")

    def rotate_image_cw(self):
        """Decreases the rotation edit parameter by 90° modulo 360 (clockwise)."""
        with self._lock:
            current = self.current_edits.get("rotation", 0)
            self.current_edits["rotation"] = (current - 90) % 360
            self._edits_rev += 1

    def rotate_image_ccw(self):
        """Increases the rotation edit parameter by 90° modulo 360 (counter-clockwise)."""
        with self._lock:
            current = self.current_edits.get("rotation", 0)
            self.current_edits["rotation"] = (current + 90) % 360
            self._edits_rev += 1


# Dictionary of ratios for QML dropdown
ASPECT_RATIOS = [
    {"name": name, "ratio": ratio} for name, ratio in INSTAGRAM_RATIOS.items()
]
