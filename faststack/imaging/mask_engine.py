# faststack/imaging/mask_engine.py
"""Stroke rasterisation and coordinate transforms for local adjustments.

Provides:
- forward_transform / inverse_transform – pure coordinate helpers
- rasterize_layer_density – strokes → per-pixel coat density for one layer
- LayerDensityCache       – disposable, resolution-keyed raster cache
"""

import dataclasses
import logging
import math
from typing import Any, Dict, Optional, Sequence, Tuple

import numpy as np

from faststack.imaging.mask import STROKE_ERASE, MaskData, MaskStroke
from faststack.imaging.optional_deps import get_cv2

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coordinate transforms
# ---------------------------------------------------------------------------


def forward_transform(
    x_norm: float,
    y_norm: float,
    edits: Dict[str, Any],
    target_shape: Tuple[int, int],
) -> Tuple[float, float]:
    """Map oriented-base-image [0,1] coords → pixel coords in the
    post-geometry (post-straighten, post-crop) rasterisation array.

    *target_shape* is ``(H, W)`` of the array being rasterised into.
    """
    straighten = float(edits.get("straighten_angle", 0.0))
    crop_box = edits.get("crop_box")
    has_crop = (
        crop_box is not None
        and len(crop_box) == 4
        and any(v != d for v, d in zip(crop_box, (0, 0, 1000, 1000), strict=True))
    )

    # Start in oriented-base-image space [0, 1]
    x, y = x_norm, y_norm

    # 0. Apply 90-degree rotation steps (matches np.rot90 in _apply_edits)
    rotation = edits.get("rotation", 0)
    k = (rotation // 90) % 4
    if k == 1:  # 90 CCW: (x, y) → (y, 1-x)
        x, y = y, 1.0 - x
    elif k == 2:  # 180:    (x, y) → (1-x, 1-y)
        x, y = 1.0 - x, 1.0 - y
    elif k == 3:  # 270 CCW: (x, y) → (1-y, x)
        x, y = 1.0 - y, x

    # 1. Apply straighten rotation around (0.5, 0.5)
    if abs(straighten) > 0.001:
        rad = math.radians(-straighten)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        dx, dy = x - 0.5, y - 0.5
        x = dx * cos_a - dy * sin_a + 0.5
        y = dx * sin_a + dy * cos_a + 0.5

    # 2. Apply crop (map full-image normalised → crop-window normalised)
    if has_crop:
        cl, ct, cr, cb = (
            crop_box[0] / 1000,
            crop_box[1] / 1000,
            crop_box[2] / 1000,
            crop_box[3] / 1000,
        )
        cw = cr - cl
        ch = cb - ct
        if cw > 0 and ch > 0:
            x = (x - cl) / cw
            y = (y - ct) / ch

    # 3. Scale to pixel coords
    th, tw = target_shape
    return x * tw, y * th


def inverse_transform(
    x_disp: float,
    y_disp: float,
    edits: Dict[str, Any],
    display_shape: Tuple[int, int],
) -> Tuple[float, float]:
    """Map display / QML normalised [0,1] coords → oriented-base-image [0,1].

    *display_shape* is ``(H, W)`` of the displayed image (not used for
    normalised inputs but kept for API symmetry / future use).

    ``x_disp`` and ``y_disp`` are assumed already normalised to [0,1]
    relative to the displayed (post-crop, post-straighten) image.
    """
    straighten = float(edits.get("straighten_angle", 0.0))
    crop_box = edits.get("crop_box")
    has_crop = (
        crop_box is not None
        and len(crop_box) == 4
        and any(v != d for v, d in zip(crop_box, (0, 0, 1000, 1000), strict=True))
    )

    x, y = x_disp, y_disp

    # Inverse crop: map crop-window normalised → full-image normalised
    if has_crop:
        cl, ct, cr, cb = (
            crop_box[0] / 1000,
            crop_box[1] / 1000,
            crop_box[2] / 1000,
            crop_box[3] / 1000,
        )
        cw = cr - cl
        ch = cb - ct
        if cw > 0 and ch > 0:
            x = x * cw + cl
            y = y * ch + ct

    # Inverse straighten: rotate by +angle (undo the -angle forward)
    if abs(straighten) > 0.001:
        rad = math.radians(straighten)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        dx, dy = x - 0.5, y - 0.5
        x = dx * cos_a - dy * sin_a + 0.5
        y = dx * sin_a + dy * cos_a + 0.5

    # Inverse 90-degree rotation (undo step 0 of forward_transform)
    rotation = edits.get("rotation", 0)
    k = (rotation // 90) % 4
    if k == 1:  # undo 90 CCW: (x, y) → (1-y, x)
        x, y = 1.0 - y, x
    elif k == 2:  # undo 180:    (x, y) → (1-x, 1-y)
        x, y = 1.0 - x, 1.0 - y
    elif k == 3:  # undo 270 CCW: (x, y) → (y, 1-x)
        x, y = y, 1.0 - x

    return x, y


# ---------------------------------------------------------------------------
# Stroke rasterisation
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class LayerDensity:
    """Coat density of one layer inside its bounding box.

    ``arr`` is float32 ``(y1 - y0, x1 - x0)``; pixels outside the box have
    density 0.  One paint stroke deposits up to 1.0, so overlapping strokes
    build up without limit.
    """

    y0: int
    y1: int
    x0: int
    x1: int
    arr: np.ndarray


def _base_diagonal_px(edits: Dict[str, Any], shape: Tuple[int, int]) -> float:
    """Diagonal of the uncropped oriented image, in *shape*'s pixels."""
    th, tw = shape
    crop_box = edits.get("crop_box")
    cw = ch = 1.0
    if crop_box is not None and len(crop_box) == 4:
        cw = (crop_box[2] - crop_box[0]) / 1000
        ch = (crop_box[3] - crop_box[1]) / 1000
        if cw <= 0 or ch <= 0:
            cw = ch = 1.0
    return math.hypot(tw / cw, th / ch)


def _stroke_pixel_points(
    stroke: MaskStroke, edits: Dict[str, Any], shape: Tuple[int, int]
) -> np.ndarray:
    pts = [forward_transform(x, y, edits, shape) for x, y in stroke.points]
    return np.asarray(pts, dtype=np.float64).reshape(-1, 2)


def _stroke_coverage(
    pixel_points: np.ndarray,
    radius_px: float,
    hardness: float,
    shape: Tuple[int, int],
) -> Optional[Tuple[int, int, int, int, np.ndarray]]:
    """Soft single-coat coverage of one stroke, cropped to its bounding box.

    Returns ``(y0, y1, x0, x1, coverage)`` or None when the stroke misses the
    image.  Coverage is 1.0 within ``radius * hardness`` of the stroke path
    and falls smoothly to 0 at ``radius``.  Using the distance to the whole
    path (rather than summing dabs) is what makes a stroke a single coat.
    """
    h, w = shape
    if pixel_points.size == 0:
        return None
    radius_px = max(0.75, float(radius_px))
    pad = radius_px + 2.0
    min_x, min_y = pixel_points.min(axis=0)
    max_x, max_y = pixel_points.max(axis=0)
    x0 = max(0, int(math.floor(min_x - pad)))
    y0 = max(0, int(math.floor(min_y - pad)))
    x1 = min(w, int(math.ceil(max_x + pad)) + 1)
    y1 = min(h, int(math.ceil(max_y + pad)) + 1)
    if x1 <= x0 or y1 <= y0:
        return None

    # The distance canvas extends past the image edge by the brush radius so
    # a stroke whose path runs just outside the frame still paints inside it.
    cx0 = int(math.floor(min(x0, min_x - pad)))
    cy0 = int(math.floor(min(y0, min_y - pad)))
    cx1 = int(math.ceil(max(x1, max_x + pad))) + 1
    cy1 = int(math.ceil(max(y1, max_y + pad))) + 1
    local = pixel_points - (cx0, cy0)

    cv2 = get_cv2()
    if cv2 is not None:
        canvas = np.full((cy1 - cy0, cx1 - cx0), 255, dtype=np.uint8)
        ipts = np.round(local).astype(np.int32)
        if len(ipts) == 1:
            canvas[ipts[0, 1], ipts[0, 0]] = 0
        else:
            cv2.polylines(canvas, [ipts.reshape(-1, 1, 2)], False, 0, 1)
        dist = cv2.distanceTransform(canvas, cv2.DIST_L2, 5)
    else:
        dist = _path_distance_numpy(local, (cy1 - cy0, cx1 - cx0), pad)

    dist = dist[y0 - cy0 : y1 - cy0, x0 - cx0 : x1 - cx0]
    inner = radius_px * min(1.0, max(0.0, hardness))
    soft = max(radius_px - inner, 1.0)
    t = np.clip((radius_px - dist) / soft, 0.0, 1.0)
    coverage = t * t * (3.0 - 2.0 * t)
    return y0, y1, x0, x1, coverage.astype(np.float32, copy=False)


def _path_distance_numpy(
    points: np.ndarray, shape: Tuple[int, int], max_dist: float
) -> np.ndarray:
    """Distance to the nearest point of a densely interpolated path (no cv2)."""
    h, w = shape
    dist = np.full((h, w), max_dist + 1.0, dtype=np.float32)
    filled = [tuple(points[0])]
    for x1, y1 in points[1:]:
        x0, y0 = filled[-1]
        n = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0))))
        for j in range(1, n + 1):
            filled.append((x0 + (x1 - x0) * j / n, y0 + (y1 - y0) * j / n))
    r = int(math.ceil(max_dist)) + 1
    for px, py in filled:
        bx0, bx1 = max(0, int(px) - r), min(w, int(px) + r + 1)
        by0, by1 = max(0, int(py) - r), min(h, int(py) + r + 1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        yy, xx = np.ogrid[by0:by1, bx0:bx1]
        d = np.sqrt((xx - px) ** 2 + (yy - py) ** 2).astype(np.float32)
        np.minimum(dist[by0:by1, bx0:bx1], d, out=dist[by0:by1, bx0:bx1])
    return dist


def _apply_strokes(
    base: Optional[LayerDensity],
    strokes: Sequence[MaskStroke],
    edits: Dict[str, Any],
    shape: Tuple[int, int],
) -> Optional[LayerDensity]:
    """Composite *strokes* in order onto *base* (never mutated)."""
    diag = _base_diagonal_px(edits, shape)
    covered = []
    for stroke in strokes:
        cov = _stroke_coverage(
            _stroke_pixel_points(stroke, edits, shape),
            stroke.radius * diag,
            stroke.hardness,
            shape,
        )
        if cov is not None:
            covered.append((stroke, cov))
    if not covered:
        return base

    boxes = [c[1][:4] for c in covered if c[0].mode != STROKE_ERASE]
    if base is not None:
        boxes.append((base.y0, base.y1, base.x0, base.x1))
    if not boxes:
        return base  # only erasing, and nothing to erase
    y0 = min(b[0] for b in boxes)
    y1 = max(b[1] for b in boxes)
    x0 = min(b[2] for b in boxes)
    x1 = max(b[3] for b in boxes)

    out = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
    if base is not None:
        out[base.y0 - y0 : base.y1 - y0, base.x0 - x0 : base.x1 - x0] = base.arr

    for stroke, (sy0, sy1, sx0, sx1, cov) in covered:
        # Clip the stroke box to the layer box (erase strokes may extend past).
        cy0, cy1 = max(sy0, y0), min(sy1, y1)
        cx0, cx1 = max(sx0, x0), min(sx1, x1)
        if cy1 <= cy0 or cx1 <= cx0:
            continue
        c = cov[cy0 - sy0 : cy1 - sy0, cx0 - sx0 : cx1 - sx0]
        region = out[cy0 - y0 : cy1 - y0, cx0 - x0 : cx1 - x0]
        if stroke.mode == STROKE_ERASE:
            region *= 1.0 - c
        else:
            region += c
    return LayerDensity(y0, y1, x0, x1, out)


def _geometry_key(edits: Dict[str, Any]) -> tuple:
    return (
        edits.get("rotation", 0),
        round(float(edits.get("straighten_angle", 0.0)), 4),
        tuple(edits.get("crop_box") or ()),
    )


class LayerDensityCache:
    """Committed-stroke densities per layer and render size.

    Live previews alternate between a few render sizes (drag tier, quick
    master, display-resolution refinement), so each layer keeps a small LRU
    of sizes rather than one entry that every size change would evict.

    Entries remember the exact stroke objects they were built from. Strokes
    are immutable once committed and the list only ever grows by appending
    (or is replaced on undo/clear), so when the current list extends a cached
    one only the new strokes are composited — committing a stroke costs one
    stroke, not a re-rasterisation of the whole layer.

    Not thread-safe: each instance belongs to a single rendering thread
    (preview worker, export, GUI overlay).
    """

    _SIZES_PER_LAYER = 4

    def __init__(self):
        # layer_id -> {(uid, shape, geometry): (strokes, density)}, LRU order
        self._entries: Dict[str, Dict[tuple, Tuple[tuple, Optional[LayerDensity]]]] = {}

    def __bool__(self) -> bool:
        return bool(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def get(
        self, layer_id: str, key: tuple
    ) -> Optional[Tuple[tuple, Optional[LayerDensity]]]:
        sizes = self._entries.get(layer_id)
        entry = sizes.pop(key, None) if sizes is not None else None
        if entry is not None:
            sizes[key] = entry  # most recently used last
        return entry

    def put(
        self,
        layer_id: str,
        key: tuple,
        strokes: tuple,
        value: Optional[LayerDensity],
    ) -> None:
        sizes = self._entries.setdefault(layer_id, {})
        sizes.pop(key, None)
        sizes[key] = (strokes, value)
        while len(sizes) > self._SIZES_PER_LAYER:
            sizes.pop(next(iter(sizes)))

    def prune(self, keep_ids) -> None:
        for layer_id in list(self._entries):
            if layer_id not in keep_ids:
                self._entries.pop(layer_id, None)


def _extends(strokes: Sequence[MaskStroke], cached: tuple) -> bool:
    """True when *strokes* starts with exactly the stroke objects in *cached*."""
    return len(strokes) >= len(cached) and all(a is b for a, b in zip(strokes, cached))


def rasterize_layer_density(
    layer_id: str,
    mask_data: MaskData,
    shape: Tuple[int, int],
    edits: Dict[str, Any],
    cache: Optional[LayerDensityCache] = None,
    include_live: bool = True,
) -> Optional[LayerDensity]:
    """Coat density of one layer at *shape*, or None if nothing is painted.

    Committed strokes are cached (incrementally, see LayerDensityCache); the
    in-progress ``live_stroke`` is composited on top fresh each call.
    """
    # Read each reference once: the GUI thread rebinds them, never mutates.
    strokes = tuple(mask_data.strokes)
    live = mask_data.live_stroke if include_live else None
    key = (mask_data.uid, tuple(shape), _geometry_key(edits))

    entry = cache.get(layer_id, key) if cache is not None else None
    if (
        entry is not None
        and len(entry[0]) == len(strokes)
        and _extends(strokes, entry[0])
    ):
        committed = entry[1]
    else:
        if entry is not None and _extends(strokes, entry[0]):
            committed = _apply_strokes(entry[1], strokes[len(entry[0]) :], edits, shape)
        else:
            committed = _apply_strokes(None, strokes, edits, shape)
        if cache is not None:
            cache.put(layer_id, key, strokes, committed)

    if live is None or not live.points:
        return committed
    return _apply_strokes(committed, [live], edits, shape)
