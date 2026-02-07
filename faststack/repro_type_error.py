import sys
from pathlib import Path

# Ensure we can import faststack
repo_root = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, repo_root)

from faststack.imaging.editor import ImageEditor
from PIL import Image
import numpy as np

editor = ImageEditor()
img = Image.new("RGB", (100, 100), (255, 0, 0))
editor.original_image = img

print("Calling _apply_edits...")
try:
    res = editor._apply_edits(img)
    print(f"Result type: {type(res)}")
    if res is not None:
        print(
            f"Result shape/size: {getattr(res, 'shape', 'N/A')} / {getattr(res, 'size', 'N/A')}"
        )
    else:
        print("Result is None!")
except Exception as e:  # noqa: BLE001
    print(f"Caught exception: {type(e).__name__}: {e}")
    import traceback

    traceback.print_exc()
