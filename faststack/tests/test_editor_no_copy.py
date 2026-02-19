import hashlib
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock, patch

from faststack.imaging.editor import ImageEditor


def fingerprint(arr: np.ndarray):
    """Strong content + identity-ish fingerprint."""
    return (
        str(arr.dtype),
        arr.shape,
        arr.strides,
        hashlib.sha256(arr.tobytes()).hexdigest(),
    )


def make_editor_with_image() -> ImageEditor:
    ed = ImageEditor()
    # Fixed, deterministic content
    img = np.linspace(0.0, 1.0, 10 * 10 * 3, dtype=np.float32).reshape((10, 10, 3))
    ed.float_image = img.copy()
    # Ensure current_edits exists (ImageEditor usually sets it)
    ed.current_edits = ed._initial_edits()
    ed.original_image = MagicMock()
    return ed


def test_apply_edits_no_copy_does_not_mutate_input():
    """
    Core safety contract: when _edits_can_share_input is True, passing float_image
    directly into _apply_edits(for_export=True) must not mutate it.
    """
    ed = make_editor_with_image()

    # Minimal sRGB-only edits; keep vignette/geometry off, keep linear edits off.
    ed.current_edits.update(
        {
            "brightness": 0.10,
            "contrast": 0.20,
            "saturation": 0.15,
            "vibrance": 0.10,
            "blacks": 0.05,
            "whites": -0.02,
            "vignette": 0.0,
            "rotation": 0,
            "straighten_angle": 0.0,
            "crop_box": None,
            "exposure": 0.0,
            "white_balance_by": 0.0,
            "white_balance_mg": 0.0,
            "highlights": 0.0,
            "shadows": 0.0,
            "clarity": 0.0,
            "texture": 0.0,
            "sharpness": 0.0,
        }
    )

    assert ed._edits_can_share_input(ed.current_edits) is True

    before = fingerprint(ed.float_image)
    _out = ed._apply_edits(ed.float_image, for_export=True)
    after = fingerprint(ed.float_image)

    assert (
        after == before
    ), "float_image was mutated by _apply_edits on the no-copy path"


def test_save_image_passes_float_image_without_copy_when_safe(tmp_path):
    """
    Wiring test: prove save_image uses the same float_image object when _edits_can_share_input is True.
    Avoid real disk I/O by mocking PIL save points, but use real files for availability checks
    to avoid global Path patches.
    """
    ed = make_editor_with_image()

    # Create a real dummy file so we don't need to patch Path.exists/stat globally
    dummy_file = tmp_path / "test.jpg"
    dummy_file.write_bytes(b"fake_jpg_content")

    # Set modify time to something non-zero for stat checks
    # (though typically write_bytes sets mtime)

    ed.current_filepath = dummy_file

    # Safe edits only (no vignette/geometry/linear edits)
    ed.current_edits.update(
        {
            "brightness": 0.10,
            "blacks": 0.02,
            "vignette": 0.0,
            "rotation": 0,
            "straighten_angle": 0.0,
            "crop_box": None,
            "exposure": 0.0,
            "white_balance_by": 0.0,
            "white_balance_mg": 0.0,
            "highlights": 0.0,
            "shadows": 0.0,
            "clarity": 0.0,
            "texture": 0.0,
            "sharpness": 0.0,
        }
    )

    assert ed._edits_can_share_input(ed.current_edits) is True

    seen = {"same_obj": False}

    # Use instance-specific spy to avoid intercepting calls from other tests/threads
    # We capture the *original* bound method of this instance
    real_apply = ed._apply_edits

    def spy_apply(arr, for_export=False, *args, **kwargs):
        # We only care about the call for this specific test instance
        if for_export and arr is ed.float_image:
            seen["same_obj"] = True
        return real_apply(arr, for_export=for_export, *args, **kwargs)

    # Mock all the save_image I/O edges locally or on the instance
    # We no longer patch Path.exists or Path.stat globally!

    # We still need to patch create_backup_file to avoid actual backup copying logic
    # or just let it run if we don't care?
    # The existing test patched it. Let's patch it to return a dummy path without side effects.

    with (
        patch.object(ed, "_apply_edits", side_effect=spy_apply),
        patch(
            "faststack.imaging.editor.create_backup_file",
            return_value=tmp_path / "backup.jpg",
        ),
        patch("PIL.Image.Image.save"),
        patch.object(ed, "_restore_file_times"),
        patch.object(ed, "_get_sanitized_exif_bytes", return_value=None),
    ):

        ed.save_image()

    assert (
        seen["same_obj"] is True
    ), "save_image did not pass self.float_image directly on safe no-copy path"


def test_edits_can_share_input_exclusions():
    ed = make_editor_with_image()

    # baseline should be safe
    assert ed._edits_can_share_input(ed.current_edits) is True

    # vignette
    ed.current_edits["vignette"] = 0.1
    assert ed._edits_can_share_input(ed.current_edits) is False
    ed.current_edits["vignette"] = 0.0

    # geometry
    ed.current_edits["rotation"] = 90
    assert ed._edits_can_share_input(ed.current_edits) is False
    ed.current_edits["rotation"] = 0

    ed.current_edits["straighten_angle"] = 1.0
    assert ed._edits_can_share_input(ed.current_edits) is False
    ed.current_edits["straighten_angle"] = 0.0

    ed.current_edits["crop_box"] = (0, 0, 5, 5)
    assert ed._edits_can_share_input(ed.current_edits) is False
    ed.current_edits["crop_box"] = None

    # linear edit
    ed.current_edits["exposure"] = 0.5
    assert ed._edits_can_share_input(ed.current_edits) is False


def test_skip_linear_export_clips_to_unit_range():
    ed = make_editor_with_image()

    # Force out-of-range via sRGB ops (brightness typically pushes above 1)
    ed.current_edits.update({"brightness": 0.8})

    assert ed._edits_skip_linear(ed.current_edits) is True

    out = ed._apply_edits(ed.float_image.copy(), for_export=True)
    assert out.min() >= 0.0
    assert out.max() <= 1.0


def test_bad_types_fallback():
    """Verify that string or None values in edits fall back to safe paths instead of crashing."""
    ed = make_editor_with_image()

    # None value for exposure
    ed.current_edits["exposure"] = None
    assert ed._edits_skip_linear(ed.current_edits) is False
    assert ed._edits_can_share_input(ed.current_edits) is False

    # String value for vignette
    ed.current_edits["exposure"] = 0.0
    ed.current_edits["vignette"] = "bad"
    assert ed._edits_can_share_input(ed.current_edits) is False

    # Non-numeric string for straighten_angle
    ed.current_edits["vignette"] = 0.0
    ed.current_edits["straighten_angle"] = "0.0001"  # Small enough string float
    assert ed._edits_can_share_input(ed.current_edits) is True
    ed.current_edits["straighten_angle"] = "very_bad"
    assert ed._edits_can_share_input(ed.current_edits) is False
