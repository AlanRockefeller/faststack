"""Centralized utilities for EXIF orientation handling."""

import logging
from pathlib import Path
from typing import Optional
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


def get_exif_orientation(image_path: Path, exif: Optional[Image.Exif] = None) -> int:
    """Read the EXIF Orientation tag from an image file or provided EXIF object.

    Args:
        image_path: Path to the image file
        exif: Optional pre-read PIL Exif object

    Returns:
        Orientation value (1-8), defaults to 1 if missing or error.
    """
    try:
        if exif is None:
            with Image.open(image_path) as img:
                exif = img.getexif()

        if not exif:
            return 1

        # EXIF Orientation tag ID is 274
        return exif.get(274, 1)
    except (OSError, IOError, AttributeError) as e:
        log.debug("Could not read EXIF orientation for %s: %s", image_path, e)
        return 1


def apply_orientation_to_np(buffer: np.ndarray, orientation: int) -> np.ndarray:
    """Apply EXIF orientation transformation to a numpy image buffer.

    Args:
        buffer: Image as numpy array (H, W, 3) RGB uint8 or float32
        orientation: Orientation value (1-8)

    Returns:
        Transformed numpy array. Guaranteed to be C-contiguous.
    """
    if orientation <= 1:
        # Ensure C-contiguity even for identity orientation
        if not buffer.flags["C_CONTIGUOUS"]:
            return np.ascontiguousarray(buffer)
        return buffer

    # Apply transformation based on orientation
    if orientation == 2:
        # Mirrored horizontally
        result = np.fliplr(buffer)
    elif orientation == 3:
        # Rotated 180 degrees
        result = np.rot90(buffer, k=2)
    elif orientation == 4:
        # Mirrored vertically
        result = np.flipud(buffer)
    elif orientation == 5:
        # Mirrored horizontally then rotated 90 CCW
        result = np.rot90(np.fliplr(buffer), k=1)
    elif orientation == 6:
        # Rotated 90 CW (270 CCW)
        result = np.rot90(buffer, k=3)
    elif orientation == 7:
        # Mirrored horizontally then rotated 90 CW
        result = np.rot90(np.fliplr(buffer), k=3)
    elif orientation == 8:
        # Rotated 90 CCW
        result = np.rot90(buffer, k=1)
    else:
        # Unknown orientation - ensure C-contiguity
        if not buffer.flags["C_CONTIGUOUS"]:
            return np.ascontiguousarray(buffer)
        return buffer

    # Ensure result is C-contiguous after flip/rotate
    if not result.flags["C_CONTIGUOUS"]:
        result = np.ascontiguousarray(result)
    return result


def apply_exif_orientation(rgb: np.ndarray, path: Path) -> np.ndarray:
    """Read EXIF orientation from path and apply it to the numpy buffer.

    Requirements:
    - Reads EXIF orientation from path using PIL.
    - If file missing / cannot read EXIF / no EXIF: return input unchanged (as C-contiguous).
    - If orientation > 1: call apply_orientation_to_np and ensure contiguity.
    - No Qt deps.
    """
    orientation = get_exif_orientation(path)
    if orientation <= 1:
        # Return input unchanged but ensure C-contiguous
        if not rgb.flags["C_CONTIGUOUS"]:
            return np.ascontiguousarray(rgb)
        return rgb

    # apply_orientation_to_np already ensures C-contiguity
    return apply_orientation_to_np(rgb, orientation)
