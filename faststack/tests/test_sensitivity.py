import os
import sys

import numpy as np

# Add parent directory to sys.path to allow importing faststack
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from faststack.imaging.editor import ImageEditor


def _dummy_image():
    """100x100 with a red left half and a green right half."""
    arr = np.zeros((100, 100, 3), dtype=np.float32)
    arr[:, :50, 0] = 0.8
    arr[:, 50:, 1] = 0.8
    return arr


def test_contrast_saturation_sensitivity():
    print("Testing contrast and saturation sensitivity...")
    editor = ImageEditor()
    arr = _dummy_image()
    editor.float_preview = arr

    # Contrast at 100 (backend value 1.0). Slider gain is still 0.4 per unit,
    # but the response is the v2 S-curve rather than a linear ramp about 0.5:
    #   factor = 1.0 + 1.0 * 0.4 = 1.4
    #   u = 0.8 ** 1.4, l = 0.2 ** 1.4, out = u / (u + l) = 0.8744...
    # The S-curve keeps the endpoints fixed instead of pushing values past the
    # clipping limits, so a strong contrast setting no longer blows highlights.
    print("Testing Contrast at 1.0...")
    edits = editor._initial_edits()
    edits["contrast"] = 1.0
    out = editor._apply_edits(arr.copy(), edits=edits)
    val = out[0, 0, 0]
    print(f"Contrast 1.0 result: {val}")
    assert np.allclose(val, 0.8744, atol=0.01), f"Expected 0.8744, got {val}"
    assert val < 1.0, "the v2 contrast curve must not drive 0.8 to clipping"

    # Saturation is unchanged by the tone-curve work: gray + (x - gray) * 1.5
    # with Rec.601 luma, so 0.8 red -> 0.2392 + 0.5608 * 1.5 = 1.0804.
    print("Testing Saturation at 1.0...")
    edits = editor._initial_edits()
    edits["saturation"] = 1.0
    out = editor._apply_edits(arr.copy(), edits=edits)
    val_sat = out[0, 0, 0]
    print(f"Saturation 1.0 result: {val_sat}")
    assert np.allclose(val_sat, 1.0804, atol=0.01), f"Expected 1.0804, got {val_sat}"
    print("All tests passed!")


def test_tone_curve_version_does_not_change_rendering():
    """A recipe stamped v1 renders identically to the same recipe stamped v2.

    v1 used to mean "linear contrast, unbounded brightness". It no longer
    selects a renderer: every recipe gets the v2 response so a photo looks the
    same wherever it is opened. The stored number is provenance only.
    """
    editor = ImageEditor()
    arr = _dummy_image()
    editor.float_preview = arr

    for key, value in (("contrast", 1.0), ("brightness", 0.5), ("contrast", -1.0)):
        rendered = {}
        for version in (1, 2):
            edits = editor._initial_edits()
            edits["tone_curve_version"] = version
            edits[key] = value
            rendered[version] = editor._apply_edits(arr.copy(), edits=edits)
        assert np.array_equal(rendered[1], rendered[2]), (
            f"{key}={value} rendered differently for a v1 recipe "
            f"(max delta {np.abs(rendered[1] - rendered[2]).max()})"
        )

    rendered = {}
    for version in (1, 2):
        edits = editor._initial_edits()
        edits.update(
            tone_curve_version=version,
            blacks=0.2,
            whites=-0.2,
            brightness=0.5,
        )
        rendered[version] = editor._apply_edits(arr.copy(), edits=edits)
    assert np.array_equal(rendered[1], rendered[2])

    # Levels maps 0.8 to (0.8 + 0.03) / 1.06 before the positive brightness
    # roll-off. Reversing those stages would produce about 0.9702 instead.
    assert np.allclose(rendered[2][0, 0, 0], 0.9974522, atol=1e-6)


def test_unsupported_tone_curve_version_is_rejected():
    """A recipe from a newer FastStack must fail loudly, not be guessed at."""
    import pytest

    editor = ImageEditor()
    arr = _dummy_image()
    editor.float_preview = arr
    edits = editor._initial_edits()
    edits["tone_curve_version"] = 99
    with pytest.raises(RuntimeError, match="Unsupported tone curve version"):
        editor._apply_edits(arr.copy(), edits=edits)


if __name__ == "__main__":
    try:
        test_contrast_saturation_sensitivity()
        test_tone_curve_version_does_not_change_rendering()
        test_unsupported_tone_curve_version_is_rejected()
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)
