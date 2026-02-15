"""Byte-aware LRU cache for storing decoded image data (CPU and GPU)."""

import logging
from pathlib import Path
from typing import Any, Callable, Optional, Union
import time
import threading
from cachetools import LRUCache

log = logging.getLogger(__name__)


def get_decoded_image_size(item) -> int:
    """Calculates the size of a DecodedImage object."""
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


class ByteLRUCache(LRUCache):
    """An LRU Cache that respects the size of its items in bytes."""

    def __init__(
        self,
        max_bytes: int,
        size_of: Callable[[Any], int] = get_decoded_image_size,
        on_evict: Optional[Callable[[Any, Any], None]] = None,
    ):
        super().__init__(maxsize=max_bytes, getsizeof=size_of)
        self.on_evict = on_evict
        # RLock is required: __setitem__ holds _lock and calls super().__setitem__(),
        # which may call our overridden popitem() for LRU eviction.  A non-reentrant
        # Lock would deadlock on that re-entry.
        self._lock = threading.RLock()
        # Tombstones to prevent race conditions where a deleted image is re-cached
        # by a lingering background thread.
        # Set of prefixes that are currently "tombstoned" (forbidden from caching).
        self._tombstones: set[str] = set()
        self._tombstone_expiry: dict[str, float] = {}
        self._pending_callbacks: Optional[list[Callable[[], None]]] = None
        self._pending_callbacks_owner: Optional[int] = None
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
        pending_callbacks = []
        with self._lock:
            # Check tombstones - prevent caching if key starts with a tombstoned prefix
            # This is critical for preventing "ghost" images after deletion
            if self._tombstones:
                key_str = str(key)
                # Fast check: iterate tombstones (usually very few)
                # Remove expired tombstones lazily
                now = time.monotonic()
                expired = [p for p, expiry in self._tombstone_expiry.items() if now > expiry]
                for p in expired:
                    self._tombstones.discard(p)
                    del self._tombstone_expiry[p]

                for prefix in self._tombstones:
                    if key_str.startswith(prefix):
                        log.debug(f"Refusing to cache tombstoned key: {key}")
                        return

            # Before adding a new item, we might need to evict others
            # This is handled by the parent class, which will call popitem if needed.
            # We override popitem to capture callbacks if they occur during this call.
            self._pending_callbacks = pending_callbacks
            self._pending_callbacks_owner = threading.get_ident()
            try:
                super().__setitem__(key, value)
            finally:
                self._pending_callbacks = None
                self._pending_callbacks_owner = None
            
            log.debug(f"Cached item '{key}'. Cache size: {self.currsize / 1024**2:.2f} MB")

        # Execute all captured eviction callbacks OUTSIDE the lock
        for callback in pending_callbacks:
            try:
                callback()
            except Exception:
                log.exception("Error in eviction callback")

    def __getitem__(self, key):
        """Thread-safe access (updates LRU order)."""
        with self._lock:
            return super().__getitem__(key)

    def __contains__(self, key):
        """Thread-safe existence check."""
        with self._lock:
            return super().__contains__(key)

    def get(self, key, default=None):
        """Thread-safe get."""
        with self._lock:
            return super().get(key, default)

    def popitem(self):
        """Extend popitem to log eviction.

        Lock note: acquires _lock, which is safe because _lock is an RLock.
        When called from __setitem__ -> super().__setitem__() (LRU eviction),
        the lock is already held by the same thread; RLock allows re-entry.
        Eviction callbacks are deferred via _pending_callbacks when inside
        __setitem__, and always execute OUTSIDE _lock.
        """
        with self._lock:
            key, value = super().popitem()
            log.debug(
                f"Evicted item '{key}'. Cache size after eviction: {self.currsize / 1024**2:.2f} MB"
            )
            
            # Create a bound callback for this specific item
            callback = None
            if self.on_evict:
                # Capture key/value in closure
                # We use a default arg to bind immediate values
                def _callback_func(k=key, v=value):
                    if self.on_evict:
                        self.on_evict(k, v)
                
                # If we are inside a __setitem__ call on the SAME thread, defer the callback
                if self._pending_callbacks is not None and threading.get_ident() == self._pending_callbacks_owner:
                    self._pending_callbacks.append(_callback_func)
                    callback = None  # Already deferred
                else:
                    callback = _callback_func

        # Execute callback OUTSIDE the lock to avoid deadlocks/reentrancy
        if callback:
            try:
                callback()
            except Exception:
                log.exception("Error in eviction callback")

        # In a real Qt app, `value` would be a tuple like (numpy_buffer, qtexture_id)
        # and we would explicitly free the GPU texture here.
        return key, value

    def clear(self):
        """Clear cache without triggering eviction callbacks."""
        # Temporarily disable callback to prevent "thrashing" warnings during mass clear
        with self._lock:
            saved_callback = self.on_evict
            self.on_evict = None
            try:
                super().clear()
            finally:
                self.on_evict = saved_callback

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
        with self._lock:
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
            log.debug(
                f"Invalidated {len(keys_to_remove)} cache entries for path: {path}"
            )

    def evict_paths(self, paths: list[Union[Path, str]]):
        """Targeted eviction of all keys starting with given paths.
        
        Args:
            paths: List of Path objects or strings.
        """
        if not paths:
            return

        # 1. Build set of prefixes (using forward slashes to match build_cache_key)
        prefixes = []
        for p in paths:
            if isinstance(p, Path):
                # Path.as_posix() returns pure forward slashes
                prefix = p.as_posix()
            else:
                # String might be Windows-style, normalize to forward slashes
                prefix = str(p).replace("\\", "/")
            
            # Append separator to ensure we match directory/file boundary
            # e.g. "foo.jpg" -> "foo.jpg::"
            prefixes.append(f"{prefix}::")

        if not prefixes:
            return

        with self._lock:
            # 2. Add tombstones immediately to block re-insertion
            now = time.monotonic()
            ttl = 5.0  # Block re-caching for 5 seconds
            for prefix in prefixes:
                self._tombstones.add(prefix)
                self._tombstone_expiry[prefix] = now + ttl

            # 3. Optimistic scan: iterate keys once and collect matches
            # Convert prefixes to tuple for fast startswith check
            prefix_tuple = tuple(prefixes)
            
            keys_to_remove = []
            for key in list(self.keys()):
                # Keys are strings like "path/to/file.jpg::0"
                if str(key).startswith(prefix_tuple):
                    keys_to_remove.append(key)

            # 4. Remove keys
            removed_bytes = 0
            for k in keys_to_remove:
                # Use super().pop to avoid re-acquiring our lock / calling our overridden pop logic.
                val = super().pop(k, None)
                if val is not None:
                    try:
                        size = get_decoded_image_size(val)
                    except Exception:
                        size = 0  # Fallback
                    removed_bytes += size

        if keys_to_remove:
            log.info(
                f"Evicted {len(keys_to_remove)} entries ({removed_bytes / 1024**2:.2f} MB) for {len(paths)} paths"
            )


def build_cache_key(image_path: Union[Path, str], display_generation: int) -> str:
    """Builds a stable cache key that survives list reordering."""
    if isinstance(image_path, Path):
        path_str = image_path.as_posix()
    else:
        path_str = str(image_path).replace("\\", "/")
    return f"{path_str}::{display_generation}"
