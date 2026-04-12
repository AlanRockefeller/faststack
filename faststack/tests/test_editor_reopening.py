import unittest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure we can import faststack
sys.path.append(str(Path(__file__).parents[2]))

from faststack.app import AppController
from faststack.models import EntryMetadata


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

    def test_reuse_blocked_when_float_image_is_none(self):
        """Matching path/mtime with float_image=None must force a real reload,
        not silently reuse a preview-only (float-less) session."""
        target = Path("test.jpg")
        self.controller.image_editor.current_filepath = target
        self.controller.image_editor.current_mtime = 123.4
        # Simulate a preview_only load: filepath/mtime set, but no float buffer.
        self.controller.image_editor.float_image = None
        self.controller.image_editor.load_image.return_value = True

        with patch("pathlib.Path.resolve", return_value=target.absolute()):
            with patch("pathlib.Path.stat") as mock_stat:
                mock_stat.return_value.st_mtime = 123.4
                res = self.controller.load_image_for_editing()

        # Must perform a real reload, not _REUSED, and return truthy.
        self.controller.image_editor.load_image.assert_called_once()
        self.assertIsNot(res, AppController._REUSED)
        self.assertTrue(res)

    def test_crop_mode_blocked_while_saving(self):
        """toggle_crop_mode must not enter crop mode when a save is in flight."""
        mock_file = MagicMock()
        target = Path("test.jpg")
        mock_file.path = target
        self.controller.image_files = [mock_file]
        self.controller.current_index = 0

        # Put the image key in saving_keys
        save_key = self.controller._key(target)
        self.controller._saving_keys = {save_key}
        self.controller.ui_state.isCropping = False

        with patch.object(self.controller, "load_image_for_editing") as mock_load:
            self.controller.toggle_crop_mode()

        # isCropping must remain False
        self.assertFalse(self.controller.ui_state.isCropping)
        mock_load.assert_not_called()

    def test_crop_mode_blocked_when_load_fails(self):
        """toggle_crop_mode must not set isCropping when load_image_for_editing fails."""
        self.controller._saving_keys = set()
        self.controller.ui_state.isCropping = False

        with patch.object(
            self.controller, "load_image_for_editing", return_value=False
        ):
            self.controller.toggle_crop_mode()

        self.assertFalse(self.controller.ui_state.isCropping)

    def test_crop_mode_blocked_no_image(self):
        """toggle_crop_mode must not enter crop mode if no image is available."""
        self.controller.image_files = []
        self.controller.current_index = -1
        self.controller.ui_state.isCropping = False

        with patch.object(self.controller, "update_status_message") as mock_msg:
            self.controller.toggle_crop_mode()

        self.assertFalse(self.controller.ui_state.isCropping)
        mock_msg.assert_called_with("No image to crop")

    def test_save_finished_does_not_clear_editor_when_edits_rev_advanced(self):
        """If _edits_rev changed after save started, _on_save_finished must not
        call image_editor.clear() — the user has unsaved changes."""
        target = Path("test.jpg")
        target_abs = self.controller._key(target)
        self.controller.image_editor.current_filepath = target
        self.controller.image_editor.session_id = "sess-1"
        # _edits_rev at save-start was 5; user bumped it to 6 during the save
        save_rev = 5
        self.controller.image_editor._edits_rev = 6  # newer than save token

        save_result = {
            "success": True,
            "result": (target, None),
            "target": target_abs,
            "save_image_key": target_abs,
            "session_token": (target_abs, None, "sess-1", save_rev),
            "editor_was_open": True,
            "started_from_restore_override": False,
        }

        # Patch list/refresh helpers the handler calls
        with patch.object(self.controller, "refresh_image_list"):
            with patch.object(self.controller, "sync_ui_state"):
                self.controller._on_save_finished(save_result)

        # Token mismatch on _edits_rev → still_on_same_image is False → no clear
        self.controller.image_editor.clear.assert_not_called()

    def test_save_finished_clears_editor_when_edits_rev_unchanged(self):
        """Normal save completion (no edits made during save) must still clear
        editor memory — the 4-part tokens are equal so still_on_same_image is True."""
        target = Path("test.jpg")
        target_abs = self.controller._key(target)
        self.controller.image_editor.current_filepath = target
        self.controller.image_editor.session_id = "sess-1"
        # _edits_rev same at save-start and now — user did not edit during save
        rev = 5
        self.controller.image_editor._edits_rev = rev

        save_result = {
            "success": True,
            "result": (target, None),
            "target": target_abs,
            "save_image_key": target_abs,
            "session_token": (target_abs, None, "sess-1", rev),
            "editor_was_open": True,
            "started_from_restore_override": False,
        }

        with patch.object(self.controller, "refresh_image_list"):
            with patch.object(self.controller, "sync_ui_state"):
                self.controller._on_save_finished(save_result)

        # Tokens equal → still_on_same_image is True → editor_was_open → clear called
        self.controller.image_editor.clear.assert_called_once()

    def test_save_finished_skips_undo_after_directory_navigation(self):
        """A save finishing after folder navigation must not repopulate the
        new directory's undo stack, but it must still update the origin sidecar."""
        main_path = Path("/folder-a/test.jpg")
        saved_path = Path("/folder-a/test-developed.jpg")
        backup_path = Path("/folder-a/test-developed-backup.jpg")
        origin_sidecar = MagicMock()
        replacement_sidecar = MagicMock()
        origin_sidecar.get_metadata.return_value = EntryMetadata(favorite=True)
        self.controller.image_files[0].path = main_path

        # Simulate the controller having navigated to another folder before the
        # save callback fires.
        self.controller.image_dir = Path("/folder-b")
        self.controller.sidecar = replacement_sidecar

        save_result = {
            "success": True,
            "result": (saved_path, backup_path),
            "target": self.controller._key(saved_path),
            "save_image_key": self.controller._key(Path("test.jpg")),
            "editor_was_open": False,
            "save_directory_key": self.controller._key(Path("/folder-a")),
            "save_metadata_path": str(main_path),
            "started_from_restore_override": False,
            "save_sidecar": origin_sidecar,
        }

        with patch.object(self.controller, "refresh_image_list"):
            with patch.object(self.controller, "_reindex_after_save"):
                self.controller._on_save_finished(save_result)

        origin_sidecar.update_metadata.assert_called_once()
        update_path, update_data = origin_sidecar.update_metadata.call_args.args
        self.assertEqual(update_path, main_path)
        self.assertTrue(update_data["edited"])
        self.assertTrue(update_data["edited_date"])
        self.assertTrue(update_data["favorite"])
        replacement_sidecar.update_metadata.assert_not_called()
        self.assertEqual(self.controller.undo_history, [])

    def test_save_finished_records_undo_in_active_directory(self):
        main_path = Path("/folder-a/test.jpg")
        saved_path = Path("/folder-a/test-developed.jpg")
        backup_path = Path("/folder-a/test-developed-backup.jpg")
        origin_sidecar = MagicMock()
        origin_sidecar.get_metadata.return_value = EntryMetadata(favorite=True)
        self.controller.image_dir = Path("/folder-a")
        self.controller.sidecar = origin_sidecar
        self.controller.image_files[0].path = main_path

        save_result = {
            "success": True,
            "result": (saved_path, backup_path),
            "target": self.controller._key(saved_path),
            "save_image_key": self.controller._key(Path("test.jpg")),
            "editor_was_open": False,
            "save_directory_key": self.controller._key(Path("/folder-a")),
            "save_metadata_path": str(main_path),
            "started_from_restore_override": False,
            "save_sidecar": origin_sidecar,
        }

        with patch.object(self.controller, "refresh_image_list"):
            with patch.object(self.controller, "_reindex_after_save"):
                self.controller._on_save_finished(save_result)

        action_type, action_data, _ = self.controller.undo_history[-1]
        self.assertEqual(action_type, "save_edit")
        self.assertEqual(action_data["saved_path"], str(saved_path))
        self.assertEqual(action_data["backup_path"], str(backup_path))
        self.assertEqual(action_data["metadata_path"], str(main_path))
        self.assertEqual(
            action_data["metadata_before"],
            {
                "stack_id": None,
                "stacked": False,
                "stacked_date": None,
                "uploaded": False,
                "uploaded_date": None,
                "edited": False,
                "edited_date": None,
                "restacked": False,
                "restacked_date": None,
                "favorite": True,
                "todo": False,
                "todo_date": None,
            },
        )
        self.assertIs(action_data["sidecar"], origin_sidecar)
        origin_sidecar.update_metadata.assert_called_once()
        update_path, _ = origin_sidecar.update_metadata.call_args.args
        self.assertEqual(update_path, main_path)

    def test_save_finished_malformed_result_clears_saving_status_message(self):
        target = self.controller._key(Path("test.jpg"))
        self.controller._saves_in_flight = {target}
        self.controller._saving_keys = {target}
        self.controller.ui_state.isSaving = True

        with patch.object(self.controller, "update_status_message") as mock_status:
            self.controller._on_save_finished(
                {
                    "success": True,
                    "result": "bad-payload",
                    "target": target,
                    "save_image_key": target,
                }
            )

        self.assertFalse(self.controller.ui_state.isSaving)
        mock_status.assert_called_once_with(
            "Save finished, but the result payload was malformed.", timeout=5000
        )

    def test_undo_save_edit_restores_only_owned_metadata_fields(self):
        main_path = Path("/folder-a/test.jpg")
        saved_path = Path("/folder-a/test-developed.jpg")
        backup_path = Path("/folder-a/test-developed-backup.jpg")
        metadata_sidecar = MagicMock()
        metadata_sidecar.data.entries = {
            "test": EntryMetadata(
                favorite=True,
                edited=True,
                edited_date="2026-04-12T12:34:56",
            )
        }
        metadata_sidecar.metadata_key_for_path.return_value = "test"

        self.controller.undo_history = [
            (
                "save_edit",
                {
                    "saved_path": str(saved_path),
                    "backup_path": str(backup_path),
                    "metadata_path": str(main_path),
                    "metadata_before": {"edited": False, "edited_date": None},
                    "sidecar": metadata_sidecar,
                },
                123.0,
            )
        ]

        with patch.object(self.controller, "_restore_backup_safe", return_value=True):
            with patch.object(self.controller, "_post_undo_refresh_and_select"):
                self.controller.undo_delete()

        metadata_sidecar.metadata_key_for_path.assert_called_once_with(main_path)
        restored_meta = metadata_sidecar.data.entries["test"]
        self.assertTrue(restored_meta.favorite)
        self.assertFalse(restored_meta.edited)
        self.assertIsNone(restored_meta.edited_date)
        metadata_sidecar.save.assert_called_once()

    def test_undo_save_edit_deletes_default_only_edit_metadata_entry(self):
        main_path = Path("/folder-a/test.jpg")
        saved_path = Path("/folder-a/test-developed.jpg")
        backup_path = Path("/folder-a/test-developed-backup.jpg")
        metadata_sidecar = MagicMock()
        metadata_sidecar.data.entries = {
            "test": EntryMetadata(
                edited=True,
                edited_date="2026-04-12T12:34:56",
            )
        }
        metadata_sidecar.metadata_key_for_path.return_value = "test"

        self.controller.undo_history = [
            (
                "save_edit",
                {
                    "saved_path": str(saved_path),
                    "backup_path": str(backup_path),
                    "metadata_path": str(main_path),
                    "metadata_before": None,
                    "sidecar": metadata_sidecar,
                },
                123.0,
            )
        ]

        with patch.object(self.controller, "_restore_backup_safe", return_value=True):
            with patch.object(self.controller, "_post_undo_refresh_and_select"):
                self.controller.undo_delete()

        metadata_sidecar.metadata_key_for_path.assert_called_once_with(main_path)
        self.assertNotIn("test", metadata_sidecar.data.entries)
        metadata_sidecar.save.assert_called_once()

    def test_quick_awb_loads_viewed_developed_variant(self):
        main_path = Path("/folder-a/test.jpg")
        developed_path = Path("/folder-a/test-developed.jpg")
        self.controller.image_files[0].path = main_path
        self.controller.view_override_path = str(developed_path)
        self.controller.view_override_kind = "developed"
        self.controller.image_editor.current_filepath = main_path
        self.controller.image_editor.load_image.return_value = True

        with patch.object(
            self.controller, "get_decoded_image", return_value=MagicMock()
        ):
            with patch.object(self.controller, "auto_white_balance", return_value=None):
                self.controller.quick_auto_white_balance()

        self.controller.image_editor.load_image.assert_called_once_with(
            str(developed_path), cached_preview=unittest.mock.ANY, preview_only=True
        )


if __name__ == "__main__":
    unittest.main()
