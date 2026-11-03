# faststack/imaging/local_adjust.py
"""Local (dodge & burn) adjustment layers.

A layer is a *recipe* — slider deltas such as ``{"exposure": 0.4,
"vibrance": 0.1}`` in edit units — plus a brush-stroke mask
(``MaskData`` keyed by the layer id).  Each paint stroke deposits one coat;
coats add up without limit.  At every pixel a slider's effective value is::

    global_value + sum(layer.density(pixel) * layer.recipe[key])

so two coats of Exposure +20 render exactly as Exposure +40 there.

Layers live in the edit dict as ``edits["local_layers"]``: a list of plain
JSON-safe dicts ``{"id": str, "visible": bool, "recipe": {key: float}}``.
The list and its dicts are treated as immutable — the controller replaces
them on every change — so shallow edit snapshots are safe to hand to the
render and save workers.

``ImageEditor._apply_edits`` renders each pipeline stage globally, then
re-renders only the bounding box of painted pixels with per-pixel parameter
maps (the vectorised stage mirrors below) and pastes it back.
"""

import dataclasses
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from faststack.imaging.mask import MaskData
from faststack.imaging.mask_engine import LayerDensityCache, rasterize_layer_density

log = logging.getLogger(__name__)

COLOR_MIX_LOCAL_KEYS: Tuple[str, ...] = tuple(
    f"color_sat_{name}"
    for name in (
        "red",
        "orange",
        "yellow",
        "green",
        "aqua",
        "blue",
        "purple",
        "magenta",
    )
)

# Every compact-editor slider can be part of a recipe.
LOCAL_ADJUST_KEYS: Tuple[str, ...] = (
    "exposure",
    "brightness",
    "contrast",
    "whites",
    "shadows",
    "blacks",
    "white_balance_by",
    "white_balance_mg",
    "vibrance",
    *COLOR_MIX_LOCAL_KEYS,
)

# Recipe keys rendered in linear light (pipeline steps 6-8); the rest are
# sRGB-stage keys (steps 14-19).
LOCAL_GAIN_KEYS = frozenset({"white_balance_by", "white_balance_mg", "exposure"})
LOCAL_LINEAR_KEYS = LOCAL_GAIN_KEYS | {"shadows"}
LOCAL_SRGB_KEYS = frozenset(LOCAL_ADJUST_KEYS) - LOCAL_LINEAR_KEYS

_EPS = 1e-6


# ---------------------------------------------------------------------------
# Layer list helpers
# ---------------------------------------------------------------------------


def normalize_recipe(raw: Any) -> Dict[str, float]:
    recipe: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return recipe
    for key, value in raw.items():
        if key not in LOCAL_ADJUST_KEYS:
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and abs(f) > _EPS:
            recipe[key] = f
    return recipe


def normalize_local_layers(value: Any) -> List[Dict[str, Any]]:
    """Validate a deserialised ``local_layers`` value into clean layer dicts."""
    if not isinstance(value, (list, tuple)):
        return []
    layers: List[Dict[str, Any]] = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        layer_id = str(item.get("id") or "")
        if not layer_id or layer_id in seen:
            continue
        seen.add(layer_id)
        layers.append(
            {
                "id": layer_id,
                "visible": bool(item.get("visible", True)),
                "recipe": normalize_recipe(item.get("recipe")),
            }
        )
    return layers


def effective_layers(edits: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Visible layers whose recipe moves at least one slider."""
    layers = edits.get("local_layers") or ()
    return [
        layer
        for layer in layers
        if isinstance(layer, dict) and layer.get("visible", True) and layer.get("recipe")
    ]


def layers_use_keys(edits: Dict[str, Any], keys: Iterable[str]) -> bool:
    keys = frozenset(keys)
    return any(keys.intersection(layer["recipe"]) for layer in effective_layers(edits))


# ---------------------------------------------------------------------------
# Per-render parameter maps
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class LocalAdjustments:
    """Per-pixel recipe deltas over the bounding box of painted pixels."""

    y0: int
    y1: int
    x0: int
    x1: int
    maps: Dict[str, np.ndarray]  # key -> float32 (y1-y0, x1-x0) delta map
    linear_identity: tuple  # changes whenever linear-stage deltas could

    @property
    def roi(self) -> Tuple[slice, slice]:
        return slice(self.y0, self.y1), slice(self.x0, self.x1)

    def has_any(self, keys: Iterable[str]) -> bool:
        return any(key in self.maps for key in keys)

    def value(self, key: str, global_value: float) -> np.ndarray:
        """Effective per-pixel value of *key* over the ROI."""
        delta = self.maps.get(key)
        g = np.float32(global_value)
        if delta is None:
            return np.full((self.y1 - self.y0, self.x1 - self.x0), g, np.float32)
        return delta + g


def build_local_adjustments(
    edits: Dict[str, Any],
    mask_assets: Dict[str, MaskData],
    shape: Tuple[int, int],
    cache: Optional[LayerDensityCache] = None,
) -> Optional[LocalAdjustments]:
    """Rasterise every effective layer at *shape* into recipe delta maps."""
    layers = effective_layers(edits)
    if cache is not None:
        cache.prune({layer.get("id") for layer in edits.get("local_layers") or ()})
    if not layers:
        return None

    painted = []
    for layer in layers:
        mask_data = mask_assets.get(layer["id"])
        if mask_data is None:
            continue
        density = rasterize_layer_density(
            layer["id"], mask_data, shape, edits, cache=cache
        )
        if density is not None:
            painted.append((layer, mask_data, density))
    if not painted:
        return None

    y0 = min(d.y0 for _, _, d in painted)
    y1 = max(d.y1 for _, _, d in painted)
    x0 = min(d.x0 for _, _, d in painted)
    x1 = max(d.x1 for _, _, d in painted)
    maps: Dict[str, np.ndarray] = {}
    linear_identity = []
    for layer, mask_data, density in painted:
        sub = (
            slice(density.y0 - y0, density.y1 - y0),
            slice(density.x0 - x0, density.x1 - x0),
        )
        for key, delta in layer["recipe"].items():
            target = maps.get(key)
            if target is None:
                target = maps[key] = np.zeros((y1 - y0, x1 - x0), np.float32)
            target[sub] += density.arr * np.float32(delta)
        linear = tuple(
            sorted((k, v) for k, v in layer["recipe"].items() if k in LOCAL_LINEAR_KEYS)
        )
        if linear:
            live = mask_data.live_stroke
            linear_identity.append(
                (
                    layer["id"],
                    mask_data.revision,
                    len(mask_data.strokes),
                    len(live.points) if live is not None else -1,
                    linear,
                )
            )
    return LocalAdjustments(y0, y1, x0, x1, maps, tuple(linear_identity))


# ---------------------------------------------------------------------------
# Vectorised stage mirrors (keep in step with ImageEditor._apply_edits)
# ---------------------------------------------------------------------------


def _wb_gains_map(by: np.ndarray, mg: np.ndarray) -> np.ndarray:
    """Per-pixel ``_normalized_wb_gains``; inputs already scaled by 0.5."""
    r = 1.0 + by
    g = 1.0 - mg
    b = 1.0 - by
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    norm = np.where(luma > 1e-6, luma, 1.0)
    return np.stack((r / norm, g / norm, b / norm), axis=-1).astype(np.float32)


def local_linear_gain_ratio(
    edits: Dict[str, Any], local: LocalAdjustments
) -> Optional[np.ndarray]:
    """Per-pixel gain to multiply onto the ROI after global WB + exposure.

    WB and exposure are pure per-channel gains, so the local result is the
    global one times ``gain(global + delta) / gain(global)``.
    """
    from faststack.imaging.editor import _normalized_wb_gains

    ratio = None
    if local.has_any(("white_balance_by", "white_balance_mg")):
        g_by = float(edits.get("white_balance_by", 0.0))
        g_mg = float(edits.get("white_balance_mg", 0.0))
        total = _wb_gains_map(
            local.value("white_balance_by", g_by) * 0.5,
            local.value("white_balance_mg", g_mg) * 0.5,
        )
        if abs(g_by * 0.5) > 0.001 or abs(g_mg * 0.5) > 0.001:
            applied = np.asarray(
                _normalized_wb_gains(g_by * 0.5, g_mg * 0.5), dtype=np.float32
            )
            total /= applied
        ratio = total
    if "exposure" in local.maps:
        g_exp = float(edits.get("exposure", 0.0))
        exp_ratio = np.exp2(local.value("exposure", g_exp))
        if abs(g_exp) > 0.001:
            exp_ratio /= np.float32(2.0**g_exp)
        exp_ratio = exp_ratio[..., None].astype(np.float32, copy=False)
        ratio = exp_ratio if ratio is None else ratio * exp_ratio
    return ratio


def apply_local_shadows(arr: np.ndarray, shadows: np.ndarray) -> np.ndarray:
    """Per-pixel mirror of the shadows half of ``_apply_highlights_shadows``."""
    from faststack.imaging.math_utils import _smoothstep01

    lum = arr[:, :, 0] * 0.2126 + arr[:, :, 1] * 0.7152 + arr[:, :, 2] * 0.0722
    lum = np.clip(lum, 1e-10, None)
    shadow_mask = _smoothstep01(1.0 - lum / 0.18)
    factor = np.where(np.abs(shadows) > 0.001, 1.0 + shadows * 0.5 * shadow_mask, 1.0)
    return arr * factor[..., None].astype(np.float32, copy=False)


def _levels_ramp_map(arr: np.ndarray, blacks: np.ndarray, whites: np.ndarray):
    bp = -blacks * 0.15
    wp = 1.0 - whites * 0.15
    # The global ramp only nudges a degenerate span; built-up local coats can
    # push it negative, which would invert the image, so floor it instead.
    span = np.maximum(wp - bp, 0.0001)
    return (arr - bp[..., None]) / span[..., None]


def _basic_srgb_map(
    arr: np.ndarray,
    brightness: np.ndarray,
    contrast: np.ndarray,
    saturation: float,
    vibrance: np.ndarray,
) -> np.ndarray:
    """Per-pixel mirror of ``_apply_basic_srgb_adjustments``."""
    from faststack.imaging.editor import _rec601_gray

    b_active = np.abs(brightness) > 0.001
    if b_active.any():
        gain = np.maximum(1.0 + brightness, 0.0)[..., None]
        out = arr * gain
        pos = brightness > 0.001
        if pos.any():
            knee = 0.90
            safe_gain = np.maximum(gain, 1e-6)
            source_knee = knee / safe_gain
            exponent = gain * (1.0 - source_knee) / (1.0 - knee)
            upper = (arr > source_knee) & pos[..., None]
            denom = np.maximum(1.0 - source_knee, 1e-6)
            remaining = np.maximum((1.0 - arr) / denom, 0.0)
            with np.errstate(over="ignore", invalid="ignore"):
                rolled = 1.0 - (1.0 - knee) * np.power(remaining, exponent)
            out = np.where(upper, rolled, out)
        arr = np.where(b_active[..., None], out, arr)

    c_active = np.abs(contrast) > 0.001
    if c_active.any():
        factor = np.maximum(1.0 + contrast * 0.4, 0.0)[..., None]
        flattened = (arr - 0.5) * factor + 0.5
        upper = np.clip(arr, 0.0, 1.0)
        lower = 1.0 - upper
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            pu = np.power(upper, factor)
            s_curve = pu / (pu + np.power(lower, factor))
        out = np.where((contrast < 0.0)[..., None], flattened, s_curve)
        arr = np.where(c_active[..., None], out, arr)

    if abs(saturation) > 0.001:
        sat_factor = 1.0 + saturation * 0.5
        gray = _rec601_gray(np.ascontiguousarray(arr, dtype=np.float32))[..., None]
        arr = gray + (arr - gray) * sat_factor

    v_active = np.abs(vibrance) > 0.001
    if v_active.any():
        cmax = arr.max(axis=2)
        cmin = arr.min(axis=2)
        sat = np.zeros_like(cmax)
        np.divide(cmax - cmin, cmax, out=sat, where=cmax > 0.0001)
        sat_mask = np.clip(1.0 - sat, 0.0, 1.0)
        factor = np.where(v_active, 1.0 + vibrance * sat_mask, 1.0)
        gray = _rec601_gray(np.ascontiguousarray(arr, dtype=np.float32))[..., None]
        arr = gray + (arr - gray) * factor[..., None]

    return arr.astype(np.float32, copy=False)


def _color_mix_map(arr: np.ndarray, values: Dict[str, np.ndarray]) -> np.ndarray:
    """Per-pixel mirror of the colour-mix step (18)."""
    from faststack.imaging.editor import (
        _COLOR_MIX_BANDS,
        _COLOR_MIX_GAIN,
        _COLOR_MIX_HALF_WIDTH,
        _COLOR_MIX_KEYS,
        _hsv_hue_degrees,
        _rec601_gray,
    )

    bands = [
        (center, values[key])
        for key, (_name, center) in zip(_COLOR_MIX_KEYS, _COLOR_MIX_BANDS)
        if key in values and np.any(np.abs(values[key]) > 0.001)
    ]
    if not bands:
        return arr
    hue = _hsv_hue_degrees(arr)
    factor = np.zeros(arr.shape[:2], dtype=np.float32)
    for center, value in bands:
        dist = np.abs(((hue - center + 180.0) % 360.0) - 180.0)
        weight = np.where(
            dist < _COLOR_MIX_HALF_WIDTH,
            0.5 * (1.0 + np.cos(np.pi * dist / _COLOR_MIX_HALF_WIDTH)),
            0.0,
        )
        factor += np.where(np.abs(value) > 0.001, value, 0.0) * _COLOR_MIX_GAIN * weight
    gray = _rec601_gray(np.ascontiguousarray(arr, dtype=np.float32))[..., None]
    return (gray + (arr - gray) * (1.0 + factor)[..., None]).astype(np.float32)


def apply_local_srgb_block(
    arr: np.ndarray,
    edits: Dict[str, Any],
    local: LocalAdjustments,
    *,
    levels_soft_knee: bool,
) -> np.ndarray:
    """Per-pixel mirror of pipeline steps 14-19 for the ROI input *arr*."""
    from faststack.imaging.editor import _apply_levels_soft_clip

    def val(key: str) -> np.ndarray:
        return local.value(key, float(edits.get(key, 0.0)))

    brightness = val("brightness")
    contrast = val("contrast")
    vibrance = val("vibrance")
    blacks = val("blacks")
    whites = val("whites")
    saturation = float(edits.get("saturation", 0.0))

    levels_active = (np.abs(blacks) > 0.001) | (np.abs(whites) > 0.001)
    levels_first = levels_active & ((brightness > 0.001) | (contrast > 0.001))
    if levels_first.any():
        ramp = _levels_ramp_map(arr, blacks, whites)
        arr = np.where(levels_first[..., None], ramp, arr)

    arr = _basic_srgb_map(arr, brightness, contrast, saturation, vibrance)
    arr = _color_mix_map(
        arr,
        {
            key: val(key)
            for key in COLOR_MIX_LOCAL_KEYS
            if key in local.maps or abs(float(edits.get(key, 0.0))) > 0.001
        },
    )

    levels_after = levels_active & ~levels_first
    if levels_after.any():
        ramp = _levels_ramp_map(arr, blacks, whites)
        arr = np.where(levels_after[..., None], ramp, arr)
    if levels_soft_knee and levels_active.any():
        clipped = _apply_levels_soft_clip(np.array(arr, dtype=np.float32))
        arr = np.where(levels_active[..., None], clipped, arr)
    return arr.astype(np.float32, copy=False)
