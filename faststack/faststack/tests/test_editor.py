import os

import pytest
from PIL import Image

from faststack.imaging.editor import ImageEditor


def test_save_image_preserves_mtime(tmp_path):
    img_path = tmp_path / "sample.jpg"
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(img_path)

    preserved_time = 1_600_000_000  # stable integer timestamp
    os.utime(img_path, (preserved_time, preserved_time))

    editor = ImageEditor()
    assert editor.load_image(str(img_path))
    assert editor.set_edit_param('brightness', 0.1)

    saved = editor.save_image()
    assert saved is not None
    saved_path, backup_path = saved

    assert saved_path == img_path
    assert backup_path.exists()

    assert img_path.stat().st_mtime == pytest.approx(preserved_time, rel=0, abs=1e-6)
