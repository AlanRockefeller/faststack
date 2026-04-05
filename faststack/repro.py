import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Repo root
sys.path.append(str(Path(".").resolve().parent))

print(f"DEBUG: sys.path[-1] is {sys.path[-1]}")

from faststack.app import AppController

# Mock dependencies
mock_engine = MagicMock()
mock_config = MagicMock()

with (
    patch("config.config"),
    patch("faststack.io.watcher.Watcher"),
    patch("faststack.io.sidecar.SidecarManager"),
    patch("faststack.imaging.prefetch.Prefetcher"),
    patch("faststack.imaging.cache.ByteLRUCache"),
    patch("faststack.thumbnail_view.ThumbnailProvider"),
):
    controller = AppController(Path("."), mock_engine)

# Setup state
# Use real list to avoid mock issues
from faststack.models import ImageFile

mock_image = ImageFile(Path("test.jpg"))
controller.image_files = [mock_image]
controller.current_index = 0
controller.auto_level_threshold = 0.001

# Mock image_editor
controller.image_editor = MagicMock()
controller.image_editor.auto_levels.return_value = (10, 240, 10, 240)  # Not full range
controller.image_editor.current_filepath = Path("test.jpg")
controller.image_editor.load_image.return_value = True

print("Calling controller.auto_levels()...")
try:
    result = controller.auto_levels()
    print(f"Result: {result}")
    controller.image_editor.auto_levels.assert_called_once()
    print("Assertion passed!")
except Exception as e:
    print(f"\nAssertion FAILED: {type(e).__name__}: {e}")
    import traceback

    traceback.print_exc()
