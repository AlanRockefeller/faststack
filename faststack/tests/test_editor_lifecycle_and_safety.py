import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure we can import faststack
sys.path.append(str(Path(__file__).parents[2]))

from faststack.app import AppController


class TestEditorLifecycleAndSafety(unittest.TestCase):
    def setUp(self):
        # Mock dependencies for AppController
        self.mock_engine = MagicMock()

        # Patch dependencies that do I/O or threading
        self.watcher_patcher = patch("faststack.app.Watcher")
        self.sidecar_patcher = patch("faststack.app.SidecarManager")
        self.prefetcher_patcher = patch("faststack.app.Prefetcher")
        self.cache_patcher = patch("faststack.app.ByteLRUCache")
        self.config_patcher = patch("faststack.app.config")

        self.mock_watcher = self.watcher_patcher.start()
        self.mock_sidecar = self.sidecar_patcher.start()
        self.mock_prefetcher = self.prefetcher_patcher.start()
        self.mock_cache = self.cache_patcher.start()
        self.mock_config = self.config_patcher.start()

        # Default config values to allow init
        self.mock_config.getfloat.return_value = 1.0
        self.mock_config.getboolean.return_value = False
        self.mock_config.getint.return_value = 4

        # Mock QCoreApplication.instance() to prevent RuntimeError
        self.qapp_patcher = patch("faststack.app.QCoreApplication")
        self.mock_qapp = self.qapp_patcher.start()
        self.mock_qapp.instance.return_value.aboutToQuit.connect = MagicMock()

        # Instantiate AppController
        with patch("faststack.app.ImageEditor") as _mock_editor_cls:
            self.controller = AppController(Path("."), self.mock_engine)
            self.mock_editor_instance = self.controller.image_editor

    def tearDown(self):
        self.watcher_patcher.stop()
        self.sidecar_patcher.stop()
        self.prefetcher_patcher.stop()
        self.cache_patcher.stop()
        self.config_patcher.stop()
        self.qapp_patcher.stop()

        # Ensure we shutdown executors to avoid hanging tests
        self.controller.shutdown_nonqt()

    def test_memory_cleanup_on_editor_close(self):
        """Verify that memory is cleared when the editor is closed."""

        # 1. Simulate opening the editor
        # (Technically we just care about the transition to closed, but good to be thorough)
        self.controller._on_editor_open_changed(True)
        self.mock_editor_instance.clear.assert_not_called()

        # 2. Simulate closing the editor
        # The signal connection is already tested by Qt usually, we test the handler logic here
        self.controller._on_editor_open_changed(False)

        # 3. Verify clear() was called on the editor
        self.mock_editor_instance.clear.assert_called_once()

        # 4. Verify preview cache was cleared
        with self.controller._preview_lock:
            self.assertIsNone(self.controller._last_rendered_preview)

    def test_histogram_worker_submission_safety(self):
        """Verify that histogram inflight flag is reset if submission fails."""

        # Setup: Pending histogram update
        self.controller._hist_pending = (1.0, 0, 0, 1.0)  # args
        self.controller._hist_inflight = False

        # Mock executor to raise an exception on submit
        self.controller._hist_executor.submit = MagicMock(
            side_effect=TypeError("Simulated submission failure")
        )

        # Mock preview preview data to ensure we try to submit
        self.controller._last_rendered_preview = MagicMock()

        # Execute
        self.controller._kick_histogram_worker()

        # Verify:
        # 1. Flag should be FALSE (reset after error)
        self.assertFalse(
            self.controller._hist_inflight,
            "Histogram inflight flag should be reset after submission error",
        )

        # 2. _hist_pending was consumed (set to None inside the method before submitting)
        # Wait, usually if it fails, we might want to retry?
        # The current implementation just logs error and clears inflight.
        # It doesn't put args back into pending unless it was an early exit (no preview data).
        # This is acceptable behavior: drop the failed frame, wait for next update.
        self.assertIsNone(self.controller._hist_pending)

    def test_switch_to_directory_rebinds_sidecar_instance(self):
        """Directory navigation must replace self.sidecar with a new manager."""
        original_sidecar = self.controller.sidecar
        next_sidecar = MagicMock()
        self.mock_sidecar.side_effect = [next_sidecar]

        with patch.object(self.controller, "load") as mock_load:
            self.controller._switch_to_directory(Path("other-folder"))

        self.assertIsNot(self.controller.sidecar, original_sidecar)
        self.assertIs(self.controller.sidecar, next_sidecar)
        mock_load.assert_called_once_with(skip_thumbnail_refresh=True)


if __name__ == "__main__":
    unittest.main()
