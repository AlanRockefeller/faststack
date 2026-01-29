import unittest
import sys
from unittest.mock import MagicMock, patch

# --- MOCK SETUP ---

# Mock PySide6
mock_pyside = MagicMock()
mock_pyside.__path__ = []
mock_pyside.__spec__ = MagicMock()


# Define a real class for QObject so inheritance works as expected
class MockQObject:
    def __init__(self, parent=None):
        pass

    def property(self, name):
        return None

    def setProperty(self, name, value):
        pass


mock_pyside.QObject = MockQObject


# Mock Slot/Signal decorators to just return the function/dummy
def MockSlot(*args, **kwargs):
    def decorator(func):
        return func

    return decorator


mock_pyside.Slot = MockSlot
mock_pyside.Signal = MagicMock()

sys.modules["PySide6"] = mock_pyside
sys.modules["PySide6.QtCore"] = mock_pyside
sys.modules["PySide6.QtGui"] = mock_pyside
sys.modules["PySide6.QtQuick"] = mock_pyside
sys.modules["PySide6.QtWidgets"] = mock_pyside
sys.modules["PySide6.QtQml"] = mock_pyside

# Mock PIL
mock_pil = MagicMock()
mock_pil.__path__ = []
mock_pil.Image = MagicMock()
sys.modules["PIL"] = mock_pil
sys.modules["PIL.Image"] = mock_pil.Image

# Mock numpy
#sys.modules["numpy"] = MagicMock()

# Mock faststack.config
mock_config_module = MagicMock()
mock_config_obj = MagicMock()
mock_config_obj.getfloat.return_value = 0.1
mock_config_obj.getboolean.return_value = False
mock_config_module.config = mock_config_obj
sys.modules["faststack.config"] = mock_config_module

# Mock faststack modules
sys.modules["faststack.ui.provider"] = MagicMock()
sys.modules["faststack.models"] = MagicMock()
sys.modules["faststack.logging_setup"] = MagicMock()
sys.modules["faststack.io.indexer"] = MagicMock()
sys.modules["faststack.io.sidecar"] = MagicMock()
sys.modules["faststack.io.watcher"] = MagicMock()
sys.modules["faststack.io.helicon"] = MagicMock()
sys.modules["faststack.io.executable_validator"] = MagicMock()
sys.modules["faststack.imaging.cache"] = MagicMock()
sys.modules["faststack.imaging.prefetch"] = MagicMock()
sys.modules["faststack.ui.keystrokes"] = MagicMock()
sys.modules["faststack.imaging.editor"] = MagicMock()
sys.modules["faststack.imaging.metadata"] = MagicMock()

import faststack

print(f"DEBUG: faststack imported from: {faststack.__file__}")
print(f"DEBUG: sys.path: {sys.path}")

# Import AppController AFTER mocking
from faststack.app import AppController
from faststack.config import config


class TestConfigSetters(unittest.TestCase):
    def setUp(self):
        # Apply patches using start/addCleanup
        self.patches = [
            patch("faststack.app.QTimer"),
            patch("faststack.app.DecodedImage"),
            patch("faststack.app.ImageEditor"),
            patch("faststack.app.Prefetcher"),
            patch("faststack.app.ByteLRUCache"),
            patch("faststack.app.SidecarManager"),
            patch("faststack.app.Keybinder"),
            patch("faststack.app.Path"),
        ]

        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

        # Initialize controller
        # Mock Path for init argument
        mock_path_cls = self.patches[
            -1
        ].target  # access the mock object ? NO, p.start returns mock
        # Ideally capture the return of start()

        # Simpler: just instantiate. The mocks are active.
        # But we need to pass a mock path to __init__
        self.controller = AppController(MagicMock(), MagicMock())

    def test_set_auto_level_clipping_threshold(self):
        config.set.reset_mock()
        config.save.reset_mock()

        # Pre-verify default value (set in __init__ using config.getfloat mock)
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), 0.1)

        new_val = 0.5
        self.controller.set_auto_level_clipping_threshold(new_val)

        # Verify
        # Verify normal set
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), new_val)
        # Should be stringified "0.5"
        config.set.assert_called_with("core", "auto_level_threshold", "0.5")
        config.save.assert_called_once()

        # Verify Clamping (High)
        config.set.reset_mock()
        self.controller.set_auto_level_clipping_threshold(1.5)
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), 1.0)
        config.set.assert_called_with("core", "auto_level_threshold", "1")

        # Verify Clamping (Low)
        config.set.reset_mock()
        self.controller.set_auto_level_clipping_threshold(-0.1)
        self.assertEqual(self.controller.get_auto_level_clipping_threshold(), 0.0)
        config.set.assert_called_with("core", "auto_level_threshold", "0")

    def test_set_auto_level_strength(self):
        config.set.reset_mock()
        config.save.reset_mock()

        # Default was 1.0 in code, but our mock config.getfloat returns 0.1
        # AppController: self.auto_level_strength = config.getfloat(..., 1.0)
        # Mock config.getfloat returns 0.1 always as setup above.

        new_val = 0.8
        self.controller.set_auto_level_strength(new_val)

        self.assertEqual(self.controller.get_auto_level_strength(), new_val)
        config.set.assert_called_with("core", "auto_level_strength", "0.8")
        config.save.assert_called_once()

        # Verify Clamping
        config.set.reset_mock()
        self.controller.set_auto_level_strength(2.0)
        self.assertEqual(self.controller.get_auto_level_strength(), 1.0)
        config.set.assert_called_with("core", "auto_level_strength", "1")

    def test_set_auto_level_strength_auto(self):
        config.set.reset_mock()
        config.save.reset_mock()

        new_val = True
        self.controller.set_auto_level_strength_auto(new_val)

        self.assertEqual(self.controller.get_auto_level_strength_auto(), new_val)
        # Should be normalized "true" string
        config.set.assert_called_with("core", "auto_level_strength_auto", "true")
        config.save.assert_called_once()

        # Test False
        config.set.reset_mock()
        self.controller.set_auto_level_strength_auto(False)
        config.set.assert_called_with("core", "auto_level_strength_auto", "false")


if __name__ == "__main__":
    unittest.main()
