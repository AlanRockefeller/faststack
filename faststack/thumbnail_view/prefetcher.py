"""Background thumbnail decode and prefetch for grid view."""

import logging
import os
import time
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
from threading import Lock
import threading
from typing import Dict, Optional, Set, Tuple, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from faststack.imaging.cache import ByteLRUCache

import numpy as np
import io
from PIL import Image
from contextlib import nullcontext

from faststack.imaging.turbo import TJPF_RGB, create_turbojpeg
from faststack.util.executors import create_priority_executor
from faststack.imaging.orientation import get_exif_orientation, apply_orientation_to_np
from faststack.io.utils import compute_path_hash
import faststack.util.thumb_debug as thumb_debug

log = logging.getLogger(__name__)

# Optional Qt dispatch so callbacks always run on Qt thread when available
try:
    from PySide6.QtCore import QObject, Signal, Qt, QCoreApplication

    class _ReadyEmitter(QObject):
        ready = Signal(str)

    _HAS_QT = True
except Exception:
    _ReadyEmitter = None
    _HAS_QT = False
    QCoreApplication = None

# Try to initialize turbojpeg with shared discovery logic.
_tj, HAS_TURBOJPEG = create_turbojpeg()
if not HAS_TURBOJPEG:
    log.debug("TurboJPEG unavailable, using PIL for thumbnail decoding")


class ThumbnailPrefetcher:
    """Background thumbnail decoder with ThreadPoolExecutor.

    Features:
    - Non-blocking decode with callback on completion
    - De-duplication of in-flight jobs
    - EXIF orientation applied in exactly one place
    - Cache key: (size, path_hash, mtime_ns)
    """

    # Priority levels
    PRIO_HIGH = 0  # Visible items
    PRIO_MED = 1  # Prefetch items

    def __init__(
        self,
        cache: "ByteLRUCache",
        on_ready_callback: Optional[Callable[[str], None]] = None,
        max_workers: int = None,
        target_size: int = 200,
        debug_timing: bool = False,
        debug_trace: bool = False,
    ):
        """Initialize the prefetcher.

        Args:
            cache: Cache to store decoded thumbnails
            on_ready_callback: Called with thumbnail_id when decode completes
            max_workers: Number of worker threads (default: min(4, cpu_count//2))
            target_size: Target thumbnail size in pixels
            debug_timing: Enable [THUMB-TIMING] log lines
            debug_trace: Enable verbose trace logs
        """
        if max_workers is None:
            max_workers = min(4, max(1, (os.cpu_count() or 4) // 2))

        self._cache = cache
        self._on_ready = on_ready_callback
        self._target_size = target_size
        self._stop_event = threading.Event()
        self._executor = create_priority_executor(
            max_workers=max_workers, thread_name_prefix="thumb"
        )

        # Track in-flight jobs to avoid duplicates
        # Key: (size, path_hash, mtime_ns)
        # Value: (rid, ThumbTimer)
        self._inflight: Dict[
            Tuple[int, str, int], Tuple[int, Optional["thumb_debug.ThumbTimer"]]
        ] = {}
        self._inflight_lock = Lock()
        self._debug_trace = debug_trace

        # Track futures for potential cancellation
        self._futures: Dict[Tuple[int, str, int], Future] = {}

        # If Qt is available AND a QApplication exists, forward ready notifications
        # to Qt/main thread. This prevents Qt warnings/crashes from worker-thread callbacks.
        self._ready_emitter = None
        if _HAS_QT and self._on_ready:
            try:
                if QCoreApplication.instance() is not None:
                    self._ready_emitter = (
                        _ReadyEmitter()
                    )  # created on constructing thread (should be Qt thread)
                    self._ready_emitter.ready.connect(
                        self._on_ready, Qt.QueuedConnection
                    )
            except Exception:
                self._ready_emitter = None

        # Timing stats
        self._debug_timing = debug_timing
        self._submit_count = 0

        log.info(
            "ThumbnailPrefetcher initialized with %d workers, target size %dpx",
            max_workers,
            target_size,
        )

    def submit(
        self,
        path: Path,
        mtime_ns: int,
        size: Optional[int] = None,
        priority: int = PRIO_MED,
        timer: Optional["thumb_debug.ThumbTimer"] = None,
    ) -> bool:
        """Submit a thumbnail decode job.

        Args:
            path: Path to the image file
            mtime_ns: File modification time in nanoseconds
            size: Target size (default: self._target_size)
            priority: Job priority (PRIO_HIGH, PRIO_MED)
            timer: Pre-existing timer from provider (optional)

        Returns:
            True if job was submitted, False if already in-flight or cached
        """
        # Don't accept new work once shutdown begins
        if self._stop_event.is_set():
            return False

        if size is None:
            size = self._target_size

        path_hash = compute_path_hash(path)
        job_key = (size, path_hash, mtime_ns)
        cache_key = f"{size}/{path_hash}/{mtime_ns}"

        # Check cache first
        if self._cache.get(cache_key) is not None:
            return False

        if timer is None:
            timer = thumb_debug.ThumbTimer(key=cache_key, path=path, reason="prefetch")

        # Check/add to inflight set
        with self._inflight_lock:
            # If already in flight, check if we want to upgrade priority
            if job_key in self._inflight:
                existing_rid, existing_timer = self._inflight[job_key]
                if existing_timer:
                    # Capture where this request originated
                    if timer:
                        existing_timer.coalesced_from = timer.reason
                    # Update effective priority if this requested one is higher
                    if (
                        existing_timer.prio_effective is not None
                        and priority < existing_timer.prio_effective
                    ):
                        existing_timer.prio_effective = priority
                        if timer:
                            thumb_debug.log_trace(
                                "prio_bump",
                                rid=existing_timer.rid,
                                new_prio=priority,
                                triggered_by_rid=timer.rid,
                            )

                if timer:
                    thumb_debug.inc("decode_coalesced")
                    thumb_debug.log_trace(
                        "coalesced", rid=timer.rid, existing_rid=existing_rid
                    )
                return False

            if timer:
                timer.t_queued = time.perf_counter()
                timer.prio_submitted = priority
                timer.prio_effective = priority
                thumb_debug.inc("decode_submitted")
                thumb_debug.log_trace(
                    "queued",
                    rid=timer.rid,
                    prio=priority,
                    qdepth=len(self._inflight) + 1,
                )

            self._inflight[job_key] = (timer.rid if timer else 0, timer)
            thumb_debug.gauge("inflight", len(self._inflight))

        # Submit decode job
        try:
            self._submit_count += 1

            future = self._executor.submit(
                self._decode_worker,
                path,
                path_hash,
                mtime_ns,
                size,
                timer,
                priority=priority,
            )

            with self._inflight_lock:
                self._futures[job_key] = future

            # Add callback *after* registering future. If already done, add_done_callback
            # may invoke immediately in this thread, so we want state initialized first.
            future.add_done_callback(
                lambda f: self._on_decode_done(f, job_key, cache_key, timer)
            )

            return True
        except RuntimeError:
            # Executor shutdown
            with self._inflight_lock:
                self._inflight.pop(job_key, None)
                thumb_debug.gauge("inflight", len(self._inflight))
            return False

    def prefetch_batch(self, entries: list, margin: int = 2):
        """Prefetch thumbnails for a batch of entries.

        Args:
            entries: List of ThumbnailEntry objects
            margin: Extra entries to prefetch beyond visible range
        """
        for entry in entries:
            if not entry.is_folder:
                self.submit(entry.path, entry.mtime_ns)

    def _decode_worker(
        self,
        path: Path,
        path_hash: str,
        mtime_ns: int,
        size: int,
        timer: Optional["thumb_debug.ThumbTimer"] = None,
    ) -> Optional[bytes]:
        """Worker function to decode a thumbnail.

        Returns JPEG bytes or None on error.
        """
        if timer:
            timer.t_worker_start = time.perf_counter()
            timer.started = True
            thumb_debug.inc("decode_started")
            thumb_debug.log_trace(
                "worker_start",
                rid=timer.rid,
                prio=getattr(timer, "prio_effective", None),
            )

        try:
            # Read and decode
            rgb_array = self._decode_image(path, size, timer=timer)

            if rgb_array is None:
                return None

            # Check cancellation early if possible
            if (timer and timer.cancelled) or self._stop_event.is_set():
                if timer:
                    thumb_debug.log_trace(
                        "cancel_honored", rid=timer.rid, stage="after_decode"
                    )
                return None

            # Get EXIF orientation and apply (single point of orientation)
            with timer.stage("orientation") if timer else nullcontext():
                orientation = get_exif_orientation(path)
                rgb_array = apply_orientation_to_np(rgb_array, orientation)

            # Check cancellation again
            if (timer and timer.cancelled) or self._stop_event.is_set():
                if timer:
                    thumb_debug.log_trace(
                        "cancel_honored", rid=timer.rid, stage="after_orientation"
                    )
                return None

            # Encode to JPEG bytes for storage
            with timer.stage("encode") if timer else nullcontext():
                pil_image = Image.fromarray(rgb_array, mode="RGB")
                buf = io.BytesIO()
                pil_image.save(buf, format="JPEG", quality=85)
                result = buf.getvalue()

            if timer and timer.cancelled:
                thumb_debug.log_trace("cancel_too_late", rid=timer.rid)

            return result

        except Exception as e:
            if timer:
                thumb_debug.log_trace("worker_error", rid=timer.rid, error=str(e))
            log.debug("Failed to decode thumbnail for %s: %s", path, e)
            return None

    def _decode_image(
        self,
        path: Path,
        target_size: int,
        timer: Optional["thumb_debug.ThumbTimer"] = None,
    ) -> Optional[np.ndarray]:
        """Decode image to numpy array at target size.

        Uses TurboJPEG if available for faster decoding.
        Returns RGB uint8 array.
        """
        suffix = path.suffix.lower()

        # Try TurboJPEG for JPEG files
        if HAS_TURBOJPEG and suffix in (".jpg", ".jpeg"):
            try:
                with timer.stage("io") if timer else nullcontext():
                    with open(path, "rb") as f:
                        jpeg_data = f.read()

                with timer.stage("decode") if timer else nullcontext():
                    # Get dimensions first
                    width, height, _, _ = _tj.decode_header(jpeg_data)

                    # Calculate scale factor for turbojpeg (powers of 2: 1, 2, 4, 8)
                    scale_factor = 1
                    while (
                        width // (scale_factor * 2) >= target_size
                        and height // (scale_factor * 2) >= target_size
                        and scale_factor < 8
                    ):
                        scale_factor *= 2

                    # Decode with scaling
                    scaling_factor = (1, scale_factor)
                    rgb = _tj.decode(
                        jpeg_data, pixel_format=TJPF_RGB, scaling_factor=scaling_factor
                    )

                # Further resize with PIL if needed
                h, w = rgb.shape[:2]
                if w > target_size or h > target_size:
                    with timer.stage("resize") if timer else nullcontext():
                        pil_img = Image.fromarray(rgb)
                        pil_img.thumbnail(
                            (target_size, target_size), Image.Resampling.LANCZOS
                        )
                        rgb = np.array(pil_img)

                return rgb

            except Exception as e:
                log.debug(
                    "TurboJPEG decode failed for %s, falling back to PIL: %s", path, e
                )

        # Fallback to PIL
        try:
            with timer.stage("decode") if timer else nullcontext():
                with Image.open(path) as pil_img:
                    # Convert to RGB if needed
                    if pil_img.mode != "RGB":
                        pil_img = pil_img.convert("RGB")

                    # Resize
                    pil_img.thumbnail(
                        (target_size, target_size), Image.Resampling.LANCZOS
                    )
                    return np.array(pil_img.copy())

        except Exception as e:
            log.debug("PIL decode failed for %s: %s", path, e)
            return None

    def _on_decode_done(
        self,
        future: Future,
        job_key: Tuple[int, str, int],
        cache_key: str,
        timer: Optional["thumb_debug.ThumbTimer"],
    ):
        """Callback when decode completes."""
        # Always remove bookkeeping first to avoid stranding entries
        with self._inflight_lock:
            self._inflight.pop(job_key, None)
            thumb_debug.gauge("inflight", len(self._inflight))
            if self._futures.get(job_key) is future:
                del self._futures[job_key]

        if timer:
            timer.t_done = time.perf_counter()

        # Then bail if shutting down
        if self._stop_event.is_set():
            if timer:
                thumb_debug.inc("decode_cancelled")
                thumb_debug.log_trace("cancelled", rid=timer.rid, factor="shutdown")
            return

        try:
            # If cancelled, don't call result()
            if future.cancelled() or (timer and timer.cancelled):
                if timer:
                    thumb_debug.inc("decode_cancelled")
                    event = (
                        "cancelled_midflight"
                        if timer.started
                        else "cancelled_before_start"
                    )
                    thumb_debug.log_trace(event, rid=timer.rid)
                return

            jpeg_bytes = future.result()
            if jpeg_bytes:
                # Store in cache
                self._cache.put(cache_key, jpeg_bytes)

                if timer:
                    thumb_debug.inc("decode_done_ok")
                    thumb_debug.log_trace("completed", rid=timer.rid)
                    timer.log_timing(cache="miss")

                # Notify ready
                if self._on_ready:
                    # If Qt emitter exists, this will run callback on Qt thread.
                    if self._ready_emitter is not None:
                        self._ready_emitter.ready.emit(cache_key)
                    else:
                        self._on_ready(cache_key)

        except Exception as e:
            log.debug("Thumbnail decode failed: %s", e)

    def cancel_all(self):
        """Cancel all pending jobs."""
        # Snapshot under lock, cancel outside lock to avoid deadlock:
        # Future.cancel() can synchronously run callbacks.
        with self._inflight_lock:
            futures = list(self._futures.values())
            inflight_timers = [t for _, t in self._inflight.values()]
            self._futures.clear()
            self._inflight.clear()
            thumb_debug.gauge("qdepth", 0)
            thumb_debug.gauge("inflight", 0)

        for timer in inflight_timers:
            if timer is not None:
                timer.cancelled = True
                thumb_debug.log_trace("cancel_requested", rid=timer.rid)

        for f in futures:
            try:
                f.cancel()
            except Exception:
                pass

    def shutdown(self):
        """Shutdown the executor."""
        self._stop_event.set()
        self.cancel_all()
        # cancel_futures=True cancels queued tasks immediately (Py3.9+)
        self._executor.shutdown(wait=False, cancel_futures=True)
        log.info("ThumbnailPrefetcher shutdown")


class ThumbnailCache:
    """Simple byte-based LRU cache for thumbnails with dual capacity limit.

    Limits:
    - max_bytes: Maximum total bytes
    - max_items: Maximum number of items
    """

    def __init__(self, max_bytes: int = 256 * 1024 * 1024, max_items: int = 5000):
        self._max_bytes = max_bytes
        self._max_items = max_items
        self._cache: Dict[str, bytes] = OrderedDict()
        self._current_bytes = 0
        self._lock = Lock()

    def get(self, key: str) -> Optional[bytes]:
        """Get item from cache, returns None if not found."""
        with self._lock:
            if key not in self._cache:
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(key, last=True)
            return self._cache[key]

    def put(self, key: str, value: bytes):
        """Put item in cache, evicting if necessary."""
        with self._lock:
            # If already present, remove old entry logic by just updating (OrderedDict handles key existence)
            # But we must update _current_bytes first if it exists
            if key in self._cache:
                self._current_bytes -= len(self._cache[key])
                self._cache.move_to_end(key, last=True)

            self._cache[key] = value
            self._current_bytes += len(value)

            # Evict if over limits
            while (
                self._current_bytes > self._max_bytes
                or len(self._cache) > self._max_items
            ):
                # Pop oldest (first item)
                _, oldest_val = self._cache.popitem(last=False)
                self._current_bytes -= len(oldest_val)

    def discard(self, key: str) -> bool:
        """Remove a single entry if present. No-op if missing.

        Returns True if the key was present and removed, False otherwise.
        """
        with self._lock:
            try:
                val = self._cache.pop(key)
            except KeyError:
                return False
            self._current_bytes -= len(val)
            return True

    def clear(self):
        """Clear the cache."""
        with self._lock:
            self._cache.clear()
            self._current_bytes = 0

    @property
    def size(self) -> int:
        """Current number of items."""
        return len(self._cache)

    @property
    def bytes_used(self) -> int:
        """Current bytes used."""
        return self._current_bytes
