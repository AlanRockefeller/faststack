import unittest
from unittest.mock import MagicMock, patch
import sys

# Important: Do NOT mock sys.modules at the top level.
# This causes pollution that breaks other tests (like test_cache_invalidation.py).

from faststack.app import AppController


class TestConfigSetters(unittest.TestCase):
    def setUp(self):
        # Apply patches for all dependencies of AppController to isolate it
        # and prevent side effects (like Qt init or file I/O).

        # Patch the config object specifically in faststack.app
        # faststack.app imports config as: from faststack.config import config
        self.config_patch = patch("faststack.app.config")
        self.mock_config = self.config_patch.start()

        # Default mock config behavior
        self.mock_config.getfloat.return_value = 0.1
        self.mock_config.getboolean.return_value = False
        self.mock_config.getint.return_value = 4

        self.patches = [
            # Qt classes
            patch("faststack.app.QTimer"),
            patch("faststack.app.QDrag"),
            patch("faststack.app.QPixmap"),
            patch("faststack.app.QMimeData"),
            patch("faststack.app.QFileDialog"),
            # Application classes
            patch("faststack.app.DecodedImage"),
            patch("faststack.app.ImageEditor"),
            patch("faststack.app.Prefetcher"),
            patch("faststack.app.ByteLRUCache"),
            patch("faststack.app.SidecarManager"),
            patch("faststack.app.Keybinder"),
            patch("faststack.app.Watcher"),
            patch("faststack.app.ThumbnailModel"),
            patch("faststack.app.ThumbnailCache"),
            patch("faststack.app.ThumbnailPrefetcher"),
            patch("faststack.app.ThumbnailProvider"),
            patch("faststack.app.PathResolver"),
            patch("faststack.app.UIState"),
            patch("faststack.app.ImageProvider"),
            # Standard lib/Other
            patch("faststack.app.Path"),
            patch("faststack.app.concurrent.futures.ThreadPoolExecutor"),
        ]

        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

        self.addCleanup(self.config_patch.stop)

        # Initialize controller with mock engine and path
        # The imports in faststack.app are now real, but the names used in __init__
        # are patched.
        self.controller = AppController(MagicMock(), MagicMock())

    def test_set_auto_level_clipping_threshold(self):
        self.mock_config.set.reset_mock()
        self.mock_config.save.reset_mock()

        # Pre-verify default value (set in __init__ using config.getfloat mock)
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), 0.1)

        new_val = 0.5
        self.controller.set_auto_level_clipping_threshold(new_val)

        # Verify normal set
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), new_val)
        # Should be stringified "0.5"
        self.mock_config.set.assert_called_with("core", "auto_level_threshold", "0.5")
        self.mock_config.save.assert_called_once()

        # Verify Clamping (High)
        self.mock_config.set.reset_mock()
        self.controller.set_auto_level_clipping_threshold(1.5)
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), 1.0)
        self.mock_config.set.assert_called_with("core", "auto_level_threshold", "1")

        # Verify Clamping (Low)
        self.mock_config.set.reset_mock()
        self.controller.set_auto_level_clipping_threshold(-0.1)
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), 0.0)
        self.mock_config.set.assert_called_with("core", "auto_level_threshold", "0")

    def test_set_auto_level_strength(self):
        self.mock_config.set.reset_mock()
        self.mock_config.save.reset_mock()

        # Default was 1.0 in code, but our mock config.getfloat returns 0.1 (as per setUp)
        # Wait, if config.getfloat returned 0.1 for threshold, did it return 0.1 for strength too?
        # Yes, line 62 in original: mock_config_obj.getfloat.return_value = 0.1
        # In setUp I set it to 0.1.

        # But wait, config.getfloat is called with default 1.0 for strength in app.py:
        # self.auto_level_strength = config.getfloat("core", "auto_level_strength", 1.0)
        # If I mock getfloat to always return 0.1, then it's 0.1.

        new_val = 0.8
        self.controller.set_auto_level_strength(new_val)

        self.assertEqual(self.controller.get_auto_level_strength(), new_val)
        self.mock_config.set.assert_called_with("core", "auto_level_strength", "0.8")
        self.mock_config.save.assert_called_once()

        # Verify Clamping
        self.mock_config.set.reset_mock()
        self.controller.set_auto_level_strength(2.0)
        self.assertEqual(self.controller.get_auto_level_strength(), 1.0)
        self.mock_config.set.assert_called_with("core", "auto_level_strength", "1")

    def test_set_auto_level_strength_auto(self):
        self.mock_config.set.reset_mock()
        self.mock_config.save.reset_mock()

        new_val = True
        self.controller.set_auto_level_strength_auto(new_val)

        self.assertEqual(self.controller.get_auto_level_strength_auto(), new_val)
        # Should be normalized "true" string
        self.mock_config.set.assert_called_with(
            "core", "auto_level_strength_auto", "true"
        )
        self.mock_config.save.assert_called_once()

        # Test False
        self.mock_config.set.reset_mock()
        self.controller.set_auto_level_strength_auto(False)
        self.mock_config.set.assert_called_with(
            "core", "auto_level_strength_auto", "false"
        )


if __name__ == "__main__":
    unittest.main()
