import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys

# Ensure we can import faststack
sys.path.append(str(Path(__file__).parents[2]))

from faststack.app import AppController


class TestEditorIntegration(unittest.TestCase):
    def setUp(self):
        # Mock dependencies for AppController
        self.mock_engine = MagicMock()
        self.mock_config = MagicMock()

        # Patch config to avoid file I/O or errors
        self.config_patcher = patch("faststack.app.config")
        self.mock_config_module = self.config_patcher.start()

        # Instantiate AppController with a dummy path
        # We need to mock Watcher and SidecarManager because they start threads/file IO
        with (
            patch("faststack.app.Watcher"),
            patch("faststack.app.SidecarManager"),
            patch("faststack.app.Prefetcher"),
            patch("faststack.app.ByteLRUCache"),
        ):
            self.controller = AppController(Path("."), self.mock_engine)

        # Mock the internal image_editor to verify delegation
        self.controller.image_editor = MagicMock()
        self.controller.image_editor.current_filepath = Path("test.jpg")
        self.controller.image_editor.float_image = MagicMock()
        self.controller.image_editor.original_image = MagicMock()

    def tearDown(self):
        self.config_patcher.stop()

    def test_missing_methods(self):
        """Verify that the methods expected by QML exist and delegate to ImageEditor."""

        # 1. set_edit_parameter
        # Try calling the method. If it doesn't exist, this will raise AttributeError
        try:
            self.controller.set_edit_parameter("exposure", 0.5)
            self.controller.image_editor.set_edit_param.assert_called_with(
                "exposure", 0.5
            )
        except AttributeError:
            self.fail("AppController is missing method 'set_edit_parameter'")

        # 2. rotate_image_cw
        try:
            self.controller.rotate_image_cw()
            self.controller.image_editor.rotate_image_cw.assert_called_once()
        except AttributeError:
            self.fail("AppController is missing method 'rotate_image_cw'")

        # 3. rotate_image_ccw
        try:
            self.controller.rotate_image_ccw()
            self.controller.image_editor.rotate_image_ccw.assert_called_once()
        except AttributeError:
            self.fail("AppController is missing method 'rotate_image_ccw'")

        # 4. reset_edit_parameters
        try:
            self.controller.reset_edit_parameters()
            self.controller.image_editor.reset_edits.assert_called_once()
        except AttributeError:
            self.fail("AppController is missing method 'reset_edit_parameters'")

        # 5. save_edited_image
        try:
            self.controller.save_edited_image()
            self.controller.image_editor.save_image.assert_called_once()
        except AttributeError:
            self.fail("AppController is missing method 'save_edited_image'")

        # 6. auto_levels
        try:
            self.controller.auto_levels()
            self.controller.image_editor.auto_levels.assert_called_once()
        except AttributeError:
            self.fail("AppController is missing method 'auto_levels'")

        # 7. update_histogram
        # This one might be complex to mock fully due to threading, but we check existence
        if not hasattr(self.controller, "update_histogram"):
            self.fail("AppController is missing method 'update_histogram'")

    def test_set_edit_parameter_gating(self):
        """Regression test for proper gating of set_edit_parameter."""

        # Setup mocks
        self.controller.image_editor = MagicMock()

        # Case 1: Editor closed (ui_state flag False), but image LOADED.
        # Should allow edits (robustness fix).
        self.controller.ui_state.isEditorOpen = False
        self.controller.image_editor.current_filepath = Path("test.jpg")
        self.controller.image_editor.original_image = MagicMock()  # Has image
        self.controller.image_editor.float_image = None

        self.controller.set_edit_parameter("exposure", 0.5)
        self.controller.image_editor.set_edit_param.assert_called_with("exposure", 0.5)

        # Reset mocks
        self.controller.image_editor.reset_mock()

        # Case 2: Editor OPEN (flag True), but NO image loaded.
        # Should BLOCK edits (safety fix).
        self.controller.ui_state.isEditorOpen = True
        self.controller.image_editor.current_filepath = None
        self.controller.image_editor.original_image = None
        self.controller.image_editor.float_image = None

        self.controller.set_edit_parameter("exposure", 0.8)
        self.controller.image_editor.set_edit_param.assert_not_called()


if __name__ == "__main__":
    unittest.main()
