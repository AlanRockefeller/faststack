"""Byte-aware LRU cache for storing decoded image data (CPU and GPU)."""

import inspect
import logging
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional, Union
import time
import threading
from cachetools import Cache, LRUCache

log = logging.getLogger(__name__)


def get_decoded_image_size(item) -> int:
    """Calculates the size of a DecodedImage object or similar buffer-holding object."""
    # Use duck typing to support DecodedImage and similar objects (e.g. in tests)
    if hasattr(item, "buffer"):
        # Handle both numpy arrays and memoryview buffers
        if hasattr(item.buffer, "nbytes"):
            return item.buffer.nbytes
        elif isinstance(item.buffer, (bytes, bytearray)):
            return len(item.buffer)
        else:
            # Fallback: estimate from dimensions (more accurate for image buffers than sys.getsizeof)
            width = getattr(item, "width", 0)
            height = getattr(item, "height", 0)
            if width <= 0 or height <= 0:
                return 1  # No usable dimensions

            if hasattr(item, "bytes_per_line") and item.bytes_per_line > 0:
                bytes_per_pixel = item.bytes_per_line // width
            else:
                bytes_per_pixel = 4  # Default to RGBA

            # Guard against 0 (e.g. bytes_per_line=0) which would yield size 0
            # and break cache accounting.  Don't clamp to 4 — that overcounts
            # legitimate RGB (3 Bpp) buffers and causes premature evictions.
            bytes_per_pixel = max(1, min(bytes_per_pixel, 16))

            return width * height * bytes_per_pixel

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
        on_evict: Optional[Callable[..., None]] = None,
    ):
        super().__init__(maxsize=max_bytes, getsizeof=size_of)
        self._on_evict_arity = self._detect_arity(on_evict)
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
        # Flag: True when __delitem__ is being called from __setitem__'s capacity
        # eviction path (popitem), as opposed to targeted removal (pop_path, evict_paths).
        self._pressure_eviction_active = False
        self._pressure_eviction_owner: Optional[int] = None
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

    @staticmethod
    def _detect_arity(callback: Optional[Callable]) -> int:
        """Detect whether callback accepts 2 args (key, value) or 3 (key, value, info)."""
        if callback is None:
            return 2
        try:
            sig = inspect.signature(callback)
            # Count parameters that can accept positional args
            positional = sum(
                1
                for p in sig.parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
                and p.default is inspect.Parameter.empty
            )
            return 3 if positional >= 3 else 2
        except (ValueError, TypeError):
            return 2

    def _fire_evict(self, key: Any, value: Any, info: dict) -> None:
        """Invoke on_evict, dispatching by detected arity."""
        if not self.on_evict:
            return
        if self._on_evict_arity >= 3:
            self.on_evict(key, value, info)
        else:
            self.on_evict(key, value)

    def _build_eviction_info(self, reason: str, pre_usage: int) -> dict:
        """Build eviction context dict captured at eviction time (inside lock)."""
        return {
            "reason": reason,
            "usage_bytes": pre_usage,
            "max_bytes": self.maxsize,
            "entry_count": len(self),
            "thread_id": threading.get_ident(),
        }

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
                expired = [
                    p for p, expiry in self._tombstone_expiry.items() if now > expiry
                ]
                for p in expired:
                    self._tombstones.discard(p)
                    del self._tombstone_expiry[p]

                for prefix in self._tombstones:
                    if key_str.startswith(prefix):
                        log.debug(f"Refusing to cache tombstoned key: {key}")
                        return

            # Check if this is a replacement to handle its callback if __delitem__ isn't called.
            _MISSING = object()
            old_value = _MISSING
            if self.on_evict and key in self:
                try:
                    # Cache.__getitem__ reads the value without updating LRU order,
                    # avoiding a subtle issue where peeking could change which item
                    # gets evicted when the subsequent __setitem__ triggers LRU eviction.
                    old_value = Cache.__getitem__(self, key)
                except KeyError:
                    old_value = _MISSING

            # Before adding a new item, we might need to evict others
            # This is handled by the parent class, which will call popitem if needed.
            # We wrap the call to super().__setitem__ to capture all eviction
            # callbacks triggered by popitem() -> __delitem__().
            self._pending_callbacks = pending_callbacks
            self._pending_callbacks_owner = threading.get_ident()
            # Mark that any __delitem__ calls from here are capacity-pressure evictions
            self._pressure_eviction_active = True
            self._pressure_eviction_owner = threading.get_ident()
            try:
                super().__setitem__(key, value)

                # If it was a replacement, we must ensure an eviction callback is added
                # for the old value, because cachetools.__setitem__ for replacements
                # does not call __delitem__ (it just overwrites the dict entry).
                if old_value is not _MISSING and self.on_evict:
                    info = self._build_eviction_info("replace", self.currsize)
                    info["inserting_key"] = str(key)

                    def _replace_cb(k=key, v=old_value, _info=info):
                        self._fire_evict(k, v, _info)

                    pending_callbacks.append(_replace_cb)
            finally:
                self._pressure_eviction_active = False
                self._pressure_eviction_owner = None
                self._pending_callbacks = None
                self._pending_callbacks_owner = None

            log.debug(
                f"Cached item '{key}'. Cache size: {self.currsize / 1024**2:.2f} MB"
            )

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

    def __delitem__(self, key):
        """Thread-safe delete with eviction callback."""
        callback = None
        with self._lock:
            # Peek at value before deletion for the callback.
            # Use Cache.__getitem__ to avoid LRU order mutation (harmless since
            # the key is about to be deleted, but avoids surprising side-effects
            # on eviction order of remaining items).
            try:
                value = Cache.__getitem__(self, key)
            except KeyError:
                raise KeyError(key) from None

            # Capture usage BEFORE deletion for accurate thrashing detection.
            # After super().__delitem__, currsize will already be decremented.
            pre_usage = self.currsize

            # Determine eviction reason based on calling context.
            # This is a heuristic: _pressure_eviction_active is only True when
            # __setitem__ is executing super().__setitem__(), which calls
            # popitem() when currsize + new_size > maxsize (cachetools LRU).
            # Any other path into __delitem__ — pop_path(), direct del,
            # popitem() from manual cache resize — is classified as "manual"
            # by design, since those are intentional removals, not capacity
            # pressure indicating the cache is too small.
            is_pressure = (
                self._pressure_eviction_active
                and threading.get_ident() == self._pressure_eviction_owner
            )
            reason = "pressure" if is_pressure else "manual"

            super().__delitem__(key)
            log.debug(
                f"Removed item '{key}'. Cache size: {self.currsize / 1024**2:.2f} MB"
            )

            if self.on_evict:
                info = self._build_eviction_info(reason, pre_usage)

                def _callback_func(k=key, v=value, _info=info):
                    self._fire_evict(k, v, _info)

                # If we are inside a call that defers callbacks (like __setitem__ or evict_paths),
                # append to the shared list.
                if (
                    self._pending_callbacks is not None
                    and threading.get_ident() == self._pending_callbacks_owner
                ):
                    self._pending_callbacks.append(_callback_func)
                else:
                    callback = _callback_func

        if callback:
            try:
                callback()
            except Exception:
                log.exception("Error in eviction callback")

    def get(self, key, default=None):
        """Thread-safe get."""
        with self._lock:
            return super().get(key, default)

    def clear(self):
        """Clear cache without triggering eviction callbacks.

        Uses _pending_callbacks discard pattern (same as evict_paths) rather
        than setting on_evict=None, which would race with closures that read
        on_evict outside the lock on other threads.
        """
        with self._lock:
            _discard: list[Callable[[], None]] = []
            self._pending_callbacks = _discard
            self._pending_callbacks_owner = threading.get_ident()
            try:
                super().clear()
            finally:
                self._pending_callbacks = None
                self._pending_callbacks_owner = None

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
        pending_callbacks = []
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

            self._pending_callbacks = pending_callbacks
            self._pending_callbacks_owner = threading.get_ident()
            try:
                for k in keys_to_remove:
                    self.pop(k, None)
            finally:
                self._pending_callbacks = None
                self._pending_callbacks_owner = None

        # Execute all captured eviction callbacks OUTSIDE the lock
        for callback in pending_callbacks:
            try:
                callback()
            except Exception:
                log.exception("Error in eviction callback")

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

            # 4. Remove keys — capture eviction callbacks but discard them,
            #    since these are intentional removals, not LRU pressure.
            #    We use _pending_callbacks to collect (and then drop) rather than
            #    setting on_evict=None, which would race with closures that read
            #    on_evict outside the lock.
            removed_bytes = 0
            _discard = []
            self._pending_callbacks = _discard
            self._pending_callbacks_owner = threading.get_ident()
            try:
                for k in keys_to_remove:
                    val = self.pop(k, None)
                    if val is not None:
                        try:
                            size = get_decoded_image_size(val)
                        except Exception:
                            size = 0  # Fallback
                        removed_bytes += size
            finally:
                self._pending_callbacks = None
                self._pending_callbacks_owner = None
            # _discard is intentionally not executed

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
