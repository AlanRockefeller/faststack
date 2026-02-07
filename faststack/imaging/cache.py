"""Byte-aware LRU cache for storing decoded image data (CPU and GPU)."""

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union

from cachetools import LRUCache

log = logging.getLogger(__name__)


class ByteLRUCache(LRUCache):
    """An LRU Cache that respects the size of its items in bytes."""

    def __init__(
        self,
        max_bytes: int,
        size_of: Callable[[Any], int] = len,
        on_evict: Optional[Callable[[], None]] = None,
    ):
        super().__init__(maxsize=max_bytes, getsizeof=size_of)
        self.on_evict = on_evict
        log.info(
            f"Initialized byte-aware LRU cache with {max_bytes / 1024**2:.2f} MB capacity."
        )

    @property
    def max_bytes(self) -> int:
        """Get the maximum cache size in bytes."""
        return self.maxsize

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        """Set the maximum cache size in bytes."""
        v = max(0, int(value))
        self.maxsize = v
        log.debug(f"Cache max_bytes updated to {v / 1024**2:.2f} MB")

    def __setitem__(self, key, value):
        # Before adding a new item, we might need to evict others
        # This is handled by the parent class, which will call popitem if needed
        super().__setitem__(key, value)
        log.debug(f"Cached item '{key}'. Cache size: {self.currsize / 1024**2:.2f} MB")

    def popitem(self):
        """Extend popitem to log eviction."""
        key, value = super().popitem()
        log.debug(
            f"Evicted item '{key}'. Cache size after eviction: {self.currsize / 1024**2:.2f} MB"
        )

        if self.on_evict:
            self.on_evict()

        # In a real Qt app, `value` would be a tuple like (numpy_buffer, qtexture_id)
        # and we would explicitly free the GPU texture here.
        return key, value

    def clear(self):
        """Clear cache without triggering eviction callbacks."""
        # Temporarily disable callback to prevent "thrashing" warnings during mass clear
        callback = self.on_evict
        self.on_evict = None
        try:
            super().clear()
        finally:
            self.on_evict = callback

    def pop_path(self, path: Union[Path, str]):
        """Targeted invalidation of all generations for a given path.
        
        Hardened to handle both Path objects and string keys, and resolved paths.
        Expected type: Union[Path, str].
        """
        targets = {path, str(path), str(path).replace("\\", "/")}
        try:
            # Handle Path objects and ensure we check the resolved variant
            p = Path(path)
            resolved = p.resolve()
            targets.update({resolved, str(resolved), resolved.as_posix()})
        except (OSError, ValueError, TypeError):
            pass

        keys_to_remove = []
        # Use list(self.keys()) to avoid mutation during iteration
        for key in list(self.keys()):
            key_str = str(key)
            # Match exact path or path::generation pattern
            for t in targets:
                t_str = str(t)
                if key_str == t_str or key_str.startswith(f"{t_str}::"):
                    keys_to_remove.append(key)
                    break
        
        for k in keys_to_remove:
            self.pop(k, None)
        
        if keys_to_remove:
            log.debug(f"Invalidated {len(keys_to_remove)} cache entries for path: {path}")


def get_decoded_image_size(item) -> int:
    """Calculates the size of a decoded image tuple (buffer, qimage)."""
    # In this simplified example, we only store the buffer.
    # In the full app, this would also account for the QImage/QTexture.
    from faststack.models import DecodedImage

    if isinstance(item, DecodedImage):
        # Handle both numpy arrays and memoryview buffers
        if hasattr(item.buffer, "nbytes"):
            return item.buffer.nbytes
        elif isinstance(item.buffer, (bytes, bytearray)):
            return len(item.buffer)
        else:
            # Fallback: estimate from dimensions (more accurate for image buffers than sys.getsizeof)
            bytes_per_pixel = getattr(item, "channels", 4)  # Default to RGBA
            return item.width * item.height * bytes_per_pixel

    log.warning(
        f"Unexpected item type in cache: {type(item)}. Returning estimated size of 1."
    )
    return 1  # Should not happen


def build_cache_key(image_path: Union[Path, str], display_generation: int) -> str:
    """Builds a stable cache key that survives list reordering."""
    if isinstance(image_path, Path):
        path_str = image_path.as_posix()
    else:
        path_str = str(image_path)
    return f"{path_str}::{display_generation}"
