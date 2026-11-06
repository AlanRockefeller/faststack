# faststack/imaging/stack_paint.py
"""Paint pixels from an in-camera focus-stack input over the stacked output.

A *stack layer* is a local layer (see ``faststack.imaging.local_adjust``)
whose dict carries ``"source": "<input file name>"`` and an empty recipe.
Its brush-stroke mask (``MaskData`` keyed by the layer id) says where the
input frame replaces the stacked output.

In-camera stacks crop and enlarge the frames, and each frame's framing shifts
slightly with focus (focus breathing), so every input is registered to the
stacked output on its own: ORB features + RANSAC similarity transform at a
reduced size, refined with ECC.  The result maps stacked-output normalised
coordinates to input pixels.

Compositing happens in *base image space* at the very start of
``ImageEditor._apply_edits`` — before rotation, straighten and crop — so the
painted pixels go through exactly the same geometry and global/local tone
edits as the rest of the photo.
"""

import dataclasses
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from faststack.imaging.mask import MaskData
from faststack.imaging.mask_engine import LayerDensityCache, rasterize_layer_density
from faststack.imaging.optional_deps import get_cv2

log = logging.getLogger(__name__)

STACK_SOURCE_KEY = "source"
# Pseudo source: the stacked photo itself. Painting it restores the stack on
# top of input frames painted by layers below it; it needs no registration.
STACK_OUTPUT_SOURCE = "__stack_output__"

# Registration works on downsized greyscale copies; this long edge keeps it
# around 0.2-0.4s per frame while staying well under a pixel at full size
# after the ECC refinement.
_ALIGN_LONG_EDGE = 1600
_ECC_LONG_EDGE = 2400
_MIN_INLIERS = 25
# Coarsest pyramid level kept for downsized preview renders.
_MIN_LEVEL_LONG_EDGE = 384

# Geometry-neutral edits: stack masks are rasterised in base image space.
BASE_SPACE_EDITS: Dict[str, Any] = {
    "rotation": 0,
    "straighten_angle": 0.0,
    "crop_box": None,
}


class StackAlignError(RuntimeError):
    """The stack input could not be registered to the stacked output."""


# ---------------------------------------------------------------------------
# Layer helpers
# ---------------------------------------------------------------------------


def is_stack_layer(layer: Any) -> bool:
    return isinstance(layer, dict) and bool(layer.get(STACK_SOURCE_KEY))


def stack_layers(
    edits: Mapping[str, Any], *, include_hidden: bool = False
) -> List[Dict[str, Any]]:
    """Stack layers bottom to top, normally limited to visible layers."""
    return [
        layer
        for layer in edits.get("local_layers") or ()
        if is_stack_layer(layer)
        and (include_hidden or layer.get("visible", True))
    ]


def stack_input_label(name: str) -> str:
    if name == STACK_OUTPUT_SOURCE:
        return "Stack output"
    return Path(name).stem


# ---------------------------------------------------------------------------
# Loading and registration
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, eq=False)
class StackSource:
    """A decoded, registered stack input.  Immutable once built.

    ``levels[i]`` is the input (RGB uint8, EXIF-oriented) downsized by
    ``2**i``.  ``out_to_in`` maps stacked-output normalised coordinates
    ``(u, v, 1)`` to continuous pixel coordinates of ``levels[0]`` (pixel
    centres at ``i + 0.5``).
    """

    name: str
    levels: Tuple[np.ndarray, ...]
    out_to_in: np.ndarray


def _to_gray_u8(arr: np.ndarray) -> np.ndarray:
    cv2 = get_cv2()
    if arr.dtype != np.uint8:
        arr = cv2.convertScaleAbs(np.asarray(arr, dtype=np.float32), alpha=255.0)
    if arr.ndim == 3:
        arr = cv2.cvtColor(np.ascontiguousarray(arr[..., :3]), cv2.COLOR_RGB2GRAY)
    return arr


def _resize_long_edge(gray: np.ndarray, long_edge: int) -> np.ndarray:
    cv2 = get_cv2()
    h, w = gray.shape[:2]
    scale = long_edge / max(h, w)
    if scale >= 1.0:
        return gray
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)


def _pixel_scale(full_shape: Tuple[int, int], small: np.ndarray) -> np.ndarray:
    """Per-axis factor from *small*'s continuous pixels to *full_shape*'s."""
    return np.array(
        [full_shape[1] / small.shape[1], full_shape[0] / small.shape[0]],
        dtype=np.float64,
    )


def _as_3x3(m: np.ndarray) -> np.ndarray:
    return np.vstack([np.asarray(m, dtype=np.float64), [0.0, 0.0, 1.0]])


def _scale_matrix(sx: float, sy: float) -> np.ndarray:
    return np.diag([sx, sy, 1.0])


def estimate_alignment(
    input_rgb: np.ndarray, output_arr: np.ndarray
) -> Tuple[np.ndarray, Dict[str, float]]:
    """Return the 2x3 map from output normalised coords to input pixels.

    Raises StackAlignError when the frames cannot be matched reliably.
    """
    cv2 = get_cv2()
    if cv2 is None:
        raise StackAlignError("OpenCV is not available")

    in_full = input_rgb.shape[:2]
    out_full = output_arr.shape[:2]
    in_gray = _to_gray_u8(input_rgb)
    out_gray = _to_gray_u8(output_arr)

    # --- Coarse: ORB + RANSAC similarity (output -> input) ---
    in_s = _resize_long_edge(in_gray, _ALIGN_LONG_EDGE)
    out_s = _resize_long_edge(out_gray, _ALIGN_LONG_EDGE)
    orb = cv2.ORB_create(nfeatures=6000)
    kp_in, des_in = orb.detectAndCompute(in_s, None)
    kp_out, des_out = orb.detectAndCompute(out_s, None)
    if des_in is None or des_out is None or len(kp_in) < 8 or len(kp_out) < 8:
        raise StackAlignError("not enough detail to match the frames")
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(des_out, des_in, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.8 * p[1].distance]
    if len(good) < _MIN_INLIERS:
        raise StackAlignError("too few matching features")
    pts_out = np.float32([kp_out[m.queryIdx].pt for m in good]) + 0.5
    pts_in = np.float32([kp_in[m.trainIdx].pt for m in good]) + 0.5
    m_s, inliers = cv2.estimateAffinePartial2D(
        pts_out,
        pts_in,
        method=cv2.RANSAC,
        ransacReprojThreshold=2.5,
        maxIters=5000,
        confidence=0.999,
        refineIters=20,
    )
    n_inliers = int(inliers.sum()) if inliers is not None else 0
    if m_s is None or n_inliers < _MIN_INLIERS:
        raise StackAlignError("frames do not line up")
    scale = math.hypot(m_s[0, 0], m_s[1, 0])
    if not 0.4 < scale < 2.5:
        raise StackAlignError(f"implausible scale {scale:.2f}")

    # Lift to full-resolution continuous pixel coords on both sides.
    s_out = _pixel_scale(out_full, out_s)
    s_in = _pixel_scale(in_full, in_s)
    full = (
        _scale_matrix(*s_in)
        @ _as_3x3(m_s)
        @ _scale_matrix(1.0 / s_out[0], 1.0 / s_out[1])
    )

    # --- Fine: ECC (affine) at a larger size, seeded with the coarse fit ---
    ecc_cc = float("nan")
    try:
        in_e = _resize_long_edge(in_gray, _ECC_LONG_EDGE)
        out_e = _resize_long_edge(out_gray, _ECC_LONG_EDGE)
        se_out = _pixel_scale(out_full, out_e)
        se_in = _pixel_scale(in_full, in_e)
        # findTransformECC wants template(x) ~ input(W x) in *index* coords.
        to_idx = np.array([[1, 0, -0.5], [0, 1, -0.5], [0, 0, 1]], dtype=np.float64)
        from_idx = np.array([[1, 0, 0.5], [0, 1, 0.5], [0, 0, 1]], dtype=np.float64)
        warp = (
            to_idx
            @ _scale_matrix(1.0 / se_in[0], 1.0 / se_in[1])
            @ full
            @ _scale_matrix(*se_out)
            @ from_idx
        )[:2].astype(np.float32)
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
        ecc_cc, refined = cv2.findTransformECC(
            out_e.astype(np.float32),
            in_e.astype(np.float32),
            warp,
            cv2.MOTION_AFFINE,
            criteria,
            None,
            5,
        )
        refined_full = (
            _scale_matrix(*se_in)
            @ from_idx
            @ _as_3x3(refined)
            @ to_idx
            @ _scale_matrix(1.0 / se_out[0], 1.0 / se_out[1])
        )
        # Trust ECC only for a small correction of the feature fit.
        corners = np.array(
            [[0, 0, 1], [out_full[1], 0, 1], [0, out_full[0], 1]], dtype=np.float64
        ).T
        shift = np.abs((refined_full - full) @ corners)[:2].max()
        if ecc_cc > 0.5 and shift < 0.01 * max(in_full):
            full = refined_full
        else:
            log.debug(
                "Stack ECC refinement rejected: cc=%.3f shift=%.1fpx", ecc_cc, shift
            )
    except cv2.error as exc:
        log.debug("Stack ECC refinement failed: %s", exc)

    # Output normalised (u, v) -> output continuous px -> input continuous px.
    out_to_in = (full @ _scale_matrix(out_full[1], out_full[0]))[:2]
    stats = {
        "inliers": float(n_inliers),
        "matches": float(len(good)),
        "scale": float(scale),
        "ecc": float(ecc_cc),
    }
    return out_to_in, stats


def _decode_input(path: Path) -> np.ndarray:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return np.asarray(im.convert("RGB"))


def _build_levels(rgb: np.ndarray) -> Tuple[np.ndarray, ...]:
    cv2 = get_cv2()
    levels = [np.ascontiguousarray(rgb)]
    while max(levels[-1].shape[:2]) / 2 >= _MIN_LEVEL_LONG_EDGE:
        levels.append(cv2.pyrDown(levels[-1]))
    for level in levels:
        level.setflags(write=False)
    return tuple(levels)


def load_stack_source(path: Path, output_arr: np.ndarray) -> StackSource:
    """Decode *path* and register it to *output_arr* (the stacked photo).

    Runs on a worker thread.  Raises OSError for unreadable files and
    StackAlignError when registration fails.
    """
    rgb = _decode_input(Path(path))
    out_to_in, stats = estimate_alignment(rgb, output_arr)
    log.info(
        "Aligned stack input %s: scale=%.4f inliers=%d/%d ecc=%.3f",
        Path(path).name,
        stats["scale"],
        stats["inliers"],
        stats["matches"],
        stats["ecc"],
    )
    out_to_in.setflags(write=False)
    return StackSource(Path(path).name, _build_levels(rgb), out_to_in)


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------


def stack_identity(
    edits: Mapping[str, Any],
    mask_assets: Mapping[str, MaskData],
    sources: Mapping[str, StackSource],
) -> tuple:
    """Changes whenever the composited pixels could (for render caches)."""
    ident = []
    for layer in stack_layers(edits):
        mask = mask_assets.get(layer["id"])
        name = layer[STACK_SOURCE_KEY]
        source = sources.get(name)
        live = mask.live_stroke if mask is not None else None
        ident.append(
            (
                layer["id"],
                name,
                (
                    "output"
                    if name == STACK_OUTPUT_SOURCE
                    else (id(source) if source is not None else None)
                ),
                (mask.uid, mask.revision, len(mask.strokes)) if mask else None,
                len(live.points) if live is not None else -1,
            )
        )
    return tuple(ident)


def _warp_source(
    source: StackSource,
    shape: Tuple[int, int],
    x0: int,
    y0: int,
    rw: int,
    rh: int,
) -> np.ndarray:
    """Input pixels (uint8) for the ``(rh, rw)`` box at ``(x0, y0)`` of a
    base-space array of *shape*."""
    cv2 = get_cv2()
    h, w = shape
    # ROI index (j, i) -> output normalised (u, v) -> input level-0 px.
    to_norm = np.array(
        [
            [1.0 / w, 0.0, (x0 + 0.5) / w],
            [0.0, 1.0 / h, (y0 + 0.5) / h],
            [0.0, 0.0, 1.0],
        ]
    )
    m = _as_3x3(source.out_to_in) @ to_norm
    ratio = math.sqrt(abs(np.linalg.det(m[:2, :2])))
    level = 0
    if ratio > 1.0:
        level = min(len(source.levels) - 1, int(math.floor(math.log2(ratio))))
    f = 0.5**level
    # Continuous level-0 px -> level index coords.
    to_level = np.array([[f, 0.0, -0.5], [0.0, f, -0.5], [0.0, 0.0, 1.0]])
    return cv2.warpAffine(
        source.levels[level],
        (to_level @ m)[:2],
        (rw, rh),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )


def warp_source_to_base(source: StackSource, shape: Tuple[int, int]) -> np.ndarray:
    """The whole input frame registered onto a base-space array of *shape*
    (float32 RGB in [0, 1]) — what painting everywhere would show."""
    h, w = shape
    return _warp_source(source, shape, 0, 0, w, h).astype(np.float32) * np.float32(
        1.0 / 255.0
    )


def composite_stack_layers(
    arr: np.ndarray,
    edits: Mapping[str, Any],
    mask_assets: Mapping[str, MaskData],
    sources: Mapping[str, StackSource],
    cache: Optional[LayerDensityCache] = None,
) -> Tuple[np.ndarray, bool]:
    """Blend painted stack-input pixels into base-space float RGB *arr*.

    Never writes into *arr*: returns ``(new_array, True)`` when anything was
    painted, else ``(arr, False)``.  *arr* may be any downsized copy of the
    stacked photo; layers whose source is not loaded yet are skipped.
    ``STACK_OUTPUT_SOURCE`` layers blend back towards *arr* itself.
    """
    layers = stack_layers(edits)
    if not layers:
        return arr, False
    cv2 = get_cv2()
    if cv2 is None:
        return arr, False

    h, w = arr.shape[:2]
    out = None
    for layer in layers:
        mask = mask_assets.get(layer["id"])
        is_output = layer[STACK_SOURCE_KEY] == STACK_OUTPUT_SOURCE
        source = None if is_output else sources.get(layer[STACK_SOURCE_KEY])
        if mask is None or (source is None and not is_output):
            continue
        density = rasterize_layer_density(
            layer["id"], mask, (h, w), BASE_SPACE_EDITS, cache=cache
        )
        if density is None:
            continue
        rh, rw = density.arr.shape
        roi = (slice(density.y0, density.y1), slice(density.x0, density.x1))
        if is_output:
            # *arr* is never written, so it still holds the stacked pixels.
            src = np.asarray(arr[roi], dtype=np.float32)
        else:
            warped = _warp_source(source, (h, w), density.x0, density.y0, rw, rh)
            src = warped.astype(np.float32) * np.float32(1.0 / 255.0)

        if out is None:
            out = np.array(arr, dtype=np.float32, copy=True)
        region = out[roi]
        alpha = np.clip(density.arr, 0.0, 1.0)[..., None]
        region += (src - region) * alpha
    if out is None:
        return arr, False
    return out, True
