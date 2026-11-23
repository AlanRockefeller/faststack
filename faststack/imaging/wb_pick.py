"""White balance from a picked spot (the compact editor's "Pick" button).

The user clicks a spot that should be white or grey: a grey card, a white
wall, a colourless stone. The spot's average colour in the source image,
before white balance and in linear light (where ``_apply_edits`` applies its
white balance gains), gives the gains that make the spot neutral. They are
returned as the Temp (B/Y) and Tint (G/M) slider values that
``_normalized_wb_gains`` turns back into those gains.

The result is absolute: it replaces the sliders' values rather than adding to
them, so clicking the same spot again gives the same answer. The editor
scales its gains so a neutral grey keeps its brightness, so a pick changes
colour, not exposure. Every later stage treats the three channels alike for a
neutral pixel (tone, levels, saturation, colour mix), so a spot neutral after
white balance stays neutral in the finished photo.
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import numpy as np
from PIL import Image

from faststack.imaging.math_utils import _srgb_to_linear

# Spot radius, as a fraction of the uncropped image diagonal (the brushes'
# unit, so the ring on screen is drawn the same way). A grey spot is usually
# far smaller than a dodge & burn brush, hence its own size and range.
PICK_RADIUS_DEFAULT = 0.01
PICK_RADIUS_MIN = 0.001
PICK_RADIUS_MAX = 0.1

# A pixel says what colour the light is only when no channel is clipped (a
# blown highlight reads white whatever the light) or down in the noise. 250
# is auto white balance's rgb_upper_bound.
_CLIP_LEVEL = 250.0 / 255.0
_DARK_LEVEL = 3.0 / 255.0
# A spot that is mostly clipped (or black) is refused rather than read from
# the few pixels at its edge.
_MIN_USABLE_SHARE = 0.25
# A large spot is read every n-th pixel, so a click stays instant.
_MAX_SAMPLE_PIXELS = 250_000

# Why a spot was refused.
PICK_OUTSIDE = "outside"
PICK_CLIPPED = "clipped"
PICK_DARK = "dark"


@dataclass(frozen=True)
class WhiteBalancePick:
    """Slider values that make the picked spot neutral, or why there are none."""

    by: float = 0.0  # Temp (B/Y) slider value, -1..1
    mg: float = 0.0  # Tint (G/M) slider value, -1..1
    # PICK_OUTSIDE / PICK_CLIPPED / PICK_DARK when the spot was refused.
    error: Optional[str] = None
    # A slider hit its end: the cast is stronger than the sliders reach, so
    # the spot is only nearly neutral.
    clamped: bool = False
    mean_linear: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    pixels: int = 0  # pixels the colour was read from


def read_disc(
    source: Union[np.ndarray, Image.Image],
    u: float,
    v: float,
    radius: float,
) -> Optional[np.ndarray]:
    """The sRGB pixels, (N, 3) float32 in [0, 1], of a disc on ``source``.

    ``(u, v)`` is the centre in [0, 1] image coordinates and ``radius`` is a
    fraction of the image diagonal. ``source`` is a float image in [0, 1]
    (8- or 16-bit integer arrays are scaled) or a PIL image. The disc is cut
    off at the image edge; a centre off the image returns None. A disc
    smaller than one pixel reads the pixel under the centre.
    """
    if isinstance(source, Image.Image):
        width, height = source.size
    else:
        height, width = source.shape[:2]
    if width <= 0 or height <= 0:
        return None
    if not (math.isfinite(u) and math.isfinite(v) and math.isfinite(radius)):
        return None
    if not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
        return None

    cx, cy = u * width, v * height
    r = max(0.0, radius) * math.hypot(width, height)
    x0 = max(0, int(math.floor(cx - r)))
    y0 = max(0, int(math.floor(cy - r)))
    x1 = min(width, int(math.ceil(cx + r)))
    y1 = min(height, int(math.ceil(cy + r)))
    if x1 <= x0 or y1 <= y0:
        # Thinner than a pixel: the pixel under the centre.
        x0 = min(width - 1, int(cx))
        y0 = min(height - 1, int(cy))
        x1, y1 = x0 + 1, y0 + 1

    stride = max(1, math.ceil(math.sqrt((x1 - x0) * (y1 - y0) / _MAX_SAMPLE_PIXELS)))
    if isinstance(source, Image.Image):
        box = source.crop((x0, y0, x1, y1))
        if box.mode != "RGB":
            box = box.convert("RGB")
        patch = np.asarray(box)[::stride, ::stride]
    else:
        patch = source[y0:y1:stride, x0:x1:stride]
    patch = _as_unit_rgb(patch)

    # Pixel centres inside the circle.
    ys = y0 + stride * np.arange(patch.shape[0]) + 0.5
    xs = x0 + stride * np.arange(patch.shape[1]) + 0.5
    inside = (xs[None, :] - cx) ** 2 + (ys[:, None] - cy) ** 2 <= r * r
    if not inside.any():
        # Under half a pixel across: the pixel nearest the centre.
        iy = int(np.argmin(np.abs(ys - cy)))
        ix = int(np.argmin(np.abs(xs - cx)))
        return patch[iy : iy + 1, ix].copy()
    return patch[inside]


def _as_unit_rgb(patch: np.ndarray) -> np.ndarray:
    """(H, W, 3) float32 in [0, 1] from a float, 8-bit or 16-bit patch."""
    patch = np.asarray(patch)
    if patch.ndim == 2:
        patch = np.repeat(patch[:, :, None], 3, axis=2)
    elif patch.shape[2] == 1:
        patch = np.repeat(patch, 3, axis=2)
    else:
        patch = patch[:, :, :3]
    if patch.dtype == np.uint8:
        return patch.astype(np.float32) / np.float32(255.0)
    if patch.dtype == np.uint16:
        return patch.astype(np.float32) / np.float32(65535.0)
    return patch.astype(np.float32, copy=False)


def solve_white_balance(srgb: np.ndarray) -> WhiteBalancePick:
    """Temp / Tint slider values that make these sRGB pixels neutral.

    Clipped and near-black pixels are left out. With
    ``_normalized_wb_gains``' gains (red ``1 + by/2``, blue ``1 - by/2``,
    green ``1 - mg/2``, before the common luminance scale), the spot's mean
    linear colour (R, G, B) comes out neutral when

        by = 2 (B - R) / (B + R)                  (red and blue equal)
        mg = 2 (1 - RB' / G)                      (green equal to them)

    where RB' is the corrected red / blue level, ``2RB / (R + B)``. When the
    cast is stronger than Temp reaches, Temp stops at its end and green is
    matched to the mean of the red and blue it leaves.
    """
    pixels = np.asarray(srgb, dtype=np.float32).reshape(-1, 3)
    total = len(pixels)
    if total == 0:
        return WhiteBalancePick(error=PICK_OUTSIDE)

    finite = np.isfinite(pixels).all(axis=1)
    high = pixels.max(axis=1)
    low = pixels.min(axis=1)
    clipped = finite & (high >= _CLIP_LEVEL)
    dark = finite & ~clipped & (low <= _DARK_LEVEL)
    usable = finite & ~clipped & ~dark
    count = int(np.count_nonzero(usable))
    if count == 0 or count < _MIN_USABLE_SHARE * total:
        reason = (
            PICK_CLIPPED
            if np.count_nonzero(clipped) >= np.count_nonzero(dark)
            else PICK_DARK
        )
        return WhiteBalancePick(error=reason, pixels=count)

    linear = _srgb_to_linear(pixels[usable].astype(np.float64))
    r, g, b = (float(c) for c in linear.mean(axis=0))
    if min(r, g, b) <= 1e-6:
        return WhiteBalancePick(error=PICK_DARK, pixels=count)

    by_exact = 2.0 * (b - r) / (b + r)
    by = min(1.0, max(-1.0, by_exact))
    half = 0.5 * by
    red_blue = 0.5 * ((1.0 + half) * r + (1.0 - half) * b)
    mg_exact = 2.0 * (1.0 - red_blue / g)
    mg = min(1.0, max(-1.0, mg_exact))
    return WhiteBalancePick(
        by=by,
        mg=mg,
        clamped=(by != by_exact or mg != mg_exact),
        mean_linear=(r, g, b),
        pixels=count,
    )
