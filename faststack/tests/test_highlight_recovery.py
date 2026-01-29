"""Tests for highlight recovery system.

Tests the new brightness-based highlight recovery that:
- Preserves hue/chroma via brightness rescaling
- Uses adaptive parameters based on headroom and clipping
- Handles both 16-bit (headroom) and 8-bit (JPEG) sources
"""

import sys
import os

# Add parent directory to path for standalone execution
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# Mock cv2 if not available (for test environments)
try:
    import cv2
except ImportError:
    from unittest import mock

    sys.modules["cv2"] = mock.MagicMock()

import numpy as np
import time

from faststack.imaging.math_utils import (
    _highlight_recover_linear,
    _highlight_boost_linear,
    _apply_headroom_shoulder,
    _analyze_highlight_state,
)


def test_monotonicity():
    """Gradient 0→2.0 should be non-decreasing after recovery."""
    # Create gradient with headroom
    gradient = np.linspace(0, 2.0, 100).reshape(10, 10)
    rgb = np.stack([gradient, gradient * 0.5, gradient * 0.3], axis=2).astype(
        np.float32
    )

    recovered = _highlight_recover_linear(rgb, amount=1.0, pivot=0.5)

    # Check max-channel brightness is non-decreasing
    brightness = recovered.max(axis=2).flatten()
    diffs = np.diff(brightness)
    eps = 1e-7

    assert np.all(diffs >= -eps), f"Monotonicity violated: min diff = {diffs.min()}"
    print("test_monotonicity passed")


def test_no_nan_inf():
    """Random input including edge cases should produce finite output."""
    np.random.seed(42)

    # Include zeros, ones, headroom, and extreme values
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
        recovered = _highlight_recover_linear(arr, amount=1.0, pivot=0.5)
        assert np.isfinite(recovered).all(), f"NaN/inf in test case {i}"

        boosted = _highlight_boost_linear(arr, amount=1.0, pivot=0.5)
        assert np.isfinite(boosted).all(), f"NaN/inf in boost test case {i}"

    print("test_no_nan_inf passed")


def test_hue_preservation():
    """Saturated highlight ramp should preserve RGB ratios (hue)."""
    # Create saturated red gradient with headroom
    brightness = np.linspace(0.1, 2.0, 50).reshape(5, 10)
    rgb = np.stack([brightness, brightness * 0.2, brightness * 0.2], axis=2).astype(
        np.float32
    )

    recovered = _highlight_recover_linear(
        rgb, amount=0.8, pivot=0.5, chroma_rolloff=0.0
    )

    # Check R:G:B ratios where brightness > 0.01
    orig_brightness = rgb.max(axis=2)
    mask = orig_brightness > 0.01

    if np.any(mask):
        # Normalize to get ratio
        orig_norm = rgb[mask] / (orig_brightness[mask, None] + 1e-7)
        rec_brightness = recovered.max(axis=2)
        rec_norm = recovered[mask] / (rec_brightness[mask, None] + 1e-7)

        # Ratios should be within 5%
        ratio_diff = np.abs(orig_norm - rec_norm).max()
        assert ratio_diff < 0.05, f"Hue shift too large: {ratio_diff}"

    print("test_hue_preservation passed")


def test_mask_isolation():
    """Pixels with max-channel below pivot should barely change."""
    # Create image with values below and above pivot
    low = np.ones((10, 10, 3), dtype=np.float32) * 0.3  # Below pivot 0.5

    recovered = _highlight_recover_linear(low, amount=1.0, pivot=0.5)

    # Changes should be minimal
    diff = np.abs(recovered - low).max()
    assert diff < 1e-4, f"Below-pivot pixels changed by {diff}"

    print("test_mask_isolation passed")


def test_plateau_stability():
    """Clipped [1,1,1] region should stay uniform after recovery (no ringing)."""
    # Uniform white plateau
    plateau = np.ones((20, 20, 3), dtype=np.float32)

    recovered = _highlight_recover_linear(plateau, amount=1.0, pivot=0.5)

    # All pixels should be the same (uniform)
    std = recovered.std()
    assert std < 1e-6, f"Plateau became non-uniform: std = {std}"

    print("test_plateau_stability passed")


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

    print("test_headroom_shoulder passed")


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

    print("test_analyze_highlight_state passed")


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

    print("test_source_clipping_detection passed")


def test_benchmark():
    """1920x1080 should be processed in reasonable time (vectorized)."""
    arr = np.random.rand(1080, 1920, 3).astype(np.float32)

    # Warm up
    _highlight_recover_linear(arr, amount=0.5, pivot=0.5)

    # Benchmark
    start = time.perf_counter()
    for _ in range(3):
        _highlight_recover_linear(arr, amount=0.5, pivot=0.5)
    elapsed = (time.perf_counter() - start) / 3

    print(f"test_benchmark: 1920x1080 recovery in {elapsed * 1000:.1f}ms")
    # Informational only - no hard assertion for CI stability


if __name__ == "__main__":
    try:
        test_monotonicity()
        test_no_nan_inf()
        test_hue_preservation()
        test_mask_isolation()
        test_plateau_stability()
        test_headroom_shoulder()
        test_analyze_highlight_state()
        test_source_clipping_detection()
        test_benchmark()
        print("\nALL TESTS PASSED")
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        exit(1)
