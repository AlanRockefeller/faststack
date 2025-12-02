
import unittest
from unittest.mock import MagicMock
from concurrent.futures import Future
from faststack.imaging.prefetch import Prefetcher

class TestPrefetcher(unittest.TestCase):
    def test_submit_task_priority_cancellation(self):
        try:
            # Mock dependencies
            mock_cache_put = MagicMock()
            mock_get_display_info = MagicMock(return_value=(100, 100, 1))
            
            # Create dummy image files
            image_files = [MagicMock() for _ in range(10)]
            
            prefetcher = Prefetcher(
                image_files=image_files,
                cache_put=mock_cache_put,
                prefetch_radius=4,
                get_display_info=mock_get_display_info
            )
            
            # Mock executor
            prefetcher.executor = MagicMock()
            
            # Helper to create a mock future
            def create_future():
                f = MagicMock(spec=Future)
                f.done.return_value = False
                f.cancel.return_value = True
                return f
            
            # Setup initial state
            f0 = create_future()
            f5 = create_future()
            
            prefetcher.futures[0] = f0
            prefetcher.futures[5] = f5
            
            print("Submitting task 4...")
            # Submit priority task for index 4
            prefetcher.submit_task(index=4, generation=0, priority=True)
            print("Task 4 submitted.")
            
            # Check if task 4 was added
            if 4 not in prefetcher.futures:
                raise Exception("Task 4 was not added to futures!")
            
            # Check cancellation of task 0 (should cancel)
            print("Checking task 0 cancellation...")
            f0.cancel.assert_called()
            print("Task 0 cancelled as expected.")
            
            # Check cancellation of task 5 (should NOT cancel)
            print("Checking task 5 cancellation...")
            f5.cancel.assert_not_called()
            print("Task 5 NOT cancelled as expected.")
            
            print("Test passed!")
        except Exception:
            import traceback
            traceback.print_exc()
            raise

if __name__ == "__main__":
    unittest.main()
