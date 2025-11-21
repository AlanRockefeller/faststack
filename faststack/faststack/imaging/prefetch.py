"""Handles prefetching and decoding of adjacent images in a background thread pool."""

import logging
import os
import io
from concurrent.futures import ThreadPoolExecutor, Future
from typing import List, Dict, Optional, Callable
import mmap

import numpy as np
from PIL import Image as PILImage, ImageCms

from faststack.models import ImageFile, DecodedImage
from faststack.imaging.jpeg import decode_jpeg_rgb, decode_jpeg_resized
from faststack.config import config

log = logging.getLogger(__name__)

# ---- Option C: ICC Color Management Setup ----
SRGB_PROFILE = ImageCms.createProfile("sRGB")

# Cache for monitor ICC profile to avoid reloading on every decode
_monitor_profile_cache: Dict[str, Optional[ImageCms.ImageCmsProfile]] = {}
_monitor_profile_warning_logged = False

def get_monitor_profile():
    """Dynamically load monitor ICC profile based on current config.
    
    Caches the profile by path to reduce overhead and log spam.
    """
    global _monitor_profile_warning_logged
    
    monitor_icc_path = config.get('color', 'monitor_icc_path', fallback="").strip()
    
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
        return profile
    except (OSError, ImageCms.PyCMSError) as e:
        log.warning("Failed to load monitor ICC profile from %s: %s", monitor_icc_path, e)
        _monitor_profile_cache[monitor_icc_path] = None
        return None


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
    def __init__(self, image_files: List[ImageFile], cache_put: Callable, prefetch_radius: int, get_display_info: Callable):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.get_display_info = get_display_info
        # Use CPU count for I/O-bound JPEG decoding
        # Rule of thumb: 2x CPU cores for I/O bound, 1x for CPU bound
        optimal_workers = min((os.cpu_count() or 1) * 2, 8)  # Cap at 8
        
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

    def set_image_files(self, image_files: List[ImageFile]):
        if self.image_files != image_files:
            self.image_files = image_files
            self.cancel_all()

    def update_prefetch(self, current_index: int, is_navigation: bool = False):
        """Updates the prefetching queue based on the current image index.
        
        Args:
            current_index: The index to prefetch around
            is_navigation: True if this is from user navigation (arrow keys, etc.)
        """
        self.generation += 1
        
        # Track navigation to expand radius after user starts moving
        if is_navigation:
            self._navigation_count += 1
            if not self._radius_expanded and self._navigation_count >= 2:
                self._radius_expanded = True
                log.info("Expanding prefetch radius from %d to %d after user navigation", self._initial_radius, self.prefetch_radius)
        
        # Use smaller radius initially to reduce cache thrash before display size is stable
        effective_radius = self._initial_radius if not self._radius_expanded else self.prefetch_radius
        
        log.debug("Updating prefetch for index %d, generation %d, radius %d", current_index, self.generation, effective_radius)

        # Cancel stale futures
        stale_keys = []
        for index, future in self.futures.items():
            if not self._is_in_prefetch_range(index, current_index, effective_radius):
                future.cancel()
                stale_keys.append(index)
        for key in stale_keys:
            del self.futures[key]

        # Submit new tasks (with deduplication)
        start = max(0, current_index - effective_radius)
        end = min(len(self.image_files), current_index + effective_radius + 1)
        
        wanted = set(range(start, end))
        scheduled = self._scheduled.setdefault(self.generation, set())
        new_indices = wanted - scheduled

        for i in new_indices:
            if i not in self.futures:
                self.submit_task(i, self.generation)
                scheduled.add(i)

    def submit_task(self, index: int, generation: int) -> Optional[Future]:
        """Submits a decoding task for a given index."""
        if index in self.futures and not self.futures[index].done():
            return self.futures[index] # Already submitted

        image_file = self.image_files[index]
        display_width, display_height, display_generation = self.get_display_info()

        future = self.executor.submit(self._decode_and_cache, image_file, index, generation, display_width, display_height, display_generation)
        self.futures[index] = future
        log.debug("Submitted prefetch task for index %d", index)
        return future

    def _decode_and_cache(self, image_file: ImageFile, index: int, generation: int, display_width: int, display_height: int, display_generation: int) -> Optional[tuple[int, int]]:
        """The actual work done by the thread pool."""
        local_generation = self.generation # Capture current generation for this worker

        if generation != local_generation:
            log.debug("Skipping stale task for index %d (gen %d != %d)", index, generation, local_generation)
            return None

        try:
            # Get current color management mode
            color_mode = config.get('color', 'mode', fallback="none").lower()

            # Option C: Full ICC pipeline with Pillow
            if color_mode == "icc":
                monitor_profile = get_monitor_profile()
                
                if monitor_profile is not None:
                    img = PILImage.open(str(image_file.path))
                    
                    # Resize before color conversion for speed
                    if display_width > 0 and display_height > 0:
                        img.thumbnail((display_width, display_height), PILImage.Resampling.LANCZOS)
                    
                    # Extract embedded ICC profile or assume sRGB
                    icc_bytes = img.info.get("icc_profile")
                    src_profile = None
                    
                    if icc_bytes:
                        try:
                            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                            log.debug("Using embedded ICC profile from %s", image_file.path)
                        except (OSError, ImageCms.PyCMSError, ValueError) as e:
                            log.warning("Failed to parse ICC profile from %s: %s", image_file.path, e)
                    
                    if src_profile is None:
                        src_profile = SRGB_PROFILE
                        log.debug("No embedded profile, assuming sRGB for %s", image_file.path)
                    
                    # Convert from source profile to monitor profile
                    log.debug("Converting image from source to monitor profile")
                    img = ImageCms.profileToProfile(
                        img,
                        src_profile,
                        monitor_profile,
                        outputMode="RGB",
                    )
                    
                    rgb = np.array(img, dtype=np.uint8)
                    h, w, _ = rgb.shape
                    bytes_per_line = w * 3
                    arr = rgb.reshape(-1).copy()
                else:
                    # Fall back to standard decode if ICC profile not available
                    log.warning("ICC mode selected but no monitor profile available, using standard decode")
                    with open(image_file.path, "rb") as f:
                        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                            jpeg_bytes = mmapped[:]
                    
                    buffer = decode_jpeg_resized(jpeg_bytes, display_width, display_height)
                    if buffer is None:
                        return None
                    
                    h, w, _ = buffer.shape
                    bytes_per_line = w * 3
                    arr = buffer.reshape(-1).copy()
            
            else:
                # Standard decode path (Option A or no color management)
                with open(image_file.path, "rb") as f:
                    with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                        jpeg_bytes = mmapped[:]
                
                buffer = decode_jpeg_resized(jpeg_bytes, display_width, display_height)
                if buffer is None:
                    return None
                    
                h, w, _ = buffer.shape
                bytes_per_line = w * 3
                arr = buffer.reshape(-1).copy()
                
                # Option A: Saturation compensation
                if color_mode == "saturation":
                    try:
                        factor = float(config.get('color', 'saturation_factor', fallback="1.0"))
                        apply_saturation_compensation(arr, w, h, bytes_per_line, factor)
                    except (ValueError, AssertionError) as e:
                        log.warning("Failed to apply saturation compensation: %s", e)
            
            # Re-check generation before caching
            if self.generation != local_generation:
                log.debug("Generation changed for index %d before caching. Skipping cache_put.", index)
                return None
            
            decoded_image = DecodedImage(
                buffer=arr.data,
                width=w,
                height=h,
                bytes_per_line=bytes_per_line,
                format=None # Placeholder for QImage.Format.Format_RGB888
            )
            cache_key = f"{index}_{display_generation}"
            self.cache_put(cache_key, decoded_image)
            log.debug("Successfully decoded and cached image at index %d for display gen %d", index, display_generation)
            return index, display_generation
            
        except Exception as e:
            log.error("Error decoding image %s at index %d: %s", image_file.path, index, e)
        
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
