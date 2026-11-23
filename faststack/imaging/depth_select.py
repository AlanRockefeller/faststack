# faststack/imaging/depth_select.py
"""Background selection from an estimated depth map.

A *depth layer* is an ordinary dodge & burn layer (a recipe plus a stroke
mask, see ``faststack.imaging.local_adjust``) whose mask also selects the
part of the photo farther from the camera than a cutoff::

    {"id": ..., "visible": ..., "recipe": {...},
     "depth": {"amount": 0.35, "feather": 0.3, "keep_sharp": 1.0}}

``amount`` (0..1) is how much of the scene, back to front, is selected: 0
selects nothing, 1 everything, and in between the cutoff sweeps from the
farthest background towards the camera.  ``feather`` (0..1) is how gradually
the selection fades in across the cutoff.  ``keep_sharp`` (0..1) is how much
in-focus detail is kept out of the selection: in a focus stack the subject is
sharp and the background is not, which catches thin, sharp parts (stems,
hairs) that the depth model sees as half background.

The depth comes from ``faststack.ai.depth`` (a model that estimates relative
depth from the single photo) and is held as a ``DepthMap``: nearness 0..1 in
the oriented base image's space, upsampled against the photo's colours so the
cutoff follows the subject's outline, plus a sharpness map (0..1) of the same
size.  It is computed once per photo by the
controller (``faststack.controller.depth_select``) or, when a save needs one
the preview never got, by the save worker; it is not stored with the edit.

The layer's mask is::

    min(1, selection * (1 - erased) + painted)

so erase strokes cut holes in the selection and paint strokes add to it (a
depth layer is one coat: painting over what is already selected does not
double the recipe).  Until the depth map exists only the strokes count.
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from faststack.imaging.mask import MaskData
from faststack.imaging.mask_engine import (
    LayerDensity,
    LayerDensityCache,
    rasterize_layer_density,
    rasterize_layer_erased,
)
from faststack.imaging.optional_deps import get_cv2

log = logging.getLogger(__name__)

DEFAULT_AMOUNT = 0.35
DEFAULT_FEATHER = 0.3
DEFAULT_KEEP_SHARP = 1.0

# Long edge of the stored map.  The model's own output is about 518 pixels;
# it is upsampled to this and edge-sharpened against the photo.
DEPTH_MAP_LONG_EDGE = 1536

# Width of the cutoff's soft edge, in nearness units, at feather 0 and 1.
_FEATHER_MIN = 0.02
_FEATHER_MAX = 0.4

# Fine detail (mean absolute difference from a 1-pixel blur of the luminance,
# at DEPTH_MAP_LONG_EDGE, 0..1 units) below which a pixel counts as not sharp
# and above which as fully sharp.  A black or blurred background measures
# about 0.001, a slightly soft leaf 0.005, stems, bark and moss in focus
# 0.014 and up.  Smooth surfaces in focus (a cap's middle) measure little, but
# the depth keeps those out of the selection.
_SHARP_LO = 0.005
_SHARP_HI = 0.012

# Joint bilateral upsampling: low-resolution neighbours on each side, and how
# quickly a neighbour's weight falls with colour difference (sRGB 0..1).
_JBU_RADIUS = 2
_JBU_SIGMA_COLOR = 0.08
_JBU_SIGMA_SPACE = 1.0

_depth_uids = itertools.count(1)


# ---------------------------------------------------------------------------
# Layer helpers
# ---------------------------------------------------------------------------


def _unit(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return min(1.0, max(0.0, f))


def normalize_depth_params(raw: Any) -> Optional[Dict[str, float]]:
    """Clean ``layer["depth"]`` value, or None when *raw* is not one."""
    if not isinstance(raw, dict):
        return None
    return {
        "amount": _unit(raw.get("amount"), DEFAULT_AMOUNT),
        "feather": _unit(raw.get("feather"), DEFAULT_FEATHER),
        "keep_sharp": _unit(raw.get("keep_sharp"), DEFAULT_KEEP_SHARP),
    }


def is_depth_layer(layer: Any) -> bool:
    return isinstance(layer, dict) and isinstance(layer.get("depth"), dict)


def depth_layers(
    edits: Dict[str, Any], include_hidden: bool = False
) -> List[Dict[str, Any]]:
    """The edit's depth layers (visible ones unless *include_hidden*)."""
    return [
        layer
        for layer in edits.get("local_layers") or ()
        if is_depth_layer(layer) and (include_hidden or layer.get("visible", True))
    ]


def edits_need_depth_map(edits: Dict[str, Any]) -> bool:
    """True when rendering *edits* reads the depth map: a visible depth layer
    that moves a slider and selects something."""
    return any(
        layer.get("recipe") and layer["depth"].get("amount", 0.0) > 0.0
        for layer in depth_layers(edits)
    )


# ---------------------------------------------------------------------------
# Depth map
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DepthMap:
    """Nearness of the oriented base image, 0 (farthest) .. 1 (nearest).

    ``near`` is float32 ``(h, w)`` with the base image's aspect ratio, never
    mutated; ``sharp`` (same shape, or None) is how in focus each pixel is,
    0..1.  ``base_shape`` is the ``(H, W)`` of the image it was computed
    from; ``uid`` identifies this map in render cache keys.
    """

    near: np.ndarray
    base_shape: Tuple[int, int]
    sharp: Optional[np.ndarray] = None
    uid: int = dataclasses.field(default_factory=lambda: next(_depth_uids))


def sharpness_map(rgb: np.ndarray) -> np.ndarray:
    """How in focus each pixel of *rgb* (float32 sRGB 0..1) is, 0..1.

    Fine detail is the luminance's mean absolute difference from a 1-pixel
    blur; an out-of-focus background has almost
    none whatever its colour, while anything in a focus stack's sharp range
    (stems, bark, hairs, the outline of a cap) has plenty.
    """
    cv2 = get_cv2()
    lum = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    detail = np.abs(lum - cv2.GaussianBlur(lum, (0, 0), 1.0))
    # Averaged over little more than a pixel, so it stops at an outline
    # instead of reaching into the background beside it.
    energy = cv2.GaussianBlur(detail, (0, 0), 1.0)
    t = np.clip((energy - _SHARP_LO) * np.float32(1.0 / (_SHARP_HI - _SHARP_LO)), 0.0, 1.0)
    sharp = (t * t * (3.0 - 2.0 * t)).astype(np.float32, copy=False)
    # Close small gaps (smoother patches of bark among the texture) without
    # moving an object's outline: dilate then erode by the same disc.
    size = max(3, int(round(max(rgb.shape[:2]) / 100)) | 1)
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    return cv2.morphologyEx(sharp, cv2.MORPH_CLOSE, disc)


def _regional_sharpness(rgb: np.ndarray, sharp: np.ndarray) -> np.ndarray:
    """*sharp* averaged over regions of similar colour.

    In-focus bark or soil is sharp only in patches at the pixel level; its
    smoother patches would otherwise be left out of what Keep sharp keeps,
    speckling the selection.  Averaged over a coarse grid and brought back
    up following the photo's colours, a patchy textured area reads as
    mostly sharp, while the colour guidance keeps it from spreading onto a
    differently coloured background beside it.  Run at half the map's
    resolution: the result is smooth anyway.
    """
    cv2 = get_cv2()
    h, w = sharp.shape
    hh, hw = max(1, h // 2), max(1, w // 2)
    ch, cw = max(1, h // 8), max(1, w // 8)
    if ch >= hh or cw >= hw:
        return sharp
    guide = cv2.resize(rgb, (hw, hh), interpolation=cv2.INTER_AREA)
    coarse = cv2.resize(sharp, (cw, ch), interpolation=cv2.INTER_AREA)
    up = joint_bilateral_upsample(guide, coarse.astype(np.float32))
    return cv2.resize(up, (w, h), interpolation=cv2.INTER_LINEAR)


def joint_bilateral_upsample(
    guide: np.ndarray, low: np.ndarray, confidence: Optional[np.ndarray] = None
) -> np.ndarray:
    """Upsample the channels of *low* to *guide*'s size, following its colours.

    *guide* is float32 RGB ``(H, W, 3)``; *low* float32 ``(h, w)`` or
    ``(h, w, c)`` with c <= 4, the same aspect ratio and smaller.  Each output
    pixel averages the low-resolution samples around it, weighted by distance
    and by how close the guide's colour there is to the sample's (Kopf et al.,
    "Joint Bilateral Upsampling", 2007).  A cap's rim therefore takes the
    cap's value and the black background beside it the background's, however
    blurred the low-resolution edge.  *confidence* ``(h, w)`` (default 1)
    scales each sample's weight.
    """
    cv2 = get_cv2()
    H, W = guide.shape[:2]
    h, w = low.shape[:2]
    single = low.ndim == 2
    low3 = low[..., None] if single else low
    channels = low3.shape[2]
    guide_low = cv2.resize(guide, (w, h), interpolation=cv2.INTER_AREA)
    # One remap gathers the colour and up to one value; values and the
    # confidence go in a second.
    extra = [low3[..., i] for i in range(channels)]
    if confidence is not None:
        extra.append(confidence)
    if len(extra) > 4:
        raise ValueError("too many channels")
    packed_a = np.ascontiguousarray(guide_low, dtype=np.float32)
    packed_b = np.ascontiguousarray(np.dstack(extra) if len(extra) > 1 else extra[0], dtype=np.float32)

    fy = (np.arange(H, dtype=np.float32) + 0.5) * np.float32(h / H) - 0.5
    fx = (np.arange(W, dtype=np.float32) + 0.5) * np.float32(w / W) - 0.5
    iy0, ix0 = np.round(fy), np.round(fx)
    r, g, b = guide[..., 0], guide[..., 1], guide[..., 2]
    acc = np.zeros((H, W, channels), np.float32)
    wsum = np.zeros((H, W), np.float32)
    inv_c = np.float32(-0.5 / _JBU_SIGMA_COLOR**2)
    inv_s = np.float32(-0.5 / _JBU_SIGMA_SPACE**2)
    for dy in range(-_JBU_RADIUS, _JBU_RADIUS + 1):
        qy = np.clip(iy0 + dy, 0, h - 1).astype(np.float32)
        ly = ((qy - fy) ** 2 * inv_s)[:, None]
        my = np.broadcast_to(qy[:, None], (H, W))
        for dx in range(-_JBU_RADIUS, _JBU_RADIUS + 1):
            qx = np.clip(ix0 + dx, 0, w - 1).astype(np.float32)
            lx = ((qx - fx) ** 2 * inv_s)[None, :]
            mx = np.broadcast_to(qx[None, :], (H, W))
            col = cv2.remap(packed_a, mx, my, cv2.INTER_NEAREST)
            val = cv2.remap(packed_b, mx, my, cv2.INTER_NEAREST)
            if val.ndim == 2:
                val = val[..., None]
            d2 = (col[..., 0] - r) ** 2 + (col[..., 1] - g) ** 2 + (col[..., 2] - b) ** 2
            wgt = np.exp(d2 * inv_c + ly + lx)
            if confidence is not None:
                wgt *= val[..., channels]
            acc += wgt[..., None] * val[..., :channels]
            wsum += wgt
    out = acc / np.maximum(wsum, np.float32(1e-12))[..., None]
    return out[..., 0] if single else out


def compute_depth_map(
    base: np.ndarray,
    estimate: Callable[[np.ndarray], np.ndarray],
    cancelled: Optional[Callable[[], bool]] = None,
) -> Optional[DepthMap]:
    """Estimate, normalise and refine the depth of *base*, and measure its
    sharpness.

    *base* is the oriented base image (float32 RGB 0..1, HxWx3); *estimate*
    returns relative inverse depth for it at any resolution with the same
    aspect ratio (``DepthEstimator.estimate``).  Deterministic: the preview
    and a save that has to compute its own map get the same one.  Returns
    None when *cancelled* says to stop.
    """
    cv2 = get_cv2()
    if cv2 is None:
        raise RuntimeError("OpenCV is not available")
    h, w = base.shape[:2]
    raw = np.asarray(estimate(base), dtype=np.float32)
    if raw.ndim != 2 or not raw.size:
        raise RuntimeError(f"unexpected depth output shape {raw.shape}")
    if cancelled is not None and cancelled():
        return None
    raw = np.nan_to_num(raw, nan=0.0, posinf=0.0, neginf=0.0)
    # Robust range: a few stray extremes must not squeeze everything else
    # into a sliver of the slider.
    lo, hi = np.percentile(raw, (0.5, 99.5))
    if hi - lo < 1e-6:
        near_small = np.zeros_like(raw)  # no depth to speak of: all "far"
    else:
        near_small = np.clip((raw - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)

    scale = min(1.0, DEPTH_MAP_LONG_EDGE / max(h, w, 1))
    mh, mw = max(1, round(h * scale)), max(1, round(w * scale))
    # No copy for the usual float32 RGB base. Shrink before clipping, so the
    # only full-size buffer is the photo itself; clip in place only on the
    # shrunk copy, never on the caller's base.
    rgb = np.ascontiguousarray(base[..., :3], dtype=np.float32)
    if (mh, mw) != (h, w):
        rgb = cv2.resize(rgb, (mw, mh), interpolation=cv2.INTER_AREA)
        np.clip(rgb, 0.0, 1.0, out=rgb)
    else:
        rgb = np.clip(rgb, 0.0, 1.0)
    sharp = sharpness_map(rgb)
    if cancelled is not None and cancelled():
        return None
    if near_small.shape[0] >= mh or near_small.shape[1] >= mw:
        # Tiny image: the model ran at least at full size.
        near = cv2.resize(near_small, (mw, mh), interpolation=cv2.INTER_AREA)
    else:
        sh, sw = near_small.shape
        # Samples on a depth edge mix both sides; trust them less.
        gx = cv2.Sobel(near_small, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(near_small, cv2.CV_32F, 0, 1, ksize=3)
        confidence = (1.0 / (1.0 + (np.hypot(gx, gy) * 10.0) ** 2) + 0.05).astype(
            np.float32
        )
        # Sharpness goes through the same colour-guided resampling so it does
        # not reach past an object's outline into the background beside it.
        sharp_small = cv2.resize(sharp, (sw, sh), interpolation=cv2.INTER_AREA)
        both = joint_bilateral_upsample(
            rgb, np.dstack([near_small, sharp_small]), confidence
        )
        near = both[..., 0]
        sharp = np.minimum(sharp, both[..., 1])
    if cancelled is not None and cancelled():
        return None
    sharp = np.maximum(sharp, _regional_sharpness(rgb, sharp))
    near = np.clip(near, 0.0, 1.0).astype(np.float32, copy=False)
    sharp = np.clip(sharp, 0.0, 1.0).astype(np.float32, copy=False)
    near.setflags(write=False)
    sharp.setflags(write=False)
    return DepthMap(near=near, base_shape=(int(h), int(w)), sharp=sharp)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def selection_from_near(near: np.ndarray, amount: float, feather: float) -> np.ndarray:
    """Selection weight 0..1 for nearness values *near* (a new array)."""
    width = _FEATHER_MIN + (_FEATHER_MAX - _FEATHER_MIN) * feather
    # The soft edge spans [upper - width, upper] in nearness; amount 0 puts
    # it wholly below 0 (nothing selected), amount 1 wholly above 1.
    upper = amount * (1.0 + width)
    t = np.clip((upper - near) * np.float32(1.0 / width), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32, copy=False)


def depth_selection(
    depth_map: Optional[DepthMap],
    params: Dict[str, float],
    shape: Tuple[int, int],
    point_transform: np.ndarray,
) -> Optional[LayerDensity]:
    """The selected-background weight at *shape*, cropped to where it is
    non-zero, or None when nothing is selected.

    *point_transform* maps base-normalised ``(x, y, 1)`` to pixel coordinates
    of the ``shape`` array (the renderer's geometry: rotation, straighten,
    crop and scale).  The map is warped first and thresholded after, so the
    selection's edge stays crisp at every render size.
    """
    cv2 = get_cv2()
    amount = float(params.get("amount", 0.0))
    if depth_map is None or cv2 is None or amount <= 0.0:
        return None
    h, w = shape
    keep = float(params.get("keep_sharp", 0.0))
    near_src = depth_map.near
    sharp_src = depth_map.sharp if keep > 0.0 else None

    def warp_matrix(dh: int, dw: int) -> np.ndarray:
        # map index -> map continuous -> base-normalised -> target -> target index
        return (
            np.array([[1.0, 0.0, -0.5], [0.0, 1.0, -0.5], [0.0, 0.0, 1.0]])
            @ np.asarray(point_transform, dtype=np.float64)
            @ np.diag([1.0 / dw, 1.0 / dh, 1.0])
            @ np.array([[1.0, 0.0, 0.5], [0.0, 1.0, 0.5], [0.0, 0.0, 1.0]])
        )

    m = warp_matrix(*near_src.shape)
    shrink = math.sqrt(abs(np.linalg.det(m[:2, :2])))
    if shrink < 0.75:
        # warpAffine has no area filter (INTER_AREA silently samples like
        # INTER_LINEAR), so a small preview would point-sample the detailed
        # sharpness map and speckle. Average down to about the target scale
        # first; the warp then only resamples by ~1x.
        dh, dw = near_src.shape
        size = (max(1, round(dw * shrink)), max(1, round(dh * shrink)))
        near_src = cv2.resize(near_src, size, interpolation=cv2.INTER_AREA)
        if sharp_src is not None:
            sharp_src = cv2.resize(sharp_src, size, interpolation=cv2.INTER_AREA)
        m = warp_matrix(*near_src.shape)
    near = cv2.warpAffine(
        near_src,
        m[:2],
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    sel = selection_from_near(near, amount, float(params.get("feather", 0.0)))
    if sharp_src is not None:
        sharp = cv2.warpAffine(
            sharp_src,
            m[:2],
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        sel *= 1.0 - np.float32(keep) * sharp
    rows = np.flatnonzero(sel.max(axis=1) > 0.0)
    if not rows.size:
        return None
    cols = np.flatnonzero(sel.max(axis=0) > 0.0)
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    return LayerDensity(y0, y1, x0, x1, sel[y0:y1, x0:x1])


def _paste(out: np.ndarray, y0: int, x0: int, part: LayerDensity):
    return out[part.y0 - y0 : part.y1 - y0, part.x0 - x0 : part.x1 - x0]


def depth_layer_density(
    layer: Dict[str, Any],
    mask_data: Optional[MaskData],
    shape: Tuple[int, int],
    edits: Dict[str, Any],
    depth_map: Optional[DepthMap],
    point_transform: np.ndarray,
    cache: Optional[LayerDensityCache] = None,
    base_shape: Optional[Tuple[int, int]] = None,
    include_live: bool = True,
) -> Optional[LayerDensity]:
    """The mask of a depth layer at *shape* (see the module docstring)."""
    layer_id = layer["id"]
    painted = erased = None
    if mask_data is not None:
        painted = rasterize_layer_density(
            layer_id,
            mask_data,
            shape,
            edits,
            cache=cache,
            include_live=include_live,
            base_shape=base_shape,
            point_transform=point_transform,
        )
    selection = depth_selection(depth_map, layer["depth"], shape, point_transform)
    if selection is not None and mask_data is not None:
        erased = rasterize_layer_erased(
            layer_id,
            mask_data,
            shape,
            edits,
            cache=cache,
            include_live=include_live,
            base_shape=base_shape,
            point_transform=point_transform,
        )
    if selection is None:
        if painted is None:
            return None
        return LayerDensity(
            painted.y0, painted.y1, painted.x0, painted.x1, np.minimum(painted.arr, 1.0)
        )
    if erased is not None:
        # selection.arr is this call's own array: scale it in place over the
        # overlap of the two boxes.
        oy0, oy1 = max(selection.y0, erased.y0), min(selection.y1, erased.y1)
        ox0, ox1 = max(selection.x0, erased.x0), min(selection.x1, erased.x1)
        if oy1 > oy0 and ox1 > ox0:
            selection.arr[
                oy0 - selection.y0 : oy1 - selection.y0,
                ox0 - selection.x0 : ox1 - selection.x0,
            ] *= 1.0 - erased.arr[
                oy0 - erased.y0 : oy1 - erased.y0, ox0 - erased.x0 : ox1 - erased.x0
            ]
    if painted is None:
        return selection
    y0, y1 = min(selection.y0, painted.y0), max(selection.y1, painted.y1)
    x0, x1 = min(selection.x0, painted.x0), max(selection.x1, painted.x1)
    out = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
    _paste(out, y0, x0, selection)[...] = selection.arr
    region = _paste(out, y0, x0, painted)
    region += painted.arr
    np.minimum(out, 1.0, out=out)
    return LayerDensity(y0, y1, x0, x1, out)
