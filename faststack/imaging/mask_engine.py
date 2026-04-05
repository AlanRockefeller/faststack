# faststack/imaging/mask_engine.py
"""Mask rasterisation, refinement, and coordinate transforms.

Layer 2 of the mask subsystem.  Provides:
- forward_transform / inverse_transform  – pure coordinate helpers
- rasterize_strokes   – draw normalised strokes onto a pixel grid
- resolve_mask        – full pipeline: strokes → confidence → feather → clamp
- MaskRasterCache     – disposable, resolution-keyed cache for raster products
"""

import logging
import math
from typing import Any, Dict, Optional, Tuple

import numpy as np

from faststack.imaging.mask import DarkenSettings, MaskData

log = logging.getLogger(__name__)

# Optional dependency -------------------------------------------------------
try:
    import cv2
except ImportError:
    cv2 = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Coordinate transforms
# ---------------------------------------------------------------------------


def _geometry_hash(edits: Dict[str, Any]) -> int:
    """Hash of the geometry edits that affect mask alignment."""
    return hash(
        (
            edits.get("rotation", 0),
            round(float(edits.get("straighten_angle", 0.0)), 3),
            tuple(edits.get("crop_box") or ()),
        )
    )


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


def _interpolate_points(points: list, max_gap: float) -> list:
    """Insert intermediate points so no two consecutive points are more than
    *max_gap* pixels apart.  Prevents dotted strokes from fast mouse movement."""
    if len(points) <= 1:
        return points
    result = [points[0]]
    for i in range(1, len(points)):
        x0, y0 = result[-1]
        x1, y1 = points[i]
        dx, dy = x1 - x0, y1 - y0
        dist = math.sqrt(dx * dx + dy * dy)
        if dist > max_gap:
            n = int(math.ceil(dist / max_gap))
            for j in range(1, n):
                t = j / n
                result.append((x0 + dx * t, y0 + dy * t))
        result.append((x1, y1))
    return result


def _draw_stroke_numpy(
    canvas: np.ndarray,
    points: list,
    radius_px: float,
) -> None:
    """Draw a stroke onto *canvas* (H, W) using numpy distance computation."""
    h, w = canvas.shape
    if radius_px < 0.5:
        radius_px = 0.5

    # Interpolate to prevent gaps from fast mouse movement
    filled = _interpolate_points(points, max_gap=max(1.0, radius_px * 0.5))

    for px, py in filled:
        # Bounding box for this circle
        x0 = max(0, int(px - radius_px - 1))
        x1 = min(w, int(px + radius_px + 2))
        y0 = max(0, int(py - radius_px - 1))
        y1 = min(h, int(py + radius_px + 2))
        if x1 <= x0 or y1 <= y0:
            continue

        yy, xx = np.ogrid[y0:y1, x0:x1]
        dist_sq = (xx - px) ** 2 + (yy - py) ** 2
        inside = dist_sq <= radius_px**2
        canvas[y0:y1, x0:x1] = np.maximum(
            canvas[y0:y1, x0:x1], inside.astype(np.float32)
        )


def _draw_stroke_cv2(
    canvas: np.ndarray,
    points: list,
    radius_px: float,
) -> None:
    """Draw a stroke onto *canvas* using cv2.circle (faster)."""
    r = max(1, int(round(radius_px)))

    # Interpolate to prevent gaps from fast mouse movement
    filled = _interpolate_points(points, max_gap=max(1.0, r * 0.5))

    for px, py in filled:
        cv2.circle(canvas, (int(round(px)), int(round(py))), r, 1.0, -1)


def rasterize_strokes(
    mask_data: MaskData,
    shape: Tuple[int, int],
    edits: Dict[str, Any],
) -> Tuple[np.ndarray, np.ndarray]:
    """Rasterise all strokes to float32 (H, W) maps.

    Returns ``(add_map, protect_map)`` each in [0, 1].  ``add_map`` marks
    background hints; ``protect_map`` marks subject protection.

    Strokes are in oriented-base-image normalised coords and are
    forward-transformed to *shape* accounting for current geometry edits.
    """
    h, w = shape
    add_map = np.zeros((h, w), dtype=np.float32)
    protect_map = np.zeros((h, w), dtype=np.float32)

    draw_fn = _draw_stroke_cv2 if cv2 is not None else _draw_stroke_numpy

    for stroke in mask_data.strokes:
        # Transform stroke points to pixel coords in target array
        pixel_points = []
        for xn, yn in stroke.points:
            px, py = forward_transform(xn, yn, edits, shape)
            pixel_points.append((px, py))

        # Radius in pixels (normalised radius × image diagonal for consistency)
        diag = math.sqrt(w * w + h * h)
        radius_px = stroke.radius * diag

        target = add_map if stroke.stroke_type == "add" else protect_map
        draw_fn(target, pixel_points, radius_px)

    # Clamp
    np.clip(add_map, 0.0, 1.0, out=add_map)
    np.clip(protect_map, 0.0, 1.0, out=protect_map)
    return add_map, protect_map


# ---------------------------------------------------------------------------
# Gaussian blur helper
# ---------------------------------------------------------------------------


def _gaussian_blur(arr: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur a 2-D float32 array."""
    if sigma < 0.5:
        return arr
    if cv2 is not None:
        ksize = int(math.ceil(sigma * 6)) | 1  # odd kernel
        return cv2.GaussianBlur(arr, (ksize, ksize), sigma)

    # Numpy-only fallback: separable 1-D convolution
    radius = int(math.ceil(sigma * 3))
    x = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    # Pad + convolve rows then columns

    out = arr.copy()
    for row in range(out.shape[0]):
        out[row, :] = np.convolve(out[row, :], kernel, mode="same")
    for col in range(out.shape[1]):
        out[:, col] = np.convolve(out[:, col], kernel, mode="same")
    return out


# ---------------------------------------------------------------------------
# Confidence map builders
# ---------------------------------------------------------------------------


def _dark_prior(image_arr: np.ndarray, dark_range: float) -> np.ndarray:
    """Higher confidence for darker pixels."""
    luma = image_arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    # smoothstep: 1 below lo, 0 above hi
    lo = dark_range * 0.3
    hi = max(lo + 0.01, dark_range * 0.7)
    t = np.clip((luma - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - t  # dark pixels → 1.0


def _neutral_prior(image_arr: np.ndarray, sensitivity: float) -> np.ndarray:
    """Higher confidence for low-chroma (neutral / grey) pixels."""
    cmax = image_arr.max(axis=2)
    cmin = image_arr.min(axis=2)
    chroma = cmax - cmin
    lo = 0.05
    hi = max(lo + 0.01, 0.15 * max(0.1, sensitivity))
    t = np.clip((chroma - lo) / (hi - lo), 0.0, 1.0)
    return 1.0 - t  # neutral → 1.0


def _border_prior(
    shape: Tuple[int, int], border_width_frac: float = 0.05
) -> np.ndarray:
    """Distance-from-border prior — pixels near edges get higher confidence."""
    h, w = shape
    bw = max(1, int(min(h, w) * border_width_frac))
    prior = np.zeros((h, w), dtype=np.float32)
    prior[:bw, :] = 1.0
    prior[-bw:, :] = 1.0
    prior[:, :bw] = 1.0
    prior[:, -bw:] = 1.0
    return prior


def _edge_magnitude(image_arr: np.ndarray) -> np.ndarray:
    """Gradient magnitude for edge stopping."""
    luma = image_arr @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    if cv2 is not None:
        gx = cv2.Sobel(luma, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(luma, cv2.CV_32F, 0, 1, ksize=3)
        mag = np.sqrt(gx**2 + gy**2)
    else:
        # Simple numpy gradient
        gy, gx = np.gradient(luma)
        mag = np.sqrt(gx**2 + gy**2)
    # Normalise to [0, 1]
    m = mag.max()
    if m > 1e-6:
        mag /= m
    return mag


# ---------------------------------------------------------------------------
# Image-content in-process cache key for cache keying
# ---------------------------------------------------------------------------


def _image_content_key(image_arr: np.ndarray) -> int:
    """Fast in-process cache key for resolved-mask cache invalidation.

    Priors (dark, neutral, edge) depend on image content, so the cache must
    be invalidated when edits change the image (exposure, WB, levels, etc.).

    Samples a 4×4 spatial grid across all channels and hashes the raw bytes.
    This catches both global adjustments and localised edits far more
    reliably than a handful of single-channel pixel reads.
    """
    h, w = image_arr.shape[:2]
    # 4 evenly-spaced row/col indices (always includes first and last)
    rows = [0, h // 3, 2 * h // 3, h - 1]
    cols = [0, w // 3, 2 * w // 3, w - 1]
    samples = b"".join(image_arr[r, c].tobytes() for r in rows for c in cols)
    return hash(samples)


# ---------------------------------------------------------------------------
# Mask resolution pipeline
# ---------------------------------------------------------------------------


def resolve_mask(
    mask_data: MaskData,
    settings: DarkenSettings,
    image_arr: np.ndarray,
    shape: Tuple[int, int],
    edits: Dict[str, Any],
    cache: Optional["MaskRasterCache"] = None,
) -> np.ndarray:
    """Full mask resolution: strokes → confidence → feather → soft mask.

    Returns float32 (H, W) in [0, 1] where 1.0 = full background effect.

    *image_arr* is the current sRGB float32 (H, W, 3) image used for
    edge-aware analysis.  *shape* must match ``image_arr.shape[:2]``.
    """
    geo_hash = _geometry_hash(edits)
    params_key = settings.params_tuple()
    img_key = _image_content_key(image_arr)

    if cache is not None:
        cached = cache.get_resolved(
            mask_data.revision, shape, geo_hash, params_key, img_key
        )
        if cached is not None:
            return cached

    # --- Rasterise strokes (may hit stroke cache) ---
    if cache is not None:
        stroke_maps = cache.get_strokes(mask_data.revision, shape, geo_hash)
    else:
        stroke_maps = None

    if stroke_maps is None:
        add_map, protect_map = rasterize_strokes(mask_data, shape, edits)
        if cache is not None:
            cache.put_strokes(
                mask_data.revision, shape, geo_hash, (add_map, protect_map)
            )
    else:
        add_map, protect_map = stroke_maps

    # --- Build auto priors based on mode ---
    mode = settings.mode
    auto_prior = np.zeros(shape, dtype=np.float32)

    # Pre-compute edges if needed by either auto priors or final edge protection.
    edges = None
    if (
        mode != "paint_only" and settings.auto_from_edges > 0.01
    ) or settings.edge_protection > 0.01:
        edges = _edge_magnitude(image_arr)

    if mode != "paint_only":
        # Dark prior
        dp = _dark_prior(image_arr, settings.dark_range)

        if mode == "border_auto":
            # Border connectivity: combine border seed with dark prior
            bp = _border_prior(shape)
            bp_blurred = _gaussian_blur(bp, sigma=min(shape) * 0.05)
            auto_prior = dp * 0.5 + bp_blurred * 0.5
        elif mode == "strong_subject":
            # Strong subject protection — use dark prior but weight protect more
            auto_prior = dp * 0.3
        else:
            # "assisted" — balanced
            auto_prior = dp * 0.4

        # Neutrality prior blended in
        if settings.neutrality_sensitivity > 0.01:
            np_arr = _neutral_prior(image_arr, settings.neutrality_sensitivity)
            auto_prior = (
                auto_prior * 0.6 + np_arr * 0.4 * settings.neutrality_sensitivity
            )

        # Edge-aware prior: areas between strong edges are likely uniform
        # background, so use inverted edge magnitude as a positive signal.
        if settings.auto_from_edges > 0.01:
            # edges is already computed above
            # Blur the edge map so the "between edges" regions fill in
            edge_blurred = _gaussian_blur(edges, sigma=min(shape) * 0.02)
            # Invert: low-edge (smooth) regions get high confidence
            edge_prior = 1.0 - edge_blurred
            w = settings.auto_from_edges
            auto_prior = auto_prior * (1.0 - w) + edge_prior * w

    # --- Combine signals ---
    # Background confidence = user strokes + auto prior where user hasn't painted
    raw_bg = np.clip(add_map + auto_prior * (1.0 - add_map), 0.0, 1.0)

    # Subject protection
    sp_weight = settings.subject_protection
    if mode == "strong_subject":
        sp_weight = min(1.0, sp_weight + 0.3)

    raw_bg = raw_bg * (1.0 - protect_map * sp_weight)

    # --- Edge stopping ---
    if settings.edge_protection > 0.01:
        # edges is already computed above
        # Reduce mask confidence at strong edges
        edge_brake = 1.0 - edges * settings.edge_protection
        raw_bg = raw_bg * np.clip(edge_brake, 0.0, 1.0)

    # --- Feather / blur ---
    feather_sigma = settings.feather * min(shape) * 0.03
    if feather_sigma > 0.5:
        raw_bg = _gaussian_blur(raw_bg, feather_sigma)

    # --- Expand / contract ---
    ec = settings.expand_contract
    if abs(ec) > 0.01 and cv2 is not None:
        ksize = max(3, int(abs(ec) * min(shape) * 0.02)) | 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
        if ec > 0:
            raw_bg = cv2.dilate(raw_bg, kernel)
        else:
            raw_bg = cv2.erode(raw_bg, kernel)

    # --- Final clamp ---
    result = np.clip(raw_bg, 0.0, 1.0)

    if cache is not None:
        cache.put_resolved(
            mask_data.revision, shape, geo_hash, params_key, img_key, result
        )

    return result


# ---------------------------------------------------------------------------
# Disposable raster cache
# ---------------------------------------------------------------------------


class MaskRasterCache:
    """Resolution-keyed cache for disposable raster products.

    Keeps one stroke-map entry and one resolved-mask entry at a time.
    Preview and export resolutions have different shapes, so they
    naturally miss and recompute — no stale cross-contamination.
    """

    def __init__(self):
        self._stroke_key: Optional[tuple] = None
        self._stroke_maps: Optional[Tuple[np.ndarray, np.ndarray]] = None

        self._resolved_key: Optional[tuple] = None
        self._resolved_mask: Optional[np.ndarray] = None

    def clear(self) -> None:
        self._stroke_key = None
        self._stroke_maps = None
        self._resolved_key = None
        self._resolved_mask = None

    # stroke maps

    def get_strokes(
        self,
        revision: int,
        shape: Tuple[int, int],
        geo_hash: int,
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        key = (revision, shape, geo_hash)
        if self._stroke_key == key:
            return self._stroke_maps
        return None

    def put_strokes(
        self,
        revision: int,
        shape: Tuple[int, int],
        geo_hash: int,
        maps: Tuple[np.ndarray, np.ndarray],
    ) -> None:
        self._stroke_key = (revision, shape, geo_hash)
        self._stroke_maps = maps

    # resolved mask

    def get_resolved(
        self,
        revision: int,
        shape: Tuple[int, int],
        geo_hash: int,
        params_key: tuple,
        img_key: int,
    ) -> Optional[np.ndarray]:
        key = (revision, shape, geo_hash, params_key, img_key)
        if self._resolved_key == key:
            return self._resolved_mask
        return None

    def put_resolved(
        self,
        revision: int,
        shape: Tuple[int, int],
        geo_hash: int,
        params_key: tuple,
        img_key: int,
        mask: np.ndarray,
    ) -> None:
        self._resolved_key = (revision, shape, geo_hash, params_key, img_key)
        self._resolved_mask = mask
