# faststack/imaging/halo_fix.py
"""Remove focus-stacking halos inside a painted area.

A *halo layer* is a local layer (see ``faststack.imaging.local_adjust``)
whose dict carries ``"halo": True`` and an empty recipe.  Its brush-stroke
mask (``MaskData`` keyed by the layer id) marks where the owner sees a halo.

A focus stack shows three flaws around a subject that stands in front of a
blurred background, all following the subject's outline:

* a dark ring right at the edge (the subject blocked part of the lens's view
  of the background in the frames focused on it),
* a ghost outline a little further out (the subject's edge at another
  focus distance, or after it moved during the stack),
* a wide soft glow (the subject's own light, spread by the frames focused on
  the background).

Inside the painted area, ``compute_halo_fix``

1. separates subject from background: in-focus detail marks subject the
   brush strayed onto, whatever its colours (the background and the halo
   are defocused, so smooth); at the outline, and on smooth subject, every
   painted pixel is compared with the colours found just outside the
   stroke, biased so that a ghost or a darkened rim counts as background.
   Only subject parts that connect to subject outside the stroke are kept;
2. fills the background next to the subject (dark ring and ghost, within
   ``NEAR_BAND`` of the outline) with LaMa inpainting;
3. subtracts the glow from the rest of the painted background: the excess of
   its low-pass brightness over a smooth surface interpolated from the
   unpainted background around it (only ever darkens, never invents);
4. brightens subject pixels the dark ring crossed (a stem passing through
   the halo) back to the level of the same subject on both sides of the
   stroke; painted subject along an outline keeps its own shading.

The result is cached per layer (``HaloResult``) in full-resolution base image
space and composited by ``composite_halo_layers`` at the start of
``ImageEditor._apply_edits``, like stack paint, so it goes through exactly
the same geometry and tone edits as the rest of the photo.  Computing it
takes seconds (LaMa), so the controller runs it on a worker after each
stroke; the render path only blends.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import numpy as np

from faststack.imaging.mask import MaskData
from faststack.imaging.mask_engine import rasterize_layer_density
from faststack.imaging.optional_deps import get_cv2
from faststack.imaging.stack_paint import (
    BASE_SPACE_EDITS,
    STACK_OUTPUT_SOURCE,
    STACK_SOURCE_KEY,
    composite_stack_layers,
    stack_layers,
)

log = logging.getLogger(__name__)

HALO_KEY = "halo"

# Distances are tuned on 20 MP stacks (5184 px long side) and scaled with
# the image's long side.
REFERENCE_LONG_SIDE = 5184
NEAR_BAND = 95.0  # px from the outline that is inpainted, not just de-glowed
CONTEXT = 260.0  # px of unpainted surroundings analysed around the stroke
OUTSIDE_RING = 30.0  # px just outside the stroke sampled for subject/background
# A painted pixel is subject only if its colour lies at least this far along
# the way from the background colour (0) to the subject colour (1), so a
# ghost, glow or darkened rim, which sit between the two, count as background.
SUBJECT_CONTRAST = 0.75
MIN_SUBJECT_DELTA_E = 18.0
PAINTED = 0.25  # density at which a pixel counts as painted
# In-focus detail: a pixel whose local fine-detail energy is this many times
# the background's (defocused, so only noise) is sharp, i.e. subject.  The
# glow and dark ring are as smooth as the background they lie on.
SHARP_RATIO = 3.0
# Detail energy is averaged over a few pixels, so the in-focus area reaches
# past the subject's edge onto the dark ring, and takes in a ghost outline
# close beside it.  Within EDGE_BAND of its edge, a pixel counts as subject
# by detail only if its own fine detail reaches EDGE_DETAIL of the subject's
# typical level (a ghost is softer than the edge it copies); the rest of the
# band is left to the colour tests.
EDGE_BAND = 16.0
EDGE_DETAIL = 0.35

Inpaint = Callable[[np.ndarray, np.ndarray], np.ndarray]


class HaloFixError(RuntimeError):
    """The painted area could not be analysed (e.g. no subject next to it)."""


def is_halo_layer(layer: Any) -> bool:
    return isinstance(layer, dict) and bool(layer.get(HALO_KEY))


def halo_layers(
    edits: Mapping[str, Any], *, include_hidden: bool = False
) -> List[Dict[str, Any]]:
    """Halo layers bottom to top, normally limited to visible layers."""
    return [
        layer
        for layer in edits.get("local_layers") or ()
        if is_halo_layer(layer) and (include_hidden or layer.get("visible", True))
    ]


def mask_signature(mask: Optional[MaskData]) -> Optional[tuple]:
    """A mask's committed strokes (no live stroke), or None when empty."""
    if mask is None or not mask.has_strokes():
        return None
    return (mask.uid, mask.revision, len(mask.strokes))


def _stack_base_identity(
    edits: Mapping[str, Any],
    mask_assets: Mapping[str, Any],
    sources: Mapping[str, Any],
) -> tuple:
    """The visible stack paint a correction is computed on top of.

    Unlike ``stack_identity`` this ignores live strokes and which load of an
    input is in memory (the save worker loads its own), only whether it is
    loaded at all: a layer whose input is missing is not painted.
    """
    ident = []
    for layer in stack_layers(edits):
        name = layer[STACK_SOURCE_KEY]
        ident.append(
            (
                layer["id"],
                name,
                name == STACK_OUTPUT_SOURCE or sources.get(name) is not None,
                mask_signature(mask_assets.get(layer["id"])),
            )
        )
    return tuple(ident)


def halo_signature(
    mask: Optional[MaskData],
    edits: Mapping[str, Any],
    mask_assets: Mapping[str, Any],
    sources: Mapping[str, Any],
) -> Optional[tuple]:
    """What a halo layer's correction is computed from: its own strokes and
    the stack paint beneath it.  None when the layer has no strokes."""
    strokes = mask_signature(mask)
    if strokes is None:
        return None
    return (strokes, _stack_base_identity(edits, mask_assets, sources))


def halo_base(
    base: np.ndarray,
    edits: Mapping[str, Any],
    mask_assets: Mapping[str, Any],
    sources: Mapping[str, Any],
) -> np.ndarray:
    """*base* with the stack paint applied, as the render path composites it
    before the halo layers: a correction computed from it keeps any stack
    paint under the stroke instead of reverting it.  Never writes *base*."""
    if not stack_layers(edits):
        return base
    out, _painted = composite_stack_layers(base, edits, mask_assets, sources)
    return out


@dataclasses.dataclass(frozen=True, eq=False)
class HaloResult:
    """Corrected pixels for one halo layer, in full-resolution base space.

    ``rgb`` (float32, 0..1) and ``alpha`` (float32) cover rows ``y0:y1`` and
    columns ``x0:x1`` of a base image of ``base_shape``.  ``scaled`` caches
    the patch resized for the preview, keyed by target size.
    """

    signature: tuple
    base_shape: Tuple[int, int]
    x0: int
    y0: int
    x1: int
    y1: int
    rgb: np.ndarray
    alpha: np.ndarray
    scaled: Dict[Tuple[int, int], Tuple[np.ndarray, np.ndarray]] = dataclasses.field(
        default_factory=dict, repr=False
    )

    def resized(self, size: Tuple[int, int]) -> Tuple[np.ndarray, np.ndarray]:
        """``(rgb, alpha)`` resized to *size* (w, h), cached per size.

        The render worker and the save worker may both call this; a race
        only computes the same arrays twice.
        """
        hit = self.scaled.get(size)
        if hit is not None:
            return hit
        cv2 = get_cv2()
        hit = (
            cv2.resize(self.rgb, size, interpolation=cv2.INTER_AREA),
            cv2.resize(self.alpha, size, interpolation=cv2.INTER_AREA),
        )
        if len(self.scaled) >= 3:  # a resize or zoom brings new sizes
            self.scaled.clear()
        self.scaled[size] = hit
        return hit


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _ellipse(cv2, radius: float):
    k = max(1, int(round(radius)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))


def _push_pull(img: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Fill invalid pixels from valid ones by pyramid push-pull."""
    cv2 = get_cv2()
    if min(img.shape[:2]) < 4:
        m = valid[..., None].astype(np.float32)
        mean = (img * m).sum((0, 1)) / max(float(m.sum()), 1.0)
        return np.where(valid[..., None], img, mean).astype(np.float32)
    h, w = valid.shape
    v = valid.astype(np.float32)
    size = ((w + 1) // 2, (h + 1) // 2)
    small = cv2.resize(img * v[..., None], size, interpolation=cv2.INTER_AREA)
    v2 = cv2.resize(v, size, interpolation=cv2.INTER_AREA)
    small = np.where(v2[..., None] > 1e-3, small / np.maximum(v2[..., None], 1e-6), 0)
    up = cv2.resize(
        _push_pull(small.astype(np.float32), v2 > 1e-3), (w, h), interpolation=cv2.INTER_LINEAR
    )
    return np.where(valid[..., None], img, up).astype(np.float32)


def _smooth_fill(img: np.ndarray, valid: np.ndarray, iterations: int = 300) -> np.ndarray:
    """Membrane interpolation of *img* into the invalid pixels (at 1/4 size)."""
    cv2 = get_cv2()
    h, w = valid.shape
    s = 4
    size = (max(1, w // s), max(1, h // s))
    small = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    vs = cv2.resize(valid.astype(np.float32), size, interpolation=cv2.INTER_AREA) > 0.999
    out = _push_pull(small, vs)
    hole = ~vs
    if hole.any():
        for _ in range(iterations):
            avg = cv2.blur(out, (3, 3))
            out[hole] = avg[hole]
    return cv2.resize(out, (w, h), interpolation=cv2.INTER_CUBIC)


def _normconv(img: np.ndarray, weight: np.ndarray, sigma: float) -> np.ndarray:
    cv2 = get_cv2()
    wt = weight.astype(np.float32)
    num = cv2.GaussianBlur(img * wt[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(wt, (0, 0), sigma)
    return num / np.maximum(den[..., None], 1e-4)


def _in_focus(
    img: np.ndarray, px: Callable[[float], float]
) -> Tuple[np.ndarray, np.ndarray]:
    """Pixels on or between in-focus detail: the subject of a focus stack.

    Fine-detail energy (the image minus a slight blur, averaged over a few
    pixels) sits at the noise level on the defocused background and on the
    halo, whose glow and dark ring are just as smooth, and well above it on
    the subject, whatever its colours.  Smooth patches between the detail (a
    dark scale between bright ones) are closed in; isolated specks (a grain
    of dust on the background) are dropped.

    Returns ``(in_focus, fine)``: the area, and the fine-detail energy over a
    smaller window for decisions at the area's edge.
    """
    cv2 = get_cv2()
    gray = cv2.cvtColor(np.clip(img, 0, 1), cv2.COLOR_RGB2GRAY)
    hp = gray - cv2.GaussianBlur(gray, (0, 0), px(2))
    energy = np.sqrt(cv2.GaussianBlur(hp * hp, (0, 0), px(3)))
    fine = np.sqrt(cv2.GaussianBlur(hp * hp, (0, 0), px(1.5)))
    # The analysed area always holds plenty of background (the halo lies on
    # it), so its quietest pixels give the noise level.
    floor = max(float(np.percentile(energy, 10)), 1e-4)
    sharp = (energy > SHARP_RATIO * floor).astype(np.uint8)
    sharp = cv2.morphologyEx(sharp, cv2.MORPH_CLOSE, _ellipse(cv2, px(6)))
    n, cc, stats, _ = cv2.connectedComponentsWithStats(sharp, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= px(25) ** 2
    keep[0] = False
    sharp = keep[cc]
    n, cc, stats, _ = cv2.connectedComponentsWithStats((~sharp).astype(np.uint8), connectivity=4)
    holes = stats[:, cv2.CC_STAT_AREA] < px(40) ** 2
    border = np.unique(np.concatenate([cc[0], cc[-1], cc[:, 0], cc[:, -1]]))
    holes[border] = False
    sharp |= holes[cc]
    return sharp, fine


def _segment_subject(
    img: np.ndarray, painted: np.ndarray, px: Callable[[float], float]
) -> Tuple[np.ndarray, np.ndarray]:
    """``(subject, region)`` masks of the analysed area.

    The subject is what the stroke goes around: the area the painted region
    encloses.  Only the enclosed area's core (well away from the stroke) is
    taken as certain: a wide brush rarely reaches into the subject's concave
    corners (between two lobes of a cap, where cap meets stem), and those
    pockets hold halo too.  For a stroke that encloses nothing (painted along
    one edge), the side of the stroke with more fine detail (the in-focus
    subject, not the blurred background) seeds the subject instead.

    GrabCut splits the rest between subject and background, starting from
    "probably background" for painted pixels because the stroke was painted
    over the halo.  A ghost of the edge or a glow sits between the two in
    colour and GrabCut tends to give it to the subject, so a pixel near the
    stroke stays subject only if its colour is most of the way from the
    background's to the subject's.

    ``region`` is where the fix applies: the painted pixels plus background
    found in the enclosed pockets.
    """
    cv2 = get_cv2()
    h, w = painted.shape
    paint_u8 = painted.astype(np.uint8)
    # Enclosed areas: unpainted components that do not touch the border.
    n, cc = cv2.connectedComponents(1 - paint_u8, connectivity=4)
    border = np.unique(np.concatenate([cc[0], cc[-1], cc[:, 0], cc[:, -1]]))
    enclosed = np.ones(n, bool)
    enclosed[border] = False
    inside = enclosed[cc] & ~painted
    if inside.sum() >= px(20) ** 2:
        core = cv2.erode(inside.astype(np.uint8), _ellipse(cv2, px(NEAR_BAND))) > 0
        if not core.any():
            core = cv2.erode(inside.astype(np.uint8), _ellipse(cv2, px(15))) > 0
    else:
        ring = (cv2.dilate(paint_u8, _ellipse(cv2, px(OUTSIDE_RING))) > 0) & ~painted
        gray = cv2.cvtColor(np.clip(img, 0, 1), cv2.COLOR_RGB2GRAY)
        detail = cv2.GaussianBlur(
            np.abs(cv2.Laplacian(gray, cv2.CV_32F)), (0, 0), px(3)
        )
        if not ring.any():
            raise HaloFixError("paint around the halo, next to the subject")
        core = ring & (detail > np.percentile(detail[ring], 70))
        inside = np.zeros_like(painted)
    if core.sum() < 25:
        raise HaloFixError("no subject found next to the painted area")
    uncertain = painted | (inside & ~core)
    # In-focus pixels under the stroke are subject the brush strayed onto.
    focus, fine = _in_focus(img, px)
    interior = cv2.erode(focus.astype(np.uint8), _ellipse(cv2, px(EDGE_BAND))) > 0
    sharp = interior.copy()
    if interior.any():
        band = focus & ~interior
        near = band & (cv2.dilate(uncertain.astype(np.uint8), _ellipse(cv2, px(40))) > 0)
        level = float(np.percentile(fine[near if near.any() else band], 90))
        edge = (band & (fine > EDGE_DETAIL * level)).astype(np.uint8)
        edge = cv2.morphologyEx(edge, cv2.MORPH_CLOSE, _ellipse(cv2, px(4))) > 0
        sharp |= edge & band
    sharp &= uncertain

    s = 2 if max(h, w) > 1200 else 1
    size = (max(1, w // s), max(1, h // s))
    small = (cv2.resize(np.clip(img, 0, 1), size, interpolation=cv2.INTER_AREA) * 255).astype(
        np.uint8
    )

    def shrink(m: np.ndarray) -> np.ndarray:
        return cv2.resize(m.astype(np.uint8), size, interpolation=cv2.INTER_NEAREST) > 0

    gc = np.full(small.shape[:2], cv2.GC_BGD, np.uint8)
    gc[shrink(painted)] = cv2.GC_PR_BGD
    gc[shrink(inside & ~core)] = cv2.GC_PR_FGD
    gc[shrink(sharp)] = cv2.GC_PR_FGD
    gc[shrink(core)] = cv2.GC_FGD
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(small, gc, None, bgd, fgd, 4, cv2.GC_INIT_WITH_MASK)
    fg = (gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD)
    subject = cv2.resize(fg.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR) > 0.5
    subject = (subject & uncertain) | core

    lab = cv2.cvtColor(np.clip(img, 0, 1), cv2.COLOR_RGB2Lab)
    near_core = core & (
        cv2.dilate(uncertain.astype(np.uint8), _ellipse(cv2, px(40))) > 0
    )
    fg_ref = lab[near_core] if near_core.any() else lab[core]
    bg_px = uncertain & ~subject
    if bg_px.sum() > 25:
        fg_c = np.median(fg_ref, axis=0)
        bg_c = np.median(lab[bg_px], axis=0)
        axis = fg_c - bg_c
        denom = float(axis @ axis)
        if denom > MIN_SUBJECT_DELTA_E**2:
            t = ((lab - bg_c) @ axis) / denom
            subject &= ~uncertain | (t > SUBJECT_CONTRAST)
    # The colour test knows one subject colour; a many-coloured subject
    # (yellow and black scales) fails it where its detail proves it subject.
    # Outside the stroke, the in-focus area joined to the core is subject too
    # (an open stroke's core is only the busiest patches beside it); the
    # glow fit and the brightening below need the background and subject
    # around the stroke right.
    subject |= sharp | (focus & ~uncertain)
    # Keep only subject connected to the certain core (no stray specks), fill
    # pinholes (a dark spot on a cap is subject), smooth the outline.
    n, cc = cv2.connectedComponents(subject.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    keep[np.unique(cc[core])] = True
    keep[0] = False
    subject = keep[cc]
    n, cc = cv2.connectedComponents((~subject).astype(np.uint8), connectivity=4)
    areas = np.bincount(cc.ravel(), minlength=n)
    border = np.unique(np.concatenate([cc[0], cc[-1], cc[:, 0], cc[:, -1]]))
    pin = areas < px(40) ** 2
    pin[border] = False
    pin[0] = False
    subject |= pin[cc]
    # The colour test follows the noise of a translucent rim; a close/open
    # and a blur give the outline the smooth course of a real edge, without
    # specks or stair steps.
    su = subject.astype(np.uint8)
    su = cv2.morphologyEx(su, cv2.MORPH_CLOSE, _ellipse(cv2, px(5)))
    su = cv2.morphologyEx(su, cv2.MORPH_OPEN, _ellipse(cv2, px(4)))
    soft = cv2.GaussianBlur(su.astype(np.float32), (0, 0), max(1.0, px(3.5)))
    subject = (soft > 0.5) | core
    region = painted | (inside & ~subject)
    return subject, region


def _crossed_subject(
    sub_in: np.ndarray, known: np.ndarray, px: Callable[[float], float]
) -> np.ndarray:
    """The painted subject parts the stroke crosses (a stem through the halo).

    Only there does unpainted subject continue on two sides, so the level it
    should have is known; painted subject along an outline (the brush strayed
    over the edge) has subject on one side only, and its shading there is the
    subject's own.
    """
    cv2 = get_cv2()
    out = np.zeros_like(sub_in)
    n, cc, stats, _ = cv2.connectedComponentsWithStats(sub_in.astype(np.uint8), connectivity=8)
    pad = int(px(3)) + 1
    min_contact = px(30)
    h, w = sub_in.shape
    for i in range(1, n):
        x, y, bw, bh = stats[i, :4]
        ys = slice(max(0, y - pad), min(h, y + bh + pad))
        xs = slice(max(0, x - pad), min(w, x + bw + pad))
        comp = (cc[ys, xs] == i).astype(np.uint8)
        touch = (cv2.dilate(comp, _ellipse(cv2, px(3))) > 0) & known[ys, xs]
        k, _lab, tstats, _ = cv2.connectedComponentsWithStats(touch.astype(np.uint8), connectivity=8)
        if int(np.sum(tstats[1:, cv2.CC_STAT_AREA] >= min_contact)) >= 2:
            out[ys, xs] |= comp.astype(bool)
    return out


GLOW_RADII = (30.0, 60.0, 100.0, 150.0)  # px (Gaussian sigma) tried for the glow


def _fit_glow(
    img: np.ndarray,
    subject: np.ndarray,
    background: np.ndarray,
    painted: np.ndarray,
    bg_known: np.ndarray,
    dist: np.ndarray,
    px: Callable[[float], float],
) -> Optional[np.ndarray]:
    """The glow on the painted background, or None if there is none.

    Frames focused on the background show the subject as a big blur, so the
    glow is modelled as ``a * blur(subject light, sigma)``.  The excess
    brightness of the painted background over a smooth surface interpolated
    from the unpainted background is fitted per channel (a robust median of
    ratios) for each candidate sigma, and the best fit is kept.  Built from
    the subject alone, it never darkens a stem or twig that crosses the
    stroke more than the glow actually brightened it.
    """
    cv2 = get_cv2()
    sample = painted & background & (dist > px(15))
    if sample.sum() < 200 or not bg_known.any():
        return None
    lp = _normconv(img, background, px(6))
    target = _smooth_fill(lp, bg_known & (dist > px(8)))
    excess = (lp - target)[sample]  # (n, 3)
    light = img * subject[..., None].astype(np.float32)
    best = None
    for radius in GLOW_RADII:
        sigma = px(radius)
        s = 4  # blur at quarter size: the glow is smooth
        h, w = subject.shape
        small = cv2.resize(light, (max(1, w // s), max(1, h // s)), interpolation=cv2.INTER_AREA)
        blur = cv2.resize(
            cv2.GaussianBlur(small, (0, 0), sigma / s), (w, h), interpolation=cv2.INTER_LINEAR
        )
        b = blur[sample]
        ok = b.mean(axis=1) > 1e-3
        if ok.sum() < 100:
            continue
        a = np.median(excess[ok] / np.maximum(b[ok], 1e-4), axis=0)
        a = np.clip(a, 0.0, 50.0)
        resid = float(np.median(np.abs(excess[ok] - a * b[ok])))
        if best is None or resid < best[0]:
            best = (resid, a, blur)
    if best is None or not np.any(best[1] > 0):
        return None
    _resid, a, blur = best
    return (blur * a).astype(np.float32)


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------


def compute_halo_fix(
    base: np.ndarray,
    mask: MaskData,
    signature: tuple,
    inpaint: Inpaint,
    *,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Optional[HaloResult]:
    """Correct the halo under *mask* in *base* (full-res float32 RGB 0..1).

    Returns None when nothing is painted.  Raises HaloFixError when the
    painted area cannot be analysed.  Runs on a worker thread.
    """
    cv2 = get_cv2()
    if cv2 is None:
        raise HaloFixError("OpenCV is not available")
    h, w = base.shape[:2]
    density = rasterize_layer_density("halo", mask, (h, w), BASE_SPACE_EDITS)
    if density is None:
        return None
    scale = max(h, w) / REFERENCE_LONG_SIDE

    def px(v: float) -> float:
        return max(1.0, v * scale)

    def check() -> None:
        if cancelled is not None and cancelled():
            raise HaloFixError("cancelled")

    # Analysed region: the stroke plus context around it.
    ctx = int(px(CONTEXT))
    x0, y0 = max(0, density.x0 - ctx), max(0, density.y0 - ctx)
    x1, y1 = min(w, density.x1 + ctx), min(h, density.y1 + ctx)
    img = np.ascontiguousarray(base[y0:y1, x0:x1, :3], dtype=np.float32)
    rh, rw = img.shape[:2]
    dens = np.zeros((rh, rw), np.float32)
    dens[
        density.y0 - y0 : density.y1 - y0, density.x0 - x0 : density.x1 - x0
    ] = np.clip(density.arr, 0.0, 1.0)
    painted = dens > PAINTED
    if not painted.any():
        return None

    subject, region = _segment_subject(img, painted, px)
    check()
    # The fix applies to the painted pixels and to halo found in pockets the
    # stroke enclosed; treat the latter as fully painted.
    dens = np.maximum(dens, (region & ~painted).astype(np.float32))
    ring = (cv2.dilate(region.astype(np.uint8), _ellipse(cv2, px(OUTSIDE_RING))) > 0) & ~region

    background = ~subject
    dist = cv2.distanceTransform((~subject).astype(np.uint8), cv2.DIST_L2, 5)
    out = img.copy()

    # --- 3. glow: the subject's own light, defocused ----------------------
    bg_known = background & ~region
    soft_paint = cv2.GaussianBlur(dens, (0, 0), px(12))
    glow = _fit_glow(img, subject, background, region, bg_known, dist, px)
    if glow is not None:
        out -= glow * (soft_paint * background)[..., None]
    check()

    # --- 2. near band: inpaint the dark ring and ghost ---------------------
    near = region & background & (dist <= px(NEAR_BAND))
    if near.any():
        hole = cv2.dilate(near.astype(np.uint8), _ellipse(cv2, px(2))) > 0
        hole &= background
        # Hide the subject from LaMa as well: shown a bright subject beside a
        # hole, it continues the subject outward (the very glow being
        # removed), most of all in a narrow notch.  Only the band's pixels
        # are kept below.
        filled = inpaint(np.clip(out, 0, 1), hole | subject)
        check()
        # Match the fine grain of the untouched background.
        hp = img - cv2.GaussianBlur(img, (0, 0), 2.0)
        sample = bg_known & ring
        grain = float(hp[sample].std()) if sample.any() else 0.004
        rng = np.random.default_rng(1)
        noise = cv2.GaussianBlur(
            rng.normal(0, 1, (rh, rw)).astype(np.float32), (0, 0), 0.8
        )
        noise *= grain / max(float(noise.std()), 1e-6)
        filled = filled + noise[..., None]
        edge = np.clip((px(NEAR_BAND) + px(6) - dist) / px(12), 0.0, 1.0)
        edge = edge * edge * (3 - 2 * edge)
        # A soft seam: the subject's own anti-aliased edge stays.
        sub_soft = cv2.GaussianBlur(subject.astype(np.float32), (0, 0), max(1.2, px(1.6)))
        wfill = (cv2.GaussianBlur(hole.astype(np.float32), (0, 0), 1.5) * edge) * (
            1 - sub_soft
        )
        out = out * (1 - wfill[..., None]) + filled * wfill[..., None]

    # --- 4. subject pixels the dark ring crossed --------------------------
    sub_in = _crossed_subject(subject & painted, subject & ~region & ~painted, px)
    if sub_in.any():
        # The ring darkens smoothly, so compare averages over a patch the size
        # of the ring's width: the subject's own texture (dark scales between
        # bright ones) averages out and is kept, not lifted to the mean.
        local = _normconv(img, subject, px(10))
        ref = _smooth_fill(local, subject & ~region & ~painted)
        gain = np.clip(ref / np.maximum(local, 1e-3), 1.0, 2.0)
        # Ordinary variation along the subject is not a dark ring.
        gain = 1.0 + np.maximum(gain - 1.08, 0.0) / 0.92 * (gain - 1.0)
        gain = cv2.GaussianBlur(gain, (0, 0), px(4))
        wsub = cv2.GaussianBlur(sub_in.astype(np.float32), (0, 0), 1.0)
        out = out * (1 + (gain - 1) * wsub[..., None])

    # Everything above was weighted by the painted density already; alpha
    # only keeps the patch from overwriting layers below it away from the
    # stroke.
    alpha = np.clip(soft_paint * 3.0, 0.0, 1.0)
    # Keep only the part the patch changes: the context around the stroke
    # was needed for the analysis but would be blended with alpha 0.
    ys, xs = np.nonzero(alpha > 1e-4)
    if ys.size == 0:
        return None
    cy0, cy1 = int(ys.min()), int(ys.max()) + 1
    cx0, cx1 = int(xs.min()), int(xs.max()) + 1
    return HaloResult(
        signature=signature,
        base_shape=(h, w),
        x0=x0 + cx0,
        y0=y0 + cy0,
        x1=x0 + cx1,
        y1=y0 + cy1,
        rgb=np.ascontiguousarray(np.clip(out[cy0:cy1, cx0:cx1], 0.0, 1.0), np.float32),
        alpha=np.ascontiguousarray(alpha[cy0:cy1, cx0:cx1], np.float32),
    )


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------


def halo_identity(
    edits: Mapping[str, Any], results: Mapping[str, HaloResult]
) -> tuple:
    """Changes whenever the composited pixels could (for render caches)."""
    return tuple(
        (layer["id"], id(results.get(layer["id"])))
        for layer in halo_layers(edits)
    )


def composite_halo_layers(
    arr: np.ndarray,
    edits: Mapping[str, Any],
    results: Mapping[str, HaloResult],
) -> Tuple[np.ndarray, bool]:
    """Blend cached halo corrections into base-space float RGB *arr*.

    *arr* may be any downsized copy of the base image.  Never writes into
    *arr*: returns ``(new_array, True)`` when anything was blended.
    """
    layers = halo_layers(edits)
    if not layers:
        return arr, False
    cv2 = get_cv2()
    if cv2 is None:
        return arr, False
    h, w = arr.shape[:2]
    out = None
    for layer in layers:
        res = results.get(layer["id"])
        if res is None:
            continue
        bh, bw = res.base_shape
        sy, sx = h / bh, w / bw
        ax0, ay0 = int(np.floor(res.x0 * sx)), int(np.floor(res.y0 * sy))
        ax1, ay1 = min(w, int(np.ceil(res.x1 * sx))), min(h, int(np.ceil(res.y1 * sy)))
        if ax1 <= ax0 or ay1 <= ay0:
            continue
        if (ax1 - ax0, ay1 - ay0) == (res.x1 - res.x0, res.y1 - res.y0):
            rgb, alpha = res.rgb, res.alpha
        else:
            rgb, alpha = res.resized((ax1 - ax0, ay1 - ay0))
        if out is None:
            out = np.array(arr, dtype=np.float32, copy=True)
        region = out[ay0:ay1, ax0:ax1, :3]
        region += (rgb - region) * alpha[..., None]
    if out is None:
        return arr, False
    return out, True
