import unittest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure we can import faststack
sys.path.append(str(Path(__file__).parents[2]))

from faststack.app import AppController


class TestEditorReopening(unittest.TestCase):
    def setUp(self):
        # 1. Heavily patch all external-touching classes
        self.patchers = [
            patch("faststack.app.Watcher"),
            patch("faststack.app.SidecarManager"),
            patch("faststack.app.Prefetcher"),
            patch("faststack.app.ByteLRUCache"),
            patch("faststack.app.ThumbnailProvider"),
            patch("faststack.app.config"),
            patch("faststack.app.setup_logging"),
            patch("faststack.app.UIState"),
            patch(
                "faststack.app.QTimer"
            ),  # <-- Fix QObject/Timer issues in headless tests
            patch("faststack.app.create_daemon_threadpool_executor"),
            patch("concurrent.futures.ThreadPoolExecutor"),
        ]
        for p in self.patchers:
            p.start()

        # 2. Instantiate controller
        self.controller = AppController(Path("."), MagicMock())

        # 3. Setup mocks for editor session logic
        self.controller.image_editor = MagicMock()
        self.controller.image_editor.current_filepath = Path("test.jpg")
        self.controller.image_editor.current_mtime = 123.4
        self.controller.image_editor.session_id = "test-session"

        # Setup files
        mock_file = MagicMock()
        mock_file.path = Path("test.jpg")
        self.controller.image_files = [mock_file]
        self.controller.current_index = 0

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_save_failure_retains_editor_state(self):
        # Simulate background worker callback firing with a failure.
        # _on_save_finished takes a single dict that contains both the
        # result and the context fields produced by save_edited_image().
        save_result = {
            "success": False,
            "error": "Disk full",
            "save_image_key": str(Path("test.jpg").resolve()),
            "session_token": ("key", None, "test-session"),
            "editor_was_open": True,
        }

        self.controller._on_save_finished(save_result)

        # VERIFY: Clear must NOT be called on failure
        self.controller.image_editor.clear.assert_not_called()

    def test_reopen_hits_reuse_path_on_matching_file(self):
        target = Path("test.jpg")
        self.controller.image_editor.current_filepath = target
        self.controller.image_editor.current_mtime = 123.4

        with patch("pathlib.Path.resolve", return_value=target.absolute()):
            with patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value.st_mtime = 123.4

                # REOPEN
                res = self.controller.load_image_for_editing()

                self.assertTrue(res)
                # VERIFY: reuse signals
                self.controller.ui_state.editorImageChanged.emit.assert_called_once()
                # VERIFY: no reload performed
                self.controller.image_editor.load_image.assert_not_called()

    def test_load_failure_closes_dialog(self):
        # Case 1: load_image fails
        self.controller.ui_state.isEditorOpen = True
        self.controller.image_editor.current_filepath = None  # Ensure no reuse
        self.controller.image_editor.load_image.return_value = False

        res = self.controller.load_image_for_editing()
        self.assertFalse(res)
        self.assertFalse(
            self.controller.ui_state.isEditorOpen, "Dialog should close on failure"
        )

        # Case 2: exception throws
        self.controller.ui_state.isEditorOpen = True
        self.controller.image_editor.load_image.side_effect = RuntimeError("IO error")
        res = self.controller.load_image_for_editing()
        self.assertFalse(res)
        self.assertFalse(
            self.controller.ui_state.isEditorOpen, "Dialog should close on error"
        )

    def test_reuse_returns_REUSED_not_True(self):
        """The reuse path must return _REUSED (truthy, but ``is True`` is False)
        so _prepare_darken_image_state can distinguish reuse from reload."""
        target = Path("test.jpg")
        self.controller.image_editor.current_filepath = target
        self.controller.image_editor.current_mtime = 123.4
        self.controller.image_editor.current_edits = {}

        with patch("pathlib.Path.resolve", return_value=target.absolute()):
            with patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value.st_mtime = 123.4

                res = self.controller.load_image_for_editing()

                # Must be truthy (success)…
                self.assertTrue(res)
                # …but not exactly True (so ``is True`` check in
                # _prepare_darken_image_state correctly skips darken reset)
                self.assertIsNot(res, True)
                self.assertEqual(res, AppController._REUSED)

    def test_prepare_darken_skips_reset_on_reuse(self):
        """_prepare_darken_image_state must NOT call _reset_darken_on_navigation
        when load_image_for_editing returns _REUSED."""
        target = Path("test.jpg")
        self.controller.image_editor.current_filepath = None  # Force a load
        self.controller.image_editor.float_image = None  # Force needs_load=True
        self.controller.image_editor.current_edits = {}

        with patch.object(
            self.controller,
            "load_image_for_editing",
            return_value=AppController._REUSED,
        ):
            with patch.object(
                self.controller, "_reset_darken_on_navigation"
            ) as mock_reset:
                result = self.controller._prepare_darken_image_state()
                self.assertTrue(result)
                mock_reset.assert_not_called()

    def test_prepare_darken_resets_on_real_reload(self):
        """_prepare_darken_image_state MUST call _reset_darken_on_navigation
        when load_image_for_editing returns True (real reload)."""
        self.controller.image_editor.current_filepath = None
        self.controller.image_editor.float_image = None

        with patch.object(self.controller, "load_image_for_editing", return_value=True):
            with patch.object(
                self.controller, "_reset_darken_on_navigation"
            ) as mock_reset:
                result = self.controller._prepare_darken_image_state()
                self.assertTrue(result)
                mock_reset.assert_called_once()

    def test_prepare_darken_aborts_on_failure(self):
        """_prepare_darken_image_state must return False when load fails."""
        self.controller.image_editor.current_filepath = None
        self.controller.image_editor.float_image = None

        with patch.object(
            self.controller, "load_image_for_editing", return_value=False
        ):
            result = self.controller._prepare_darken_image_state()
            self.assertFalse(result)


    def test_save_closes_ui_immediately_but_keeps_memory(self):
        # 1. Setup
        target = Path("test.jpg")
        target_abs = self.controller._key(target)
        self.controller.ui_state.isEditorOpen = True
        self.controller.image_editor.current_filepath = target
        self.controller.image_editor.session_id = "sess-1"
        self.controller.image_editor.current_mtime = 123.4

        # Mock snapshot
        self.controller.image_editor.snapshot_for_export.return_value = MagicMock()

        with patch.object(self.controller, "_save_executor") as mock_executor:
            # 2. CALL SAVE
            self.controller.save_edited_image()

            # VERIFY: UI closed immediately in controller state
            self.assertFalse(self.controller.ui_state.isEditorOpen)

            # 3. SIMULATE SIGNAL TRIGGERED BY UI CLOSURE
            # In the real app, setting isEditorOpen=False emits signal -> calls _on_editor_open_changed(False)
            self.controller._on_editor_open_changed(False)

            # VERIFY: Clear was NOT called (because save is in flight for this key)
            self.controller.image_editor.clear.assert_not_called()

            # VERIFY: Save in-flight markers are present
            self.assertIn(target_abs, self.controller._saving_keys)

            # 4. RE-OPEN (Simulation)
            # Should be REUSED since memory wasn't cleared
            with patch("pathlib.Path.resolve", return_value=target.absolute()):
                with patch("pathlib.Path.stat") as mock_stat:
                    mock_stat.return_value.st_mtime = 123.4
                    res = self.controller.load_image_for_editing()
                    self.assertEqual(res, AppController._REUSED)
                    self.controller.image_editor.load_image.assert_not_called()

    def test_editor_close_clears_memory_if_no_save_active(self):
        # 1. Setup
        self.controller.image_editor.current_filepath = Path("no_save.jpg")

        # 2. Simulate closure via signal while NO save is in flight
        self.controller._on_editor_open_changed(False)

        # VERIFY: Clear IS called because no save active for this file
        self.controller.image_editor.clear.assert_called_once()


if __name__ == "__main__":
    unittest.main()
