import os

import numpy as np
from PIL import Image

from faststack.imaging.editor import ImageEditor


def test_cache_stability(tmp_path):
    """The upstream-edits hash is stable across reloads of an unchanged file
    and changes once the file's mtime changes."""
    img_path = tmp_path / "test_cache.jpg"
    arr = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    Image.fromarray(arr).save(img_path)

    editor = ImageEditor()

    editor.load_image(str(img_path))
    hash1 = editor._get_upstream_edits_hash(editor.current_edits)

    # Reloading the same unmodified file must not invalidate heavy caches.
    editor.load_image(str(img_path))
    hash2 = editor._get_upstream_edits_hash(editor.current_edits)
    assert hash1 == hash2

    # Bump mtime explicitly instead of sleeping past coarse mtime resolution.
    st = img_path.stat()
    os.utime(img_path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))

    editor.load_image(str(img_path))
    hash3 = editor._get_upstream_edits_hash(editor.current_edits)
    assert hash3 != hash2
