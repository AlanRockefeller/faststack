"""Executor utilities for background task management."""

from __future__ import annotations

import heapq
import logging
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


import weakref
from concurrent.futures.thread import _threads_queues, _worker


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are daemon threads.

    Near-literal copy of CPython 3.12.2 ``_adjust_thread_count``
    (Lib/concurrent/futures/thread.py) with the sole change:
    ``t.daemon = True`` before ``t.start()``.

    No hasattr guard — if CPython internals change, this will raise
    AttributeError immediately rather than silently falling back to
    non-daemon threads.

    Thread-safety note: ``_adjust_thread_count`` is only called from
    ``submit()``, which already holds ``_global_shutdown_lock``, so the
    mutation of ``_threads_queues`` is safe without acquiring it again.
    """

    def _adjust_thread_count(self) -> None:
        # if idle threads are available, don't spin new threads
        if self._idle_semaphore.acquire(timeout=0):
            return

        # When the executor gets lost, the weakref callback will wake up
        # the worker threads.
        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
            )
            t.daemon = True
            t.start()
            self._threads.add(t)
            # Safe without explicit locking: submit() already holds
            # _global_shutdown_lock when calling _adjust_thread_count().
            _threads_queues[t] = self._work_queue


def create_daemon_threadpool_executor(
    max_workers: int, thread_name_prefix: str = ""
) -> DaemonThreadPoolExecutor:
    """
    Create a ThreadPoolExecutor whose worker threads are daemon threads.
    Returns a DaemonThreadPoolExecutor instance which is a subclass of ThreadPoolExecutor.
    """
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")

    return DaemonThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix=thread_name_prefix
    )


def create_priority_executor(
    max_workers: int, thread_name_prefix: str = "", maxsize: int = 0
) -> "PriorityExecutor":
    """
    Create a PriorityExecutor (daemon-threaded by default).

    Useful for thumbnail loading where visible items take precedence.
    """
    return PriorityExecutor(
        max_workers=max_workers,
        thread_name_prefix=thread_name_prefix,
        maxsize=maxsize,
    )


class PriorityExecutor:
    """A thread pool executor that uses a priority queue for task scheduling.

    Tasks are processed in order of:
      1) queue group (lower number = earlier scheduling class)
      2) priority (lower number = higher priority within the group)
      3) explicit queue order, or -seq by default (LIFO within ties)

    Workers are daemon threads.

    Concurrency admission: a worker waits for work to EXIST, then reserves a
    slot, then dequeues. Reserving before dequeuing means a temporarily reduced
    limit (see ``set_concurrency_limit``) throttles what starts without letting
    already-dequeued low-priority work occupy every worker while it blocks --
    queued items keep their full priority ordering and the next admission takes
    the queue head. Waiting for work first means an idle worker holds no
    reservation, so a limit lowered while the queue is empty applies to the very
    next submission instead of being pre-empted by stale reservations.
    ``_admitted`` therefore counts work admitted to execute, never idle polling.
    """

    def __init__(
        self, max_workers: int, thread_name_prefix: str = "", maxsize: int = 0
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")

        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._queue: queue.PriorityQueue[
            tuple[
                int,
                int,
                int,
                int,
                Callable[..., Any],
                tuple[Any, ...],
                dict[str, Any],
                Future,
            ]
        ] = queue.PriorityQueue(maxsize=maxsize)
        self._workers: list[threading.Thread] = []
        self._stop_event = threading.Event()

        # Monotonic counter for stable LIFO ordering within same priority.
        self._count = 0
        self._count_lock = threading.Lock()

        # Admission state. Guarded by its own condition so no caller ever holds
        # it together with _count_lock or the queue mutex.
        self._admission = threading.Condition(threading.Lock())
        self._concurrency_limit = max_workers
        self._admitted = 0
        self._running = 0

        # O(1) queued counters per queue group. Preload All can enqueue
        # thousands of items, so diagnostics must never scan the heap.
        # _stats_lock is a leaf: nothing else is acquired while it is held.
        self._stats_lock = threading.Lock()
        self._queued_by_group: dict[int, int] = {}

        for i in range(max_workers):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"{thread_name_prefix}_{i}",
                daemon=True,
            )
            self._workers.append(t)
        for t in self._workers:
            t.start()

    # ---- Admission control -------------------------------------------------

    def set_concurrency_limit(self, limit: Optional[int]) -> int:
        """Cap how many queued tasks may run at once; None restores the pool size.

        Returns the effective (clamped) limit. Raising the limit wakes every
        waiting worker so queued work resumes immediately.
        """
        effective = (
            self._max_workers
            if limit is None
            else max(0, min(int(limit), self._max_workers))
        )
        with self._admission:
            previous = self._concurrency_limit
            self._concurrency_limit = effective
            if effective > previous:
                self._admission.notify_all()
        return effective

    @property
    def concurrency_limit(self) -> int:
        with self._admission:
            return self._concurrency_limit

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def _acquire_admission(self, *, blocking: bool) -> bool:
        """Reserve one running slot. Shutdown is never gated by the limit."""
        with self._admission:
            while True:
                if self._stop_event.is_set():
                    # Draining/exiting must not depend on the governor.
                    self._admitted += 1
                    return True
                if self._admitted < self._concurrency_limit:
                    self._admitted += 1
                    return True
                if not blocking:
                    return False
                # Bounded wait: notifications drive the fast path, the timeout
                # only guarantees a missed wake-up can never wedge a worker.
                self._admission.wait(0.25)

    def _release_admission(self, *, was_running: bool) -> None:
        with self._admission:
            self._admitted = max(0, self._admitted - 1)
            if was_running:
                self._running = max(0, self._running - 1)
            self._admission.notify()

    def _note_running(self) -> None:
        with self._admission:
            self._running += 1

    # No suspend/resume admission API on purpose: a task must never block a
    # worker on another task of this executor. Releasing the admission slot
    # would not release the physical thread, so enough waiters can occupy every
    # worker while the dependencies they wait on stay queued. Chain dependent
    # work off the dependency's completion callback instead (see
    # Prefetcher._submit_after_future).

    def concurrency_snapshot(self) -> dict[str, Any]:
        """Cheap diagnostic view of admission and queue state."""
        with self._admission:
            limit = self._concurrency_limit
            admitted = self._admitted
            running = self._running
        with self._stats_lock:
            queued = {group: count for group, count in self._queued_by_group.items()}
        return {
            "max_workers": self._max_workers,
            "limit": limit,
            "admitted": admitted,
            "running": running,
            "queued_by_group": queued,
            "queued_total": sum(queued.values()),
        }

    def queued_in_group(self, queue_group: int) -> int:
        """Number of queued (not-yet-started) tasks in one scheduling class."""
        with self._stats_lock:
            return self._queued_by_group.get(int(queue_group), 0)

    def _note_queued(self, queue_group: int, delta: int) -> None:
        with self._stats_lock:
            remaining = self._queued_by_group.get(queue_group, 0) + delta
            if remaining > 0:
                self._queued_by_group[queue_group] = remaining
            else:
                self._queued_by_group.pop(queue_group, None)

    def _wait_for_work(self, timeout: float) -> bool:
        """Block until the queue looks non-empty. Never removes an item.

        Idle workers must not hold admission slots: a slot reserved while the
        queue was empty would survive a later limit reduction and let every
        idle worker start new work at once. So availability is observed first,
        and only then is a slot reserved.

        Relies on the same CPython queue internals as bump_priority().
        """
        with self._queue.not_empty:
            if self._queue.queue:
                return True
            self._queue.not_empty.wait(timeout)
            return bool(self._queue.queue)

    def _worker_loop(self) -> None:
        # Drain behavior:
        # - If stop_event is set, workers will exit only after the queue becomes empty,
        #   unless queued items are explicitly cancelled via shutdown(cancel_futures=True).
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                break

            if not self._wait_for_work(0.1):
                # Nothing to run: stay unadmitted so a constraint entered while
                # idle takes effect immediately for the next submission.
                continue

            if not self._acquire_admission(blocking=False):
                # Constrained: re-check the stop/drain condition and wait again.
                continue

            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                # Another admitted worker took the item first.
                self._release_admission(was_running=False)
                continue
            except BaseException:
                self._release_admission(was_running=False)
                raise

            self._note_running()
            queue_group, _priority, _order, _neg_seq, fn, args, kwargs, fut = item
            self._note_queued(queue_group, -1)
            try:
                if fut.set_running_or_notify_cancel():
                    try:
                        fut.set_result(fn(*args, **kwargs))
                    except BaseException as e:
                        fut.set_exception(e)
            except BaseException as e:
                # If we blow up here, make sure the future doesn't get stranded.
                try:
                    fut.set_exception(e)
                except Exception:
                    pass
                log.error("Error in PriorityExecutor worker: %s", e)
            finally:
                self._release_admission(was_running=True)
                # Always mark item done so join/drain semantics work.
                try:
                    self._queue.task_done()
                except Exception:
                    pass

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        priority: int = 1,
        queue_group: int = 0,
        queue_order: int | None = None,
        **kwargs: Any,
    ) -> Future:
        """Submit a task to the priority queue.

        Args:
            fn: Function to execute
            priority: Lower number means higher priority within a queue group.
            queue_group: Lower groups run first. The default preserves existing
                scheduling; background bulk work may opt into a later group.
            queue_order: Explicit lower-first ordering within a group/priority.
                When omitted, the existing newest-first (LIFO) ordering applies.
            *args, **kwargs: Passed to fn

        Returns:
            Future object for the task.
        """
        if self._stop_event.is_set():
            raise RuntimeError("Executor shutdown")

        fut: Future = Future()

        with self._count_lock:
            self._count += 1
            seq = self._count

        order = -seq if queue_order is None else int(queue_order)
        group = int(queue_group)
        # Counted before the put: a worker may dequeue the item before this
        # call returns, and its decrement must never run against a zero count.
        self._note_queued(group, 1)
        try:
            self._queue.put(
                (
                    group,
                    priority,
                    order,
                    -seq,
                    fn,
                    args,
                    kwargs,
                    fut,
                ),
                block=False,
            )
        except queue.Full:
            self._note_queued(group, -1)
            fut.set_exception(RuntimeError("PriorityQueue full"))
        return fut

    def qsize(self) -> int:
        """Approximate number of queued (not-yet-started) tasks. Diagnostics only."""
        return self._queue.qsize()

    def bump_priority(
        self,
        future: Future,
        priority: int,
        *,
        queue_group: int = 0,
    ) -> bool:
        """Raise the priority of a queued task if it has not started yet.

        Returns True when the task was still queued and its queue ordering was
        updated. Running, done, cancelled, or already-higher-priority tasks are
        left untouched.
        """
        with self._count_lock:
            self._count += 1
            neg_seq = -self._count

        # Relies on CPython queue.Queue internals (.mutex and .queue heap);
        # recheck this on major Python upgrades.
        moved_from: Optional[int] = None
        with self._queue.mutex:
            for idx, item in enumerate(self._queue.queue):
                (
                    current_group,
                    current_priority,
                    _old_order,
                    _old_neg_seq,
                    fn,
                    args,
                    kwargs,
                    fut,
                ) = item
                if fut is not future:
                    continue
                if fut.cancelled() or fut.running() or fut.done():
                    return False
                if (queue_group, priority) >= (current_group, current_priority):
                    return False
                self._queue.queue[idx] = (
                    queue_group,
                    priority,
                    neg_seq,
                    neg_seq,
                    fn,
                    args,
                    kwargs,
                    fut,
                )
                heapq.heapify(self._queue.queue)
                moved_from = current_group
                if moved_from != queue_group:
                    # Adjusted under the queue mutex so the item cannot be
                    # dequeued (and decremented) between the two updates.
                    self._note_queued(moved_from, -1)
                    self._note_queued(queue_group, 1)
                break
        return moved_from is not None

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Shutdown the executor.

        If cancel_futures is True, queued (not-yet-started) tasks are cancelled immediately.
        If cancel_futures is False, workers will drain the queue before exiting.
        """
        self._stop_event.set()
        # Workers parked on the admission governor -- or waiting for work to
        # appear -- must exit, not wait either condition out.
        with self._admission:
            self._admission.notify_all()
        with self._queue.not_empty:
            self._queue.not_empty.notify_all()

        if cancel_futures:
            # Cancel queued work so workers can exit once queue empties.
            while True:
                try:
                    (
                        queue_group,
                        _priority,
                        _order,
                        _neg_seq,
                        _fn,
                        _args,
                        _kwargs,
                        fut,
                    ) = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._note_queued(queue_group, -1)
                try:
                    fut.cancel()
                finally:
                    try:
                        self._queue.task_done()
                    except Exception:
                        pass

        if wait:
            for t in self._workers:
                try:
                    t.join(timeout=1.0)
                except Exception:
                    pass
