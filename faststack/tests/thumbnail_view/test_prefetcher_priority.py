import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from faststack.thumbnail_view.prefetcher import ThumbnailCache, ThumbnailPrefetcher


@pytest.fixture
def cache():
    return ThumbnailCache(max_bytes=1024 * 1024, max_items=100)


def test_prefetcher_priority(cache):
    """Verify that high priority jobs jump ahead of medium priority ones."""
    started = threading.Event()
    release = threading.Event()
    finished_jobs = []

    def mock_decode(path, path_hash, mtime_ns, size, *args, **kwargs):
        # med_0 holds the single worker until every other job is queued, so the
        # queue ordering under test is what decides the result -- not how fast
        # the runner happens to schedule threads.
        if path.name == "med_0.jpg":
            started.set()
            release.wait(2.0)
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
            # 1. Submit 5 medium priority jobs. med_0 occupies the worker; the
            # rest queue up behind it.
            pf.submit(Path("med_0.jpg"), 1000, priority=pf.PRIO_MED)
            assert started.wait(2.0)

            for i in range(1, 5):
                pf.submit(Path(f"med_{i}.jpg"), 1000, priority=pf.PRIO_MED)

            # 2. Submit 1 high priority job
            pf.submit(Path("high_0.jpg"), 1000, priority=pf.PRIO_HIGH)

            # 3. Let the worker drain the queue and wait for all to finish
            release.set()
            deadline = time.time() + 5.0
            while len(finished_jobs) < 6 and time.time() < deadline:
                time.sleep(0.01)

            # Verification:
            # - finished_jobs[0] should be med_0.jpg (started first)
            # - finished_jobs[1] should be high_0.jpg (jumped the queue)
            # - others should follow

            assert len(finished_jobs) == 6
            assert finished_jobs[0] == "med_0.jpg"
            assert finished_jobs[1] == "high_0.jpg"

    finally:
        release.set()
        pf.shutdown()


def test_prefetcher_lifo_behavior(cache):
    """Verify that jobs within same priority have LIFO behavior (most recent first)."""
    started = threading.Event()
    release = threading.Event()
    finished_jobs = []

    def mock_decode(path, path_hash, mtime_ns, size, *args, **kwargs):
        # job_0 pins the single worker until 1..3 are all queued. Sleeping
        # between submits instead would let the worker pick one up early and
        # scramble the stack order on a fast or heavily loaded machine.
        if path.name == "job_0.jpg":
            started.set()
            release.wait(2.0)
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
            assert started.wait(2.0)

            # Submit sequential jobs; they stack up behind job_0
            pf.submit(Path("job_1.jpg"), 1000)
            pf.submit(Path("job_2.jpg"), 1000)
            pf.submit(Path("job_3.jpg"), 1000)

            # Wait for all
            release.set()
            deadline = time.time() + 5.0
            while len(finished_jobs) < 4 and time.time() < deadline:
                time.sleep(0.01)

            assert len(finished_jobs) == 4
            assert finished_jobs[0] == "job_0.jpg"
            # job_3 should be second because it was submitted LAST (LIFO)
            assert finished_jobs[1] == "job_3.jpg"
            assert finished_jobs[2] == "job_2.jpg"
            assert finished_jobs[3] == "job_1.jpg"
    finally:
        release.set()
        pf.shutdown()


def test_coalesced_priority_upgrade_reorders_queued_job(cache):
    """A duplicate high-priority submit should bump the queued original job."""
    started = threading.Event()
    release = threading.Event()
    finished_jobs = []

    def mock_decode(path, path_hash, mtime_ns, size, *args, **kwargs):
        if path.name == "blocker.jpg":
            started.set()
            release.wait(2.0)
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
            assert pf.submit(Path("blocker.jpg"), 1000, priority=pf.PRIO_MED)
            assert started.wait(1.0)

            assert pf.submit(Path("target_visible.jpg"), 1000, priority=pf.PRIO_MED)
            assert not pf.submit(
                Path("target_visible.jpg"), 1000, priority=pf.PRIO_HIGH
            )
            assert pf.submit(Path("newer_medium.jpg"), 1000, priority=pf.PRIO_MED)

            release.set()
            deadline = time.time() + 2.0
            while len(finished_jobs) < 3 and time.time() < deadline:
                time.sleep(0.05)

            assert len(finished_jobs) == 3
            assert finished_jobs == [
                "blocker.jpg",
                "target_visible.jpg",
                "newer_medium.jpg",
            ]
    finally:
        pf.shutdown()
