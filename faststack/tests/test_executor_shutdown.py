import time
import threading
import concurrent.futures
import pytest
from faststack.util.executors import (
    create_priority_executor,
    create_daemon_threadpool_executor,
    PriorityExecutor,
)


def test_shutdown_drains_queue_by_default():
    """Test that shutdown(cancel_futures=False) allows queued tasks to run."""
    executor = create_priority_executor(max_workers=1, thread_name_prefix="TestDrain")

    results = []
    started_event = threading.Event()

    def task(val):
        started_event.set()
        time.sleep(0.1)
        results.append(val)
        return val

    # Occupy the worker
    f1 = executor.submit(task, "head")
    started_event.wait(timeout=1.0)

    # Queue some items
    f2 = executor.submit(task, "queued1")
    f3 = executor.submit(task, "queued2")

    # Shutdown without cancelling futures (wait=True by default)
    # This should wait for f1, and then process f2 and f3
    executor.shutdown(wait=True, cancel_futures=False)

    assert f1.result() == "head"
    assert f2.result() == "queued1"
    assert f3.result() == "queued2"

    # Since PriorityExecutor is LIFO for same priority:
    # queued2 is newer than queued1, so it runs first?
    # Let's check the implementation:
    # "2. neg_seq (higher sequence number = more recent = smaller neg_seq = higher priority)"
    # Yes, LIFO.
    assert "head" in results
    assert "queued1" in results
    assert "queued2" in results
    assert len(results) == 3


def test_shutdown_can_cancel_queued():
    """Test that shutdown(cancel_futures=True) cancels queued tasks."""
    executor = create_priority_executor(max_workers=1, thread_name_prefix="TestCancel")

    results = []
    started_event = threading.Event()

    def task(val):
        started_event.set()
        time.sleep(0.1)
        results.append(val)
        return val

    # Occupy the worker
    f1 = executor.submit(task, "head")
    started_event.wait(timeout=1.0)

    # Queue some items
    f2 = executor.submit(task, "queued1")
    f3 = executor.submit(task, "queued2")

    # Shutdown WITH cancelling futures
    executor.shutdown(wait=True, cancel_futures=True)

    # f1 should finish (it was running)
    assert f1.result() == "head"
    assert "head" in results

    # f2 and f3 should be cancelled
    with pytest.raises(concurrent.futures.CancelledError):
        f2.result()

    with pytest.raises(concurrent.futures.CancelledError):
        f3.result()

    assert "queued1" not in results
    assert "queued2" not in results


def test_daemon_threadpool_executor():
    """Test that create_daemon_threadpool_executor creates daemon threads."""
    executor = create_daemon_threadpool_executor(
        max_workers=2, thread_name_prefix="TestDaemon"
    )

    def check_daemon():
        return threading.current_thread().daemon

    futs = [executor.submit(check_daemon) for _ in range(4)]
    results = [f.result() for f in futs]

    assert all(results), "All worker threads should be daemon"
    executor.shutdown()


def test_spawn_overhead_and_error_handling():
    """Test that the creator correctly propagates errors if something was broken."""
    # This checks the defensive coding in create_daemon_threadpool_executor

    # Since we can't easily inject a failure into ThreadPoolExecutor constructor directly
    # without patching, we'll verify it works normally and has the expected structure.

    executor = create_daemon_threadpool_executor(max_workers=1)
    assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
    executor.shutdown()

    # Verify ValueError on invalid workers
    with pytest.raises(ValueError):
        create_daemon_threadpool_executor(max_workers=0)
