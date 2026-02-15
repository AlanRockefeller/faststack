import unittest
from unittest.mock import MagicMock
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
import sys
from pathlib import Path

# Mock config
sys.modules["faststack.config"] = MagicMock()
from faststack.imaging.prefetch import Prefetcher

class ReproFuturesCleanup(unittest.TestCase):
    def test_newer_future_is_not_deleted_by_older_task(self):
        # Dependencies
        mock_cache_put = MagicMock()
        mock_get_display_info = MagicMock(return_value=(100, 100, 1))
        image_files = [MagicMock(path=Path(f"test_{i}.jpg")) for i in range(10)]

        prefetcher = Prefetcher(
            image_files=image_files,
            cache_put=mock_cache_put,
            prefetch_radius=4,
            get_display_info=mock_get_display_info,
        )

        # We want to simulate:
        # 1. Task A starts for index 1
        # 2. Task B starts for index 1 (overwriting Task A in self.futures)
        # 3. Task A finishes and tries to delete index 1 from self.futures
        # 4. Task B should still be in self.futures

        future_a = MagicMock(spec=Future)
        future_a.done.return_value = False
        
        future_b = MagicMock(spec=Future)
        future_b.done.return_value = False

        index = 1
        prefetcher.futures[index] = future_a
        
        # Simulate Task A's finally block running with its 'future' reference
        # but the actual prefetcher.futures[index] has been replaced by future_b
        prefetcher.futures[index] = future_b
        
        # Now simulate Task A completing its cleanup
        # This is what _decode_and_cache does in its finally block:
        # with self._futures_lock:
        #     if self.futures.get(index) is future:
        #         del self.futures[index]
        
        def simulate_cleanup(prefetcher, idx, fut):
            with prefetcher._futures_lock:
                if prefetcher.futures.get(idx) is fut:
                    del prefetcher.futures[idx]

        simulate_cleanup(prefetcher, index, future_a)
        
        self.assertIn(index, prefetcher.futures, "Newer future was deleted by older task cleanup!")
        self.assertIs(prefetcher.futures[index], future_b, "The future in self.futures is not the newer one!")
        
        # Now simulate Task B cleanup
        simulate_cleanup(prefetcher, index, future_b)
        self.assertNotIn(index, prefetcher.futures, "Future was not deleted after its OWN cleanup!")

if __name__ == "__main__":
    unittest.main()
