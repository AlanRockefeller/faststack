import numpy as np

from faststack.imaging.editor import ImageEditor
from faststack.imaging.optional_deps import get_cv2


def test_preview_detail_radii_match_downsampled_full_resolution_export():
    cv2 = get_cv2()
    assert cv2 is not None
    height, width = 192, 256
    y, x = np.mgrid[:height, :width].astype(np.float32)
    target = (
        0.45
        + 0.18 * np.sin(x * 2.0 * np.pi / 32.0)
        + 0.10 * np.sin(y * 2.0 * np.pi / 9.0)
        + 0.05 * (((x.astype(int) // 2 + y.astype(int) // 2) % 2) * 2 - 1)
    )
    source = np.repeat(np.clip(target, 0.0, 1.0)[..., None], 3, axis=2)
    preview_source = cv2.resize(
        source, (width // 2, height // 2), interpolation=cv2.INTER_AREA
    )
    editor = ImageEditor()
    edits = dict(editor.current_edits)
    edits.update({"clarity": 0.45, "texture": 0.35, "sharpness": 0.25})

    full = editor._apply_edits(source.copy(), edits=edits, for_export=True)
    expected_preview = cv2.resize(
        full, (width // 2, height // 2), interpolation=cv2.INTER_AREA
    )
    preview = editor._apply_edits(
        preview_source.copy(),
        edits=edits,
        for_export=False,
        detail_source_scale=0.5,
    )

    mean_error = float(np.mean(np.abs(preview - expected_preview)))
    assert mean_error < 0.015
