import unittest
from unittest.mock import MagicMock
from PySide6.QtGui import QImage

# Import the class to test (assuming it's importable)
# We might need to mock imports if they depend on full Qt app structure
import sys
import os

# Adjust path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from faststack.ui.provider import ImageProvider


class TestGenerationAwarePreview(unittest.TestCase):
    def setUp(self):
        self.mock_controller = MagicMock()
        self.mock_controller.ui_state = MagicMock()
        self.mock_controller.ui_state.isEditorOpen = True
        self.mock_controller.ui_state.isZoomed = False
        self.mock_controller.current_index = 0

        # Setup mock images
        self.mock_preview = MagicMock()
        self.mock_preview.buffer = b"\x00" * 100
        self.mock_preview.width = 10
        self.mock_preview.height = 10
        self.mock_preview.bytes_per_line = 30
        self.mock_preview.format = QImage.Format.Format_RGB888

        self.mock_decoded = MagicMock()
        self.mock_decoded.buffer = b"\xff" * 100
        self.mock_decoded.width = 10
        self.mock_decoded.height = 10
        self.mock_decoded.bytes_per_line = 30
        self.mock_decoded.format = QImage.Format.Format_RGB888

        self.mock_controller._last_rendered_preview = self.mock_preview
        self.mock_controller.get_decoded_image.return_value = self.mock_decoded

        self.provider = ImageProvider(self.mock_controller)

    def test_matching_generation(self):
        """Should serve preview when generation matches."""
        # Setup matching state
        self.mock_controller._last_rendered_preview_index = 0
        self.mock_controller._last_rendered_preview_gen = 5

        # Request with matching generation
        img = self.provider.requestImage("0/5", None, None)

        # Should be the preview (dark gray placeholder if fails, but here we mocked QImage creation?)
        # Wait, requestImage creates a QImage from the buffer.
        # We check WHICH buffer was used.
        # Since we cannot easily check the pixels of the returned QImage without a GUI instance,
        # we can check if get_decoded_image was called.

        # If it used preview, get_decoded_image should NOT be called (or only if preview is None)
        # But wait, logic is:
        # image_data = self.app_controller._last_rendered_preview if use_editor_preview else self.app_controller.get_decoded_image(index)

        # So we reset the mock
        self.mock_controller.get_decoded_image.reset_mock()

        self.provider.requestImage("0/5", None, None)

        self.mock_controller.get_decoded_image.assert_not_called()

    def test_mismatched_generation(self):
        """Should fallback to decoded image when generation does not match."""
        # Setup state: preview is old (gen 4)
        self.mock_controller._last_rendered_preview_index = 0
        self.mock_controller._last_rendered_preview_gen = 4

        # Request new generation (5)
        self.mock_controller.get_decoded_image.reset_mock()

        self.provider.requestImage("0/5", None, None)

        self.mock_controller.get_decoded_image.assert_called_with(0)

    def test_mismatched_index(self):
        """Should fallback when index does not match."""
        self.mock_controller._last_rendered_preview_index = 1
        self.mock_controller._last_rendered_preview_gen = 5

        self.mock_controller.get_decoded_image.reset_mock()
        self.provider.requestImage("0/5", None, None)

        self.mock_controller.get_decoded_image.assert_called_with(0)

    def test_no_generation_checking_if_not_provided(self):
        """If generation not provided in ID, should ignore tracking?
        The code says: (gen is None or getattr(...) == gen)
        If ID is '0', gen is None.
        (None is None) is True. So it matches.
        So legacy requests (without gen) will still serve preview if index matches.
        """
        self.mock_controller._last_rendered_preview_index = 0
        self.mock_controller._last_rendered_preview_gen = 99

        self.mock_controller.get_decoded_image.reset_mock()
        # Request without generation
        self.provider.requestImage("0", None, None)

        self.mock_controller.get_decoded_image.assert_not_called()


if __name__ == "__main__":
    unittest.main()
