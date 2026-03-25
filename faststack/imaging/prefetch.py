"""Handles prefetching and decoding of adjacent images in a background thread pool."""

import logging
import os
import io
import hashlib
import mmap
from pathlib import Path
from concurrent.futures import Future
from typing import List, Dict, Optional, Callable
import threading
import time


import numpy as np
from PIL import Image as PILImage, ImageCms

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage
except ImportError:
    QTimer = None
    QImage = None

from faststack.models import ImageFile, DecodedImage
from faststack.imaging.jpeg import decode_jpeg_rgb, decode_jpeg_resized
from faststack.imaging.cache import build_cache_key
from faststack.imaging.orientation import apply_orientation_to_np
from faststack.config import config
from faststack.util.executors import create_daemon_threadpool_executor

log = logging.getLogger(__name__)

# RAW extensions that Pillow typically cannot decode (no embedded JPEG preview).
# When decode fails for these, we generate a placeholder instead of returning None.
_RAW_EXTENSIONS = frozenset(
    {".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng"}
)


def _make_raw_placeholder(width: int, height: int) -> np.ndarray:
    """Generate a themed 'Preview unavailable' placeholder for undecodable RAW files.

    Draws a circle-with-slash icon and centered text so the placeholder is
    visually distinct from actual image content.  Theme-aware via config.
    """
    if width <= 0 or height <= 0:
        width, height = 256, 256

    # Theme-aware palette
    theme = config.get("core", "theme", fallback="dark")
    if theme == "dark":
        bg_color = (30, 30, 30)
        text_color = (120, 120, 120)
        icon_color = (80, 80, 80)
    else:
        bg_color = (240, 240, 240)
        text_color = (140, 140, 140)
        icon_color = (180, 180, 180)

    from PIL import ImageDraw, ImageFont

    img = PILImage.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    cx, cy = width // 2, height // 2
    short = min(width, height)
    icon_r = max(10, short // 8)

    # Circle-with-slash "no preview" icon
    if icon_r >= 10:
        stroke = max(2, icon_r // 10)
        icon_cy = cy - icon_r  # icon above center
        draw.ellipse(
            [cx - icon_r, icon_cy - icon_r, cx + icon_r, icon_cy + icon_r],
            outline=icon_color,
            width=stroke,
        )
        draw.line(
            [cx - icon_r, icon_cy + icon_r, cx + icon_r, icon_cy - icon_r],
            fill=icon_color,
            width=stroke,
        )

    # "Preview unavailable" text below icon
    text = "Preview unavailable"
    font_size = max(12, short // 20)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        # Older Pillow without size= parameter
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    text_y = cy + (icon_r // 2 if icon_r >= 10 else 0)
    draw.text(
        ((width - tw) // 2, text_y),
        text,
        fill=text_color,
        font=font,
    )

    return np.array(img)

# ---- Option C: ICC Color Management Setup ----
SRGB_PROFILE = ImageCms.createProfile("sRGB")

# Cache for monitor ICC profile to avoid reloading on every decode
_monitor_profile_cache: Dict[str, Optional[ImageCms.ImageCmsProfile]] = {}
_monitor_profile_warning_logged = False

# Cache for ICC transforms to avoid rebuilding on every image
_icc_transform_cache: Dict[tuple, ImageCms.ImageCmsTransform] = {}

# Thread lock for all ICC caches
_icc_cache_lock = threading.Lock()


def get_icc_transform(
    src_profile: ImageCms.ImageCmsProfile,
    monitor_profile: ImageCms.ImageCmsProfile,
    src_profile_key: str,
    monitor_profile_path: str,
) -> ImageCms.ImageCmsTransform:
    """Get or create a cached ICC transform.

    Building transforms is expensive, so we cache them by stable keys:
    - src_profile_key: SHA-256 digest of the embedded ICC bytes
    - monitor_profile_path: file path to the monitor ICC profile
    """
    key = (src_profile_key, monitor_profile_path)
    with _icc_cache_lock:
        if key not in _icc_transform_cache:
            _icc_transform_cache[key] = ImageCms.buildTransform(
                src_profile, monitor_profile, "RGB", "RGB"
            )
            log.debug(
                "Built new ICC transform for profile pair (src=%s, monitor=%s)",
                src_profile_key[:16],
                monitor_profile_path,
            )
        return _icc_transform_cache[key]


def clear_icc_caches():
    """Clear all ICC-related caches (profiles and transforms)."""
    global _monitor_profile_cache, _icc_transform_cache, _monitor_profile_warning_logged
    with _icc_cache_lock:
        _monitor_profile_cache.clear()
        _icc_transform_cache.clear()
        _monitor_profile_warning_logged = False
        log.info("Cleared ICC profile and transform caches")


def get_monitor_profile() -> Optional[ImageCms.ImageCmsProfile]:
    """Dynamically load monitor ICC profile based on current config.

    Caches the profile by path to reduce overhead and log spam.
    """
    global _monitor_profile_warning_logged

    monitor_icc_path = config.get("color", "monitor_icc_path", fallback="").strip()

    with _icc_cache_lock:
        # Check cache first
        if monitor_icc_path in _monitor_profile_cache:
            return _monitor_profile_cache[monitor_icc_path]

        # Handle empty path case
        if not monitor_icc_path:
            if not _monitor_profile_warning_logged:
                log.warning("ICC mode enabled but no monitor_icc_path configured")
                _monitor_profile_warning_logged = True
            _monitor_profile_cache[monitor_icc_path] = None
            return None

        # Load and cache the profile
        try:
            profile = ImageCms.ImageCmsProfile(monitor_icc_path)
            log.debug("Loaded monitor ICC profile: %s", monitor_icc_path)
            _monitor_profile_cache[monitor_icc_path] = profile
        except (OSError, ImageCms.PyCMSError) as e:
            log.warning(
                "Failed to load monitor ICC profile from %s: %s", monitor_icc_path, e
            )
            _monitor_profile_cache[monitor_icc_path] = None

        return _monitor_profile_cache[monitor_icc_path]


# apply_orientation_to_np and apply_exif_orientation imported from orientation.py


def apply_saturation_compensation(
    arr: np.ndarray,
    width: int,
    height: int,
    bytes_per_line: int,
    factor: float,
):
    """
    In-place saturation scale in RGB space (Option A).

    arr: 1D uint8 array of length height * bytes_per_line
    width, height, bytes_per_line: dimensions of the image stored in arr
    factor: 0.0-1.0 range, where 1.0 = no change, <1.0 = less saturated

    Note: While the algorithm supports values >1.0 for increased saturation,
    the UI constrains the factor to [0.0, 1.0] for saturation reduction only.
    """
    if factor == 1.0:
        return

    # Treat the buffer as [height, bytes_per_line]
    assert arr.size == height * bytes_per_line, (
        f"Unexpected buffer size for saturation compensation: "
        f"{arr.size} != {height} * {bytes_per_line}"
    )
    buf2d = arr.reshape((height, bytes_per_line))

    # Only the first width*3 bytes per row are actual RGB pixels
    rgb_region = buf2d[:, : width * 3]

    # Interpret as H x W x 3
    rgb = rgb_region.reshape((height, width, 3)).astype(np.float32)

    # Simple saturation scaling: move each channel toward its per-pixel average
    gray = rgb.mean(axis=2, keepdims=True)
    rgb = gray + factor * (rgb - gray)

    np.clip(rgb, 0, 255, out=rgb)

    # Write back into the same memory
    rgb_region[:] = rgb.reshape(height, width * 3).astype(np.uint8)


class Prefetcher:
    def __init__(
        self,
        image_files: List[ImageFile],
        cache_put: Callable,
        prefetch_radius: int,
        get_display_info: Callable,
        debug: bool = False,
    ):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.get_display_info = get_display_info
        self.debug = debug
        # Use CPU count for I/O-bound JPEG decoding
        # Rule of thumb: 2x CPU cores for I/O bound, 1x for CPU bound
        optimal_workers = min(
            (os.cpu_count() or 1) * 2, 8
        )  # Cap at 8 for fast navigation

        self.executor = create_daemon_threadpool_executor(
            max_workers=optimal_workers,
            thread_name_prefix="Prefetcher",
        )
        self._futures_lock = threading.RLock()
        self.futures: Dict[int, Future] = {}
        self.future_paths: Dict[int, Path] = {}
        self.generation = 0
        self._scheduled: Dict[int, set] = {}  # generation -> set of scheduled indices

        # Cooperative cancellation flag for shutdown
        self._stop_event = threading.Event()

        # Adaptive prefetch: start with smaller radius, expand after user navigates
        self._initial_radius = 4  # Increased for faster initial responsiveness
        self._navigation_count = 0  # Track how many times user has navigated
        self._radius_expanded = False

        # Directional prefetching
        self._last_navigation_direction: int = 1  # 1 = forward, -1 = backward
        self._direction_bias: float = 0.85  # 85% of radius in travel direction

    def set_image_files(self, image_files: List[ImageFile]):
        with self._futures_lock:
            if self.image_files != image_files:
                self.image_files = image_files
                self._cancel_all_locked()

    def update_prefetch(
        self,
        current_index: int,
        is_navigation: bool = False,
        direction: Optional[int] = None,
    ):
        """Updates the prefetching queue based on the current image index.

        Args:
            current_index: The index to prefetch around
            is_navigation: True if this is from user navigation (arrow keys, etc.)
            direction: 1 for forward, -1 for backward, None to use last direction
        """
        if self.debug:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} update_prefetch: START index={current_index} dir={direction}"
            )

        # NOTE: Generation is NOT incremented here. It only changes when display size,
        # zoom state, or color mode changes - events that actually invalidate cached images.
        # Navigation just shifts which indices to prefetch.

        # Track navigation direction
        if direction is not None:
            self._last_navigation_direction = direction

        # Track navigation to expand radius after user starts moving
        if is_navigation:
            self._navigation_count += 1
            if not self._radius_expanded and self._navigation_count >= 2:
                self._radius_expanded = True
                log.info(
                    "Expanding prefetch radius from %d to %d after user navigation",
                    self._initial_radius,
                    self.prefetch_radius,
                )

        # Use smaller radius initially to reduce cache thrash before display size is stable
        effective_radius = (
            self._initial_radius if not self._radius_expanded else self.prefetch_radius
        )

        if self.debug:
            log.info(
                "Prefetch radius: initial=%d, configured=%d, effective=%d",
                self._initial_radius,
                self.prefetch_radius,
                effective_radius,
            )

        # Calculate asymmetric range based on direction, ensuring at least
        # 1 slot per side even when effective_radius is very small.
        if self._last_navigation_direction > 0:  # Moving forward
            behind = max(1, int(effective_radius * (1 - self._direction_bias)))
            ahead = effective_radius - behind
            if ahead < 1:
                ahead = 1
                behind = effective_radius - ahead
        else:  # Moving backward
            ahead = max(1, int(effective_radius * (1 - self._direction_bias)))
            behind = effective_radius - ahead
            if behind < 1:
                behind = 1
                ahead = effective_radius - behind

        # Invariant: All reads/writes of self.futures, self._scheduled, self.generation,
        # and self.image_files that participate in scheduling or cancellation MUST
        # happen under _futures_lock.

        # Snapshoting and range computation inside lock
        with self._futures_lock:
            image_files = self.image_files
            n = len(image_files)
            if n == 0:
                return
            # Ensure current_index is clamped
            safe_current = max(0, min(n - 1, current_index))

            start = max(0, safe_current - behind)
            end = min(n, safe_current + ahead + 1)

            log.debug(
                "Prefetch range: [%d, %d) for index %d (direction=%d, behind=%d, ahead=%d)",
                start,
                end,
                safe_current,
                self._last_navigation_direction,
                behind,
                ahead,
            )

            # Build priority order: current first, then in direction of travel
            priority_order = [safe_current]
            if self._last_navigation_direction > 0:
                priority_order.extend(range(safe_current + 1, end))
                priority_order.extend(range(safe_current - 1, start - 1, -1))
            else:
                priority_order.extend(range(safe_current - 1, start - 1, -1))
                priority_order.extend(range(safe_current + 1, end))

            # Cancel stale futures and remove from scheduled
            tasks_submitted = 0
            # Clean up old generation entries to prevent memory leak
            old_generations = [
                g for g in list(self._scheduled.keys()) if g < self.generation
            ]
            for g in old_generations:
                self._scheduled.pop(g, None)

            # Get scheduled set for current generation
            scheduled = self._scheduled.setdefault(self.generation, set())

            for index, future in list(self.futures.items()):
                if index < start or index >= end:
                    if future.cancel():
                        self.futures.pop(index, None)
                        scheduled.discard(index)

            for i in priority_order:
                if i < 0 or i >= n:
                    continue
                if i not in scheduled and i not in self.futures:
                    self.submit_task(i, self.generation)
                    scheduled.add(i)
                    tasks_submitted += 1

        if self.debug:
            _t_end = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_end*1000:.3f} update_prefetch: DONE submitted={tasks_submitted} total={(_t_end - _t_start)*1000:.2f}ms"
            )

    def submit_task(
        self,
        index: int,
        generation: int,
        priority: bool = False,
        override_path: Optional[Path] = None,
    ) -> Optional[Future]:
        """Submits a decoding task for a given index."""
        if self._stop_event.is_set():
            return None

        if self.debug and priority:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} submit_task: PRIORITY index={index} gen={generation} override={override_path}"
            )

        with self._futures_lock:
            # Bounds check must happen inside the lock to stay consistent
            # with self.image_files (which set_image_files can replace under lock).
            if index < 0 or index >= len(self.image_files):
                return None

            requested_path = (
                override_path
                if override_path is not None
                else self.image_files[index].path
            )

            # We track by index. If we already have a job for this index,
            # we must cancel it if the requested path is different
            # (e.g. switching between main and variants).
            if index in self.futures and not self.futures[index].done():
                current_path = self.future_paths.get(index)
                if current_path != requested_path:
                    # Force cancel the old one to switch paths
                    self.futures[index].cancel()
                    self.futures.pop(index, None)
                    self.future_paths.pop(index, None)
                else:
                    return self.futures[index]

            if priority:
                cancelled_count = 0
                safe_radius = 2
                direction = self._last_navigation_direction

                for task_index, future in list(self.futures.items()):
                    if task_index == index or abs(task_index - index) <= safe_radius:
                        continue

                    # Don't cancel tasks ahead of travel direction — we'll need them
                    if direction > 0 and task_index > index:
                        continue
                    if direction < 0 and task_index < index:
                        continue

                    if not future.done() and future.cancel():
                        cancelled_count += 1
                        self.futures.pop(task_index, None)
                        self.future_paths.pop(task_index, None)
                if cancelled_count > 0:
                    log.debug(
                        "Cancelled %d pending prefetch tasks to prioritize index %d",
                        cancelled_count,
                        index,
                    )

            image_file = self.image_files[index]
            display_width, display_height, display_generation = self.get_display_info()

            future = self.executor.submit(
                self._decode_and_cache,
                image_file,
                index,
                generation,
                display_width,
                display_height,
                display_generation,
                override_path,
            )
            self.futures[index] = future
            self.future_paths[index] = requested_path
            future.add_done_callback(lambda f, idx=index: self._cleanup_future(idx, f))
            return future

    def _decode_and_cache(
        self,
        image_file: ImageFile,
        index: int,
        generation: int,
        display_width: int,
        display_height: int,
        display_generation: int,
        override_path: Optional[Path] = None,
    ) -> Optional[tuple[Path, int]]:
        """The actual work done by the thread pool."""
        if generation != self.generation or self._stop_event.is_set():
            return None

        # Use override path if provided, otherwise default to image_file.path
        target_path = override_path if override_path is not None else image_file.path

        try:
            if os.path.getsize(target_path) == 0:
                log.warning("Skipping empty image file: %s", target_path)
                return None

            color_mode = config.get("color", "mode", fallback="none").lower()
            optimize_for = config.get("core", "optimize_for", fallback="speed").lower()
            fast_dct = optimize_for == "speed"
            use_resized = optimize_for == "speed"
            should_resize = display_width > 0 and display_height > 0
            is_jpeg = target_path.suffix.lower() in {".jpg", ".jpeg", ".jpe"}

            buffer = None
            icc_bytes = None

            orientation = 1
            if color_mode == "icc":
                monitor_profile = get_monitor_profile()
                monitor_icc_path = config.get(
                    "color", "monitor_icc_path", fallback=""
                ).strip()

                if monitor_profile is not None:
                    if is_jpeg:
                        try:
                            with open(target_path, "rb") as f:
                                with mmap.mmap(
                                    f.fileno(), 0, access=mmap.ACCESS_READ
                                ) as mmapped:
                                    if use_resized and should_resize:
                                        buffer = decode_jpeg_resized(
                                            mmapped,
                                            display_width,
                                            display_height,
                                            fast_dct=fast_dct,
                                        )
                                    else:
                                        buffer = decode_jpeg_rgb(
                                            mmapped, fast_dct=fast_dct
                                        )
                                        if buffer is not None and should_resize:
                                            img = PILImage.fromarray(buffer)
                                            img.thumbnail(
                                                (display_width, display_height),
                                                PILImage.Resampling.LANCZOS,
                                            )
                                            buffer = np.array(img)

                                    if buffer is not None:
                                        try:
                                            # Seek to beginning of mmapped file for PIL
                                            mmapped.seek(0)
                                            with PILImage.open(mmapped) as pil_img:
                                                icc_bytes = pil_img.info.get(
                                                    "icc_profile"
                                                )
                                                orientation = pil_img.getexif().get(
                                                    274, 1
                                                )
                                        except Exception:
                                            log.debug(
                                                "Failed to read EXIF from mmap for %s",
                                                target_path,
                                                exc_info=True,
                                            )
                        except Exception as e:
                            log.warning(
                                "Decode failed (ICC path) index=%d path=%s: %s",
                                index,
                                target_path,
                                e,
                            )
                            buffer = None

                    if buffer is None:
                        try:
                            with PILImage.open(target_path) as img:
                                orientation = img.getexif().get(274, 1)
                                img = img.convert("RGB")
                                if should_resize:
                                    img.thumbnail(
                                        (display_width, display_height),
                                        PILImage.Resampling.LANCZOS,
                                    )
                                buffer = np.array(img)
                        except Exception as e:
                            log.warning(
                                "Decode failed (ICC fallback) index=%d path=%s: %s",
                                index,
                                target_path,
                                e,
                            )
                            if target_path.suffix.lower() in _RAW_EXTENSIONS:
                                buffer = _make_raw_placeholder(
                                    display_width, display_height
                                )
                            else:
                                return None

                    img = PILImage.fromarray(buffer)

                    if icc_bytes is None:
                        try:
                            # Try to get ICC if we don't have it yet (but orientation should be set)
                            with PILImage.open(target_path) as orig:
                                icc_bytes = orig.info.get("icc_profile")
                                if orientation == 1:
                                    orientation = orig.getexif().get(274, 1)
                        except Exception as e:
                            log.warning(
                                "Failed to read metadata from %s: %s", target_path, e
                            )

                    src_profile = None
                    src_profile_key = None
                    if icc_bytes:
                        try:
                            src_profile = ImageCms.ImageCmsProfile(
                                io.BytesIO(icc_bytes)
                            )
                            src_profile_key = hashlib.sha256(icc_bytes).hexdigest()
                        except Exception as e:
                            log.warning("Failed to parse ICC profile: %s", e)

                    if src_profile is None:
                        src_profile = SRGB_PROFILE
                        src_profile_key = "srgb_builtin"

                    try:
                        transform = get_icc_transform(
                            src_profile,
                            monitor_profile,
                            src_profile_key,
                            monitor_icc_path,
                        )
                        ImageCms.applyTransform(img, transform, inPlace=True)
                        buffer = np.array(img, dtype=np.uint8)
                    except Exception as e:
                        log.warning("ICC conversion failed: %s", e)

            if buffer is None:
                if is_jpeg:
                    try:
                        with open(target_path, "rb") as f:
                            with mmap.mmap(
                                f.fileno(), 0, access=mmap.ACCESS_READ
                            ) as mmapped:
                                if use_resized and should_resize:
                                    buffer = decode_jpeg_resized(
                                        mmapped,
                                        display_width,
                                        display_height,
                                        fast_dct=fast_dct,
                                    )
                                else:
                                    buffer = decode_jpeg_rgb(mmapped, fast_dct=fast_dct)
                                    if buffer is not None and should_resize:
                                        img = PILImage.fromarray(buffer)
                                        img.thumbnail(
                                            (display_width, display_height),
                                            PILImage.Resampling.LANCZOS,
                                        )
                                        buffer = np.array(img)

                                # Capture orientation if we have a buffer
                                if buffer is not None:
                                    try:
                                        mmapped.seek(0)
                                        with PILImage.open(mmapped) as pil_img:
                                            orientation = pil_img.getexif().get(274, 1)
                                    except Exception:
                                        log.debug(
                                            "Failed to read EXIF from mmap for %s",
                                            target_path,
                                            exc_info=True,
                                        )
                    except Exception:
                        buffer = None

                if buffer is None:
                    try:
                        with PILImage.open(target_path) as img:
                            orientation = img.getexif().get(274, 1)
                            img = img.convert("RGB")
                            if should_resize:
                                img.thumbnail(
                                    (display_width, display_height),
                                    PILImage.Resampling.LANCZOS,
                                )
                            buffer = np.array(img)
                    except Exception as e:
                        log.warning(
                            "Decode failed index=%d path=%s: %s", index, target_path, e
                        )
                        if target_path.suffix.lower() in _RAW_EXTENSIONS:
                            buffer = _make_raw_placeholder(
                                display_width, display_height
                            )
                        else:
                            return None

            if buffer is None:
                return None

            buffer = np.ascontiguousarray(buffer)

            # Apply captured orientation
            try:
                if orientation > 1:
                    buffer = apply_orientation_to_np(buffer, orientation)
            except Exception as e:
                log.warning("Failed to apply EXIF orientation: %s", e)

            bytes_per_line = buffer.strides[0]

            if color_mode == "saturation":
                # Safer pattern for custom config wrappers
                val = config.get("color", "saturation_factor", fallback="1.0")
                saturation_factor = float(val) if val is not None else 1.0
                if saturation_factor != 1.0:
                    apply_saturation_compensation(
                        buffer.ravel(),
                        buffer.shape[1],
                        buffer.shape[0],
                        bytes_per_line,
                        saturation_factor,
                    )

            mv = memoryview(buffer).cast("B")
            decoded = DecodedImage(
                buffer=mv,
                width=buffer.shape[1],
                height=buffer.shape[0],
                bytes_per_line=bytes_per_line,
                format=QImage.Format.Format_RGB888 if QImage else None,
            )

            if generation != self.generation or self._stop_event.is_set():
                return None

            cache_key = build_cache_key(target_path, display_generation)
            self.cache_put(cache_key, decoded)
            return (target_path, display_generation)

        except Exception as e:
            # Downgraded from ERROR to prevent log noise on bad files
            log.warning("Error in _decode_and_cache: %s", e, exc_info=True)
            return None

    def _cleanup_future(self, index: int, future: Future):
        """Removes the future from the tracking dictionary upon completion."""
        with self._futures_lock:
            # Only remove if it's the specific future we're tracking
            # (to avoid race if a new task for the same index was submitted)
            if self.futures.get(index) is future:
                self.futures.pop(index, None)
                self.future_paths.pop(index, None)

    def _cancel_all_locked(self):
        """Internal helper to cancel all pending prefetching tasks.
        Assumes _futures_lock is already held.
        """
        self.generation += 1  # Invalidate in-flight tasks
        # Snapshot values before cancelling
        all_futures = list(self.futures.values())
        for future in all_futures:
            future.cancel()
        self.futures.clear()
        self.future_paths.clear()
        self._scheduled.clear()

    def cancel_all(self):
        """Cancels all pending prefetching tasks."""
        with self._futures_lock:
            self._cancel_all_locked()

    def shutdown(self):
        """Initiates a clean shutdown of the prefetcher."""
        log.info("Shutting down Prefetcher...")
        self._stop_event.set()
        self.cancel_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
