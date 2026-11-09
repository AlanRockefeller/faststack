"""Loupe image display: decode gating, the non-blocking loupe source, and the
provider's decode entry points.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.
"""

import concurrent.futures
import logging
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Slot,
)

from faststack.controller.common import app_debug_mode
from faststack.imaging.cache import (
    build_cache_key,
)
from faststack.models import (
    DecodedImage,
)
from faststack.ui.provider import select_loupe_preview_buffer

log = logging.getLogger(__name__)


class LoupeDisplayMixin:
    def _loupe_decode_allowed(self) -> bool:
        """Predicate to check if full-resolution decoding is allowed."""
        # Only decode full-res when we are in loupe mode and the folder is loaded.
        return (not self._is_grid_view_active) and self._folder_loaded

    # ---- Non-blocking loupe source -----------------------------------------
    #
    # The loupe Image loads synchronously, so ImageProvider.requestImage runs
    # on the GUI thread and get_decoded_image blocks it on a cache miss. Rather
    # than hand QML a source that is not decoded yet, currentImageSource keeps
    # returning the source QML already shows while the target decodes in the
    # background, then publishes the target once it is cached. Adjacent arrow
    # navigation already works this way (_request_paced_navigation_step); this
    # covers jumps, zoom, resize, refresh and folder load.

    def _clear_published_loupe_source(self) -> None:
        """Forget the held source (grid view, folder change)."""
        self._published_loupe_source = ""
        self._published_loupe_source_dir = None
        self._loupe_source_failed_target = None
        self._drop_loupe_source_pending()

    def _drop_loupe_source_pending(self) -> None:
        if self._loupe_source_pending is None:
            return
        # The decode keeps running and still lands in the cache; only its
        # completion callback is ignored from now on.
        self._loupe_source_pending = None
        if self.ui_state:
            self.ui_state.isLoupeImagePending = False
            if self.debug_cache:
                self.ui_state.isDecoding = False

    def _publish_loupe_source(self, target: str) -> str:
        self._published_loupe_source = target
        self._published_loupe_source_dir = self.image_dir
        self._loupe_source_failed_target = None
        self._loupe_source_attempts = ("", 0)
        self._drop_loupe_source_pending()
        return target

    def _loupe_source_path(self, index: int, seq: Optional[int]) -> Path:
        """The file the provider will serve for ``index`` (mirrors requestImage)."""
        if self.view_override_path and index == self.current_index:
            path: object = self.view_override_path
        else:
            path = self.image_files[index].path
        return Path(self._nav_expected_path(seq, path))

    def _loupe_source_ready(self, index: int, seq: Optional[int]) -> bool:
        """True when the provider can serve ``index`` without decoding."""
        if not self._loupe_decode_allowed():
            return True  # The provider returns a placeholder without decoding.
        if not (0 <= index < len(self.image_files)):
            return True
        # Same policy as requestImage: a servable preview buffer, or a live
        # preview that get_decoded_image returns without decoding.
        selection = select_loupe_preview_buffer(self, index, self.ui_refresh_generation)
        if selection.kind is not None or selection.has_current_live_preview:
            return True
        path = self._loupe_source_path(index, seq)
        _, _, display_gen = self.get_display_info()
        return any(
            self.image_cache.get(build_cache_key(path, display_gen, quality))
            is not None
            for quality in ("fast", "cover")
        )

    def _schedule_loupe_source_decode(
        self, target: str, index: int, seq: Optional[int]
    ) -> bool:
        """Start the background decode a held source waits on."""
        path = self._loupe_source_path(index, seq)
        try:
            future = self.prefetcher.submit_task(
                index,
                self.prefetcher.generation,
                priority=True,
                override_path=path,
                quality="fast",
                nav_seq=seq,
            )
        except Exception:
            log.exception("Background loupe decode submit failed for index %d", index)
            return False
        if future is None or future.done():
            # Already finished (cached or failed): let the provider handle it.
            return False

        pending = {"target": target, "future": future}
        self._loupe_source_pending = pending
        self._record_nav_cache_outcome(seq, self.ui_refresh_generation, "miss-async")
        self.ui_state.isLoupeImagePending = True
        if self.debug_cache:
            self.ui_state.isDecoding = True

        def _on_done(_fut, *, request=pending):
            try:
                self._loupeSourceDecodeReady.emit(request)
            except RuntimeError:
                pass  # Controller already destroyed at shutdown.

        future.add_done_callback(_on_done)
        return True

    def _resolve_loupe_image_source(
        self, target: str, index: int, seq: Optional[int]
    ) -> str:
        """Return the provider URL the loupe should bind right now.

        ``target`` is the URL for the current image. It is returned when the
        provider can serve it without blocking; otherwise the previously
        published URL is returned and a background decode is started.
        """
        held = self._published_loupe_source
        if target == held:
            return target
        if held and self._published_loupe_source_dir != self.image_dir:
            held = ""
        if target == self._loupe_source_failed_target or self._loupe_source_ready(
            index, seq
        ):
            return self._publish_loupe_source(target)

        pending = self._loupe_source_pending
        if (
            pending is not None
            and pending["target"] == target
            and not pending["future"].done()
        ):
            return held
        if getattr(self, "_entering_loupe", False):
            # The grid-to-loupe switch is still being delivered to QML, and
            # the loupe may be about to report a new image-area size. A decode
            # started here would be for the old size and would occupy the
            # demand worker; _finish_entering_loupe() requests the right one
            # as soon as the switch has been delivered.
            return held
        if not self._schedule_loupe_source_decode(target, index, seq):
            return self._publish_loupe_source(target)
        return held

    @Slot(object)
    def _on_loupe_source_decode_ready(self, request) -> None:
        """A held source's decode finished: re-evaluate the loupe source."""
        if request is not self._loupe_source_pending or self._shutting_down:
            return
        self._drop_loupe_source_pending()
        target = request["target"]
        future = request["future"]
        # Re-evaluating publishes the target once it is cached, or schedules a
        # fresh decode when it is not (cancelled by a generation bump, result
        # rejected by the cache, display generation moved on). Count those
        # unproductive rounds and hand the target to the provider after a few
        # instead of looping.
        name, count = self._loupe_source_attempts
        count = count + 1 if name == target else 1
        self._loupe_source_attempts = (target, count)
        failed = count >= 3
        if not future.cancelled():
            try:
                failed = failed or not future.result()
            except Exception:  # noqa: BLE001 - the provider retry logs it
                failed = True
        if failed:
            self._loupe_source_failed_target = target
        self.ui_state.currentImageSourceChanged.emit()

    def _maybe_decode_current_image(
        self, reason: str = "", override_path: Optional[Path] = None
    ) -> None:
        """Trigger decode of current image if allowed."""
        if not self._loupe_decode_allowed():
            return

        # Log reason for debugging
        if app_debug_mode() and reason:
            log.debug("Triggering decode: %s", reason)

        # Trigger prefetch/decode for current index
        self._do_prefetch(self.current_index, override_path=override_path)

    def get_decoded_image(
        self,
        index: int,
        nav_seq: Optional[int] = None,
        request_generation: Optional[int] = None,
        requested_path: Optional[Path | str] = None,
    ) -> Optional[DecodedImage]:
        """Retrieves a decoded image, blocking until ready to ensure correct display.

        This blocks the UI thread on cache miss, but that's acceptable for an image viewer
        where users expect to see the correct image immediately. The prefetcher minimizes
        cache misses by decoding adjacent images in advance.
        """
        if not self._loupe_decode_allowed():
            if app_debug_mode():
                log.debug(
                    "get_decoded_image called but loupe decode not allowed (index=%d)",
                    index,
                )
            return None

        _t_start = time.perf_counter() if self.debug_cache_trace else 0.0
        if self.debug_cache_trace:
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} get_decoded_image: START index={index}"
            )

        if not self.image_files or index < 0 or index >= len(self.image_files):
            log.warning(
                "get_decoded_image called with empty image_files or out of bounds index."
            )
            return None

        if self._has_current_live_preview_for_index(index):
            self._record_nav_cache_outcome(
                nav_seq,
                request_generation,
                "live-preview",
            )
            return self._last_rendered_preview

        display_width, display_height, display_gen = self.get_display_info()

        # The provider passes the immutable path captured for this navigation.
        # If a watcher/sort/delete shifts the list while the request is in
        # flight, decode that exact file rather than whatever now occupies the
        # same numeric index. The provider still verifies returned provenance.
        current_override = None
        if requested_path is not None:
            image_path = Path(requested_path)
            current_override = image_path
        elif self.view_override_path and index == self.current_index:
            if app_debug_mode():
                log.debug(
                    "DISPLAY OVERRIDE kind=%s path=%s",
                    self.view_override_kind,
                    self.view_override_path,
                )
            image_path = Path(self.view_override_path)
            current_override = image_path
        else:
            image_path = self.image_files[index].path
        path_str = image_path.as_posix()
        preferred_quality = "fast" if self._navigation_fast_mode_active() else "cover"
        quality_order = (
            ("fast", "cover") if preferred_quality == "fast" else ("cover", "fast")
        )

        # Check cache. Use a single get() instead of `in` + `[]` so a concurrent
        # LRU eviction between the two lookups can't raise a KeyError here.
        decoded = None
        cache_key = None
        for candidate_quality in quality_order:
            candidate_key = build_cache_key(
                image_path,
                display_gen,
                candidate_quality,
            )
            decoded = self.image_cache.get(candidate_key)
            if decoded is not None:
                cache_key = candidate_key
                break
        if decoded is not None:
            self._record_nav_cache_outcome(
                nav_seq,
                request_generation,
                "hit",
                cache_key,
            )
            self.image_cache.hits += 1  # Increment hit counter
            self._update_cache_stats()  # Update UI with new stats
            with self._last_image_lock:
                self.last_displayed_image = decoded

            if self.debug_cache_trace:
                _t_end = time.perf_counter()
                log.info(
                    f"[DBGCACHE] {_t_end*1000:.3f} get_decoded_image: CACHE HIT index={index} total={(_t_end - _t_start)*1000:.2f}ms"
                )

            return decoded

        # A miss always requests the inexpensive display tier. Cover quality is
        # an asynchronous settled-image upgrade and never blocks navigation.
        decode_result_quality = (
            "fast" if display_width > 0 and display_height > 0 else "cover"
        )
        cache_key = build_cache_key(
            image_path,
            display_gen,
            decode_result_quality,
        )

        self.image_cache.misses += 1  # Increment miss counter
        self._record_nav_cache_outcome(
            nav_seq,
            request_generation,
            "miss",
            cache_key,
        )
        self._update_cache_stats()  # Update UI with new stats
        if self.debug_cache_trace:
            prefix = f"{path_str}::"
            cached_variants = [
                key[len(prefix) :]
                for key in self.image_cache.keys_snapshot()
                if key.startswith(prefix)
            ]
            cache_usage_gb = self.image_cache.currsize / (1024**3)
            _t_miss = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_miss*1000:.3f} get_decoded_image: CACHE MISS index={index} gen={display_gen} (after {(_t_miss - _t_start)*1000:.2f}ms)"
            )
            log.info(
                "Cache miss for %s (index=%d gen=%d). Cached variants: %s. Cache usage=%.2fGB entries=%d",
                image_path.name,
                index,
                display_gen,
                cached_variants or "none",
                cache_usage_gb,
                len(self.image_cache),
            )

        # Cache miss: need to decode synchronously to ensure correct image displays
        if app_debug_mode():
            decode_start = time.perf_counter()
            log.info(
                "Cache miss for index %d (gen: %d). Blocking decode.",
                index,
                display_gen,
            )

        # Show decoding indicator if debug cache is enabled
        if self.debug_cache:
            self.ui_state.isDecoding = True
            # Note: processEvents() caused crashes, so the indicator might not update immediately
            # QCoreApplication.processEvents()

        try:
            # A prefetch worker can populate the key between the first lookup
            # and this demand path (especially while debug logging is active).
            # Recheck synchronously so an already-ready image never waits
            # behind another executor queue item.
            decoded = self.image_cache.get(cache_key)
            if decoded is not None:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "late-hit",
                    cache_key,
                )
                with self._last_image_lock:
                    self.last_displayed_image = decoded
                if self.debug_cache_trace:
                    _t_decoded = time.perf_counter()
                    log.info(
                        f"[DBGCACHE] {_t_decoded*1000:.3f} get_decoded_image: LATE CACHE HIT index={index} total={(_t_decoded - _t_start)*1000:.2f}ms"
                    )
                return decoded

            # Submit with priority=True to cancel pending prefetch tasks and free up workers.
            # nav_seq threads this demand decode's navigation correlation id to
            # the worker so its timing attaches to the exact frame, not by index.
            future = self.prefetcher.submit_task(
                index,
                self.prefetcher.generation,
                priority=True,
                override_path=current_override,
                quality="fast",
                nav_seq=nav_seq,
            )
            if not future:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "miss-submit-failed",
                    cache_key,
                )
                return None

            try:
                # The existing frame remains on screen while this synchronous
                # provider request is in progress. Do not time out and publish
                # a placeholder (or the wrong previous image) while the target
                # decode is still legitimately running.
                result = future.result()
            except concurrent.futures.CancelledError:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "miss-canceled",
                    cache_key,
                )
                log.debug("Decode cancelled for index %d", index)
                return None
            except Exception:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "miss-error",
                    cache_key,
                )
                log.exception("Error decoding image at index %d", index)
                return None

            if not result:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "miss-no-result",
                    cache_key,
                )
                if app_debug_mode():
                    log.debug("Decode returned no result for index %d", index)
                return None

            decoded_path, decoded_display_gen = result
            cache_key = build_cache_key(
                decoded_path,
                decoded_display_gen,
                decode_result_quality,
            )
            decoded = self.image_cache.get(cache_key)
            if decoded is not None:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "miss-decoded",
                    cache_key,
                )
                with self._last_image_lock:
                    self.last_displayed_image = decoded
                if app_debug_mode():
                    elapsed = time.perf_counter() - decode_start
                    log.info("Decoded image %d in %.3fs", index, elapsed)
                if self.debug_cache_trace:
                    _t_decoded = time.perf_counter()
                    log.info(
                        f"[DBGCACHE] {_t_decoded*1000:.3f} get_decoded_image: DECODED index={index} total={(_t_decoded - _t_start)*1000:.2f}ms"
                    )
                return decoded
            else:
                self._record_nav_cache_outcome(
                    nav_seq,
                    request_generation,
                    "miss-cache-rejected",
                    cache_key,
                )
                if app_debug_mode():
                    log.debug(
                        "Decode finished but cache_key missing (index=%d, key=%s)",
                        index,
                        cache_key,
                    )
                return None
        finally:
            # Hide decoding indicator
            if self.debug_cache:
                self.ui_state.isDecoding = False

    def _get_decoded_image_safe(self, index: int) -> Optional[DecodedImage]:
        """Thread-safe version of get_decoded_image for background workers.

        Does NOT update UI iteration or access QObjects.
        """
        if not self.image_files or index < 0 or index >= len(self.image_files):
            return None

        # Lock to ensure thread safety when reading shared state if necessary (though simple reads are usually safe)
        # However, get_display_info reads 'self.is_zoomed' which is fine.
        # Accessing self.image_files is safe as long as list isn't cleared concurrently,
        # which only happens on directory change/refresh on main thread.
        # Since we are in a worker, there's a small race risk if directory changes *while* we run,
        # but the worker would likely just fail gracefully or get an old image.

        _, _, display_gen = self.get_display_info()
        try:
            image_path = self.image_files[index].path
        except IndexError:
            return None

        # Check cache (thread-safe read). We don't update stats/hits here to
        # avoid racing the GUI-thread counters.
        for quality in ("fast", "cover"):
            decoded = self.image_cache.get(
                build_cache_key(image_path, display_gen, quality)
            )
            if decoded is not None:
                return decoded

        # Cache miss: decode synchronously (in this worker thread)
        try:
            # Submit with priority=True
            # Note: prefetcher.submit_task logic needs to be thread-safe.
            # Assuming futures dict access in submit_task handles strict GIL/thread safety or we might need locks there.
            # But usually submitting to Executor is thread safe.
            # The danger is 'self.futures' management in Prefetcher.
            future = self.prefetcher.submit_task(
                index,
                self.prefetcher.generation,
                priority=True,
                quality="fast",
            )
            if future:
                try:
                    result = future.result(timeout=5.0)
                except concurrent.futures.TimeoutError:
                    log.warning(
                        "Timeout decoding image at index %d (background)", index
                    )
                    return None
                except concurrent.futures.CancelledError:
                    log.debug(
                        "Decode cancelled for image at index %d (background)", index
                    )
                    return None

                if result:
                    decoded_path, decoded_display_gen = result
                    # Re-verify key
                    for quality in ("fast", "cover"):
                        decoded = self.image_cache.get(
                            build_cache_key(
                                decoded_path,
                                decoded_display_gen,
                                quality,
                            )
                        )
                        if decoded is not None:
                            return decoded
        except Exception:
            log.exception("_get_decoded_image_safe failed for index %d", index)

        return None
