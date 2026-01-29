import sys
import os
import time
import shutil
from pathlib import Path
import numpy as np
from PIL import Image

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from faststack.imaging.editor import ImageEditor


def test_cache_stability():
    """Verify that cache hash remains stable when reloading the same unmodified file."""

    # Setup dummy image
    test_dir = Path("tests/dummy_images_cache")
    test_dir.mkdir(parents=True, exist_ok=True)

    img_path = test_dir / "test_cache.jpg"

    # Create a dummy image
    arr = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    Image.fromarray(arr).save(img_path)

    editor = ImageEditor()

    # 1. Load image and get hash
    editor.load_image(str(img_path))
    hash1 = editor._get_upstream_edits_hash(editor.current_edits)

    # 2. Reload same image (simulate switching back and forth)
    # Even if we create a new editor or reload, if the file hasn't changed,
    # the ideal cache key for *content-dependent* heavy ops should be stable.
    # However, the current implementation uses id(self.float_image), so we expect this to change
    # if we reload, because float_image will be a new object.

    editor.load_image(str(img_path))
    hash2 = editor._get_upstream_edits_hash(editor.current_edits)

    print(f"Hash 1: {hash1}")
    print(f"Hash 2: {hash2}")

    # Current behavior: Hashes DIFFERENT because id() changed
    # Desired behavior: Hashes SAME because content/mtime is same

    if hash1 == hash2:
        print("PASS: Hash is stable across reloads.")
    else:
        print("FAIL: Hash changed across reloads (unnecessary invalidation).")

    # 3. Touch file to update mtime
    time.sleep(1.1)  # Ensure mtime changes (some systems have 1s resolution)
    img_path.touch()

    editor.load_image(str(img_path))
    hash3 = editor._get_upstream_edits_hash(editor.current_edits)

    print(f"Hash 3 (after touch): {hash3}")

    if hash3 != hash2:
        print("PASS: Hash changed after mtime update.")
    else:
        print("FAIL: Hash did NOT change after mtime update.")

    # Cleanup
    try:
        shutil.rmtree(test_dir)
    except:
        pass


if __name__ == "__main__":
    test_cache_stability()
