"""Handles prefetching and decoding of adjacent images in a background thread pool."""

import logging
import os
import io
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future
from typing import List, Dict, Optional, Callable
import mmap

import numpy as np
from PIL import Image as PILImage, ImageCms

from faststack.models import ImageFile, DecodedImage
from faststack.imaging.jpeg import decode_jpeg_rgb, decode_jpeg_resized, TURBO_AVAILABLE
from faststack.imaging.cache import build_cache_key
from faststack.config import config

log = logging.getLogger(__name__)

import threading

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
            log.debug("Built new ICC transform for profile pair (src=%s, monitor=%s)", src_profile_key[:16], monitor_profile_path)
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
    
    monitor_icc_path = config.get('color', 'monitor_icc_path', fallback="").strip()
    
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
            log.warning("Failed to load monitor ICC profile from %s: %s", monitor_icc_path, e)
            _monitor_profile_cache[monitor_icc_path] = None
        
        return _monitor_profile_cache[monitor_icc_path]

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
    def __init__(self, image_files: List[ImageFile], cache_put: Callable, prefetch_radius: int, get_display_info: Callable, debug: bool = False):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.get_display_info = get_display_info
        self.debug = debug
        # Use CPU count for I/O-bound JPEG decoding
        # Rule of thumb: 2x CPU cores for I/O bound, 1x for CPU bound
        optimal_workers = min((os.cpu_count() or 1) * 2, 4)  # Cap at 4
        
        self.executor = ThreadPoolExecutor(
            max_workers=optimal_workers,
            thread_name_prefix="Prefetcher"
        )
        self.futures: Dict[int, Future] = {}
        self.generation = 0
        self._scheduled: Dict[int, set] = {}  # generation -> set of scheduled indices
        
        # Adaptive prefetch: start with smaller radius, expand after user navigates
        self._initial_radius = 2  # Small radius at startup to reduce cache thrash
        self._navigation_count = 0  # Track how many times user has navigated
        self._radius_expanded = False
        
        # Directional prefetching
        self._last_navigation_direction: int = 1  # 1 = forward, -1 = backward
        self._direction_bias: float = 0.7  # 70% of radius in travel direction

    def set_image_files(self, image_files: List[ImageFile]):
        if self.image_files != image_files:
            self.image_files = image_files
            self.cancel_all()

    def update_prefetch(self, current_index: int, is_navigation: bool = False, direction: Optional[int] = None):
        """Updates the prefetching queue based on the current image index.
        
        Args:
            current_index: The index to prefetch around
            is_navigation: True if this is from user navigation (arrow keys, etc.)
            direction: 1 for forward, -1 for backward, None to use last direction
        """
        # NOTE: Generation is NOT incremented here. It only changes when display size,
        # zoom state, or color mode changes - events that actually invalidate cached images.
        # Navigation just shifts which indices to prefetch.
        
        # Clean up old generation entries to prevent memory leak
        old_generations = [g for g in self._scheduled if g < self.generation]
        for g in old_generations:
            del self._scheduled[g]
        
        # Track navigation direction
        if direction is not None:
            self._last_navigation_direction = direction
        
        # Track navigation to expand radius after user starts moving
        if is_navigation:
            self._navigation_count += 1
            if not self._radius_expanded and self._navigation_count >= 2:
                self._radius_expanded = True
                log.info("Expanding prefetch radius from %d to %d after user navigation", self._initial_radius, self.prefetch_radius)
        
        # Use smaller radius initially to reduce cache thrash before display size is stable
        effective_radius = self._initial_radius if not self._radius_expanded else self.prefetch_radius
        
        if self.debug:
            log.info("Prefetch radius: initial=%d, configured=%d, effective=%d", 
                     self._initial_radius, self.prefetch_radius, effective_radius)
        
        # Calculate asymmetric range based on direction
        if self._last_navigation_direction > 0:  # Moving forward
            behind = max(1, int(effective_radius * (1 - self._direction_bias)))
            ahead = effective_radius - behind + 1
        else:  # Moving backward
            ahead = max(1, int(effective_radius * (1 - self._direction_bias)))
            behind = effective_radius - ahead + 1
        
        start = max(0, current_index - behind)
        end = min(len(self.image_files), current_index + ahead + 1)
        
        log.debug("Prefetch range: [%d, %d) for index %d (direction=%d, behind=%d, ahead=%d)", 
                  start, end, current_index, self._last_navigation_direction, behind, ahead)

        # Get scheduled set for current generation
        scheduled = self._scheduled.setdefault(self.generation, set())
        
        # Cancel stale futures and remove from scheduled
        stale_keys = []
        for index, future in list(self.futures.items()):
            if index < start or index >= end:
                if future.cancel():
                    stale_keys.append(index)
                    scheduled.discard(index)  # Remove from scheduled set
        for key in stale_keys:
            del self.futures[key]

        # Submit new tasks - prioritize current image and direction of travel
        
        # Build priority order: current first, then in direction of travel
        priority_order = [current_index]
        if self._last_navigation_direction > 0:
            priority_order.extend(range(current_index + 1, end))
            priority_order.extend(range(current_index - 1, start - 1, -1))
        else:
            priority_order.extend(range(current_index - 1, start - 1, -1))
            priority_order.extend(range(current_index + 1, end))
        
        for i in priority_order:
            if i < 0 or i >= len(self.image_files):
                continue
            if i not in scheduled and i not in self.futures:
                self.submit_task(i, self.generation)
                scheduled.add(i)

    def submit_task(self, index: int, generation: int, priority: bool = False) -> Optional[Future]:
        """Submits a decoding task for a given index.
        
        Args:
            index: Image index to decode
            generation: Generation number for cache invalidation
            priority: If True, cancels lower-priority pending tasks to free up workers
        """
        if index in self.futures and not self.futures[index].done():
            return self.futures[index] # Already submitted

        # For high-priority tasks (current image), cancel pending prefetch tasks
        # to free up worker threads and reduce blocking time
        if priority:
            cancelled_count = 0
            # Don't cancel tasks that are very close to the requested index (e.g. +/- 2)
            # This prevents thrashing when the user is navigating quickly
            safe_radius = 2
            
            for task_index, future in list(self.futures.items()):
                # Skip the current task
                if task_index == index:
                    continue
                
                # Skip tasks within safe radius
                if abs(task_index - index) <= safe_radius:
                    continue

                if not future.done() and future.cancel():
                    cancelled_count += 1
                    del self.futures[task_index]
            if cancelled_count > 0:
                log.debug("Cancelled %d pending prefetch tasks to prioritize index %d", cancelled_count, index)

        image_file = self.image_files[index]
        display_width, display_height, display_generation = self.get_display_info()

        future = self.executor.submit(self._decode_and_cache, image_file, index, generation, display_width, display_height, display_generation)
        self.futures[index] = future
        log.debug("Submitted %s task for index %d", "priority" if priority else "prefetch", index)
        return future

    def _decode_and_cache(self, image_file: ImageFile, index: int, generation: int, display_width: int, display_height: int, display_generation: int) -> Optional[tuple[Path, int]]:
        """The actual work done by the thread pool."""
        import time
        
        t_start = time.perf_counter()
        
        # Early check: if generation has already advanced since this task was submitted, skip it
        if generation != self.generation:
            log.debug("Skipping stale task for index %d (submitted gen %d != current gen %d)", index, generation, self.generation)
            return None

        try:
            # Get current color management mode and optimization setting
            color_mode = config.get('color', 'mode', fallback="none").lower()
            optimize_for = config.get('core', 'optimize_for', fallback='speed').lower()
            fast_dct = (optimize_for == 'speed')
            use_resized = (optimize_for == 'speed')  # Use decode_jpeg_resized for speed, decode_jpeg_rgb for quality
            
            # Determine if we should resize
            should_resize = (display_width > 0 and display_height > 0)

            # Option C: Full ICC pipeline - Use TurboJPEG for decode, Pillow only for ICC conversion
            if color_mode == "icc":
                monitor_profile = get_monitor_profile()
                monitor_icc_path = config.get('color', 'monitor_icc_path', fallback="").strip()
                
                if monitor_profile is not None:
                    # FAST: Use TurboJPEG for decode + resize
                    t_before_read = time.perf_counter()
                    with open(image_file.path, "rb") as f:
                        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                            # Pass mmap directly - no copy! Decoders accept bytes-like objects
                            if use_resized and should_resize:
                                buffer = decode_jpeg_resized(mmapped, display_width, display_height, fast_dct=fast_dct)
                            else:
                                # Quality mode or Full Res: decode full image then resize with high quality
                                buffer = decode_jpeg_rgb(mmapped, fast_dct=fast_dct)
                                if buffer is not None and should_resize:
                                    img = PILImage.fromarray(buffer)
                                    img.thumbnail((display_width, display_height), PILImage.Resampling.LANCZOS)
                                    buffer = np.array(img)
                    t_after_read = time.perf_counter()
                    if buffer is None:
                        return None
                    t_after_decode = time.perf_counter()
                    
                    # Convert numpy array to PIL Image for ICC conversion
                    img = PILImage.fromarray(buffer)
                    t_after_array_to_pil = time.perf_counter()
                    
                    # Extract ICC profile from original file (need to read header only)
                    t_before_profile_read = time.perf_counter()
                    with PILImage.open(image_file.path) as orig:
                        icc_bytes = orig.info.get("icc_profile")
                    t_after_profile_read = time.perf_counter()
                    
                    src_profile = None
                    src_profile_key = None
                    if icc_bytes:
                        try:
                            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                            # Compute stable key: SHA-256 digest of ICC bytes
                            src_profile_key = hashlib.sha256(icc_bytes).hexdigest()
                            log.debug("Using embedded ICC profile from %s", image_file.path)
                        except (OSError, ImageCms.PyCMSError, ValueError) as e:
                            log.warning("Failed to parse ICC profile from %s: %s", image_file.path, e)
                    
                    if src_profile is None:
                        src_profile = SRGB_PROFILE
                        # Use a constant key for sRGB since it's always the same
                        src_profile_key = "srgb_builtin"
                        log.debug("No embedded profile, assuming sRGB for %s", image_file.path)
                    
                    # Convert from source profile to monitor profile using cached transform
                    try:
                        log.debug("Converting image from source to monitor profile")
                        t_before_icc = time.perf_counter()
                        transform = get_icc_transform(src_profile, monitor_profile, src_profile_key, monitor_icc_path)
                        # Alan 11-20-25 - Add inPlace=True to speed up copy, shouldn't have many negative effects
                        ImageCms.applyTransform(img, transform, inPlace=True)
                        t_after_icc = time.perf_counter()
                        
                        rgb = np.array(img, dtype=np.uint8)
                        h, w, _ = rgb.shape
                        bytes_per_line = w * 3
                        arr = rgb.reshape(-1).copy()
                        t_after_copy = time.perf_counter()
                        
                        if self.debug:
                            decoder = "TurboJPEG" if TURBO_AVAILABLE else "Pillow"
                            log.info("ICC decode timing for index %d (%s): read=%.3fs, decode=%.3fs, array_to_pil=%.3fs, profile_read=%.3fs, icc=%.3fs, copy=%.3fs, total=%.3fs, size=%dx%d",
                                     index, decoder, t_after_read - t_before_read, t_after_decode - t_after_read,
                                     t_after_array_to_pil - t_after_decode, t_after_profile_read - t_before_profile_read,
                                     t_after_icc - t_before_icc, t_after_copy - t_after_icc,
                                     t_after_copy - t_start, w, h)
                    except (OSError, ImageCms.PyCMSError, ValueError) as e:
                        # ICC conversion failed, fall back to standard decode
                        log.warning("ICC profile conversion failed for %s: %s, falling back to standard decode", image_file.path, e)
                        t_before_fallback_read = time.perf_counter()
                        with open(image_file.path, "rb") as f:
                            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                                # Pass mmap directly - no copy!
                                if use_resized and should_resize:
                                    buffer = decode_jpeg_resized(mmapped, display_width, display_height, fast_dct=fast_dct)
                                else:
                                    # Quality mode or Full Res: decode full image then resize with high quality
                                    buffer = decode_jpeg_rgb(mmapped, fast_dct=fast_dct)
                                    if buffer is not None and should_resize:
                                        img = PILImage.fromarray(buffer)
                                        img.thumbnail((display_width, display_height), PILImage.Resampling.LANCZOS)
                                        buffer = np.array(img)
                        t_after_fallback_read = time.perf_counter()
                        if buffer is None:
                            return None
                        t_after_fallback_decode = time.perf_counter()
                        
                        h, w, _ = buffer.shape
                        bytes_per_line = w * 3
                        arr = buffer.reshape(-1).copy()
                        
                        if self.debug:
                            decoder = "TurboJPEG" if TURBO_AVAILABLE else "Pillow"
                            log.info("ICC fallback decode timing for index %d (%s): read=%.3fs, decode=%.3fs, total=%.3fs, size=%dx%d",
                                     index, decoder, t_after_fallback_read - t_before_fallback_read,
                                     t_after_fallback_decode - t_after_fallback_read,
                                     t_after_fallback_decode - t_start, w, h)
                else:
                    # Fall back to standard decode if ICC profile not available
                    log.warning("ICC mode selected but no monitor profile available, using standard decode")
                    t_before_read = time.perf_counter()
                    with open(image_file.path, "rb") as f:
                        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                            # Pass mmap directly - no copy!
                            if use_resized and should_resize:
                                buffer = decode_jpeg_resized(mmapped, display_width, display_height, fast_dct=fast_dct)
                            else:
                                # Quality mode or Full Res: decode full image then resize with high quality
                                buffer = decode_jpeg_rgb(mmapped, fast_dct=fast_dct)
                                if buffer is not None and should_resize:
                                    img = PILImage.fromarray(buffer)
                                    img.thumbnail((display_width, display_height), PILImage.Resampling.LANCZOS)
                                    buffer = np.array(img)
                    t_after_read = time.perf_counter()
                    if buffer is None:
                        return None
                    t_after_decode = time.perf_counter()
                    
                    h, w, _ = buffer.shape
                    bytes_per_line = w * 3
                    arr = buffer.reshape(-1).copy()
                    
                    if self.debug:
                        decoder = "TurboJPEG" if TURBO_AVAILABLE else "Pillow"
                        log.info("Standard decode timing (no ICC profile) for index %d (%s): read=%.3fs, decode=%.3fs, total=%.3fs, size=%dx%d",
                                 index, decoder, t_after_read - t_before_read, t_after_decode - t_after_read,
                                 t_after_decode - t_start, w, h)
            
            else:
                # Standard decode path (Option A or no color management)
                t_before_read = time.perf_counter()
                with open(image_file.path, "rb") as f:
                    with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                        # Pass mmap directly - no copy! Decoders accept bytes-like objects
                        if use_resized and should_resize:
                            buffer = decode_jpeg_resized(mmapped, display_width, display_height, fast_dct=fast_dct)
                        else:
                            # Quality mode or Full Res: decode full image then resize with high quality
                            buffer = decode_jpeg_rgb(mmapped, fast_dct=fast_dct)
                            if buffer is not None and should_resize:
                                img = PILImage.fromarray(buffer)
                                img.thumbnail((display_width, display_height), PILImage.Resampling.LANCZOS)
                                buffer = np.array(img)
                t_after_read = time.perf_counter()
                if buffer is None:
                    return None
                t_after_decode = time.perf_counter()
                    
                h, w, _ = buffer.shape
                bytes_per_line = w * 3
                arr = buffer.reshape(-1).copy()
                t_after_copy = time.perf_counter()

            # Apply saturation compensation if enabled
            if color_mode == "saturation":
                try:
                    factor = float(config.get('color', 'saturation_factor', fallback="1.0"))
                    apply_saturation_compensation(arr, w, h, bytes_per_line, factor)
                    t_after_saturation = time.perf_counter()
                    
                    if self.debug:
                        decoder = "TurboJPEG" if TURBO_AVAILABLE else "Pillow"
                        log.info("Saturation decode timing for index %d (%s): read=%.3fs, decode=%.3fs, copy=%.3fs, saturation=%.3fs, total=%.3fs, size=%dx%d",
                                 index, decoder, t_after_read - t_before_read, t_after_decode - t_after_read,
                                 t_after_copy - t_after_decode, t_after_saturation - t_after_copy,
                                 t_after_saturation - t_start, w, h)
                except (ValueError, AssertionError) as e:
                    log.warning("Failed to apply saturation compensation: %s", e)
            else:
                # No color management - log standard timing
                if self.debug:
                    decoder = "TurboJPEG" if TURBO_AVAILABLE else "Pillow"
                    log.info("Standard decode timing for index %d (%s): read=%.3fs, decode=%.3fs, copy=%.3fs, total=%.3fs, size=%dx%d",
                             index, decoder, t_after_read - t_before_read, t_after_decode - t_after_read,
                             t_after_copy - t_after_decode, t_after_copy - t_start, w, h)
            
            # Re-check generation before caching (in case it changed during decode)
            if self.generation != generation:
                log.debug("Generation changed for index %d before caching (current gen %d != submitted gen %d). Skipping cache_put.", index, self.generation, generation)
                return None
            
            decoded_image = DecodedImage(
                buffer=arr.data,
                width=w,
                height=h,
                bytes_per_line=bytes_per_line,
                format=None # Placeholder for QImage.Format.Format_RGB888
            )
            cache_key = build_cache_key(image_file.path, display_generation)
            self.cache_put(cache_key, decoded_image)
            log.debug("Successfully decoded and cached image at index %d for display gen %d", index, display_generation)
            return image_file.path, display_generation
            
        except Exception:
            log.exception("Error decoding image %s at index %d", image_file.path, index)
        
        return None

    def _is_in_prefetch_range(self, index: int, current_index: int, radius: Optional[int] = None) -> bool:
        """Checks if an index is within the current prefetch window.
        
        Args:
            index: The index to check
            current_index: The center of the prefetch window
            radius: Optional custom radius; if None, uses self.prefetch_radius
        """
        if radius is None:
            radius = self.prefetch_radius
        return abs(index - current_index) <= radius

    def cancel_all(self):
        """Cancels all pending prefetch tasks."""
        log.info("Cancelling all prefetch tasks.")
        self.generation += 1
        for future in self.futures.values():
            future.cancel()
        self.futures.clear()
        self._scheduled.clear()  # Clear scheduled indices when bumping generation

    def shutdown(self):
        """Shuts down the thread pool executor."""
        log.info("Shutting down prefetcher thread pool.")
        self.cancel_all()
        self.executor.shutdown(wait=False)
