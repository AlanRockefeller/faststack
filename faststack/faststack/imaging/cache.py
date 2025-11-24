"""Byte-aware LRU cache for storing decoded image data (CPU and GPU)."""

import logging
from typing import Any, Callable

from cachetools import LRUCache

log = logging.getLogger(__name__)

class ByteLRUCache(LRUCache):
    """An LRU Cache that respects the size of its items in bytes."""
    def __init__(self, max_bytes: int, size_of: Callable[[Any], int] = len):
        super().__init__(maxsize=max_bytes, getsizeof=size_of)
        log.info(f"Initialized byte-aware LRU cache with {max_bytes / 1024**2:.2f} MB capacity.")

    def __setitem__(self, key, value):
        # Before adding a new item, we might need to evict others
        # This is handled by the parent class, which will call popitem if needed
        super().__setitem__(key, value)
        log.debug(f"Cached item '{key}'. Cache size: {self.currsize / 1024**2:.2f} MB")

    def popitem(self):
        """Extend popitem to log eviction."""
        key, value = super().popitem()
        log.debug(f"Evicted item '{key}' to free up space. Cache size: {self.currsize / 1024**2:.2f} MB")
        # In a real Qt app, `value` would be a tuple like (numpy_buffer, qtexture_id)
        # and we would explicitly free the GPU texture here.
        return key, value

# Example usage:
def get_decoded_image_size(item) -> int:
    """Calculates the size of a decoded image tuple (buffer, qimage)."""
    # In this simplified example, we only store the buffer.
    # In the full app, this would also account for the QImage/QTexture.
    from faststack.models import DecodedImage
    if isinstance(item, DecodedImage):
        # Handle both numpy arrays and memoryview buffers
        if hasattr(item.buffer, 'nbytes'):
            return item.buffer.nbytes
        elif isinstance(item.buffer, (bytes, bytearray)):
            return len(item.buffer)
        else:
            # Fallback: estimate from dimensions (more accurate for image buffers than sys.getsizeof)
            bytes_per_pixel = getattr(item, 'channels', 4)  # Default to RGBA
            return item.width * item.height * bytes_per_pixel
        
    return 1 # Should not happen
