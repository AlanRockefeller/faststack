import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from faststack.ui.provider import UIState


class TestUIPrefetchSafety(unittest.TestCase):
    def setUp(self):
        self.app_controller = MagicMock()
        self.model = MagicMock()
        self.prefetcher = MagicMock()

        # Setup model and prefetcher on app_controller
        self.app_controller._thumbnail_model = self.model
        self.app_controller._thumbnail_prefetcher = self.prefetcher

        # Mock prefetcher constants
        self.prefetcher.PRIO_MED = 1

        # Fake clock state
        self.current_time = 100.0

        def fake_clock():
            return self.current_time

        self.ui_state = UIState(self.app_controller, clock_func=fake_clock)

        # Default mock behavior
        self.model.rowCount.return_value = 5000
        self.model.thumbnail_size = 256

        def get_entry_mock(i):
            entry = MagicMock()
            entry.path = Path(f"image_{i}.jpg")
            entry.mtime_ns = 123456789
            entry.is_folder = False
            return entry

        self.model.get_entry.side_effect = get_entry_mock

    def test_budget_trimming(self):
        """Verify that range is trimmed to maxCount."""
        self.ui_state.gridPrefetchRange(0, 4999, maxCount=200)
        # Expected: 200 submissions (index 0 to 199)
        self.assertEqual(self.prefetcher.submit.call_count, 200)

    def test_hard_cap(self):
        """Verify that budget is capped at HARD_LIMIT (800)."""
        self.ui_state.gridPrefetchRange(0, 4999, maxCount=2000)
        # Expected: 800 submissions (index 0 to 799)
        self.assertEqual(self.prefetcher.submit.call_count, 800)

    def test_duplicate_suppression(self):
        """Verify identical requests within 30ms are suppressed."""
        # 1. First call - should succeed
        self.ui_state.gridPrefetchRange(10, 50, maxCount=100)
        self.assertEqual(self.prefetcher.submit.call_count, 41)
        self.prefetcher.submit.reset_mock()

        # 2. Duplicate call at t+10ms - should be suppressed
        self.current_time += 0.010
        self.ui_state.gridPrefetchRange(10, 50, maxCount=100)
        self.assertEqual(self.prefetcher.submit.call_count, 0)

        # 3. Duplicate call at t+40ms (from start) - should succeed (cooldown expired)
        self.current_time += 0.030
        self.ui_state.gridPrefetchRange(10, 50, maxCount=100)
        self.assertEqual(self.prefetcher.submit.call_count, 41)
        self.prefetcher.submit.reset_mock()

        # 4. Different range at t+10ms (from last) - should succeed (not duplicate)
        self.current_time += 0.010
        self.ui_state.gridPrefetchRange(10, 51, maxCount=100)
        self.assertEqual(self.prefetcher.submit.call_count, 42)

    def test_index_sanity(self):
        """Verify handling of out-of-bounds, empty model, and invalid ranges."""
        # Case 1: Empty model
        self.model.rowCount.return_value = 0
        self.prefetcher.submit.reset_mock()
        self.ui_state.gridPrefetchRange(0, 10, 100)
        self.assertEqual(self.prefetcher.submit.call_count, 0)

        # Case 2: startIndex > endIndex
        self.model.rowCount.return_value = 5000
        self.ui_state.gridPrefetchRange(100, 50, 100)
        self.assertEqual(self.prefetcher.submit.call_count, 0)

        # Case 3: Out-of-bounds clamping
        self.prefetcher.submit.reset_mock()
        self.ui_state.gridPrefetchRange(-10, 10000, 100)
        # Should be clamped to [0, 4999], then budgeted to [0, 99]
        self.ui_state.gridPrefetchRange(-10, 10000, 100)
        self.assertEqual(self.prefetcher.submit.call_count, 100)


if __name__ == "__main__":
    unittest.main()
