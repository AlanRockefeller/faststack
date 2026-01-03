
import threading
import time
import pytest
import numpy as np
from pathlib import Path
from concurrent.futures import Future

from faststack.imaging.prefetch import Prefetcher
from faststack.models import ImageFile

# Mock objects to isolate Prefetcher logic
class MockImageFile:
    def __init__(self, index):
        self.path = Path(f"/mock/image_{index}.jpg")

def mock_get_display_info():
    return 1920, 1080, 1

def mock_cache_put(key, value):
    pass

@pytest.fixture
def prefetcher():
    image_files = [MockImageFile(i) for i in range(100)]
    # Use a small radius to force more activity
    p = Prefetcher(image_files, mock_cache_put, prefetch_radius=5, get_display_info=mock_get_display_info, debug=False)
    
    # Mock the internal decode method to avoid actual I/O and processing
    # We just return a dummy result after a tiny sleep
    def mock_decode_and_cache(*args, **kwargs):
        time.sleep(0.0001) # fast sleep
        return Path("/mock/image_x.jpg"), 1

    p._decode_and_cache = mock_decode_and_cache
    
    yield p
    p.shutdown()

def test_prefetch_concurrency(prefetcher):
    """
    Stress test for race conditions in Prefetcher.
    Simulates concurrent navigation (update_prefetch), cancellation (cancel_all),
    and file list updates (set_image_files).
    """
    
    # Configuration
    num_loops = 5000
    num_threads = 4
    
    # Shared state for error tracking
    errors = []
    
    # Barrier to synchronize start
    barrier = threading.Barrier(num_threads)
    
    stop_event = threading.Event()

    def worker_update():
        try:
            barrier.wait()
            for i in range(num_loops):
                if stop_event.is_set(): break
                # Randomly jump around
                idx = i % 100
                prefetcher.update_prefetch(idx, is_navigation=True, direction=1)
        except Exception as e:
            errors.append(e)
            stop_event.set()

    def worker_cancel():
        try:
            barrier.wait()
            for i in range(num_loops):
                if stop_event.is_set(): break
                if i % 10 == 0: # Cancel less frequently
                    prefetcher.cancel_all()
        except Exception as e:
            errors.append(e)
            stop_event.set()

    def worker_set_files():
        try:
            barrier.wait()
            # Generate two lists to toggle between
            list1 = [MockImageFile(i) for i in range(100)]
            list2 = [MockImageFile(i) for i in range(50)] # Different size
            
            for i in range(num_loops):
                if stop_event.is_set(): break
                if i % 100 == 0: # Reload files occasionally
                    new_list = list2 if i % 200 == 0 else list1
                    prefetcher.set_image_files(new_list)
        except Exception as e:
            errors.append(e)
            stop_event.set()

    # Create threads
    threads = [
        threading.Thread(target=worker_update),
        threading.Thread(target=worker_update), # Two updaters
        threading.Thread(target=worker_cancel),
        threading.Thread(target=worker_set_files)
    ]

    # Start threads
    for t in threads:
        t.start()

    # Wait for completion
    for t in threads:
        t.join()

    # Assertions
    if errors:
        pytest.fail(f"Exceptions occurred in worker threads: {errors}")

    # Verify internal consistency
    with prefetcher._futures_lock:
        # Check that scheduled matches generation (basic check)
        for gen, scheduled_set in prefetcher._scheduled.items():
            if gen > prefetcher.generation:
                 pytest.fail(f"Found scheduled set for future generation {gen} > {prefetcher.generation}")
        
        # Check futures dict consistency
        # It's hard to assert exact size since threads stopped at random times,
        # but we can check if keys in futures are valid integers roughly
        assert isinstance(prefetcher.futures, dict)
            
