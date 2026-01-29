import sys
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

# Mock modules before importing editor
# Note: These mocks remain in sys.modules for the test to use
sys.modules["cv2"] = MagicMock()
sys.modules["PIL"] = MagicMock()
sys.modules["PySide6.QtGui"] = MagicMock()

# Now import the class
from faststack.imaging.editor import ImageEditor


class TestCrash(unittest.TestCase):
    def test_imread_none_crash(self):
        """
        Simulate cv2.imread returning None and see if it crashes.
        """
        editor = ImageEditor()
        editor.original_image = MagicMock()  # Pillow image mock
        editor.original_image.convert.return_value = np.zeros(
            (100, 100, 3), dtype=np.uint8
        )

        # Mock cv2.imread to return None
        sys.modules["cv2"].imread.return_value = None
        sys.modules["cv2"].IMREAD_UNCHANGED = -1

        # Path must exist for the check at the start of load_image,
        # or we mock Path.exists
        with patch("pathlib.Path.exists", return_value=True):
            try:
                print("Attempting to load image with mocks...")
                success = editor.load_image("dummy_path.jpg")
                print(f"Load result: {success}")
            except Exception as e:
                print(f"CRASHED: {e}")
                raise e


if __name__ == "__main__":
    unittest.main()
