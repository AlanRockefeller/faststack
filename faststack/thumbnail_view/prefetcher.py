"""Background thumbnail decode and prefetch for grid view."""

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from threading import Lock
from typing import Dict, Optional, Set, Tuple, Callable

import numpy as np
from PIL import Image

from faststack.imaging.orientation import get_exif_orientation, apply_orientation_to_np

log = logging.getLogger(__name__)

# Try to import turbojpeg for faster JPEG decoding
try:
    from turbojpeg import TurboJPEG, TJPF_RGB, TJSAMP_444

    _tj = TurboJPEG()
    HAS_TURBOJPEG = True
except ImportError:
    _tj = None
    HAS_TURBOJPEG = False
    log.debug("TurboJPEG not available, using PIL for thumbnail decoding")


def _compute_path_hash(path: Path) -> str:
    """Compute a stable hash of the path for cache key purposes."""
    return hashlib.md5(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


class ThumbnailPrefetcher:
    """Background thumbnail decoder with ThreadPoolExecutor.

    Features:
    - Non-blocking decode with callback on completion
    - De-duplication of in-flight jobs
    - EXIF orientation applied in exactly one place
    - Cache key: (size, path_hash, mtime_ns)
    """

    def __init__(
        self,
        cache: "ByteLRUCache",
        on_ready_callback: Optional[Callable[[str], None]] = None,
        max_workers: int = None,
        target_size: int = 200,
    ):
        """Initialize the prefetcher.

        Args:
            cache: Cache to store decoded thumbnails
            on_ready_callback: Called with thumbnail_id when decode completes
            max_workers: Number of worker threads (default: min(4, cpu_count//2))
            target_size: Target thumbnail size in pixels
        """
        if max_workers is None:
            max_workers = min(4, max(1, (os.cpu_count() or 4) // 2))

        self._cache = cache
        self._on_ready = on_ready_callback
        self._target_size = target_size
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="thumb"
        )

        # Track in-flight jobs to avoid duplicates
        # Key: (size, path_hash, mtime_ns)
        self._inflight: Set[Tuple[int, str, int]] = set()
        self._inflight_lock = Lock()

        # Track futures for potential cancellation
        self._futures: Dict[Tuple[int, str, int], Future] = {}

        log.info(
            "ThumbnailPrefetcher initialized with %d workers, target size %dpx",
            max_workers,
            target_size,
        )

    def submit(self, path: Path, mtime_ns: int, size: int = None) -> bool:
        """Submit a thumbnail decode job.

        Args:
            path: Path to the image file
            mtime_ns: File modification time in nanoseconds
            size: Target size (default: self._target_size)

        Returns:
            True if job was submitted, False if already in-flight or cached
        """
        if size is None:
            size = self._target_size

        path_hash = _compute_path_hash(path)
        job_key = (size, path_hash, mtime_ns)
        cache_key = f"{size}/{path_hash}/{mtime_ns}"

        # Check cache first
        if self._cache.get(cache_key) is not None:
            return False

        # Check/add to inflight set
        with self._inflight_lock:
            if job_key in self._inflight:
                return False
            self._inflight.add(job_key)

        # Submit decode job
        try:
            future = self._executor.submit(
                self._decode_worker,
                path,
                path_hash,
                mtime_ns,
                size,
            )
            future.add_done_callback(
                lambda f: self._on_decode_done(f, job_key, cache_key)
            )

            with self._inflight_lock:
                self._futures[job_key] = future

            return True
        except RuntimeError:
            # Executor shutdown
            with self._inflight_lock:
                self._inflight.discard(job_key)
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
    ) -> Optional[bytes]:
        """Worker function to decode a thumbnail.

        Returns JPEG bytes or None on error.
        """
        try:
            # Read and decode
            rgb_array = self._decode_image(path, size)
            if rgb_array is None:
                return None

            # Get EXIF orientation and apply (single point of orientation)
            orientation = get_exif_orientation(path)
            rgb_array = apply_orientation_to_np(rgb_array, orientation)

            # Encode to JPEG bytes for storage
            pil_image = Image.fromarray(rgb_array, mode="RGB")

            # Use BytesIO to encode to JPEG
            import io

            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        except Exception as e:
            log.debug("Failed to decode thumbnail for %s: %s", path, e)
            return None

    def _decode_image(self, path: Path, target_size: int) -> Optional[np.ndarray]:
        """Decode image to numpy array at target size.

        Uses TurboJPEG if available for faster decoding.
        Returns RGB uint8 array.
        """
        suffix = path.suffix.lower()

        # Try TurboJPEG for JPEG files
        if HAS_TURBOJPEG and suffix in (".jpg", ".jpeg"):
            try:
                with open(path, "rb") as f:
                    jpeg_data = f.read()

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
            with Image.open(path) as img:
                # Convert to RGB if needed
                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Resize
                img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                return np.array(img)

        except Exception as e:
            log.debug("PIL decode failed for %s: %s", path, e)
            return None

    def _on_decode_done(
        self, future: Future, job_key: Tuple[int, str, int], cache_key: str
    ):
        """Callback when decode completes."""
        # Remove from inflight
        with self._inflight_lock:
            self._inflight.discard(job_key)
            self._futures.pop(job_key, None)

        try:
            jpeg_bytes = future.result()
            if jpeg_bytes:
                # Store in cache
                self._cache.put(cache_key, jpeg_bytes)

                # Notify ready
                if self._on_ready:
                    # Extract thumbnail_id from cache_key (same format)
                    self._on_ready(cache_key)

        except Exception as e:
            log.debug("Thumbnail decode failed: %s", e)

    def cancel_all(self):
        """Cancel all pending jobs."""
        with self._inflight_lock:
            for future in self._futures.values():
                future.cancel()
            self._futures.clear()
            self._inflight.clear()

    def shutdown(self):
        """Shutdown the executor."""
        self.cancel_all()
        self._executor.shutdown(wait=False)
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
        self._cache: Dict[str, bytes] = {}
        self._order: list = []  # LRU order (oldest first)
        self._current_bytes = 0
        self._lock = Lock()

    def get(self, key: str) -> Optional[bytes]:
        """Get item from cache, returns None if not found."""
        with self._lock:
            if key not in self._cache:
                return None
            # Move to end (most recently used)
            self._order.remove(key)
            self._order.append(key)
            return self._cache[key]

    def put(self, key: str, value: bytes):
        """Put item in cache, evicting if necessary."""
        with self._lock:
            # If already present, remove old entry first
            if key in self._cache:
                old_value = self._cache[key]
                self._current_bytes -= len(old_value)
                self._order.remove(key)

            # Add new entry
            self._cache[key] = value
            self._order.append(key)
            self._current_bytes += len(value)

            # Evict if over limits
            while (
                self._current_bytes > self._max_bytes
                or len(self._cache) > self._max_items
            ) and self._order:
                oldest = self._order.pop(0)
                if oldest in self._cache:
                    self._current_bytes -= len(self._cache[oldest])
                    del self._cache[oldest]

    def clear(self):
        """Clear the cache."""
        with self._lock:
            self._cache.clear()
            self._order.clear()
            self._current_bytes = 0

    @property
    def size(self) -> int:
        """Current number of items."""
        return len(self._cache)

    @property
    def bytes_used(self) -> int:
        """Current bytes used."""
        return self._current_bytes
