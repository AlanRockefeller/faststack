import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

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
        self.prefetcher.PRIO_HIGH = 0
        self.prefetcher.PRIO_MED = 1

        # Fake clock state
        self.current_time = 100.0

        def fake_clock():
            return self.current_time

        self.ui_state = UIState(self.app_controller, clock_func=fake_clock)

        # Default mock behavior
        self.model.rowCount.return_value = 5000
        self.model.thumbnail_size = 256

        # Plain objects: building hundreds of MagicMock entries per call
        # dominated this file's runtime.
        def get_entry_mock(i):
            return SimpleNamespace(
                path=Path(f"image_{i}.jpg"),
                mtime_ns=123456789,
                source_identity="0",
                is_folder=False,
            )

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

    def test_visible_range_is_submitted_first_at_high_priority(self):
        """Visible rows should not sit behind scroll-ahead thumbnail work."""
        self.model.rowCount.return_value = 20

        self.ui_state.gridPrefetchRange(0, 9, 10, 2, 4)

        calls = self.prefetcher.submit.call_args_list
        submitted_names = [call.args[0].name for call in calls[:3]]
        submitted_priorities = [call.kwargs["priority"] for call in calls[:3]]

        # Submitted in reverse because PriorityExecutor is LIFO within priority.
        self.assertEqual(submitted_names, ["image_4.jpg", "image_3.jpg", "image_2.jpg"])
        self.assertEqual(submitted_priorities, [self.prefetcher.PRIO_HIGH] * 3)
        self.assertTrue(
            all(
                call.kwargs["priority"] == self.prefetcher.PRIO_MED
                for call in calls[3:]
            )
        )


if __name__ == "__main__":
    unittest.main()
