import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock things we don't want to import fully or that need QObject
# We need to test AppController methods, but AppController inherits QObject.
# To test logic without full Qt app, we can either:
# 1. Use QTest (requires PySide6 installed and GUI context)
# 2. Extract logic or subclass AppController with mocked QObject?
# Let's try to minimal import.

# Assuming we can instantiate AppController or minimal subclass
# But AppController creates threads and QObjects in __init__.
# Better to mock AppController's state and just test the methods if possible.
# But methods are bound to 'self'.
# We can create a dummy class that looks like AppController for these methods.


class DummyController:
    def __init__(self):
        self.current_edit_source_mode = "jpeg"
        self.image_files = []
        self.current_index = 0
        self.ui_state = MagicMock()
        self.ui_state.isHistogramVisible = False
        self.editSourceModeChanged = MagicMock()  # Signal stub

    # Copy methods to test
    try:
        from faststack.app import AppController

        get_active_edit_path = AppController.get_active_edit_path
        is_valid_working_tif = AppController.is_valid_working_tif
        _set_current_index = AppController._set_current_index
        enable_raw_editing = AppController.enable_raw_editing
    except Exception as e:
        print(f"CRITICAL ERROR importing AppController: {e}")
        import traceback

        traceback.print_exc()
        # Define dummy placeholders to prevent AttributeError during test collection/execution
        get_active_edit_path = lambda *args: None
        is_valid_working_tif = lambda *args: False
        _set_current_index = lambda *args: None
        enable_raw_editing = lambda *args: None

    def sync_ui_state(self):
        pass

    def _reset_crop_settings(self):
        pass

    def _do_prefetch(self, *args, **kwargs):
        pass

    def update_histogram(self):
        pass

    def load_image_for_editing(self):
        pass

    def _develop_raw_backend(self):
        pass


class TestRawMode(unittest.TestCase):
    def setUp(self):
        self.controller = DummyController()

        # Create mock image files
        self.img_jpg = MagicMock()
        self.img_jpg.path = Path(
            "test.jpg"
        )  # suffix is derived from Path, not assigned
        self.img_jpg.raw_pair = Path("test.CR2")
        self.img_jpg.working_tif_path = Path("test.tif")
        self.img_jpg.has_working_tif = False  # Initially false

        self.img_raw_only = MagicMock()
        self.img_raw_only.path = Path(
            "orphan.CR2"
        )  # suffix is derived from Path, not assigned
        self.img_raw_only.raw_pair = None

        self.controller.image_files = [self.img_jpg, self.img_raw_only]

    def test_default_mode(self):
        """Test 1: Default mode should be JPEG."""
        self.controller.current_index = 0
        path = self.controller.get_active_edit_path(0)
        self.assertEqual(path, Path("test.jpg"))
        self.assertEqual(self.controller.current_edit_source_mode, "jpeg")

    def test_switch_to_raw_with_development(self):
        """Test 2: Enabling RAW should switch mode and trigger develop if no TIFF."""
        self.controller.current_index = 0

        # Mock _develop_raw_backend
        self.controller._develop_raw_backend = MagicMock()

        self.controller.enable_raw_editing()

        self.assertEqual(self.controller.current_edit_source_mode, "raw")
        self.controller._develop_raw_backend.assert_called_once()

        # Path check: even if we switch mode, if TIFF is invalid, get_active_edit_path might return RAW path?
        # Logic says: if mode=raw, check valid TIFF, else return raw_pair.
        # So it should return the RAW file if TIFF not ready.
        path = self.controller.get_active_edit_path(0)
        self.assertEqual(path, Path("test.CR2"))

    def test_switch_to_raw_with_existing_tiff(self):
        """Test 3: Enabling RAW should load TIFF if valid."""
        self.controller.current_index = 0
        self.img_jpg.has_working_tif = True

        # Mock is_valid_working_tif to return True
        with patch.object(self.controller, "is_valid_working_tif", return_value=True):
            # Create mocks BEFORE calling enable_raw_editing
            self.controller.load_image_for_editing = MagicMock()
            self.controller._develop_raw_backend = MagicMock()

            self.controller.enable_raw_editing()

            self.assertEqual(self.controller.current_edit_source_mode, "raw")
            # Should NOT develop (mock was set up before the call)
            self.controller._develop_raw_backend.assert_not_called()
            # Should load immediately
            self.controller.load_image_for_editing.assert_called_once()

            # Helper should return TIF
            path = self.controller.get_active_edit_path(0)
            self.assertEqual(path, Path("test.tif"))

    def test_raw_only_case(self):
        """Test 4: Opening RAW-only files should force RAW mode."""
        # Navigate to index 1 (RAW only)
        # Using _set_current_index logic

        # Need to mock the logic in _set_current_index...
        # Wait, I copied _set_current_index to DummyController.
        # But it requires `from faststack.io.indexer import RAW_EXTENSIONS`.
        # I need to mock that import or ensure it works.

        with patch("faststack.io.indexer.RAW_EXTENSIONS", {".CR2", ".ARW"}):
            self.controller._set_current_index(1)

        self.assertEqual(self.controller.current_index, 1)
        self.assertEqual(self.controller.current_edit_source_mode, "raw")

        path = self.controller.get_active_edit_path(1)
        self.assertEqual(path, Path("orphan.CR2"))

    def test_navigation_resets_mode(self):
        """Test 5: Navigating to a normal pair should reset mode to JPEG."""
        # First set raw mode on index 0
        self.controller.current_index = 0
        self.controller.current_edit_source_mode = "raw"

        # Navigate to index 0 again via _set_current_index (like jumping or reloading)
        # Or pretend we have another image. Let's make index 0 a normal pair.

        with patch("faststack.io.indexer.RAW_EXTENSIONS", {".CR2", ".ARW"}):
            self.controller._set_current_index(0)

        self.assertEqual(self.controller.current_edit_source_mode, "jpeg")


if __name__ == "__main__":
    unittest.main()
