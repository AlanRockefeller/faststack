import numpy as np
import pytest

from faststack.imaging.editor import ImageEditor


@pytest.mark.parametrize(
    "rotation,angle,crop",
    [
        (0, 8.0, None),
        (0, 0.0, (120, 160, 880, 840)),
        (0, 8.0, (120, 160, 880, 840)),
        (90, -7.0, None),
    ],
)
def test_preview_and_export_share_authoritative_geometry(rotation, angle, crop):
    editor = ImageEditor()
    source = np.zeros((60, 100, 3), dtype=np.float32)
    source[12:20, 25:35] = 1.0
    edits = editor._initial_edits()
    edits.update({"rotation": rotation, "straighten_angle": angle, "crop_box": crop})

    preview = editor._apply_edits(source.copy(), edits=edits, for_export=False)
    export = editor._apply_edits(source.copy(), edits=edits, for_export=True)

    assert preview.shape == export.shape
    preview_points = np.argwhere(preview[..., 0] > 0.5)
    export_points = np.argwhere(export[..., 0] > 0.5)
    assert preview_points.min(axis=0).tolist() == export_points.min(axis=0).tolist()
    assert preview_points.max(axis=0).tolist() == export_points.max(axis=0).tolist()


def test_straighten_only_changes_preview_geometry():
    editor = ImageEditor()
    source = np.zeros((60, 100, 3), dtype=np.float32)
    edits = editor._initial_edits()
    edits["straighten_angle"] = 10.0

    preview = editor._apply_edits(source, edits=edits, for_export=False)

    assert preview.shape[:2] != source.shape[:2]
