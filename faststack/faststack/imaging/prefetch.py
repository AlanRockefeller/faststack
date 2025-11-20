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

def get_monitor_profile():
    """Dynamically load monitor ICC profile based on current config."""
    try:
        monitor_icc_path = config.get('color', 'monitor_icc_path', fallback="").strip()
        if monitor_icc_path:
            profile = ImageCms.ImageCmsProfile(monitor_icc_path)
            log.debug(f"Loaded monitor ICC profile: {monitor_icc_path}")
            return profile
        else:
            log.warning("ICC mode enabled but no monitor_icc_path configured")
            return None
    except Exception as e:
        log.warning(f"Failed to load monitor ICC profile: {e}")
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
    factor: 1.0 = no change, <1.0 = less saturated, >1.0 = more saturated
    """
    if factor == 1.0:
        return

    # Treat the buffer as [height, bytes_per_line]
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

    def set_image_files(self, image_files: List[ImageFile]):
        if self.image_files != image_files:
            self.image_files = image_files
            self.cancel_all()

    def update_prefetch(self, current_index: int):
        """Updates the prefetching queue based on the current image index."""
        self.generation += 1
        log.debug(f"Updating prefetch for index {current_index}, generation {self.generation}")

        # Cancel stale futures
        stale_keys = []
        for index, future in self.futures.items():
            if not self._is_in_prefetch_range(index, current_index):
                future.cancel()
                stale_keys.append(index)
        for key in stale_keys:
            del self.futures[key]

        # Submit new tasks
        start = max(0, current_index - self.prefetch_radius)
        end = min(len(self.image_files), current_index + self.prefetch_radius + 1)

        for i in range(start, end):
            if i not in self.futures:
                self.submit_task(i, self.generation)

    def submit_task(self, index: int, generation: int) -> Optional[Future]:
        """Submits a decoding task for a given index."""
        if index in self.futures and not self.futures[index].done():
            return self.futures[index] # Already submitted

        image_file = self.image_files[index]
        display_width, display_height, display_generation = self.get_display_info()

        future = self.executor.submit(self._decode_and_cache, image_file, index, generation, display_width, display_height, display_generation)
        self.futures[index] = future
        log.debug(f"Submitted prefetch task for index {index}")
        return future

    def _decode_and_cache(self, image_file: ImageFile, index: int, generation: int, display_width: int, display_height: int, display_generation: int) -> Optional[tuple[int, int]]:
        """The actual work done by the thread pool."""
        local_generation = self.generation # Capture current generation for this worker

        if generation != local_generation:
            log.debug(f"Skipping stale task for index {index} (gen {generation} != {local_generation})")
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
                            log.debug(f"Using embedded ICC profile from {image_file.path}")
                        except Exception as e:
                            log.warning(f"Failed to parse ICC profile from {image_file.path}: {e}")
                    
                    if src_profile is None:
                        src_profile = SRGB_PROFILE
                        log.debug(f"No embedded profile, assuming sRGB for {image_file.path}")
                    
                    # Convert from source profile to monitor profile
                    log.debug(f"Converting image from source to monitor profile")
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
                    except Exception as e:
                        log.warning(f"Failed to apply saturation compensation: {e}")
            
            # Re-check generation before caching
            if self.generation != local_generation:
                log.debug(f"Generation changed for index {index} before caching. Skipping cache_put.")
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
            log.debug(f"Successfully decoded and cached image at index {index} for display gen {display_generation}")
            return index, display_generation
            
        except Exception as e:
            log.error(f"Error decoding image {image_file.path} at index {index}: {e}")
        
        return None

    def _is_in_prefetch_range(self, index: int, current_index: int) -> bool:
        """Checks if an index is within the current prefetch window."""
        return abs(index - current_index) <= self.prefetch_radius

    def cancel_all(self):
        """Cancels all pending prefetch tasks."""
        log.info("Cancelling all prefetch tasks.")
        self.generation += 1
        for future in self.futures.values():
            future.cancel()
        self.futures.clear()

    def shutdown(self):
        """Shuts down the thread pool executor."""
        log.info("Shutting down prefetcher thread pool.")
        self.cancel_all()
        self.executor.shutdown(wait=False)
