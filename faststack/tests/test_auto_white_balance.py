import numpy as np
from PIL import Image

from faststack.imaging.editor import ImageEditor


def _channel_spread(arr: np.ndarray) -> tuple[float, np.ndarray]:
    means = arr.reshape(-1, 3).mean(axis=0)
    return float(np.max(means) - np.min(means)), means


def test_estimate_auto_white_balance_reduces_mixed_cast():
    editor = ImageEditor()

    base = np.full((180, 180, 3), 0.45, dtype=np.float32)
    cast = base.copy()
    cast[:, :, 0] *= 0.72  # too little red
    cast[:, :, 1] *= 1.12  # too much green
    cast[:, :, 2] *= 1.30  # too much blue

    # Distractor patch: strongly coloured but not representative of the neutral field.
    cast[:90, :90] = np.array([0.12, 0.82, 0.12], dtype=np.float32)

    editor.float_preview = np.clip(cast, 0.0, 1.0)

    estimate = editor.estimate_auto_white_balance(
        strength=1.0,
        warm_bias=0,
        tint_bias=0,
        target_pixels=120_000,
    )

    assert estimate is not None
    assert estimate["by_value"] > 0.0
    assert estimate["mg_value"] > 0.0

    editor.current_edits = editor._initial_edits()
    editor.current_edits["white_balance_by"] = estimate["by_value"]
    editor.current_edits["white_balance_mg"] = estimate["mg_value"]

    corrected = editor._apply_edits(editor.float_preview.copy())

    before_spread, before_means = _channel_spread(cast[90:, 90:, :])
    after_spread, after_means = _channel_spread(corrected[90:, 90:, :])

    assert after_spread < before_spread * 0.45
    assert abs(after_means[0] - after_means[2]) < abs(before_means[0] - before_means[2])
    assert abs(after_means[1] - after_means[0]) < abs(before_means[1] - before_means[0])


def test_estimate_auto_white_balance_leaves_neutral_image_near_zero():
    editor = ImageEditor()
    editor.float_preview = np.full((96, 96, 3), 0.5, dtype=np.float32)

    estimate = editor.estimate_auto_white_balance(
        strength=1.0,
        warm_bias=0,
        tint_bias=0,
        target_pixels=20_000,
    )

    assert estimate is not None
    assert abs(estimate["by_value"]) < 0.02
    assert abs(estimate["mg_value"]) < 0.02


def test_save_image_uint8_white_balance_fast_path(tmp_path):
    editor = ImageEditor()

    arr = np.zeros((40, 40, 3), dtype=np.uint8)
    arr[:, :, 0] = 90
    arr[:, :, 1] = 120
    arr[:, :, 2] = 180

    img = Image.fromarray(arr, "RGB")
    image_path = tmp_path / "awb-fast.jpg"
    img.save(image_path, quality=95)

    editor.original_image = img
    editor.current_filepath = image_path
    editor.current_edits = editor._initial_edits()
    editor.set_edit_param("white_balance_by", 0.4)

    result = editor.save_image_uint8_white_balance()

    assert result is not None
    saved_path, backup_path = result
    assert saved_path.exists()
    assert backup_path.exists()

    saved = np.asarray(Image.open(saved_path).convert("RGB"), dtype=np.float32)
    assert saved[:, :, 0].mean() > arr[:, :, 0].mean()
    assert saved[:, :, 2].mean() < arr[:, :, 2].mean()
