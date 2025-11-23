import os
import shutil
import glob
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from io import BytesIO

from faststack.models import DecodedImage
from PySide6.QtGui import QImage

# Aspect Ratios for cropping
INSTAGRAM_RATIOS = {
    "Freeform": None,
    "1:1 (Square)": (1, 1),
    "4:5 (Portrait)": (4, 5),
    "1.91:1 (Landscape)": (191, 100),
    "9:16 (Story)": (9, 16),
}

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
        }

    def load_image(self, filepath: str, cached_preview: Optional[DecodedImage] = None):
        """Load a new image for editing."""
        if not filepath or not Path(filepath).exists():
            self.original_image = None
            self.current_filepath = None
            self._preview_image = None
            return False
        
        self.current_filepath = Path(filepath)
        # Reset edits
        self.current_edits = self._initial_edits()
        
        try:
            # We must load and close the original file handle immediately
            self.original_image = Image.open(self.current_filepath).convert("RGB")

            # Use the cached, display-sized preview if available
            if cached_preview:
                self._preview_image = Image.frombytes(
                    "RGB",
                    (cached_preview.width, cached_preview.height),
                    bytes(cached_preview.buffer)
                )
            else:
                # Fallback: create a thumbnail if no preview is provided
                self._preview_image = self.original_image.copy()
                self._preview_image.thumbnail((1920, 1080)) # Reasonable fallback size

            return True
        except Exception as e:
            print(f"Error loading image for editing: {e}")
            self.original_image = None
            self._preview_image = None
            return False

    def _apply_edits(self, img: Image.Image) -> Image.Image:
        """Applies all current edits to the provided PIL Image."""
        # 1. Rotation
        rotation = self.current_edits['rotation']
        if rotation == 90:
            img = img.transpose(Image.Transpose.ROTATE_90)
        elif rotation == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif rotation == 270:
            img = img.transpose(Image.Transpose.ROTATE_270)

        # 2. Cropping
        crop_box = self.current_edits.get('crop_box')
        if crop_box:
            width, height = img.size
            left = int(crop_box[0] * width / 1000)
            top = int(crop_box[1] * height / 1000)
            right = int(crop_box[2] * width / 1000)
            bottom = int(crop_box[3] * height / 1000)
            img = img.crop((left, top, right, bottom))
        
        # 3. Exposure (gamma-based)
        exposure = self.current_edits['exposure']
        if abs(exposure) > 0.001:
            gamma = 1.0 / (1.0 + exposure) if exposure >= 0 else 1.0 - exposure
            arr = np.array(img, dtype=np.float32) / 255.0
            arr = np.power(arr, gamma)
            arr = (arr * 255).clip(0, 255).astype(np.uint8)
            img = Image.fromarray(arr)

        # 4. Blacks/Whites (Levels)
        blacks = self.current_edits['blacks']
        whites = self.current_edits['whites']
        if abs(blacks) > 0.001 or abs(whites) > 0.001:
            arr = np.array(img, dtype=np.float32)
            black_point = -blacks * 40
            white_point = 255 + whites * 40
            arr = (arr - black_point) * (255.0 / (white_point - black_point))
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 5. Highlights/Shadows
        highlights = self.current_edits['highlights']
        shadows = self.current_edits['shadows']
        if abs(highlights) > 0.001 or abs(shadows) > 0.001:
            arr = np.array(img, dtype=np.float32)
            if abs(shadows) > 0.001:
                shadow_mask = 1.0 - np.clip(arr / 128.0, 0, 1)
                arr += shadows * 60 * shadow_mask
            if abs(highlights) > 0.001:
                highlight_mask = np.clip((arr - 128) / 127.0, 0, 1)
                arr += highlights * 60 * highlight_mask
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 6. Brightness
        bright_factor = 1.0 + self.current_edits['brightness']
        if abs(bright_factor - 1.0) > 0.001:
            img = ImageEnhance.Brightness(img).enhance(bright_factor)

        # 7. Contrast
        contrast_factor = 1.0 + self.current_edits['contrast']
        if abs(contrast_factor - 1.0) > 0.001:
            img = ImageEnhance.Contrast(img).enhance(contrast_factor)

        # 8. Clarity
        clarity = self.current_edits['clarity']
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
        saturation_factor = 1.0 + self.current_edits['saturation']
        if abs(saturation_factor - 1.0) > 0.001:
            img = ImageEnhance.Color(img).enhance(saturation_factor)

        # 10. Vibrance
        vibrance = self.current_edits['vibrance']
        if abs(vibrance) > 0.001:
            arr = np.array(img, dtype=np.float32)
            sat = (arr.max(axis=2) - arr.min(axis=2)) / 255.0
            boost = (1.0 - sat) * vibrance
            gray = arr.mean(axis=2, keepdims=True)
            arr = gray + (arr - gray) * (1.0 + boost[:, :, np.newaxis])
            img = Image.fromarray(arr.clip(0, 255).astype(np.uint8))

        # 11. White Balance
        by_val = self.current_edits['white_balance_by'] * 0.5
        mg_val = self.current_edits['white_balance_mg'] * 0.5
        if abs(by_val) > 0.001 or abs(mg_val) > 0.001:
            arr = np.array(img, dtype=np.float32)
            by_shift = by_val * 127.5
            mg_shift = mg_val * 127.5
            arr[:, :, 0] += by_shift + mg_shift
            arr[:, :, 1] += by_shift - mg_shift
            arr[:, :, 2] -= by_shift - mg_shift
            np.clip(arr, 0, 255, out=arr)
            img = Image.fromarray(arr.astype(np.uint8))

        # 12. Sharpness
        sharp_factor = 1.0 + self.current_edits['sharpness']
        if abs(sharp_factor - 1.0) > 0.001:
            img = ImageEnhance.Sharpness(img).enhance(sharp_factor)
        
        # 13. Vignette
        vignette = self.current_edits['vignette']
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

        return img

    def get_preview_data(self) -> Optional[DecodedImage]:
        """Apply current edits and return the data as a DecodedImage."""
        if self._preview_image is None:
            return None

        # Always start from a fresh copy of the small preview image
        img = self._preview_image.copy()
        img = self._apply_edits(img)

        # The image is in RGB mode after _apply_edits
        buffer = img.tobytes()
        return DecodedImage(
            buffer=memoryview(buffer),
            width=img.width,
            height=img.height,
            bytes_per_line=img.width * 3,  # 3 bytes per pixel for RGB
            format=QImage.Format.Format_RGB888
        )

    def set_edit_param(self, key: str, value: Any) -> bool:
        """Update a single edit parameter."""
        if key in self.current_edits and key != 'crop_box':
            self.current_edits[key] = value
            return True
        return False
    
    def set_crop_box(self, crop_box: Tuple[int, int, int, int]):
        """Set the normalized crop box (left, top, right, bottom) from 0-1000."""
        self.current_edits['crop_box'] = crop_box

    def save_image(self) -> Optional[Tuple[Path, Path]]:
        """Saves the edited image, backing up the original.
        
        Returns:
            A tuple of (saved_path, backup_path) on success, otherwise None.
        """
        if self.original_image is None or self.current_filepath is None:
            return None

        final_img = self.original_image.copy()
        final_img = self._apply_edits(final_img)

        original_path = self.current_filepath
        
        # Extract base name without any existing -backup suffix
        stem = original_path.stem
        # Remove any existing -backup, -backup2, -backup-1, etc. (handles both old and new formats)
        import re
        base_stem = re.sub(r'-backup(-?\d+)?$', '', stem)
        
        # Try filename-backup.jpg first
        backup_path = original_path.parent / f"{base_stem}-backup{original_path.suffix}"
        
        # If that exists, try filename-backup2.jpg, filename-backup3.jpg, etc.
        i = 2
        while backup_path.exists():
            backup_path = original_path.parent / f"{base_stem}-backup{i}{original_path.suffix}"
            i += 1
        
        try:
            # Perform the backup and overwrite
            shutil.copy2(original_path, backup_path)
            
            # Re-open original to correctly detect format and get EXIF
            with Image.open(original_path) as original_img:
                original_format = original_img.format or original_path.suffix.lstrip('.').upper()
                exif_data = original_img.info.get('exif')

            save_kwargs = {}
            if original_format == 'JPEG':
                save_kwargs['format'] = 'JPEG'
                save_kwargs['quality'] = 95
                if exif_data:
                    save_kwargs['exif'] = exif_data
            else:
                save_kwargs['format'] = original_format

            try:
                final_img.save(original_path, **save_kwargs)
            except Exception as e:
                print(f"Warning: Could not save with original format settings: {e}")
                # Fallback to saving based on suffix
                final_img.save(original_path)

            return original_path, backup_path
        except Exception as e:
            print(f"Failed to save edited image or backup: {e}")
            return None

# Dictionary of ratios for QML dropdown
ASPECT_RATIOS = [{"name": name, "ratio": ratio} for name, ratio in INSTAGRAM_RATIOS.items()]
