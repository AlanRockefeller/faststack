"""Handles prefetching and decoding of adjacent images in a background thread pool.

This version bypasses PyTurboJPEG and decodes images using QImage, with a
Pillow fallback to ensure JPEGs still load even if Qt's image plugins are
unavailable.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, Future
from typing import List, Dict, Optional, Callable

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtCore import Qt

from faststack.models import ImageFile, DecodedImage

log = logging.getLogger(__name__)


class Prefetcher:
    def __init__(
        self,
        image_files: List[ImageFile],
        cache_put: Callable,
        prefetch_radius: int,
        get_display_info: Callable,
    ):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.get_display_info = get_display_info

        # Use CPU count for I/O-bound decoding; cap at 8 workers
        optimal_workers = min((os.cpu_count() or 1) * 2, 8)

        self.executor = ThreadPoolExecutor(
            max_workers=optimal_workers,
            thread_name_prefix="Prefetcher",
        )
        self.futures: Dict[int, Future] = {}
        self.generation = 0

    def set_image_files(self, image_files: List[ImageFile]):
        """Update the image list and cancel any outstanding work."""
        if self.image_files != image_files:
            self.image_files = image_files
            self.cancel_all()

    def update_prefetch(self, current_index: int):
        """Updates the prefetching queue based on the current image index."""
        self.generation += 1
        log.debug(f"Updating prefetch for index {current_index}, generation {self.generation}")

        # Cancel futures outside the window
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
            return self.futures[index]  # Already submitted

        if not self.image_files:
            return None

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
        )
        self.futures[index] = future
        log.debug(f"Submitted prefetch task for index {index}")
        return future

    def _decode_and_cache(
        self,
        image_file: ImageFile,
        index: int,
        generation: int,
        display_width: int,
        display_height: int,
        display_generation: int,
    ) -> Optional[tuple[int, int]]:
        """
        Worker-thread function: load the image (prefer QImage, fall back to Pillow),
        resize, and cache as a DecodedImage.
        """
        local_generation = self.generation  # capture snapshot

        # Drop stale tasks early
        if generation != local_generation:
            log.debug(f"Skipping stale task for index {index} (gen {generation} != {local_generation})")
            return None

        try:
            # First try QImage
            qimg = QImage(str(image_file.path))

            if qimg.isNull():
                log.warning(f"QImage failed to load {image_file.path}, falling back to Pillow.")
                from PIL import Image

                img = Image.open(str(image_file.path))

                # If display size is known, downscale via Pillow first
                if display_width > 0 and display_height > 0:
                    img.thumbnail((display_width, display_height), Image.Resampling.LANCZOS)

                rgb = np.array(img.convert("RGB"))
                h, w, _ = rgb.shape
                bytes_per_line = w * 3
                # Flatten to 1D; provider will use (width, height, bytes_per_line)
                arr = rgb.reshape(-1).copy()
            else:
                # Optional resize to fit display area; if width/height are 0, keep original size
                if display_width > 0 and display_height > 0:
                    qimg = qimg.scaled(
                        display_width,
                        display_height,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )

                # Ensure we have RGB888 format (3 bytes per pixel, no alpha)
                if qimg.format() != QImage.Format_RGB888:
                    qimg = qimg.convertToFormat(QImage.Format_RGB888)

                w = qimg.width()
                h = qimg.height()
                bytes_per_line = qimg.bytesPerLine()  # may be >= w * 3 (includes padding)

                # Access raw bits; PySide6 gives a memoryview, so we just use frombuffer
                ptr = qimg.bits()  # memoryview
                # Read exactly h * bytes_per_line bytes and copy so memory is owned by NumPy
                arr = np.frombuffer(ptr, dtype=np.uint8, count=h * bytes_per_line).copy()

            # Re-check generation before caching to avoid race conditions
            if self.generation != local_generation:
                log.debug(
                    f"Generation changed for index {index} before caching. "
                    f"Skipping cache_put (gen {generation} -> {self.generation})."
                )
                return None

            decoded_image = DecodedImage(
                buffer=arr,             # numpy array supports buffer protocol
                width=w,
                height=h,
                bytes_per_line=bytes_per_line,
                format=None,            # always treated as RGB888 in provider
            )
            cache_key = f"{index}_{display_generation}"
            self.cache_put(cache_key, decoded_image)
            log.debug(
                f"Decoded and cached image at index {index} "
                f"(w={w}, h={h}, bpl={bytes_per_line}) for display gen {display_generation}"
            )
            return index, display_generation

        except Exception as e:
            log.error(f"Error decoding image {image_file.path} at index {index}: {e}", exc_info=True)

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
