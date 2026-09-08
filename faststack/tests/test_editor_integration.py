import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure we can import faststack
sys.path.append(str(Path(__file__).parents[2]))

from faststack.app import AppController
from faststack.tests.conftest import make_config_mock


class TestEditorIntegration(unittest.TestCase):
    def setUp(self):
        # Mock dependencies for AppController
        self.mock_engine = MagicMock()
        self.mock_config = MagicMock()

        # Patch config to avoid file I/O or errors
        self.config_patcher = patch(
            "faststack.app.config", new_callable=make_config_mock
        )
        self.mock_config_module = self.config_patcher.start()

        # Instantiate AppController with a dummy path
        # We need to mock Watcher and SidecarManager because they start threads/file IO
        with (
            patch("faststack.app.Watcher"),
            patch("faststack.app.SidecarManager"),
            patch("faststack.app.Prefetcher"),
            patch("faststack.app.ByteLRUCache"),
            patch("faststack.app.ThumbnailProvider"),
        ):
            self.controller = AppController(Path("."), self.mock_engine)
            # Stub out refresh_image_list so later calls during the test
            # do not trigger disk I/O or overwrite mock state.
            self.controller.refresh_image_list = MagicMock()

        # Mock the internal image_editor to verify delegation
        self.controller.image_editor = MagicMock()
        self.controller.image_editor.current_edits = {}
        self.controller.image_editor.current_filepath = Path("test.jpg")
        self.controller.image_editor.float_image = MagicMock()
        self.controller.image_editor.original_image = MagicMock()

        # Initialize state for delegation tests
        self.controller.image_files = [MagicMock(path=Path("test.jpg"))]
        self.controller.current_index = 0
        self.controller.auto_level_threshold = 0.001

        # Mock returns for methods that unpack results
        self.controller.image_editor.auto_levels.return_value = (0, 255, 0, 255)
        self.controller.image_editor.analyze_auto_levels.return_value = (0, 255, 0, 255)
        self.controller.image_editor.save_image.return_value = (Path("test.jpg"), None)

        # Mock _save_executor to be synchronous to avoid race conditions
        self.controller._save_executor = MagicMock()

        def mock_submit(fn, *args, **kwargs):
            # Execute synchronously
            result = fn(*args, **kwargs)
            # Return a mock future that triggers callbacks immediately
            mock_future = MagicMock()
            mock_future.result.return_value = result

            def mock_add_done_callback(callback):
                callback(mock_future)

            mock_future.add_done_callback.side_effect = mock_add_done_callback
            return mock_future

        self.controller._save_executor.submit.side_effect = mock_submit

        # ui_state is a MagicMock, so flags read truthy unless set. The save and
        # crop paths check these before doing any work.
        self.controller.ui_state.isCropping = False
        self.controller.ui_state.isSaving = False
        self.controller.ui_state.isEditorOpen = True
        self.controller.ui_state.isEditorExpanded = True

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
            # AppController delegates rotation via set_edit_param("rotation", ...)
            self.controller.image_editor.set_edit_param.assert_any_call("rotation", 270)
        except AttributeError:
            self.fail("AppController is missing method 'rotate_image_cw'")

        # 3. rotate_image_ccw
        try:
            self.controller.rotate_image_ccw()
            # AppController delegates rotation via set_edit_param("rotation", ...)
            self.controller.image_editor.set_edit_param.assert_any_call("rotation", 90)
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
            snapshot_sentinel = {"mock": "snapshot"}
            self.controller.image_editor.snapshot_for_export.return_value = (
                snapshot_sentinel
            )
            # A successful save must report both the output and its backup;
            # a None backup is treated as a failure and auto-retried once.
            self.controller.image_editor.save_from_snapshot.return_value = (
                Path("test.jpg"),
                Path("test.jpg.bak"),
            )
            self.controller.ui_state.isSaving = False
            # A save is only prepared for a session that has both a meaningful
            # edit and a revision newer than the last submitted one.
            self.controller.image_editor.current_edits = {"exposure": 0.5}
            self.controller.image_editor._edits_rev = 1
            self.controller._ensure_live_edit_session_state()
            self.controller.image_editor._edits_rev = 2
            self.controller.save_edited_image()
            self.controller.image_editor.snapshot_for_export.assert_called_once()
            self.controller.image_editor.save_from_snapshot.assert_called_once_with(
                snapshot_sentinel
            )
        except AttributeError:
            self.fail("AppController is missing method 'save_edited_image'")

        # 6. auto_levels
        try:
            self.controller.auto_levels()
            self.controller.image_editor.analyze_auto_levels.assert_called_once()
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
