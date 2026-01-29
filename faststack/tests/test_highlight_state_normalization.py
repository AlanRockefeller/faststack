"""Unit test for highlightState normalization in UIState."""

import unittest
from unittest.mock import MagicMock
from faststack.ui.provider import UIState


class TestUIStateNormalization(unittest.TestCase):
    def setUp(self):
        # Mock app_controller and image_editor
        self.mock_controller = MagicMock()
        self.mock_editor = MagicMock()
        self.mock_controller.image_editor = self.mock_editor
        self.ui_state = UIState(self.mock_controller)

    def test_highlight_state_normalization_legacy_keys(self):
        """Test with legacy keys."""
        self.mock_editor._last_highlight_state = {
            "headroom_pct": 0.1,
            "clipped_pct": 0.2,
            "near_white_pct": 0.3,
        }
        state = self.ui_state.highlightState
        self.assertEqual(state["headroom_pct"], 0.1)
        self.assertEqual(state["source_clipped_pct"], 0.0)
        self.assertEqual(state["current_nearwhite_pct"], 0.0)

    def test_highlight_state_normalization_standard(self):
        """Test with canonical keys present."""
        self.mock_editor._last_highlight_state = {
            "headroom_pct": 0.1,
            "source_clipped_pct": 0.4,
            "current_nearwhite_pct": 0.5,
        }
        state = self.ui_state.highlightState
        self.assertEqual(state["headroom_pct"], 0.1)
        self.assertEqual(state["source_clipped_pct"], 0.4)
        self.assertEqual(state["current_nearwhite_pct"], 0.5)

    def test_highlight_state_normalization_empty(self):
        """Test with empty state."""
        self.mock_editor._last_highlight_state = None
        state = self.ui_state.highlightState
        self.assertEqual(state["headroom_pct"], 0.0)
        self.assertEqual(state["source_clipped_pct"], 0.0)
        self.assertEqual(state["current_nearwhite_pct"], 0.0)

    def test_highlight_state_normalization_missing_keys(self):
        """Test with missing keys."""
        self.mock_editor._last_highlight_state = {"headroom_pct": 0.1}
        state = self.ui_state.highlightState
        self.assertEqual(state["headroom_pct"], 0.1)
        self.assertEqual(state["source_clipped_pct"], 0.0)
        self.assertEqual(state["current_nearwhite_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
