import logging
import os
import shutil
import glob
import re
import math
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from io import BytesIO

from faststack.models import DecodedImage
try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None

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
    base_stem = re.sub(r'-backup(-?\d+)?$', '', stem)
    
    # Try filename-backup.jpg first
    backup_path = original_path.parent / f"{base_stem}-backup{original_path.suffix}"
    
    # If that exists, try filename-backup2.jpg, filename-backup3.jpg, etc.
    i = 2
    while backup_path.exists():
        backup_path = original_path.parent / f"{base_stem}-backup{i}{original_path.suffix}"
        i += 1
    
    try:
        # Perform the backup
        shutil.copy2(original_path, backup_path)
        return backup_path
    except OSError as e:
        log.exception(f"Failed to create backup: {e}")
        return None

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


def rotate_autocrop_rgb(img: Image.Image, angle_deg: float, inset: int = 2) -> Image.Image:
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
        # A smaller version of the original image for fast previews
        self._preview_image: Optional[Image.Image] = None
        # Stores the currently applied edits (used for preview)
        self.current_edits: Dict[str, Any] = self._initial_edits()
        self.current_filepath: Optional[Path] = None
        
        # Caching support for smooth updates
        self._lock = threading.RLock()
        self._edits_rev = 0
        self._cached_rev = -1
        self._cached_preview = None
        
    def clear(self):
        """Clear all editor state so the next edit starts from a clean slate."""
        with self._lock:
            self.original_image = None
            self.current_filepath = None
            self._preview_image = None
            self._edits_rev += 1
            self._cached_preview = None
            self._cached_rev = -1
        # Optionally also reset edits if that matches your mental model:
        # self.current_edits = self._initial_edits()

    def reset_edits(self):
        """Reset edits to initial values and bump revision."""
        with self._lock:
            self.current_edits = self._initial_edits()
            self._edits_rev += 1

    def _initial_edits(self) -> Dict[str, Any]:
        return {
            'brightness': 0.0,
            'contrast': 0.0,
            'saturation': 0.0,
            'white_balance_by': 0.0, # Blue/Yellow (Cool/Warm)
            'white_balance_mg': 0.0, # Magenta/Green (Tint)
            'crop_box': None, # (left, top, right, bottom) normalized to 0-1000
            'sharpness': 0.0,
            'rotation': 0,
            'exposure': 0.0,
            'highlights': 0.0,
            'shadows': 0.0,
            'vibrance': 0.0,
            'vignette': 0.0,
            'blacks': 0.0,
            'whites': 0.0,
            'clarity': 0.0,
            'texture': 0.0,
            'straighten_angle': 0.0,
        }

    def load_image(self, filepath: str, cached_preview: Optional[DecodedImage] = None):
        """Load a new image for editing."""
        if not filepath or not Path(filepath).exists():
            with self._lock:
                self.original_image = None
                self.current_filepath = None
                self._preview_image = None
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1
            return False
        
        self.current_filepath = Path(filepath)
        
        try:
            # We must load and close the original file handle immediately
            with Image.open(self.current_filepath) as im:
                original = im.convert("RGB")

            # Use the cached, display-sized preview if available
            if cached_preview:
                preview = Image.frombytes(
                    "RGB",
                    (cached_preview.width, cached_preview.height),
                    bytes(cached_preview.buffer)
                )
            else:
                # Fallback: create a thumbnail if no preview is provided
                preview = original.copy()
                preview.thumbnail((1920, 1080)) # Reasonable fallback size

            with self._lock:
                self.original_image = original
                self._preview_image = preview
                # Reset edits
                self.current_edits = self._initial_edits()
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1

            return True
        except Exception as e:
            log.exception(f"Error loading image for editing: {e}")
            with self._lock:
                self.original_image = None
                self._preview_image = None
                self._edits_rev += 1
                self._cached_preview = None
                self._cached_rev = -1
            return False


    def _apply_edits(self, img: Image.Image, edits: Optional[Dict[str, Any]] = None, *, for_export: bool = False) -> Image.Image:
        """Applies all current edits to the provided PIL Image."""
        
        if edits is None:
            edits = self.current_edits

        # 1. Rotation (90 degree steps)
        # (This remains first as it changes the coordinate system basis)
        rotation = edits.get('rotation', 0)
        if rotation == 90:
            img = img.transpose(Image.Transpose.ROTATE_270)
        elif rotation == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif rotation == 270:
            img = img.transpose(Image.Transpose.ROTATE_90)

        # ---------------------------------------------------------
        # CHANGE: Apply Free Rotation (Straighten) BEFORE Cropping
        # ---------------------------------------------------------
        straighten_angle = float(edits.get('straighten_angle', 0.0))
        has_crop_box = 'crop_box' in edits and edits['crop_box']

        # Only apply rotation if it's significant AND we are exporting.
        # During preview (for_export=False), QML handles the visual rotation.
        if for_export and abs(straighten_angle) > 0.001:
            if has_crop_box:
                # Scenario A: Manual Crop. 
                # Just rotate the image (expanding canvas). The subsequent 
                # manual crop will trim off the black wedges.
                img = img.convert("RGB").rotate(
                    -straighten_angle,
                    resample=Image.Resampling.BICUBIC,
                    expand=True,
                    fillcolor=(0, 0, 0) # These will be cropped out shortly
                )
            else:
                # Scenario B: Straighten Only (No manual crop).
                # Use your existing helper to Rotate + Auto-Shrink to remove wedges.
                img = rotate_autocrop_rgb(img, straighten_angle)

        # ---------------------------------------------------------
        # CHANGE: Apply Cropping LAST
        # ---------------------------------------------------------
        if has_crop_box:
            crop_box = edits['crop_box']
            if len(crop_box) == 4:
                # Normalize coordinates (0-1000) to pixel coordinates
                # Note: We calculate this based on the *current* img size, 
                # which might be larger now due to the rotation above.
                w, h = img.size
                left = int(crop_box[0] * w / 1000)
                t = int(crop_box[1] * h / 1000)
                r = int(crop_box[2] * w / 1000)
                b = int(crop_box[3] * h / 1000)
                
                # Basic boundary checks
                left = max(0, left)
                t = max(0, t)
                r = min(w, r)
                b = min(h, b)
                
                if r > left and b > t:
                    img = img.crop((left, t, r, b))

        # 3. Exposure (gamma-based)
        exposure = edits['exposure']
        if abs(exposure) > 0.001:
            gamma = 1.0 / (1.0 + exposure) if exposure >= 0 else 1.0 - exposure
            arr = np.array(img, dtype=np.float32) / 255.0
            arr = np.power(arr, gamma)
            arr = (arr * 255).clip(0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

        blacks = edits['blacks']
        whites = edits['whites']
        if abs(blacks) > 0.001 or abs(whites) > 0.001:
            arr = np.array(img, dtype=np.float32)
            black_point = -blacks * 40
            white_point = 255 - whites * 40
            # Prevent division by zero
            if abs(white_point - black_point) < 0.001:
                white_point = black_point + 0.001
            arr = (arr - black_point) * (255.0 / (white_point - black_point))
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 5. Highlights/Shadows
        highlights = edits['highlights']
        shadows = edits['shadows']

        if abs(highlights) > 0.001 or abs(shadows) > 0.001:
            arr = np.array(img, dtype=np.float32)
            if abs(shadows) > 0.001:
                shadow_mask = 1.0 - np.clip(arr / 128.0, 0, 1)
                arr += shadows * 60 * shadow_mask

            if highlights < -0.001: # Negative highlights (recovery)
                mask = np.clip((arr - 128) / 127.0, 0, 1) # targets bright pixels
                # highlights is negative here, so 1.0 + (negative * positive) = something less than 1.0
                factor = 1.0 + (highlights * 0.75 * mask)
                arr = arr * factor
            elif highlights > 0.001: # Positive highlights (keep existing)
                highlight_mask = np.clip((arr - 128) / 127.0, 0, 1)
                arr += highlights * 60 * highlight_mask
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 6. Brightness
        bright_factor = 1.0 + edits['brightness']
        if abs(bright_factor - 1.0) > 0.001:
            img = ImageEnhance.Brightness(img).enhance(bright_factor)

        # 7. Contrast
        contrast_factor = 1.0 + edits['contrast']
        if abs(contrast_factor - 1.0) > 0.001:
            img = ImageEnhance.Contrast(img).enhance(contrast_factor)

        # 8. Clarity
        clarity = edits['clarity']
        if abs(clarity) > 0.001:
            arr = np.array(img, dtype=np.float32)
            luminance = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
            lum_img = Image.fromarray(luminance.astype(np.uint8))
            blurred = lum_img.filter(ImageFilter.GaussianBlur(radius=20))
            blurred_arr = np.array(blurred, dtype=np.float32)
            midtone_mask = 1.0 - np.abs(luminance - 128) / 128.0
            local_contrast = (luminance - blurred_arr) * clarity * midtone_mask
            for c in range(3):
                arr[:,:,c] += local_contrast
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 9. Saturation
        saturation_factor = 1.0 + edits['saturation']
        if abs(saturation_factor - 1.0) > 0.001:
            img = ImageEnhance.Color(img).enhance(saturation_factor)

        # 10. Vibrance
        vibrance = edits['vibrance']
        if abs(vibrance) > 0.001:
            arr = np.array(img, dtype=np.float32)
            sat = (arr.max(axis=2) - arr.min(axis=2)) / 255.0
            boost = (1.0 - sat) * vibrance
            gray = arr.mean(axis=2, keepdims=True)
            arr = gray + (arr - gray) * (1.0 + boost[:, :, np.newaxis])
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 11. White Balance
        by_val = edits['white_balance_by'] * 0.5
        mg_val = edits['white_balance_mg'] * 0.5
        if abs(by_val) > 0.001 or abs(mg_val) > 0.001:
            arr = np.array(img, dtype=np.float32)
            # Multiplicative White Balance (Gain-based)
            # This preserves black levels (0 * gain = 0) while adjusting the color balance of brighter pixels.

            # Temperature (Blue-Yellow):
            # Positive = Warm (Yellow/Red), Negative = Cool (Blue)
            r_gain = 1.0 + by_val
            b_gain = 1.0 - by_val

            # Tint (Magenta-Green):
            # Positive = Magenta (Red+Blue boost or Green cut), Negative = Green (Green boost)
            # Standard approach: Adjust Green channel opposite to the tint value.
            g_gain = 1.0 - mg_val

            # Apply gains
            arr[:, :, 0] = arr[:, :, 0] * r_gain
            arr[:, :, 1] = arr[:, :, 1] * g_gain
            arr[:, :, 2] = arr[:, :, 2] * b_gain

            np.clip(arr, 0, 255, out=arr)
            img = Image.fromarray(arr.astype(np.uint8))

        # 12. Sharpness
        sharp_factor = 1.0 + edits['sharpness']
        if abs(sharp_factor - 1.0) > 0.001:
            img = ImageEnhance.Sharpness(img).enhance(sharp_factor)

        # 13. Vignette
        vignette = edits['vignette']
        if vignette > 0.001:
            arr = np.array(img, dtype=np.float32)
            h, w = arr.shape[:2]
            y, x = np.ogrid[:h, :w]
            cx, cy = w / 2, h / 2
            dist = np.sqrt((x - cx)**2 + (y - cy)**2)
            max_dist = np.sqrt(cx**2 + cy**2)
            dist = dist / max_dist
            vignette_mask = 1.0 - (dist ** 2) * vignette
            vignette_mask = vignette_mask[:, :, np.newaxis]
            arr = arr * vignette_mask
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 14. Texture (Fine Detail Local Contrast)
        # Similar to Clarity but with a smaller radius to target texture/fine details
        texture = edits.get('texture', 0.0)
        if abs(texture) > 0.001:
            arr = np.array(img, dtype=np.float32)
            luminance = 0.299 * arr[:,:,0] + 0.587 * arr[:,:,1] + 0.114 * arr[:,:,2]
            lum_img = Image.fromarray(luminance.astype(np.uint8))
            # Smaller radius for texture compared to clarity (20)
            blurred = lum_img.filter(ImageFilter.GaussianBlur(radius=2.0))
            blurred_arr = np.array(blurred, dtype=np.float32)
            # Apply texture enhancement primarily to midtones
            midtone_mask = 1.0 - np.abs(luminance - 128) / 128.0
            local_details = (luminance - blurred_arr) * texture * midtone_mask
            for c in range(3):
                arr[:,:,c] += local_details
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))


        return img

    def auto_levels(self, threshold_percent: float = 0.1) -> Tuple[float, float]:
        """
        Automatically adjusts blacks and whites based on image histogram.
        
        Args:
            threshold_percent: value 0.0-10.0, percentage of pixels to clip at each end.
        
        Returns:
            Tuple of (blacks, whites) parameter values.
        """
        if self.original_image is None:
            return 0.0, 0.0
            
        # Use preview image for speed if available, otherwise original
        img = self._preview_image if self._preview_image else self.original_image
        
        # Convert to numpy array for histogram analysis
        arr = np.array(img.convert('L')) # Use luminance for levels
        
        # Calculate percentiles
        low_p = threshold_percent
        high_p = 100.0 - threshold_percent
        
        p_low, p_high = np.percentile(arr, [low_p, high_p])
        
        # Calculate parameters to map p_low->0 and p_high->255
        # Logic matches _apply_edits:
        # black_point = -blacks * 40
        # white_point = 255 - whites * 40
        
        # We want black_point to be p_low
        # p_low = -blacks * 40 => blacks = -float(p_low) / 40.0
        blacks = -float(p_low) / 40.0
        
        # We want white_point to be p_high
        # p_high = 255 - whites * 40 => whites = (255.0 - float(p_high)) / 40.0
        whites = (255.0 - float(p_high)) / 40.0
        
        # Update state
        with self._lock:
            self.current_edits['blacks'] = blacks
            self.current_edits['whites'] = whites
            self._edits_rev += 1
        
        return blacks, whites

    def get_preview_data_cached(self, allow_compute: bool = True) -> Optional[DecodedImage]:
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
            base = self._preview_image.copy() if self._preview_image is not None else None
            edits = dict(self.current_edits)
            rev = self._edits_rev

        if base is None:
            return None

        # Heavy computation outside lock using snapshot
        img = self._apply_edits(base, edits=edits, for_export=False)

        if QImage is None:
            raise ImportError("PySide6.QtGui.QImage is required for get_preview_data_cached")

        # The image is in RGB mode after _apply_edits
        buffer = img.tobytes()
        decoded = DecodedImage(
            buffer=memoryview(buffer),
            width=img.width,
            height=img.height,
            bytes_per_line=img.width * 3,
            format=QImage.Format.Format_RGB888
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
            if key == 'rotation':
                # Guard against arbitrary angles in 'rotation'. It expects 90-degree steps.
                # For arbitrary rotation (drag to rotate), use 'straighten_angle'.
                try:
                    # Round to nearest 90 degrees
                    val_deg = float(value)
                    rounded_deg = round(val_deg / 90.0) * 90
                    final_val = int(rounded_deg) % 360
                    
                    if abs(val_deg - rounded_deg) > 1.0:
                         log.warning(f"'rotation' received {value}. Rounding to {final_val}. Use 'straighten_angle' for free rotation.")
                    
                    self.current_edits[key] = final_val
                    self._edits_rev += 1
                    return True
                except (ValueError, TypeError) as e:
                    log.warning(f"Invalid value for rotation {value!r}: {e}")
                    return False



            if key in self.current_edits and key != 'crop_box':
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
    
    def set_crop_box(self, crop_box: Tuple[int, int, int, int]):
        """Set the normalized crop box (left, top, right, bottom) from 0-1000."""
        with self._lock:
            self.current_edits['crop_box'] = crop_box
            self._edits_rev += 1

    def save_image(self) -> Optional[Tuple[Path, Path]]:
        """Saves the edited image, backing up the original.
        
        Returns:
            A tuple of (saved_path, backup_path) on success, otherwise None.
        """
        if self.original_image is None or self.current_filepath is None:
            return None

        final_img = self.original_image.copy()
        final_img = self._apply_edits(final_img, for_export=True)

        original_path = self.current_filepath
        try:
            original_stat = original_path.stat()
        except OSError as e:
            log.warning(f"Unable to read timestamps for {original_path}: {e}")
            original_stat = None
        
        # Use the reusable backup function
        backup_path = create_backup_file(original_path)
        if backup_path is None:
            return None
        
        try:
            
            # Re-open original to correctly detect format and get EXIF
            with Image.open(original_path) as original_img:
                original_format = original_img.format or original_path.suffix.lstrip('.').upper()
                
                # Handle EXIF
                exif_bytes = original_img.info.get('exif')
                
                # Try to reset orientation to Normal (1) if EXIF exists
                if exif_bytes:
                    try:
                        # Load exif data as an object
                        exif = original_img.getexif()
                        # Tag 274 is Orientation. Set to 1 (Normal)
                        if 274 in exif:
                            exif[274] = 1
                            # Serialize back to bytes - Pillow >= 8.2.0 required for tobytes()
                            # If tobytes() is missing, we might skip writing modified EXIF or write original
                            if hasattr(exif, 'tobytes'):
                                exif_bytes = exif.tobytes()
                            else:
                                # Fallback for older Pillow: skip writing EXIF if we can't sanitize it
                                # to avoid double-rotation bug.
                                log.warning("Pillow too old to sanitize EXIF bytes. Skipping EXIF write to prevent double-rotation.")
                                exif_bytes = None
                    except Exception as e:
                        log.warning(f"Failed to sanitize EXIF orientation: {e}")
                        # Fallback: safer to skip EXIF than write bad orientation
                        exif_bytes = None

            save_kwargs = {}
            if original_format == 'JPEG':
                save_kwargs['format'] = 'JPEG'
                save_kwargs['quality'] = 95
                if exif_bytes:
                    save_kwargs['exif'] = exif_bytes
            else:
                save_kwargs['format'] = original_format

            try:
                # First attempt: preserve EXIF (if any) and original format settings
                final_img.save(original_path, **save_kwargs)
            except Exception as e:
                exif_was_requested = 'exif' in save_kwargs
                log.warning(
                    f"Could not save with original format settings"
                    f"{' (with EXIF)' if exif_was_requested else ''}: {e}"
                )

                # If EXIF was requested, try again without EXIF but keep format/quality
                if exif_was_requested:
                    retry_kwargs = dict(save_kwargs)
                    retry_kwargs.pop('exif', None)
                    try:
                        final_img.save(original_path, **retry_kwargs)
                        log.info(
                            "Image saved without EXIF metadata; "
                            "EXIF may be corrupted or incompatible with the edited image."
                        )
                    except Exception as e2:
                        log.warning(f"Could not save even without EXIF metadata: {e2}")
                        # Fall through to the final fallback below

                # Final fallback: let Pillow infer format from suffix / image mode
                try:
                    final_img.save(original_path)
                    log.warning(
                        "Used final fallback save; image may not use the original "
                        "format settings and EXIF metadata is likely lost."
                    )
                except Exception as e3:
                    log.exception(f"Failed to save edited image even with fallback: {e3}")
                    # Reraise so the outer except logs and returns None
                    raise

            if original_stat is not None:
                self._restore_file_times(original_path, original_stat)

            return original_path, backup_path
        except Exception as e:
            log.exception(f"Failed to save edited image or backup: {e}")
            return None

    def _restore_file_times(self, path: Path, original_stat: os.stat_result) -> None:
        """Best-effort restoration of access/modify timestamps after saving."""
        try:
            os.utime(path, (original_stat.st_atime, original_stat.st_mtime))
        except OSError as e:
            log.warning(f"Unable to restore timestamps for {path}: {e}")

    def rotate_image_cw(self):
        """Decreases the rotation edit parameter by 90° modulo 360 (clockwise)."""
        with self._lock:
            current = self.current_edits.get('rotation', 0)
            self.current_edits['rotation'] = (current - 90) % 360
            self._edits_rev += 1

    def rotate_image_ccw(self):
        """Increases the rotation edit parameter by 90° modulo 360 (counter-clockwise)."""
        with self._lock:
            current = self.current_edits.get('rotation', 0)
            self.current_edits['rotation'] = (current + 90) % 360
            self._edits_rev += 1

# Dictionary of ratios for QML dropdown
ASPECT_RATIOS = [{"name": name, "ratio": ratio} for name, ratio in INSTAGRAM_RATIOS.items()]
