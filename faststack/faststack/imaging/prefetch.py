"""Handles prefetching and decoding of adjacent images in a background thread pool."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, Future
from typing import List, Dict, Optional, Callable

from faststack.models import ImageFile, DecodedImage
from faststack.imaging.jpeg import decode_jpeg_rgb

log = logging.getLogger(__name__)

class Prefetcher:
    def __init__(self, image_files: List[ImageFile], cache_put: Callable, prefetch_radius: int):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.executor = ThreadPoolExecutor(
            max_workers=min(4, os.cpu_count() or 1),
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
        future = self.executor.submit(self._decode_and_cache, image_file, index, generation)
        self.futures[index] = future
        log.debug(f"Submitted prefetch task for index {index}")
        return future

    def _decode_and_cache(self, image_file: ImageFile, index: int, generation: int) -> Optional[int]:
        """The actual work done by the thread pool."""
        local_generation = self.generation # Capture current generation for this worker

        if generation != local_generation:
            log.debug(f"Skipping stale task for index {index} (gen {generation} != {local_generation})")
            return None

        try:
            with open(image_file.path, "rb") as f:
                jpeg_bytes = f.read()
            
            buffer = decode_jpeg_rgb(jpeg_bytes)
            if buffer is not None:
                # Re-check generation before caching to prevent race conditions
                if self.generation != local_generation:
                    log.debug(f"Generation changed for index {index} before caching. Skipping cache_put.")
                    return None

                h, w, _ = buffer.shape
                # In a real Qt app, we would create the QImage here in the main thread
                # For now, we'll just store the raw buffer data.
                decoded_image = DecodedImage(
                    buffer=buffer.data,
                    width=w,
                    height=h,
                    bytes_per_line=w * 3,
                    format=None # Placeholder for QImage.Format.Format_RGB888
                )
                self.cache_put(index, decoded_image)
                log.debug(f"Successfully decoded and cached image at index {index}")
                return index
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
