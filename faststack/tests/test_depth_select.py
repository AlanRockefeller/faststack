"""Background selection by depth (faststack.imaging.depth_select).

The depth model is not available in tests; a stub estimator that returns a
synthetic map (a near disc in the middle of a far background) stands in.
"""

import numpy as np
import pytest

from faststack.imaging.depth_select import (
    DepthMap,
    joint_bilateral_upsample,
    sharpness_map,
    compute_depth_map,
    depth_layer_density,
    depth_selection,
    edits_need_depth_map,
    is_depth_layer,
    normalize_depth_params,
    selection_from_near,
)
from faststack.imaging.editor import geometry_normalized_affine
from faststack.imaging.local_adjust import (
    build_local_adjustments,
    effective_layers,
    normalize_local_layers,
)
from faststack.imaging.mask import STROKE_ERASE, STROKE_PAINT, MaskData, MaskStroke
from faststack.imaging.mask_engine import LayerDensityCache, rasterize_layer_erased

pytest.importorskip("cv2")


def _disc_near(h, w, radius_frac=0.25):
    """Nearness 1 inside a centred disc, 0 outside."""
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(yy - (h - 1) / 2, xx - (w - 1) / 2)
    return (r < radius_frac * min(h, w)).astype(np.float32)


def _depth_map(h=120, w=160):
    near = _disc_near(h, w)
    near.setflags(write=False)
    return DepthMap(near=near, base_shape=(h, w))


def _layer(amount=0.5, feather=0.0, recipe=None):
    return {
        "id": "L1",
        "visible": True,
        "recipe": recipe if recipe is not None else {"exposure": -0.5},
        "depth": {"amount": amount, "feather": feather},
    }


def _identity_transform(shape):
    h, w = shape
    return np.diag([float(w), float(h), 1.0])


# ---------------------------------------------------------------------------
# Parameters and layer list
# ---------------------------------------------------------------------------


def test_normalize_depth_params_clamps_and_defaults():
    assert normalize_depth_params(None) is None
    assert normalize_depth_params("x") is None
    p = normalize_depth_params({"amount": 2.0, "feather": "bad"})
    assert p["amount"] == 1.0
    assert 0.0 <= p["feather"] <= 1.0
    assert normalize_depth_params({"amount": float("nan")})["amount"] < 1.0


def test_depth_key_survives_normalize_local_layers():
    layers = normalize_local_layers([_layer(0.4, 0.2)])
    assert is_depth_layer(layers[0])
    assert layers[0]["depth"] == {"amount": 0.4, "feather": 0.2, "keep_sharp": 1.0}
    # Stack and halo layers never become depth layers.
    stack = normalize_local_layers(
        [{"id": "S", "source": "a.jpg", "depth": {"amount": 1}}]
    )
    assert not is_depth_layer(stack[0])


def test_edits_need_depth_map_only_for_visible_effective_selection():
    assert edits_need_depth_map({"local_layers": [_layer()]})
    assert not edits_need_depth_map({"local_layers": [_layer(recipe={})]})
    assert not edits_need_depth_map({"local_layers": [_layer(amount=0.0)]})
    hidden = {**_layer(), "visible": False}
    assert not edits_need_depth_map({"local_layers": [hidden]})


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_sweeps_far_to_near():
    near = np.linspace(0.0, 1.0, 101, dtype=np.float32)
    nothing = selection_from_near(near, 0.0, 0.3)
    everything = selection_from_near(near, 1.0, 0.3)
    assert nothing.max() == 0.0
    assert everything.min() == pytest.approx(1.0)
    previous = nothing
    for amount in (0.2, 0.4, 0.6, 0.8):
        sel = selection_from_near(near, amount, 0.3)
        # Far (low nearness) is selected before near.
        assert np.all(np.diff(sel) <= 1e-6)
        assert np.all(sel >= previous - 1e-6)
        previous = sel


def test_feather_widens_the_transition():
    near = np.linspace(0.0, 1.0, 1001, dtype=np.float32)
    hard = selection_from_near(near, 0.5, 0.0)
    soft = selection_from_near(near, 0.5, 1.0)
    partial = lambda s: int(np.count_nonzero((s > 0.01) & (s < 0.99)))  # noqa: E731
    assert partial(soft) > 5 * partial(hard)


def test_depth_selection_selects_background_not_subject():
    dm = _depth_map()
    shape = (60, 80)
    sel = depth_selection(dm, {"amount": 0.5, "feather": 0.0}, shape, _identity_transform(shape))
    full = np.zeros(shape, np.float32)
    full[sel.y0 : sel.y1, sel.x0 : sel.x1] = sel.arr
    assert full[0, 0] == pytest.approx(1.0)  # far corner selected
    assert full[30, 40] == pytest.approx(0.0)  # near centre kept


def test_depth_selection_none_when_amount_zero_or_no_map():
    shape = (60, 80)
    t = _identity_transform(shape)
    assert depth_selection(_depth_map(), {"amount": 0.0, "feather": 0.3}, shape, t) is None
    assert depth_selection(None, {"amount": 0.5, "feather": 0.3}, shape, t) is None


def test_depth_selection_follows_rotation():
    # A map whose left half is far: after a 90 degree rotation the far half
    # must land where the left side went.
    h, w = 40, 60
    near = np.ones((h, w), np.float32)
    near[:, : w // 2] = 0.0
    dm = DepthMap(near=near, base_shape=(h, w))
    edits = {"rotation": 90}
    out_shape = (w, h)  # rotated
    t = np.diag([out_shape[1], out_shape[0], 1.0]) @ geometry_normalized_affine(edits, (h, w))
    sel = depth_selection(dm, {"amount": 0.5, "feather": 0.0}, out_shape, t)
    full = np.zeros(out_shape, np.float32)
    full[sel.y0 : sel.y1, sel.x0 : sel.x1] = sel.arr
    # Base point (x=0.1, y=0.5) is far; check that its image is selected and
    # base point (0.9, 0.5) is not.
    for bx, want in ((0.1, 1.0), (0.9, 0.0)):
        px, py, _ = t @ np.array([bx, 0.5, 1.0])
        assert full[int(py), int(px)] == pytest.approx(want, abs=1e-3)


# ---------------------------------------------------------------------------
# Strokes combined with the selection
# ---------------------------------------------------------------------------


def _stroke(mode, x=0.5, y=0.5, radius=0.05):
    return MaskStroke(points=[(x, y)], radius=radius, hardness=1.0, mode=mode)


def test_erase_cuts_the_selection_and_paint_adds_to_it():
    dm = _depth_map()
    shape = (120, 160)
    t = _identity_transform(shape)
    layer = _layer(0.5, 0.0)
    mask = MaskData()
    # Erase a far corner, paint the (unselected) near centre.
    mask.add_stroke(_stroke(STROKE_ERASE, 0.05, 0.05))
    mask.add_stroke(_stroke(STROKE_PAINT, 0.5, 0.5))
    d = depth_layer_density(layer, mask, shape, {}, dm, t, cache=LayerDensityCache())
    full = np.zeros(shape, np.float32)
    full[d.y0 : d.y1, d.x0 : d.x1] = d.arr
    assert full[6, 8] == pytest.approx(0.0)  # erased out of the selection
    assert full[60, 80] == pytest.approx(1.0)  # painted onto the subject
    assert full[110, 150] == pytest.approx(1.0)  # untouched background
    assert full.max() <= 1.0  # painting over the selection does not double it


def test_without_depth_map_only_strokes_apply():
    shape = (120, 160)
    layer = _layer(0.5, 0.0)
    mask = MaskData()
    assert depth_layer_density(layer, mask, shape, {}, None, _identity_transform(shape)) is None
    mask.add_stroke(_stroke(STROKE_PAINT))
    mask.add_stroke(_stroke(STROKE_PAINT))  # two coats still clamp to one
    d = depth_layer_density(layer, mask, shape, {}, None, _identity_transform(shape))
    assert d is not None and d.arr.max() == pytest.approx(1.0)


def test_rasterize_layer_erased_ignores_paint_and_is_cached():
    shape = (50, 50)
    mask = MaskData()
    mask.add_stroke(_stroke(STROKE_PAINT))
    assert rasterize_layer_erased("L", mask, shape, {}) is None
    mask.add_stroke(_stroke(STROKE_ERASE))
    cache = LayerDensityCache()
    first = rasterize_layer_erased("L", mask, shape, {}, cache=cache)
    assert first is not None and first.arr.max() == pytest.approx(1.0)
    assert rasterize_layer_erased("L", mask, shape, {}, cache=cache) is first
    # Pruning the layer drops its erased entries too.
    cache.prune({"other"})
    assert not cache


# ---------------------------------------------------------------------------
# Render integration
# ---------------------------------------------------------------------------


def test_depth_layer_without_strokes_is_rendered():
    shape = (120, 160)
    edits = {"local_layers": [_layer(0.5, 0.0)]}
    assert effective_layers(edits)
    local = build_local_adjustments(
        edits,
        {},
        shape,
        LayerDensityCache(),
        base_shape=shape,
        point_transform=_identity_transform(shape),
        depth_map=_depth_map(),
    )
    assert local is not None
    exposure = np.zeros(shape, np.float32)
    exposure[local.roi] = local.maps["exposure"]
    assert exposure[0, 0] == pytest.approx(-0.5)
    assert exposure[60, 80] == pytest.approx(0.0)


def test_linear_identity_changes_with_amount_and_map():
    shape = (120, 160)
    dm = _depth_map()

    def identity(amount, depth_map):
        local = build_local_adjustments(
            {"local_layers": [_layer(amount, 0.0)]},
            {},
            shape,
            None,
            base_shape=shape,
            point_transform=_identity_transform(shape),
            depth_map=depth_map,
        )
        return local.linear_identity

    assert identity(0.5, dm) != identity(0.6, dm)
    assert identity(0.5, dm) != identity(0.5, _depth_map())


# ---------------------------------------------------------------------------
# Depth map computation
# ---------------------------------------------------------------------------


def test_compute_depth_map_normalises_and_keeps_aspect():
    h, w = 300, 400
    base = np.full((h, w, 3), 0.3, np.float32)
    calls = []

    def estimate(img):
        calls.append(img.shape)
        return _disc_near(42, 56) * 7.0 + 3.0  # arbitrary scale and offset

    dm = compute_depth_map(base, estimate)
    assert calls == [(h, w, 3)]
    assert dm.base_shape == (h, w)
    assert dm.near.shape == (h, w)  # smaller than DEPTH_MAP_LONG_EDGE: full size
    assert dm.near.min() >= 0.0 and dm.near.max() <= 1.0
    assert dm.near[150, 200] > 0.9 and dm.near[5, 5] < 0.1
    assert not dm.near.flags.writeable


@pytest.mark.parametrize("shape", [(300, 400, 3), (1800, 2400, 3)])
def test_compute_depth_map_leaves_out_of_range_base_untouched(shape):
    # Headroom past 1.0 (and below 0) is clipped only in the map's own copy,
    # at full size and when the photo is shrunk to DEPTH_MAP_LONG_EDGE.
    base = np.linspace(-0.5, 1.5, int(np.prod(shape)), dtype=np.float32).reshape(shape)
    before = base.copy()
    dm = compute_depth_map(base, lambda img: _disc_near(42, 56))
    np.testing.assert_array_equal(base, before)
    assert dm.sharp.min() >= 0.0 and dm.sharp.max() <= 1.0


def test_compute_depth_map_flat_output_selects_everything_as_far():
    base = np.zeros((40, 40, 3), np.float32)
    dm = compute_depth_map(base, lambda img: np.full((14, 14), 2.0, np.float32))
    assert dm.near.max() == 0.0


def test_compute_depth_map_honours_cancel():
    base = np.zeros((40, 40, 3), np.float32)
    assert compute_depth_map(base, lambda img: np.ones((14, 14)), cancelled=lambda: True) is None


def test_export_computes_missing_map_or_leaves_it_out(monkeypatch):
    from faststack.ai import depth as ai_depth
    from faststack.imaging.editor import ImageEditor

    source = np.zeros((40, 60, 3), np.float32)
    edits = {"local_layers": [_layer()]}

    class Stub:
        def estimate(self, img):
            return _disc_near(28, 42)

    monkeypatch.setattr(ai_depth, "get_estimator", lambda: Stub())
    dm = ImageEditor._depth_map_for_export(edits, None, source)
    assert dm is not None and dm.base_shape == (40, 60)
    # A preview map of the right shape is reused as is.
    assert ImageEditor._depth_map_for_export(edits, dm, source) is dm
    # Nothing to compute when no layer needs it.
    assert ImageEditor._depth_map_for_export({"local_layers": []}, None, source) is None

    def unavailable():
        raise ai_depth.DepthUnavailable("not installed")

    monkeypatch.setattr(ai_depth, "get_estimator", unavailable)
    assert ImageEditor._depth_map_for_export(edits, None, source) is None


# ---------------------------------------------------------------------------
# Edge refinement and sharpness
# ---------------------------------------------------------------------------


def _disc_photo(h=240, w=320, radius_frac=0.25):
    """A bright, finely textured disc on a flat black background."""
    rng = np.random.default_rng(0)
    disc = _disc_near(h, w, radius_frac) > 0.5
    rgb = np.zeros((h, w, 3), np.float32)
    texture = 0.6 + 0.15 * rng.standard_normal((h, w)).astype(np.float32)
    rgb[disc] = np.clip(texture[disc], 0, 1)[:, None] * np.array([1.0, 0.7, 0.6], np.float32)
    return rgb, disc


def test_upsampling_keeps_the_rim_at_the_objects_depth():
    rgb, disc = _disc_photo()
    # The model's view: the disc at low resolution, its edge blurred.
    import cv2

    low = cv2_blur(_disc_near(40, 54), 3)
    # As compute_depth_map weights them: samples on the edge count less.
    gx = cv2.Sobel(low, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(low, cv2.CV_32F, 0, 1, ksize=3)
    confidence = (1.0 / (1.0 + (np.hypot(gx, gy) * 10.0) ** 2) + 0.05).astype(np.float32)
    up = joint_bilateral_upsample(rgb, low, confidence)
    rim = disc & ~(cv2_erode(disc.astype(np.uint8), 7) > 0)
    outside_ring = (cv2_dilate(disc.astype(np.uint8), 7) > 0) & ~disc
    # Plain interpolation smears the edge both ways (about 0.56 / 0.38 here).
    plain = cv2.resize(low, (320, 240), interpolation=cv2.INTER_LINEAR)
    assert up[rim].mean() > 0.75 > plain[rim].mean() + 0.15
    assert up[outside_ring].mean() < 0.15 < plain[outside_ring].mean() - 0.15


def test_sharpness_map_separates_texture_from_flat_background():
    rgb, disc = _disc_photo()
    sharp = sharpness_map(rgb)
    inner = cv2_erode(disc.astype(np.uint8), 15) > 0
    far = ~(cv2_dilate(disc.astype(np.uint8), 15) > 0)
    assert sharp[inner].mean() > 0.8
    assert sharp[far].max() < 0.05


def test_keep_sharp_excludes_sharp_pixels_from_the_selection():
    h, w = 60, 80
    near = np.zeros((h, w), np.float32)  # everything is "far"
    sharp = np.zeros((h, w), np.float32)
    sharp[20:40, 30:50] = 1.0  # a sharp, thin object the model missed
    dm = DepthMap(near=near, base_shape=(h, w), sharp=sharp)
    t = _identity_transform((h, w))

    def full(keep):
        d = depth_selection(dm, {"amount": 0.5, "feather": 0.0, "keep_sharp": keep}, (h, w), t)
        out = np.zeros((h, w), np.float32)
        out[d.y0 : d.y1, d.x0 : d.x1] = d.arr
        return out

    assert full(0.0)[30, 40] == pytest.approx(1.0)
    assert full(1.0)[30, 40] == pytest.approx(0.0)
    assert full(0.5)[30, 40] == pytest.approx(0.5)
    assert full(1.0)[5, 5] == pytest.approx(1.0)  # blurred background still selected


def test_compute_depth_map_includes_sharpness():
    rgb, disc = _disc_photo()
    dm = compute_depth_map(rgb, lambda img: _disc_near(40, 54) * 5.0)
    assert dm.sharp is not None and dm.sharp.shape == dm.near.shape
    assert not dm.sharp.flags.writeable
    assert dm.sharp[~(cv2_dilate(disc.astype(np.uint8), 15) > 0)].max() < 0.05


def cv2_blur(a, k):
    import cv2

    return cv2.blur(a.astype(np.float32), (k, k))


def cv2_erode(a, k):
    import cv2

    return cv2.erode(a, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))


def cv2_dilate(a, k):
    import cv2

    return cv2.dilate(a, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))


# ---------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------


def test_export_with_no_depth_map_does_not_use_the_live_one():
    """A save whose map could not be computed passes None: the live editor's
    map (possibly another photo's by then) must not be used instead."""
    from faststack.imaging.editor import ImageEditor

    editor = ImageEditor()
    editor._depth_map = _depth_map()
    edits = dict(editor.current_edits)
    edits["local_layers"] = [_layer(0.5, 0.0)]
    source = np.full((120, 160, 3), 0.5, np.float32)
    live = editor._apply_edits(source.copy(), edits=edits, for_export=True)
    none = editor._apply_edits(
        source.copy(), edits=edits, for_export=True, depth_map_override=None
    )
    assert live[0, 0, 0] < 0.48  # the default still uses the live map
    assert none[0, 0, 0] == pytest.approx(none[60, 80, 0])


def test_shrunk_selection_averages_the_sharpness_map():
    """warpAffine has no area filter: a small preview must average the
    sharpness map, not point-sample it."""
    h, w = 400, 600
    near = np.zeros((h, w), np.float32)  # everything far: all selected
    sharp = np.zeros((h, w), np.float32)
    # Fine 1-px texture, 50% sharp on average.
    sharp[np.random.default_rng(1).random((h, w)) < 0.5] = 1.0
    near.setflags(write=False)
    sharp.setflags(write=False)
    dm = DepthMap(near=near, base_shape=(h, w), sharp=sharp)
    shape = (40, 60)
    sel = depth_selection(
        dm,
        {"amount": 1.0, "feather": 0.0, "keep_sharp": 1.0},
        shape,
        _identity_transform(shape),
    )
    assert sel is not None
    assert float(sel.arr.std()) < 0.05
    assert float(sel.arr.mean()) == pytest.approx(0.5, abs=0.05)


class _BkgdStub:
    """Just enough controller for DepthSelectMixin.select_background."""

    def __init__(self, layers, *, brush_mode=False, editing=None):
        from types import SimpleNamespace

        from faststack.controller.depth_select import DepthSelectMixin

        self.mixin = DepthSelectMixin
        self.ui_state = SimpleNamespace(
            isBrushMode=brush_mode,
            brushTool="local" if brush_mode else "",
            depthState={},
        )
        self.image_editor = SimpleNamespace(
            current_edits={"local_layers": layers},
            current_filepath="/photos/a.jpg",
            _depth_map=None,
        )
        self._local_editing_layer_id = editing
        self._depth_install_requested_path = None
        self._is_grid_view_active = False
        self.image_files = ["a.jpg"]
        self.shown_path = "/photos/a.jpg"
        self.selected = []
        self.added = 0
        self.scheduled = 0

    def __getattr__(self, name):
        attr = getattr(self.mixin, name)
        return attr.__get__(self)

    def _refresh_depth_install_status(self):
        from types import SimpleNamespace

        return SimpleNamespace(ready=True)

    def _refresh_halo_install_status(self):
        pass

    def _set_halo_state(self):
        pass

    def _set_depth_state(self, **changes):
        pass

    @staticmethod
    def _key(p):
        return str(p) if p is not None else None

    def _local_layer(self, layer_id):
        for layer in self.image_editor.current_edits["local_layers"]:
            if layer["id"] == layer_id:
                return layer
        return None

    def _current_image_path(self):
        return self.shown_path

    def _prepare_local_brush_image_state(self):
        return True

    def _enter_brush_mode(self, tool):
        self.ui_state.isBrushMode = True
        self.ui_state.brushTool = tool
        return True

    def select_local_layer(self, layer_id):
        self.selected.append(layer_id)
        self._local_editing_layer_id = layer_id

    def _add_depth_layer(self):
        self.added += 1

    def _schedule_depth_job(self):
        self.scheduled += 1

    def _refresh_local_brush_ui(self):
        pass

    def _update_local_overlay(self):
        pass

    def update_status_message(self, *args, **kwargs):
        pass


def test_bkgd_selects_restored_layer_outside_brush_mode():
    stub = _BkgdStub([_layer()])
    stub.select_background()
    assert stub.added == 0
    assert stub.selected == ["L1"]
    assert stub.ui_state.isBrushMode and stub.ui_state.brushTool == "local"


def test_bkgd_after_leaving_brush_mode_reselects_instead_of_adding():
    # Brush mode was left with the background layer still marked selected.
    stub = _BkgdStub([_layer()], brush_mode=False, editing="L1")
    stub.select_background()
    assert stub.added == 0
    assert stub.selected == []  # already selected: not toggled off
    assert stub.ui_state.isBrushMode


def test_bkgd_while_background_selected_adds_another():
    stub = _BkgdStub([_layer()], brush_mode=True, editing="L1")
    stub.select_background()
    assert stub.added == 1


def test_finished_download_adds_layer_only_to_the_photo_that_asked():
    stub = _BkgdStub([])
    stub._depth_install_requested_path = "/photos/a.jpg"
    stub._on_depth_install_done({"error": None})
    assert stub.added == 1

    stub = _BkgdStub([])
    stub._depth_install_requested_path = "/photos/a.jpg"
    stub.shown_path = "/photos/b.jpg"  # moved on meanwhile; the editor
    # may still hold a.jpg until brush mode reloads it
    stub._on_depth_install_done({"error": None})
    assert stub.added == 0
    assert stub.scheduled == 1

    # A photo with a restored layer gets it selected, not a second one.
    stub = _BkgdStub([_layer()])
    stub._depth_install_requested_path = "/photos/a.jpg"
    stub._on_depth_install_done({"error": None})
    assert stub.added == 0 and stub.selected == ["L1"]


def test_bkgd_reads_layers_after_brush_mode_reloads_the_photo():
    """Entering brush mode can load the shown photo into the editor; the
    decision must follow the photo loaded, not the stale editor."""
    stub = _BkgdStub([])  # the editor still holds a photo without layers

    def enter(tool):
        stub.image_editor.current_edits = {"local_layers": [_layer()]}
        stub._local_editing_layer_id = None
        stub.ui_state.isBrushMode = True
        stub.ui_state.brushTool = tool
        return True

    stub._enter_brush_mode = enter
    stub.select_background()
    assert stub.added == 0 and stub.selected == ["L1"]


def test_download_prompt_remembers_the_photo_on_screen():
    from types import SimpleNamespace

    stub = _BkgdStub([])
    stub._refresh_depth_install_status = lambda: SimpleNamespace(ready=False)
    stub.image_editor.current_filepath = "/photos/old.jpg"  # stale editor
    stub.select_background()
    assert stub._depth_install_requested_path == "/photos/a.jpg"


def test_effective_background_layer_adds_photo_to_batch():
    stub = _BkgdStub([])
    added = []
    stub._auto_add_edited_to_batch_if_enabled = added.append
    stub._auto_add_depth_edit_to_batch(_layer(0.5, recipe={}))
    stub._auto_add_depth_edit_to_batch(_layer(0.0))
    assert added == []
    stub._auto_add_depth_edit_to_batch(_layer(0.5))
    assert [str(p) for p in added] == ["/photos/a.jpg"]


class _OverlayStub:
    """The overlay-hold half of the controller: Ctrl and the depth sliders."""

    from faststack.app import AppController as _App
    from faststack.controller.depth_select import DepthSelectMixin as _Mixin

    _local_overlay_hold_sources = _App._local_overlay_hold_sources
    _set_local_overlay_held = _App._set_local_overlay_held
    set_depth_slider_active = _Mixin.set_depth_slider_active

    def __init__(self):
        from types import SimpleNamespace

        self.ui_state = SimpleNamespace(localOverlayHeld=False)
        self.updates = 0

    def _update_local_overlay(self):
        self.updates += 1

    def set_editor_slider_drag_active(self, active):
        pass


def test_releasing_a_depth_slider_keeps_the_mask_while_ctrl_holds_it():
    stub = _OverlayStub()
    stub._set_local_overlay_held(True)  # Ctrl down
    stub.set_depth_slider_active(True)
    stub.set_depth_slider_active(False)
    assert stub.ui_state.localOverlayHeld  # Ctrl is still down
    stub._set_local_overlay_held(False)  # Ctrl up
    assert not stub.ui_state.localOverlayHeld


def test_releasing_ctrl_keeps_the_mask_while_a_depth_slider_is_dragged():
    stub = _OverlayStub()
    stub.set_depth_slider_active(True)
    stub._set_local_overlay_held(True)
    stub._set_local_overlay_held(False)
    assert stub.ui_state.localOverlayHeld  # still dragging
    stub.set_depth_slider_active(False)
    assert not stub.ui_state.localOverlayHeld
    assert stub.updates == 2  # shown once, hidden once
