"""Tests for the Highlights slider (edge-aware local tone mapping).

The operator splits brightness into an edge-preserving base and the detail
riding on it, compresses only the base, and applies the result as a single
gain per pixel:
- Preserves hue/chroma (one gain for all three channels)
- Leaves regions below the pivot untouched
- Darkens bright regions while keeping the texture inside them
- Pulls exposure headroom back under display white
"""

import os
import sys

# Add parent directory to path for standalone execution
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

import time

import numpy as np
import pytest

from faststack.imaging.math_utils import (
    _analyze_highlight_state,
    _apply_headroom_shoulder,
    apply_highlight_tone,
    compute_highlight_base,
)


def _highlights(rgb: np.ndarray, amount) -> np.ndarray:
    p, base, headroom = compute_highlight_base(rgb)
    return apply_highlight_tone(rgb, amount, base, headroom, p=p)


def test_monotonicity():
    """A smooth ramp 0→2.0 should stay non-decreasing after recovery."""
    ramp = np.linspace(0, 2.0, 200, dtype=np.float32)
    gradient = np.tile(ramp, (16, 1))
    rgb = np.stack([gradient, gradient * 0.5, gradient * 0.3], axis=2)

    recovered = _highlights(rgb, -1.0)

    brightness = recovered[8, :, 0]
    diffs = np.diff(brightness)
    assert np.all(diffs >= -1e-6), f"Monotonicity violated: min diff = {diffs.min()}"
    # Headroom is brought back toward display white.
    assert brightness[-1] < 1.2, f"Headroom not compressed: {brightness[-1]}"


def test_no_nan_inf():
    """Random input including edge cases should produce finite output."""
    np.random.seed(42)

    test_cases = [
        np.random.rand(50, 50, 3).astype(np.float32),  # Normal
        np.zeros((10, 10, 3), dtype=np.float32),  # All zeros
        np.ones((10, 10, 3), dtype=np.float32),  # All ones
        np.ones((10, 10, 3), dtype=np.float32) * 2.0,  # Headroom
        np.array(
            [[[0, 0, 0], [1e-10, 1e-10, 1e-10], [10.0, 5.0, 2.0]]], dtype=np.float32
        ),  # Edge cases
    ]

    for i, arr in enumerate(test_cases):
        for amount in (-1.0, 1.0):
            out = _highlights(arr, amount)
            assert np.isfinite(out).all(), f"NaN/inf in case {i}, amount {amount}"


def test_hue_preservation():
    """Saturated highlights keep their RGB ratios (hue)."""
    ramp = np.linspace(0.1, 2.0, 100, dtype=np.float32)
    brightness = np.tile(ramp, (10, 1))
    rgb = np.stack([brightness, brightness * 0.2, brightness * 0.2], axis=2)

    recovered = _highlights(rgb, -0.8)

    orig_ratio = rgb[..., 1] / rgb[..., 0]
    rec_ratio = recovered[..., 1] / np.maximum(recovered[..., 0], 1e-12)
    assert np.abs(orig_ratio - rec_ratio).max() < 1e-4


def test_mask_isolation():
    """Regions below the pivot are untouched."""
    low = np.ones((10, 10, 3), dtype=np.float32) * 0.15  # p ≈ 0.39 < pivot

    for amount in (-1.0, -0.5, 0.5, 1.0):
        out = _highlights(low, amount)
        diff = np.abs(out - low).max()
        assert diff < 1e-6, f"Below-pivot pixels changed by {diff} at {amount}"


def test_plateau_stability():
    """Clipped [1,1,1] region should stay uniform after recovery (no ringing)."""
    plateau = np.ones((20, 20, 3), dtype=np.float32)

    recovered = _highlights(plateau, -1.0)

    assert recovered.std() < 1e-6, f"Plateau non-uniform: std = {recovered.std()}"
    assert recovered.mean() < 0.9, "Plateau should be darkened"


def test_headroom_shoulder():
    """Global shoulder should compress values > 1.0 correctly."""
    x = np.array([0.5, 1.0, 1.5, 2.0, 5.0], dtype=np.float32)
    out = _apply_headroom_shoulder(x, max_overshoot=0.05)

    # f(x) for x <= 1 should be unchanged
    assert out[0] == 0.5
    assert out[1] == 1.0

    # f(x) for x > 1 should be > 1 but < x
    for i in range(2, len(x)):
        assert out[i] > 1.0, f"Value at {x[i]} should be > 1.0, got {out[i]}"
        assert out[i] < x[i], f"Value at {x[i]} should be compressed, got {out[i]}"

    # Should be monotonic
    assert np.all(np.diff(out) >= 0), "Shoulder is not monotonic"


def test_analyze_highlight_state():
    """Highlight state analysis should detect headroom and clipping."""
    # Image with headroom
    headroom_img = np.ones((10, 10, 3), dtype=np.float32) * 1.5
    state = _analyze_highlight_state(headroom_img)
    assert (
        state["headroom_pct"] > 0.9
    ), f"Should detect headroom: {state['headroom_pct']}"

    # Normal image
    normal_img = np.ones((10, 10, 3), dtype=np.float32) * 0.5
    state = _analyze_highlight_state(normal_img)
    assert (
        state["headroom_pct"] < 0.01
    ), f"Should not detect headroom: {state['headroom_pct']}"


def test_source_clipping_detection():
    """Verify that srgb_u8 correctly influences clipping results even if linear is dimmed."""
    # 1. Create a "clipped" source image (uint8)
    srgb_u8 = np.ones((10, 10, 3), dtype=np.uint8) * 255

    # 2. Create a "dimmed" linear image (it was clipped in source, but exposure pulled it down)
    # Even though it's 0.2, it WAS clipped at the source.
    rgb_linear = np.ones((10, 10, 3), dtype=np.float32) * 0.2

    # 3. Analyze WITHOUT srgb_u8 -> should report 0 clipping because 0.2 < threshold
    state_no_u8 = _analyze_highlight_state(rgb_linear, srgb_u8=None)
    assert state_no_u8["source_clipped_pct"] == 0.0

    # 4. Analyze WITH srgb_u8 -> should report 100% clipping because srgb_u8 is 255
    state_with_u8 = _analyze_highlight_state(rgb_linear, srgb_u8=srgb_u8)
    assert state_with_u8["source_clipped_pct"] == 1.0


@pytest.mark.skipif(
    not os.environ.get("FASTSTACK_BENCH"),
    reason="informational benchmark; set FASTSTACK_BENCH=1 to run",
)
def test_benchmark():
    """1920x1080 should be processed in reasonable time (vectorized)."""
    arr = np.random.rand(1080, 1920, 3).astype(np.float32)

    _highlights(arr, -0.5)  # Warm up

    start = time.perf_counter()
    for _ in range(3):
        _highlights(arr, -0.5)
    elapsed = (time.perf_counter() - start) / 3

    print(f"test_benchmark: 1920x1080 highlights in {elapsed * 1000:.1f}ms")
    # Informational only - no hard assertion for CI stability


def test_amount_zero_identity():
    """amount=0 returns the input unchanged, scalar or per-pixel map."""
    arr = np.random.rand(20, 20, 3).astype(np.float32) * 1.5  # Include headroom

    assert np.array_equal(_highlights(arr, 0.0), arr)
    zero_map = np.zeros(arr.shape[:2], dtype=np.float32)
    assert np.abs(_highlights(arr, zero_map) - arr).max() < 1e-6


def test_detail_preserved():
    """Recovery darkens a bright textured region without flattening its texture."""
    rng = np.random.default_rng(7)
    p = np.clip(0.9 + rng.normal(0.0, 0.02, (128, 128)), 0, None).astype(np.float32)
    v = p * p
    rgb = np.stack([v, v, v], axis=2)

    out = _highlights(rgb, -1.0)

    p_out = np.sqrt(out[..., 0])
    assert p_out.mean() < p.mean() - 0.05, "Bright region should get darker"
    assert p_out.std() >= p.std(), "Texture amplitude must not shrink"


def test_increasing_amount_increases_compression():
    """Higher amount should result in more compression of highlights."""
    bright = np.ones((10, 10, 3), dtype=np.float32) * 1.5

    recovered_low = _highlights(bright, -0.3)
    recovered_high = _highlights(bright, -0.9)

    avg_low = recovered_low.mean()
    avg_high = recovered_high.mean()
    assert (
        avg_high <= avg_low
    ), f"Higher amount should compress more: avg_low={avg_low:.4f}, avg_high={avg_high:.4f}"
