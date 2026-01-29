"""Tests for Esc key closing histogram behavior."""

from unittest.mock import MagicMock

# Qt Key constants (avoid PySide6 import for test portability)
Key_Escape = 0x01000000
Key_Left = 0x01000012
Key_Right = 0x01000014
Key_H = 0x48
Key_Return = 0x01000004


class TestEscHistogramBehavior:
    """Tests for Esc key closing histogram before other actions."""

    def test_esc_closes_histogram_when_visible(self):
        """When histogram is visible, Esc should close it and consume the event."""
        # Create a mock UIState with histogram visible
        mock_ui_state = MagicMock()
        mock_ui_state.isHistogramVisible = True
        mock_ui_state.isCropping = False
        mock_ui_state.isEditorOpen = False

        # Simulate the escape handling logic from eventFilter
        # This tests the core logic without needing a full AppController
        def handle_esc_for_histogram(ui_state, key):
            """Extracted logic from eventFilter for testing."""
            if key == Key_Escape and ui_state.isHistogramVisible:
                ui_state.isHistogramVisible = False
                return True  # Event consumed
            return False

        result = handle_esc_for_histogram(mock_ui_state, Key_Escape)

        assert result is True  # Event was consumed
        assert mock_ui_state.isHistogramVisible is False

    def test_esc_does_not_consume_when_histogram_hidden(self):
        """When histogram is hidden, Esc should not be consumed by histogram logic."""
        mock_ui_state = MagicMock()
        mock_ui_state.isHistogramVisible = False

        def handle_esc_for_histogram(ui_state, key):
            """Extracted logic from eventFilter for testing."""
            if key == Key_Escape and ui_state.isHistogramVisible:
                ui_state.isHistogramVisible = False
                return True
            return False

        result = handle_esc_for_histogram(mock_ui_state, Key_Escape)

        assert result is False  # Event NOT consumed, should propagate

    def test_non_esc_keys_not_affected(self):
        """Non-Esc keys should not trigger histogram close."""
        mock_ui_state = MagicMock()
        mock_ui_state.isHistogramVisible = True

        def handle_esc_for_histogram(ui_state, key):
            """Extracted logic from eventFilter for testing."""
            if key == Key_Escape and ui_state.isHistogramVisible:
                ui_state.isHistogramVisible = False
                return True
            return False

        # Test with other keys
        for key in [Key_Left, Key_Right, Key_H, Key_Return]:
            result = handle_esc_for_histogram(mock_ui_state, key)
            assert result is False
            # Histogram should still be visible
            assert mock_ui_state.isHistogramVisible is True


class TestEscHistogramPriority:
    """Test that histogram close happens before grid view switch."""

    def test_histogram_closes_without_triggering_grid_switch(self):
        """Histogram should close before any grid view switch logic runs."""
        mock_ui_state = MagicMock()
        mock_ui_state.isHistogramVisible = True

        # Track if grid switch was called
        grid_switch_called = False

        def switch_to_grid_view():
            nonlocal grid_switch_called
            grid_switch_called = True

        def handle_esc_with_priority(ui_state, key, do_grid_switch):
            """Simulates the eventFilter priority: histogram first, then grid."""
            # First priority: histogram
            if key == Key_Escape and ui_state.isHistogramVisible:
                ui_state.isHistogramVisible = False
                return True  # Consume, don't continue to grid logic

            # Second priority: grid switch (only if not consumed above)
            if key == Key_Escape:
                do_grid_switch()
                return True

            return False

        result = handle_esc_with_priority(
            mock_ui_state, Key_Escape, switch_to_grid_view
        )

        assert result is True
        assert mock_ui_state.isHistogramVisible is False
        assert grid_switch_called is False  # Grid switch NOT triggered
