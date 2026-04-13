import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(str(Path(__file__).parents[2]))

from faststack.app import AppController


class TestFolderLoadState(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.image_dir = Path(self.temp_dir.name)
        self.mock_engine = MagicMock()

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

        self.mock_config.getfloat.return_value = 1.0
        self.mock_config.getboolean.return_value = False
        self.mock_config.getint.return_value = 4

        self.qapp_patcher = patch("faststack.app.QCoreApplication")
        self.mock_qapp = self.qapp_patcher.start()
        self.mock_qapp.instance.return_value.aboutToQuit.connect = MagicMock()

        with patch("faststack.app.ImageEditor"):
            self.controller = AppController(self.image_dir, self.mock_engine)

        self.controller.refresh_image_list = MagicMock()
        self.controller.sync_ui_state = MagicMock()
        self.controller.dataChanged = MagicMock()
        self.controller._do_prefetch = MagicMock()
        self.controller.image_files = []
        self.controller.ui_state = MagicMock()

    def tearDown(self):
        self.controller.shutdown_nonqt()
        self.watcher_patcher.stop()
        self.sidecar_patcher.stop()
        self.prefetcher_patcher.stop()
        self.cache_patcher.stop()
        self.config_patcher.stop()
        self.qapp_patcher.stop()
        self.temp_dir.cleanup()

    def test_switch_to_directory_marks_folder_loading_before_reload(self):
        next_dir = self.image_dir / "child"
        next_dir.mkdir()

        self.controller._folder_loaded = True
        self.controller.ui_state.isFolderLoadedChanged.emit.reset_mock()

        with patch.object(self.controller, "load") as mock_load:
            self.controller._switch_to_directory(next_dir, update_base_directory=False)

        self.assertFalse(self.controller._folder_loaded)
        self.controller.ui_state.isFolderLoadedChanged.emit.assert_called_once()
        mock_load.assert_called_once_with(skip_thumbnail_refresh=True)

    def test_load_marks_current_folder_loaded_when_scan_finishes(self):
        self.controller._folder_loaded = False
        self.controller.ui_state.isFolderLoadedChanged.emit.reset_mock()

        self.controller.load(skip_thumbnail_refresh=True)

        self.assertTrue(self.controller._folder_loaded)
        self.controller.ui_state.isFolderLoadedChanged.emit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
