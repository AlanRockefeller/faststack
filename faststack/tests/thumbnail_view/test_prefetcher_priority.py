import time
from unittest.mock import MagicMock, patch
import pytest
from pathlib import Path
from faststack.thumbnail_view.prefetcher import ThumbnailCache, ThumbnailPrefetcher


@pytest.fixture
def cache():
    return ThumbnailCache(max_bytes=1024 * 1024, max_items=100)


def test_prefetcher_priority(cache):
    """Verify that high priority jobs jump ahead of medium priority ones."""
    finished_jobs = []

    def mock_decode(path, path_hash, mtime_ns, size, *args, **kwargs):
        # Simulate some work
        time.sleep(0.1)
        finished_jobs.append(path.name)
        return b"fake_data"

    # Single worker to make the queue behavior deterministic
    pf = ThumbnailPrefetcher(
        cache=cache,
        on_ready_callback=lambda x: None,
        max_workers=1,
        target_size=200,
    )

    try:
        with patch.object(pf, "_decode_worker", side_effect=mock_decode):
            # 1. Submit 5 medium priority jobs
            # med_0 will start immediately on the single worker thread
            pf.submit(Path("med_0.jpg"), 1000, priority=pf.PRIO_MED)

            # small sleep to ensure med_0 is pulled by the worker
            time.sleep(0.02)

            for i in range(1, 5):
                pf.submit(Path(f"med_{i}.jpg"), 1000, priority=pf.PRIO_MED)

            # 2. Submit 1 high priority job
            pf.submit(Path("high_0.jpg"), 1000, priority=pf.PRIO_HIGH)

            # 3. Wait for all to finish
            deadline = time.time() + 2.0
            while len(finished_jobs) < 6 and time.time() < deadline:
                time.sleep(0.1)

            # Verification:
            # - finished_jobs[0] should be med_0.jpg (started first)
            # - finished_jobs[1] should be high_0.jpg (jumped the queue)
            # - others should follow

            assert len(finished_jobs) == 6
            assert finished_jobs[0] == "med_0.jpg"
            assert finished_jobs[1] == "high_0.jpg"

    finally:
        pf.shutdown()


def test_prefetcher_lifo_behavior(cache):
    """Verify that jobs within same priority have LIFO behavior (most recent first)."""
    finished_jobs = []

    def mock_decode(path, path_hash, mtime_ns, size, *args, **kwargs):
        time.sleep(0.05)
        finished_jobs.append(path.name)
        return b"fake_data"

    pf = ThumbnailPrefetcher(
        cache=cache,
        on_ready_callback=lambda x: None,
        max_workers=1,
        target_size=200,
    )

    try:
        with patch.object(pf, "_decode_worker", side_effect=mock_decode):
            # Submit first job to busy the worker
            pf.submit(Path("job_0.jpg"), 1000)
            time.sleep(0.01)

            # Submit sequential jobs
            pf.submit(Path("job_1.jpg"), 1000)
            time.sleep(0.01)
            pf.submit(Path("job_2.jpg"), 1000)
            time.sleep(0.01)
            pf.submit(Path("job_3.jpg"), 1000)

            # Wait for all
            deadline = time.time() + 2.0
            while len(finished_jobs) < 4 and time.time() < deadline:
                time.sleep(0.05)

            assert len(finished_jobs) == 4
            assert finished_jobs[0] == "job_0.jpg"
            # job_3 should be second because it was submitted LAST (LIFO)
            assert finished_jobs[1] == "job_3.jpg"
            assert finished_jobs[2] == "job_2.jpg"
            assert finished_jobs[3] == "job_1.jpg"
    finally:
        pf.shutdown()
