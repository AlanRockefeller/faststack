"""Regression test for FS-P1-003: daemon pools must not block interpreter exit.

``ThreadPoolExecutor`` registers each worker in
``concurrent.futures.thread._threads_queues``, and CPython's interpreter-exit
hook joins every registered worker -- daemon flag or not. A pool worker blocked
in a long decode therefore kept FastStack alive during finalisation, *after*
the 7-second ``os._exit`` backstop in ``_shutdown_with_timeout`` had already
been cancelled: an unkillable hang on quit.

This is deliberately a subprocess test. The behaviour only manifests during
real interpreter finalisation, and it depends on private ``concurrent.futures``
internals, so it must be verified on whatever CPython actually runs the suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Long enough to be unambiguous, short enough that a regression is not a
# multi-minute CI stall. A healthy exit takes well under a second.
EXIT_TIMEOUT_SECONDS = 20

CHILD = textwrap.dedent(
    """
    import sys, threading
    sys.path.insert(0, %r)
    from faststack.util.executors import create_daemon_threadpool_executor

    started = threading.Event()
    block = threading.Event()

    def blocked_forever():
        started.set()
        block.wait()          # never released: simulates a stuck decode

    executor = create_daemon_threadpool_executor(
        max_workers=1, thread_name_prefix="ExitRepro"
    )
    queued = executor.submit(blocked_forever)
    assert started.wait(10), "worker never started"

    # A second task stays queued behind the blocked one.
    never_runs = executor.submit(blocked_forever)

    executor.shutdown(wait=False, cancel_futures=True)
    assert never_runs.cancelled(), "queued work should be cancellable"

    print("CHILD_OK", flush=True)
    # Main thread now returns; the interpreter must finalise without joining
    # the abandoned daemon worker.
    """
)


def _repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])


def test_blocked_daemon_worker_does_not_block_interpreter_exit():
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD % _repo_root()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Never leave a hung child behind, or pytest itself stalls at exit.
        proc.kill()
        proc.communicate()
        pytest.fail(
            f"Child process did not exit within {EXIT_TIMEOUT_SECONDS}s on "
            f"{sys.version.split()[0]}: a blocked daemon worker is still "
            "participating in CPython's interpreter-exit join (FS-P1-003)."
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert "CHILD_OK" in stdout, f"child failed early:\n{stdout}\n{stderr}"
    assert proc.returncode == 0, f"child exited {proc.returncode}:\n{stderr}"


def test_daemon_pool_is_not_registered_with_concurrent_futures():
    """The mechanism itself: no worker of ours enters the stdlib join registry."""
    import threading
    from concurrent.futures.thread import _threads_queues

    from faststack.util.executors import create_daemon_threadpool_executor

    executor = create_daemon_threadpool_executor(
        max_workers=2, thread_name_prefix="RegistryProbe"
    )
    try:
        futures = [executor.submit(lambda: threading.current_thread()) for _ in range(4)]
        workers = {f.result(timeout=10) for f in futures}
        assert workers, "no worker threads ran"
        assert all(t.daemon for t in workers), "workers must be daemon threads"
        registered = workers & set(_threads_queues)
        assert not registered, (
            f"workers registered for interpreter-exit join: {registered}"
        )
    finally:
        executor.shutdown(wait=True)


def test_shutdown_and_submit_contract():
    """The surface FastStack's callers rely on must keep working."""
    from concurrent.futures import Future

    from faststack.util.executors import create_daemon_threadpool_executor

    executor = create_daemon_threadpool_executor(max_workers=2, thread_name_prefix="X")
    try:
        future = executor.submit(lambda a, b=0: a + b, 2, b=3)
        assert isinstance(future, Future)
        assert future.result(timeout=10) == 5

        boom = executor.submit(lambda: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            boom.result(timeout=10)
    finally:
        executor.shutdown(wait=True)

    with pytest.raises(RuntimeError):
        executor.submit(lambda: None)

    # Idempotent shutdown.
    executor.shutdown(wait=True)

    with pytest.raises(ValueError):
        create_daemon_threadpool_executor(max_workers=0)


def test_shutdown_drains_queued_work_when_not_cancelling():
    from faststack.util.executors import create_daemon_threadpool_executor

    executor = create_daemon_threadpool_executor(max_workers=1, thread_name_prefix="D")
    results = []
    try:
        futures = [executor.submit(results.append, i) for i in range(5)]
        executor.shutdown(wait=True, cancel_futures=False)
        assert all(f.done() and not f.cancelled() for f in futures)
        assert sorted(results) == [0, 1, 2, 3, 4]
    finally:
        executor.shutdown(wait=False)
