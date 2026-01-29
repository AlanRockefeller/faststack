import sys
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from faststack.app import AppController

    print("Imported AppController")
except Exception as e:
    print(f"Failed to import AppController: {e}")
    sys.exit(1)


class DummyController:
    def __init__(self):
        self.current_edit_source_mode = "jpeg"
        self.image_files = []
        self.current_index = 0
        self.ui_state = MagicMock()
        self.ui_state.isHistogramVisible = False
        self.editSourceModeChanged = MagicMock()

    # Copy methods
    get_active_edit_path = AppController.get_active_edit_path
    is_valid_working_tif = AppController.is_valid_working_tif
    _set_current_index = AppController._set_current_index
    enable_raw_editing = AppController.enable_raw_editing

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


def log(msg):
    with open("verify_result.txt", "a") as f:
        f.write(msg + "\n")


def run_checks():
    # Clear log
    with open("verify_result.txt", "w") as f:
        f.write("Starting Verification\n")

    controller = DummyController()

    # Setup data
    img_jpg = MagicMock()
    img_jpg.path = Path("test.jpg")  # suffix is derived from Path, not assigned
    img_jpg.raw_pair = Path("test.CR2")
    img_jpg.working_tif_path = Path("test.tif")
    img_jpg.has_working_tif = False

    img_raw = MagicMock()
    img_raw.path = Path("orphan.CR2")  # suffix is derived from Path, not assigned
    img_raw.raw_pair = None

    controller.image_files = [img_jpg, img_raw]

    log("--- Test 1: Default Mode ---")
    controller.current_index = 0
    path = controller.get_active_edit_path(0)
    if path == Path("test.jpg") and controller.current_edit_source_mode == "jpeg":
        log("PASS")
    else:
        log(f"FAIL: path={path}, mode={controller.current_edit_source_mode}")

    log("--- Test 2: Enable RAW (trigger dev) ---")
    controller._develop_raw_backend = MagicMock()
    controller.enable_raw_editing()
    if controller.current_edit_source_mode == "raw":
        log("PASS: Mode switched")
    else:
        log("FAIL: Mode not switched")
    controller._develop_raw_backend.assert_called_once()
    log("PASS: Dev triggered")

    log("--- Test 3: Valid TIFF ---")
    img_jpg.has_working_tif = True
    with patch.object(controller, "is_valid_working_tif", return_value=True):
        controller.load_image_for_editing = MagicMock()
        controller._develop_raw_backend = MagicMock()
        controller.current_edit_source_mode = "jpeg"  # Reset
        controller.enable_raw_editing()

        if (
            controller.current_edit_source_mode == "raw"
            and controller.get_active_edit_path(0) == Path("test.tif")
        ):
            log("PASS: Mode raw, Returns TIFF")
        else:
            log(f"FAIL: returns {controller.get_active_edit_path(0)}")

        controller._develop_raw_backend.assert_not_called()
        log("PASS: No dev triggered")

    log("--- Test 4: RAW Only ---")
    # Mock RAW_EXTENSIONS import
    # Note: Logic in app.py uses local import: from faststack.io.indexer import RAW_EXTENSIONS
    # Patching faststack.io.indexer.RAW_EXTENSIONS works if module is already loaded or loads fresh.
    # Since we imported AppController (which imports indexer), it is loaded.
    with patch("faststack.io.indexer.RAW_EXTENSIONS", {".CR2"}):
        # We also need to patch JPG_EXTENSIONS maybe? No, defaults are fine.
        controller._set_current_index(1)
        if controller.current_edit_source_mode == "raw":
            log("PASS: Auto raw mode")
        else:
            log(f"FAIL: Mode is {controller.current_edit_source_mode}")


run_checks()
