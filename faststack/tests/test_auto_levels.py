import numpy as np
import pytest
from PIL import Image

from faststack.imaging.editor import ImageEditor

# analyze_auto_levels quantizes to 1024 bins before taking percentiles, so an
# endpoint recovered in 0-255 space lands within one bin (255/1023 ~= 0.25) of
# the original 8-bit value rather than exactly on it.
_BIN = 255.0 / 1023.0


def _approx_255(value):
    return pytest.approx(value, abs=_BIN)


def _approx_slider(value):
    """Same tolerance, expressed in slider units (endpoints are divided by 40)."""
    return pytest.approx(value, abs=_BIN / 40.0)


def test_auto_levels_pins_highlights_if_clipped():
    editor = ImageEditor()
    # 10x10 image
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 100

    # Clip Blue: Set last pixel to 255
    arr[9, 9, 2] = 255

    img = Image.fromarray(arr, "RGB")
    editor.original_image = img
    editor._preview_image = img

    # Use threshold 0.0 to make p_low deterministic (min value)
    # This prevents fragility with per-channel percentiles on small arrays
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.0)

    # 1 pixel of 255 in 100 is 1%. Eps (from threshold 0.0) would be 0.0.
    # Actually logic is eps = min(threshold, 0.01). If threshold 0.0, eps=0.0.
    # 1% > 0.0% -> Pins.

    assert p_high == _approx_255(255.0)
    assert whites == 0.0

    # p_low should be the strict minimum (100)
    assert p_low == _approx_255(100.0)


def test_auto_levels_pins_shadows_if_clipped():
    editor = ImageEditor()
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 100

    # Clip Red shadow: 1 pixel at 0.
    arr[0, 0, 0] = 0

    img = Image.fromarray(arr, "RGB")
    editor.original_image = img
    editor._preview_image = img

    # Threshold 0.0 -> eps=0.0. 1% detected > 0.0 -> Pins.
    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.0)

    assert p_low == _approx_255(0.0)
    assert blacks == 0.0

    # Whites should be normal (max is 100)
    assert p_high == _approx_255(100.0)
    assert whites == _approx_slider((255.0 - 100.0) / 40.0)


def test_auto_levels_tiny_hot_pixel_ignored():
    """
    Verify that a very small number of clipped pixels (below eps check)
    does NOT trigger pinning, and does NOT get picked up by percentile
    if strictly below the threshold.
    """
    editor = ImageEditor()
    # 200x200 = 40,000 pixels
    w, h = 200, 200
    arr = np.zeros((h, w, 3), dtype=np.uint8)

    # Base: 150
    arr[:] = 150

    # Set top ~2.5% pixels to 200 (1000 pixels)
    # This ensures the 99.9th percentile lands on 200, not 150.
    # Flattening for easier assignment
    flat = arr.reshape(-1, 3)
    flat[0:1000, :] = 200
    arr = flat.reshape(h, w, 3)

    # Add ONE hot pixel at 255 in Red channel
    arr[0, 0, 0] = 255

    img = Image.fromarray(arr, "RGB")
    editor.original_image = img
    editor._preview_image = img

    # Threshold 0.1%. Eps = 0.01%.
    # 1 pixel / 40000 = 0.0025%.
    # 0.0025% < 0.01%. Should NOT pin.

    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.1)

    # p_high should be 200 (from the 200-level plateau), ignoring the 255.
    assert p_high == _approx_255(200.0)
    assert p_high != 255.0  # Check explicitly not pinned
    assert whites > 0.0


def test_auto_levels_degenerate_image():
    editor = ImageEditor()
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 128

    img = Image.fromarray(arr, "RGB")
    editor.original_image = img
    editor._preview_image = img

    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.1)

    assert p_high == _approx_255(128.0)
    assert p_low == _approx_255(128.0)
    assert blacks == 0.0
    assert whites == 0.0


def test_auto_levels_normal_range():
    editor = ImageEditor()
    w, h = 10, 10
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[:] = 128
    arr[0, 0, :] = 50  # Low
    arr[9, 9, :] = 200  # High

    img = Image.fromarray(arr, "RGB")
    editor.original_image = img
    editor._preview_image = img

    blacks, whites, p_low, p_high = editor.auto_levels(threshold_percent=0.0)

    assert p_high == _approx_255(200.0)
    assert p_low == _approx_255(50.0)

    assert p_high != 255.0  # Not pinned
    assert p_low != 0.0  # Not pinned

    assert whites == _approx_slider((255.0 - 200.0) / 40.0)
    assert blacks == _approx_slider(-50.0 / 40.0)


def _gradient_editor():
    """Editor on a smooth 0..255 ramp with a few near-white highlights."""
    editor = ImageEditor()
    ramp = np.linspace(20, 200, 200 * 200).reshape(200, 200)
    arr = np.repeat(ramp[:, :, None], 3, axis=2)
    # 0.5% of the frame is a bright highlight that a 2% clip blows out.
    arr[:1, :, :] = 245
    img = Image.fromarray(arr.astype(np.uint8), "RGB")
    editor.original_image = img
    editor._preview_image = img
    return editor


def test_white_threshold_none_matches_shared_threshold():
    editor = _gradient_editor()
    baseline = editor.analyze_auto_levels(0.5)
    explicit = editor.analyze_auto_levels(0.5, white_threshold_percent=None)
    assert explicit == baseline


def test_white_threshold_moves_only_the_white_point():
    editor = _gradient_editor()
    b0, w0, lo0, hi0 = editor.analyze_auto_levels(2.0)
    b1, w1, lo1, hi1 = editor.analyze_auto_levels(2.0, white_threshold_percent=0.01)
    # Blacks stay bit-identical; the tighter highlight clip brightens less.
    assert (b1, lo1) == (b0, lo0)
    assert hi1 > hi0
    assert w1 < w0


def test_white_threshold_still_lifts_a_dark_photo_fully():
    # No bright pixels at all: a tiny whites clip must not stop the stretch.
    editor = ImageEditor()
    ramp = np.linspace(10, 90, 100 * 100).reshape(100, 100)
    arr = np.repeat(ramp[:, :, None], 3, axis=2).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    editor.original_image = img
    editor._preview_image = img
    _b, whites, _lo, p_high = editor.analyze_auto_levels(
        0.1, white_threshold_percent=0.0001
    )
    assert p_high == _approx_255(90.0)
    assert whites == _approx_slider((255.0 - 90.0) / 40.0)


def _recommend(editor, white=0.0):
    import types

    from faststack.app import AppController

    ns = types.SimpleNamespace(
        image_editor=editor,
        auto_level_threshold=0.1,
        auto_level_black_threshold=0.0,
        auto_level_white_threshold=white,
        auto_level_strength=1.0,
        auto_level_strength_auto=False,
        auto_level_midtone=True,
        auto_level_midtone_target=0.38,
        auto_level_channel_budget=3.0,
    )
    ns._black_threshold_percent = lambda: None
    ns._white_threshold_percent = lambda: white if white > 0 else None
    return AppController._compute_auto_levels_recommendation(ns)


def _dim_subject_editor(highlight=True):
    editor = ImageEditor()
    ramp = np.linspace(5, 90, 200 * 200).reshape(200, 200)
    arr = np.repeat(ramp[:, :, None], 3, axis=2)
    if highlight:
        arr[:2, :, :] = 250  # 1% bright highlight
    img = Image.fromarray(arr.astype(np.uint8), "RGB")
    editor.original_image = img
    editor._preview_image = img
    return editor


def test_explicit_whites_clip_blocks_midtone_lift_into_highlights():
    editor = _dim_subject_editor()
    default = _recommend(editor)
    capped = _recommend(editor, white=0.01)
    # The default path lifts the dim subject; an explicit Whites Clip % keeps
    # the lift from rolling the full-stretch white point toward white.
    assert default["base_brightness"] > 0.0
    assert capped["base_brightness"] == 0.0
    assert capped["base_blacks"] == default["base_blacks"]


def test_levels_endpoints_never_cross():
    from faststack.imaging.editor import _apply_levels_ramp, _levels_endpoints

    # Slider extremes: Whites +300 (6.0) with Blacks -300 (-3.0) would put
    # the white point below the black point and render a negative.
    bp, wp = _levels_endpoints(-3.0, 6.0)
    assert wp - bp == pytest.approx(0.05)
    ramp = _apply_levels_ramp(np.array([0.0, 0.5, 1.0], dtype=np.float32), -3.0, 6.0)
    assert np.all(np.diff(ramp) > 0)
    # Anything the old +/-100 sliders could reach is untouched.
    assert _levels_endpoints(-1.0, 2.0) == pytest.approx((0.15, 0.7))


def test_whites_clip_skips_lift_under_positive_contrast():
    editor = _dim_subject_editor()
    editor.current_edits["contrast"] = 0.5
    assert _recommend(editor, white=0.01)["base_brightness"] <= 0.0
