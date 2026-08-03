"""QML Image Provider and application state bridge."""

import collections
import logging
import math
import threading
import time
from numbers import Real
from pathlib import Path

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from faststack.config import config
from faststack.imaging.cache import build_cache_key
from faststack.io.utils import normalize_path_key

# Try to import QColorSpace if available (Qt 6+)
try:
    from PySide6.QtGui import QColorSpace

    HAS_COLOR_SPACE = True
except ImportError:
    HAS_COLOR_SPACE = False

log = logging.getLogger(__name__)


class ImageProvider(QQuickImageProvider):
    def __init__(self, app_controller):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self.app_controller = app_controller
        self.placeholder = QImage(256, 256, QImage.Format.Format_RGB888)
        self.placeholder.fill(Qt.GlobalColor.darkGray)
        # Transparent 1x1 fallback for mask overlays (prevents grey-screen bug)
        self._transparent = QImage(1, 1, QImage.Format.Format_ARGB32)
        self._transparent.fill(Qt.GlobalColor.transparent)
        # Keepalive queue to prevent GC of buffers currently in use by QImage
        # Increased to 128 to prevent crashes during rapid scrolling/thrashing where
        # QML might hold onto textures slightly longer than the Python GC expects.
        self._keepalive = collections.deque(maxlen=128)
        # Lock to protect keepalive deque from concurrent access by QML rendering threads
        self._keepalive_lock = threading.Lock()
        # Pre-built sRGB color space, reused across requests instead of
        # constructing a new QColorSpace on every requestImage() call.
        self._srgb_color_space = (
            QColorSpace(QColorSpace.NamedColorSpace.SRgb) if HAS_COLOR_SPACE else None
        )

    def _fallback_image(self) -> QImage:
        return self.placeholder.copy()

    def _log_provider_fallback(
        self,
        request_id: str,
        reason: str,
        *,
        stale: bool,
        index: object = None,
        expected_path: object = None,
        exc_info: bool = False,
    ) -> None:
        if stale:
            log.debug(
                "Ignoring stale image provider request %s: %s", request_id, reason
            )
            return
        # Invariant: a placeholder is being returned for the CURRENT frame while
        # a decode for it is still in flight -- the user sees gray unnecessarily.
        future_note = ""
        if isinstance(index, int):
            prefetcher = getattr(self.app_controller, "prefetcher", None)
            has_active = getattr(prefetcher, "has_active_future", None)
            try:
                if callable(has_active) and has_active(index, expected_path):
                    future_note = " (WARNING: decode future still active)"
            except Exception:
                future_note = ""
        log.warning(
            "Image provider could not satisfy current request %s: %s%s",
            request_id,
            reason,
            future_note,
            exc_info=exc_info,
        )

    def _record_provider(
        self,
        nav_seq: object,
        index: int,
        gen: object,
        elapsed_ms: float,
        image_data: object,
        *,
        outcome: str,
        expected_path: object = None,
        provenance_kind: str = "display",
        placeholder_reason: str | None = None,
        future_active: bool = False,
    ) -> None:
        """Report the provider round-trip + served-pixel provenance by seq."""
        record_timing = getattr(self.app_controller, "_record_provider_timing", None)
        if not callable(record_timing):
            return
        served_quality = None
        served_path = None
        worker = None
        if image_data is not None:
            served_quality = getattr(image_data, "quality", None)
            served_path = getattr(image_data, "source_path", None)
            worker = getattr(image_data, "decode_trace", None)
        try:
            record_timing(
                nav_seq,
                index,
                gen,
                elapsed_ms,
                outcome=outcome,
                expected_path=str(expected_path) if expected_path is not None else None,
                provenance_kind=provenance_kind,
                served_quality=served_quality,
                served_path=served_path,
                placeholder_reason=placeholder_reason,
                future_active=future_active,
                worker=worker if isinstance(worker, dict) else None,
            )
        except Exception:
            log.debug("Navigation provider telemetry failed", exc_info=True)

    def _has_active_future(
        self,
        index: object,
        expected_path: object = None,
    ) -> bool:
        if not isinstance(index, int):
            return False
        prefetcher = getattr(self.app_controller, "prefetcher", None)
        has_active = getattr(prefetcher, "has_active_future", None)
        try:
            return bool(callable(has_active) and has_active(index, expected_path))
        except Exception:
            return False

    def requestImage(self, id: str, size: object, requestedSize: object) -> QImage:
        """Handles image requests from QML."""
        _debug = getattr(self.app_controller, "debug_cache", False)
        # Per-request [DBGCACHE] lines are trace-level; --debugcache keeps only
        # summaries + the one consolidated [NAVTRACE] line.
        _trace = _debug and getattr(self.app_controller, "debug_cache_trace", False)
        _t_start = time.perf_counter() if _debug else 0.0
        if _trace:
            log.info(f"[DBGCACHE] {_t_start*1000:.3f} requestImage: START id={id}")

        if not id:
            return self._fallback_image()

        # Bound before the parse so the terminal fallbacks can attribute a
        # returned placeholder to its navigation seq.
        index = None
        nav_seq = None
        gen = None
        expected_path = None
        request_is_stale = False
        try:
            # Handle mask overlay requests
            if id.startswith("mask_overlay/"):
                overlay = getattr(
                    self.app_controller.ui_state, "_darken_overlay_image", None
                )
                if overlay is not None and not overlay.isNull():
                    return overlay.copy()
                return self._transparent

            # Parse index and optional generation + navigation seq
            parts = id.split("/")
            try:
                index = int(parts[0])
                gen = int(parts[1]) if len(parts) > 1 else None
                nav_seq = int(parts[2]) if len(parts) > 2 else None
            except ValueError as e:
                log.warning("Invalid image ID requested from QML: %s. Error: %s", id, e)
                return self._fallback_image()

            current_index = getattr(self.app_controller, "current_index", None)
            current_generation = getattr(
                self.app_controller,
                "ui_refresh_generation",
                None,
            )
            ui_state = getattr(self.app_controller, "ui_state", None)
            grid_active_value = getattr(ui_state, "isGridViewActive", False)
            grid_active = (
                grid_active_value if isinstance(grid_active_value, bool) else False
            )
            index_is_current = not isinstance(current_index, int) or (
                index == current_index
            )
            generation_is_current = (
                gen is None
                or not isinstance(
                    current_generation,
                    int,
                )
                or (gen == current_generation)
            )
            request_is_stale = (
                grid_active or not index_is_current or not generation_is_current
            )

            image_files = getattr(self.app_controller, "image_files", None)
            image_count = len(image_files) if isinstance(image_files, list) else None
            if image_count is not None and (index < 0 or index >= image_count):
                stale_bounds_request = request_is_stale or image_count == 0
                self._log_provider_fallback(
                    id,
                    f"index {index} outside image list of {image_count}",
                    stale=stale_bounds_request,
                )
                if _debug and not stale_bounds_request:
                    active = self._has_active_future(index, expected_path)
                    self._record_provider(
                        nav_seq,
                        index,
                        gen,
                        (time.perf_counter() - _t_start) * 1000.0,
                        None,
                        outcome="placeholder",
                        placeholder_reason="index outside image list",
                        future_active=active,
                    )
                return self._fallback_image()

            if isinstance(image_files, list):
                if (
                    getattr(self.app_controller, "view_override_path", None)
                    and index == current_index
                ):
                    expected_path = self.app_controller.view_override_path
                else:
                    expected_path = image_files[index].path
            nav_expected = getattr(
                self.app_controller,
                "_nav_expected_path",
                None,
            )
            if callable(nav_expected):
                expected_path = nav_expected(nav_seq, expected_path)

            # For a stale request, a provider URL still fully identifies its
            # content by index, so serving the real pixels for the requested
            # index is always correct — QML discards the reply if it no
            # longer wants it. Try a non-blocking cache lookup first; only
            # fall back to gray if nothing is cached. Never decode here.
            stale_cached_image_data = None
            if request_is_stale:
                try:
                    path = image_files[index].path
                    _, _, display_gen = self.app_controller.get_display_info()
                    for quality in ("fast", "cover"):
                        stale_cached_image_data = self.app_controller.image_cache.get(
                            build_cache_key(path, display_gen, quality)
                        )
                        if stale_cached_image_data is not None:
                            break
                except Exception:
                    stale_cached_image_data = None
                if stale_cached_image_data is None:
                    self._log_provider_fallback(
                        id,
                        (
                            "request no longer matches current image source "
                            f"(current index={current_index}, generation={current_generation})"
                        ),
                        stale=True,
                    )
                    return self._fallback_image()

            # If editor is open, use the background-rendered preview buffer
            # BUT only if the requested index matches the currently edited index!
            # AND the generation matches (to avoid stale frames during rotation/param changes)
            # FIX: If zoomed in, force full-res image instead of low-res preview

            # Also accept the editor-rendered preview when an auto-adjust
            # session is live (rapid '-' / '=' before the debounced save),
            # so the main loupe visibly tracks each keypress.
            has_active_auto_adjust = (
                getattr(self.app_controller, "_active_auto_adjust_state", None)
                is not None
            )
            # Also accept the editor-rendered preview when the live edit
            # session holds meaningful edits (e.g. a crop applied outside the
            # editor), so the main loupe reflects those edits immediately.
            has_current_live_preview = False
            live_preview_check = getattr(
                self.app_controller, "_has_current_live_preview_for_index", None
            )
            if callable(live_preview_check):
                try:
                    has_current_live_preview = bool(live_preview_check(index))
                except Exception:
                    has_current_live_preview = False
            # Whether there is a usable rendered preview buffer for this index.
            # The stored session key must match the current live edit session so
            # a preview from an old/replaced editor session can never be served.
            # This is independent of the meaningful-edits requirement, so a valid
            # editor-open preview displays without satisfying the stricter
            # _has_current_live_preview_for_index() check a second time.
            current_preview_provenance = None
            get_preview_provenance = getattr(
                self.app_controller,
                "_get_current_preview_session_provenance",
                None,
            )
            if callable(get_preview_provenance):
                try:
                    current_preview_provenance = get_preview_provenance()
                except Exception:
                    current_preview_provenance = None

            current_preview_session_key = (
                (
                    current_preview_provenance[0],
                    current_preview_provenance[1],
                )
                if current_preview_provenance is not None
                else None
            )
            current_compare_session_key = (
                tuple(current_preview_provenance)
                if current_preview_provenance is not None
                else None
            )

            has_valid_preview_buffer = (
                current_preview_session_key is not None
                and self.app_controller._last_rendered_preview is not None
                and self.app_controller._last_rendered_preview_index == index
                and getattr(
                    self.app_controller, "_last_rendered_preview_session_key", None
                )
                == current_preview_session_key
                and (
                    gen is None
                    or getattr(self.app_controller, "_last_rendered_preview_gen", None)
                    == gen
                )
            )

            # A committed live preview (session-key match) is sufficient on its
            # own. When the editor is open or an auto-adjust session is active we
            # also accept the editor-rendered preview, provided a valid buffer
            # exists for the current index.
            use_editor_preview = (
                (
                    self.app_controller.ui_state.isEditorOpen
                    or has_active_auto_adjust
                    or has_current_live_preview
                )
                and index == self.app_controller.current_index
                and not self.app_controller.ui_state.isZoomed
                and has_valid_preview_buffer
            )

            # Unlike use_editor_preview above, this intentionally has no
            # isZoomed check: while comparing against the original, showing
            # the low-res original preview is semantically correct even when
            # zoomed in, since the user is explicitly asking to see the
            # pre-edit image rather than a zoomed detail of the edited one.
            use_original_compare_preview = (
                getattr(self.app_controller, "_original_compare_active", False)
                and index == self.app_controller.current_index
                and current_compare_session_key is not None
                and self.app_controller._original_compare_preview is not None
                and self.app_controller._original_compare_index == index
                and getattr(
                    self.app_controller,
                    "_original_compare_session_key",
                    None,
                )
                == current_compare_session_key
                and (
                    gen is None
                    or getattr(self.app_controller, "_original_compare_gen", None)
                    == gen
                )
            )

            if _trace:
                _t_get = time.perf_counter()

            if use_original_compare_preview:
                image_data = self.app_controller._original_compare_preview
                buffer_expected_path = current_compare_session_key[0]
                provenance_kind = "original-compare-preview"
            elif use_editor_preview:
                image_data = self.app_controller._last_rendered_preview
                buffer_expected_path = current_preview_session_key[0]
                provenance_kind = "editor-preview"
            elif stale_cached_image_data is not None:
                # Stale request with a cache hit found above: use it directly
                # rather than triggering a (possibly blocking) decode.
                image_data = stale_cached_image_data
                buffer_expected_path = expected_path
                provenance_kind = "display"
            else:
                image_data = self.app_controller.get_decoded_image(
                    index,
                    nav_seq,
                    gen,
                    requested_path=expected_path,
                )
                buffer_expected_path = expected_path
                provenance_kind = "display"

            if _trace:
                _t_got = time.perf_counter()
                log.info(
                    f"[DBGCACHE] {_t_got*1000:.3f} requestImage: got image_data in {(_t_got - _t_get)*1000:.2f}ms"
                )

            if image_data:
                served_path = getattr(image_data, "source_path", None)
                if served_path is not None and buffer_expected_path is not None:
                    expected_key = normalize_path_key(buffer_expected_path)
                    served_key = normalize_path_key(served_path)
                    if expected_key != served_key:
                        reason = (
                            f"wrong decoded buffer: expected {buffer_expected_path}, "
                            f"served {served_path}"
                        )
                        self._log_provider_fallback(
                            id,
                            reason,
                            stale=request_is_stale,
                            index=index,
                            expected_path=buffer_expected_path,
                        )
                        if _debug and not request_is_stale:
                            active = self._has_active_future(
                                index,
                                buffer_expected_path,
                            )
                            self._record_provider(
                                nav_seq,
                                index,
                                gen,
                                (time.perf_counter() - _t_start) * 1000.0,
                                image_data,
                                outcome="wrong-image-blocked",
                                expected_path=buffer_expected_path,
                                provenance_kind=provenance_kind,
                                placeholder_reason=reason,
                                future_active=active,
                            )
                        return self._fallback_image()

                # Handle format being None (from prefetcher) or missing
                fmt = getattr(image_data, "format", None)
                if fmt is None:
                    fmt = QImage.Format.Format_RGB888

                qimg = QImage(
                    image_data.buffer,
                    image_data.width,
                    image_data.height,
                    image_data.bytes_per_line,
                    fmt,
                )
                if qimg.isNull():
                    active = self._has_active_future(index, buffer_expected_path)
                    self._log_provider_fallback(
                        id,
                        "decoded buffer produced a null QImage",
                        stale=request_is_stale,
                        index=index,
                        expected_path=buffer_expected_path,
                    )
                    if _debug and not request_is_stale:
                        self._record_provider(
                            nav_seq,
                            index,
                            gen,
                            (time.perf_counter() - _t_start) * 1000.0,
                            image_data,
                            outcome="placeholder",
                            expected_path=buffer_expected_path,
                            provenance_kind=provenance_kind,
                            placeholder_reason="decoded buffer produced a null QImage",
                            future_active=active,
                        )
                    return self._fallback_image()

                # Detach from Python buffer to prevent ownership issues and force proper texture upload
                # OPTIMIZATION: Only do this expensive copy when serving the live editor preview,
                # where we need to detach from the shared memory buffer that might change.
                # For standard browsing/prefetch, the buffer is stable enough.
                if (
                    self.app_controller.ui_state.isEditorOpen
                    or has_active_auto_adjust
                    or has_current_live_preview
                    or use_original_compare_preview
                ) and index == self.app_controller.current_index:
                    qimg = qimg.copy()
                else:
                    # SAFETY: Keep a reference to the underlying buffer to prevent garbage collection
                    # while Qt holds the QImage. QImage created from bytes does NOT own the data.
                    # Lock protects against concurrent access from QML rendering threads.
                    with self._keepalive_lock:
                        self._keepalive.append(image_data.buffer)

                # Set sRGB color space for proper color management (if available)
                # Skip this when using ICC mode - pixels are already in monitor space
                color_mode = config.get("color", "mode", fallback="none").lower()
                if HAS_COLOR_SPACE and color_mode != "icc":
                    try:
                        qimg.setColorSpace(self._srgb_color_space)
                    except (RuntimeError, ValueError) as e:
                        log.warning(f"Failed to set color space: {e}")
                elif color_mode == "icc":
                    log.debug(
                        "ICC mode: skipping Qt color space (pixels already in monitor space)"
                    )

                if _debug:
                    _t_end = time.perf_counter()
                    if _trace:
                        log.info(
                            f"[DBGCACHE] {_t_end*1000:.3f} requestImage: DONE id={id} total={(_t_end - _t_start)*1000:.2f}ms"
                        )
                    # Attribute this provider round-trip to its navigation seq,
                    # recording the provenance of the pixels actually served so
                    # the wrong-image invariant can compare requested vs served.
                    decoded_placeholder = bool(
                        getattr(image_data, "is_placeholder", False)
                    )
                    self._record_provider(
                        nav_seq,
                        index,
                        gen,
                        (_t_end - _t_start) * 1000.0,
                        image_data,
                        outcome=(
                            "decoded-placeholder" if decoded_placeholder else "image"
                        ),
                        expected_path=buffer_expected_path,
                        provenance_kind=provenance_kind,
                        placeholder_reason=getattr(
                            image_data,
                            "placeholder_reason",
                            None,
                        ),
                    )

                # When we took the copy() branch above, qimg owns its own
                # buffer and the source buffer needs no further protection.
                # Otherwise the keepalive append above keeps it alive.
                return qimg

        except (ValueError, IndexError) as e:
            log.warning("Invalid image ID requested from QML: %s. Error: %s", id, e)
            return self._fallback_image()
        except Exception:
            active = self._has_active_future(index, expected_path)
            self._log_provider_fallback(
                id,
                "unexpected provider error",
                stale=request_is_stale,
                index=index,
                expected_path=expected_path,
                exc_info=True,
            )
            if _debug and not request_is_stale and isinstance(index, int):
                self._record_provider(
                    nav_seq,
                    index,
                    gen,
                    (time.perf_counter() - _t_start) * 1000.0,
                    None,
                    outcome="placeholder",
                    expected_path=expected_path,
                    placeholder_reason="unexpected provider error",
                    future_active=active,
                )
            return self._fallback_image()

        self._log_provider_fallback(
            id,
            "decode returned no image data",
            stale=request_is_stale,
            index=index,
            expected_path=expected_path,
        )
        # A placeholder is being returned for this navigation; record it so the
        # present ack marks it as a placeholder rather than a real first frame.
        if _debug and not request_is_stale:
            active = self._has_active_future(index, expected_path)
            self._record_provider(
                nav_seq,
                index,
                gen,
                (time.perf_counter() - _t_start) * 1000.0,
                None,
                outcome="placeholder",
                expected_path=expected_path,
                placeholder_reason="decode returned no image data",
                future_active=active,
            )
        return self._fallback_image()


class UIState(QObject):
    """Manages the state exposed to the QML user interface."""

    # Signals
    currentIndexChanged = Signal()
    imageCountChanged = Signal()
    currentImageSourceChanged = Signal()
    metadataChanged = Signal()
    themeChanged = Signal()
    preloadingStateChanged = Signal()
    preloadProgressChanged = Signal()

    # Recycle Bin Signals
    recycleBinStatsTextChanged = Signal()
    recycleBinDetailedTextChanged = Signal()
    hasRecycleBinItemsChanged = Signal()

    isZoomedChanged = Signal()
    statusMessageChanged = Signal()  # New signal for status messages
    statusMessageColorChanged = Signal()  # Optional override color for the status message
    resetZoomPanRequested = Signal()  # Signal to tell QML to reset zoom/pan
    absoluteZoomRequested = Signal(
        float
    )  # New: Request absolute zoom level (1.0, 2.0, etc.)
    stackSummaryChanged = Signal()  # Signal for stack summary updates
    filterStringChanged = Signal()  # Signal for filter string updates
    colorModeChanged = Signal()  # Signal for color mode updates
    sortModeChanged = Signal()  # Signal for sort mode updates
    saturationFactorChanged = Signal()  # Signal for saturation factor updates
    awbModeChanged = Signal()
    awbStrengthChanged = Signal()
    awbWarmBiasChanged = Signal()
    awbTintBiasChanged = Signal()
    awbLumaLowerBoundChanged = Signal()
    awbLumaUpperBoundChanged = Signal()
    awbRgbLowerBoundChanged = Signal()
    awbRgbUpperBoundChanged = Signal()
    currentDirectoryChanged = Signal()  # Signal when working directory changes
    stackDirectorySwitchChanged = Signal()
    autoLevelClippingThresholdChanged = Signal(float)
    autoLevelStrengthChanged = Signal(float)
    autoLevelStrengthAutoChanged = Signal(bool)
    autoVibranceEnabledChanged = Signal(bool)
    autoLevelMidtoneChanged = Signal(bool)
    autoLevelMidtoneTargetChanged = Signal(float)
    autoLevelChannelBudgetChanged = Signal(float)
    levelsSoftKneeChanged = Signal(bool)
    exportDitherChanged = Signal(bool)
    awbTintDampChanged = Signal(float)
    # Image Editor Signals
    is_editor_open_changed = Signal(bool)
    is_editor_expanded_changed = Signal(bool)
    editorImageChanged = (
        Signal()
    )  # New signal for when the image loaded in editor changes
    originalCompareActiveChanged = Signal(bool)
    is_cropping_changed = Signal(bool)
    is_crop_rotating_changed = Signal(bool)

    is_histogram_visible_changed = Signal(bool)
    histogram_data_changed = Signal()
    highlightStateChanged = Signal()  # New signal for highlight analysis updates
    brightness_changed = Signal(float)
    contrast_changed = Signal(float)
    saturation_changed = Signal(float)
    white_balance_by_changed = Signal(float)
    white_balance_mg_changed = Signal(float)
    aspect_ratio_names_changed = Signal(list)
    current_aspect_ratio_index_changed = Signal(int)
    current_crop_box_changed = Signal(
        tuple
    )  # (left, top, right, bottom) normalized to 0-1000
    crop_rotation_changed = Signal(float)
    sharpness_changed = Signal(float)
    rotation_changed = Signal(int)
    exposure_changed = Signal(float)
    highlights_changed = Signal(float)
    shadows_changed = Signal(float)
    vibrance_changed = Signal(float)
    vignette_changed = Signal(float)
    blacks_changed = Signal(float)
    whites_changed = Signal(float)
    clarity_changed = Signal(float)
    texture_changed = Signal(float)

    # Per-hue saturation (color mix) signals
    color_sat_red_changed = Signal(float)
    color_sat_orange_changed = Signal(float)
    color_sat_yellow_changed = Signal(float)
    color_sat_green_changed = Signal(float)
    color_sat_aqua_changed = Signal(float)
    color_sat_blue_changed = Signal(float)
    color_sat_purple_changed = Signal(float)
    color_sat_magenta_changed = Signal(float)

    # Background Darkening Signals
    is_darkening_changed = Signal(bool)
    darken_overlay_generation_changed = Signal()
    darken_overlay_visible_changed = Signal(bool)
    darken_amount_changed = Signal(float)
    darken_edge_protection_changed = Signal(float)
    darken_subject_protection_changed = Signal(float)
    darken_feather_changed = Signal(float)
    darken_dark_range_changed = Signal(float)
    darken_neutrality_changed = Signal(float)
    darken_expand_contract_changed = Signal(float)
    darken_auto_edges_changed = Signal(float)
    darken_mode_changed = Signal(str)
    darken_brush_radius_changed = Signal(float)

    # Debug Cache Signals
    debugCacheChanged = Signal(bool)
    cacheStatsChanged = Signal(str)
    isDecodingChanged = Signal(bool)
    debugModeChanged = Signal(bool)  # General debug mode signal
    debugThumbTimingChanged = Signal(bool)  # Thumbnail pipeline timing
    isDialogOpenChanged = Signal(bool)  # New signal for dialog state
    editSourceModeChanged = Signal(str)  # Notify when JPEG/RAW mode changes
    rawDevelopmentStateChanged = Signal()
    saveBehaviorMessageChanged = Signal()  # Signal for save behavior message updates
    isSavingChanged = Signal(bool)  # Signal for save operation in progress
    batchAutoLevelsProgressChanged = Signal()
    batchAutoLevelsActiveChanged = Signal()
    autoAddEditedToBatchChanged = Signal()

    # Variant badges
    variantBadgesChanged = Signal()
    variantSaveHintChanged = Signal()

    def __init__(self, app_controller, clock_func=None):
        super().__init__()
        self.app_controller = app_controller
        self._clock = clock_func or time.monotonic
        self._last_prefetch_data = (
            None  # (startIndex, endIndex, maxCount, visibleStartIndex, visibleEndIndex)
        )
        self._last_prefetch_time = 0
        self._is_preloading = False
        self._preload_progress = 0
        # 1 = light, 0 = dark (controller will overwrite this on startup)
        self._theme = 1
        self._status_message = ""  # New private variable for status message
        self._status_message_color = ""  # "" means use the default text color
        # Image Editor State
        self._is_editor_open = False
        self._is_editor_expanded = False
        self._original_compare_active = False
        self._is_cropping = False
        self._is_crop_rotating = False
        self._is_histogram_visible = False
        self._histogram_data = {}  # Will be a dict with 'r', 'g', 'b' arrays
        self._brightness = 0.0
        self._contrast = 0.0
        self._saturation = 0.0
        self._white_balance_by = 0.0
        self._white_balance_mg = 0.0
        self._current_crop_box = (0, 0, 1000, 1000)
        self._crop_rotation = 0.0
        self._debug_mode = False
        self._aspect_ratio_names = [
            "Freeform",
            "1:1 (Square)",
            "4:5 (Portrait)",
            "1.91:1 (Landscape)",
            "16:9 (Wide)",
            "9:16 (Story)",
        ]
        self._current_aspect_ratio_index = 0
        self._sharpness = 0.0
        self._rotation = 0
        self._exposure = 0.0
        self._highlights = 0.0
        self._shadows = 0.0
        self._vibrance = 0.0
        self._vignette = 0.0
        self._blacks = 0.0
        self._whites = 0.0
        self._clarity = 0.0
        self._texture = 0.0
        self._color_sat_red = 0.0
        self._color_sat_orange = 0.0
        self._color_sat_yellow = 0.0
        self._color_sat_green = 0.0
        self._color_sat_aqua = 0.0
        self._color_sat_blue = 0.0
        self._color_sat_purple = 0.0
        self._color_sat_magenta = 0.0

        # Background Darkening State
        self._is_darkening = False
        self._darken_overlay_visible = True
        self._darken_overlay_generation = 0
        self._darken_overlay_image = None  # QImage for mask overlay
        self._darken_amount = 0.5
        self._darken_edge_protection = 0.5
        self._darken_subject_protection = 0.5
        self._darken_feather = 0.5
        self._darken_dark_range = 0.5
        self._darken_neutrality = 0.5
        self._darken_expand_contract = 0.0
        self._darken_auto_edges = 0.0
        self._darken_mode = "assisted"
        self._darken_brush_radius = 0.03

        # Debug Cache State
        self._debug_cache = False
        self._cache_stats = ""
        self._is_decoding = False
        self._is_dialog_open = False
        self._is_saving = False  # Save operation in progress
        self._debug_thumb_timing = False
        self._batch_al_current = 0
        self._batch_al_total = 0
        self._batch_al_active = False
        self._auto_add_edited_to_batch = True  # Load from config in app_controller

        # Connect to controller's dialog state signal
        self.app_controller.dialogStateChanged.connect(self._on_dialog_state_changed)

        # Connect to controller's mode change signal
        # We need to ensure the signal exists on controller first (it does, I added it)
        if hasattr(self.app_controller, "editSourceModeChanged"):
            self.app_controller.editSourceModeChanged.connect(
                self.editSourceModeChanged
            )
            self.app_controller.editSourceModeChanged.connect(
                lambda _: self.saveBehaviorMessageChanged.emit()
            )
        if hasattr(self.app_controller, "rawDevelopmentStateChanged"):
            self.app_controller.rawDevelopmentStateChanged.connect(
                self.rawDevelopmentStateChanged
            )
            self.app_controller.rawDevelopmentStateChanged.connect(
                self.metadataChanged.emit
            )
            self.app_controller.rawDevelopmentStateChanged.connect(
                self.saveBehaviorMessageChanged.emit
            )

        # Connect batch auto levels progress signals
        if hasattr(self.app_controller, "batchAutoLevelsProgress"):
            self.app_controller.batchAutoLevelsProgress.connect(
                self._on_batch_al_progress
            )
        if hasattr(self.app_controller, "batchAutoLevelsFinished"):
            self.app_controller.batchAutoLevelsFinished.connect(
                self._on_batch_al_finished
            )

        # Ensure image source updates when switching grid/loupe
        self.isGridViewActiveChanged.connect(
            lambda _: self.currentImageSourceChanged.emit()
        )
        self.currentDirectoryChanged.connect(self.stackDirectorySwitchChanged)

    def _on_batch_al_progress(self, current: int, total: int):
        self._batch_al_current = current
        self._batch_al_total = total
        if not self._batch_al_active:
            self._batch_al_active = True
            self.batchAutoLevelsActiveChanged.emit()
        self.batchAutoLevelsProgressChanged.emit()

    def _on_batch_al_finished(self, processed: int, total: int):
        self._batch_al_active = False
        self._batch_al_current = 0
        self._batch_al_total = 0
        self.batchAutoLevelsActiveChanged.emit()
        self.batchAutoLevelsProgressChanged.emit()

    def _on_dialog_state_changed(self, is_open: bool):
        self.isDialogOpen = is_open

    # ---- THEME PROPERTY ----
    @Property(int, notify=themeChanged)
    def theme(self):
        return self._theme

    @theme.setter
    def theme(self, value: int):
        value = int(value)
        if value == self._theme:
            return
        self._theme = value
        self.themeChanged.emit()

    # ---- ZOOM ----
    @Property(bool, notify=isZoomedChanged)
    def isZoomed(self):
        return self.app_controller.is_zoomed

    @Slot(bool)
    def setZoomed(self, zoomed: bool):
        self.app_controller.set_zoomed(zoomed)

    @Slot(float)
    def request_absolute_zoom(self, scale):
        """Request the UI to set zoom to an absolute scale (1.0 = 100%)."""
        self.absoluteZoomRequested.emit(scale)

    # ---- PRELOADING ----
    @Property(bool, notify=preloadingStateChanged)
    def isPreloading(self):
        return self._is_preloading

    @isPreloading.setter
    def isPreloading(self, value):
        if self._is_preloading != value:
            self._is_preloading = value
            self.preloadingStateChanged.emit()

    @Property(int, notify=preloadProgressChanged)
    def preloadProgress(self):
        return self._preload_progress

    @preloadProgress.setter
    def preloadProgress(self, value):
        if self._preload_progress != value:
            self._preload_progress = value
            self.preloadProgressChanged.emit()

    # ---- IMAGE / METADATA ----
    @Property(int, notify=currentIndexChanged)
    def currentIndex(self):
        return self.app_controller.current_index

    @Property(int, notify=imageCountChanged)
    def imageCount(self):
        return len(self.app_controller.image_files)

    @Property(str, notify=currentImageSourceChanged)
    def currentImageSource(self):
        # Prevent QML from requesting full-res images when in grid view
        if self.isGridViewActive:
            return ""
        # Some list/filter/delete paths change current_index directly instead
        # of going through _set_current_index. Mint the diagnostic seq lazily
        # as this displayed source is evaluated, so every actual target change
        # still receives an immutable path identity.
        ensure_nav = getattr(
            self.app_controller,
            "_ensure_nav_record_for_current_target",
            None,
        )
        if callable(ensure_nav):
            ensure_nav()
        # The nav seq is embedded so the provider/worker/present all share one
        # correlation id, and so returning to the same index/generation still
        # yields a distinct URL (guaranteeing a reload + a fresh present ack).
        seq = getattr(self.app_controller, "_current_nav_seq", 0)
        generation = self.app_controller.ui_refresh_generation
        note_requested = getattr(
            self.app_controller,
            "_note_nav_source_requested",
            None,
        )
        if callable(note_requested):
            note_requested(seq, generation)
        return (
            f"image://provider/{self.app_controller.current_index}"
            f"/{generation}/{seq}"
        )

    @Property(int, notify=currentImageSourceChanged)
    def currentNavSeq(self):
        """Navigation correlation id for the current image source.

        QML captures this when the loupe Image source changes and hands it back
        via notifyImageReady() once the frame reaches Image.Ready, so the
        controller can log target-index-changed -> frame-actually-rendered.
        """
        return getattr(self.app_controller, "_current_nav_seq", 0)

    @Property(int, notify=currentImageSourceChanged)
    def currentImageGeneration(self):
        """Generation embedded in the current provider URL."""
        return self.app_controller.ui_refresh_generation

    @Slot(int, int)
    def notifyImageReady(self, seq: int, generation: int):
        """The exact (seq, generation) provider source reached Image.Ready.

        Ready means loaded, not on-screen; the on-screen timestamp is taken by
        the controller from the window's frameSwapped signal.
        """
        handler = getattr(self.app_controller, "_on_frame_ready", None)
        if callable(handler):
            handler(seq, generation)

    @Property(int, notify=currentImageSourceChanged)
    def currentNativeImageWidth(self):
        return self.app_controller.get_current_display_native_size()[0]

    @Property(int, notify=currentImageSourceChanged)
    def currentNativeImageHeight(self):
        return self.app_controller.get_current_display_native_size()[1]

    @Property(str, notify=metadataChanged)
    def currentFilename(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("filename", "")

    @Property(str, notify=metadataChanged)
    def exifBrief(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("exif_brief", "")

    @Property(bool, notify=metadataChanged)
    def isStacked(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("stacked", False)

    @Property(str, notify=metadataChanged)
    def stackedDate(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("stacked_date", "")

    @Property(str, notify=metadataChanged)
    def stackInfoText(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("stack_info_text", "")

    @Property(bool, notify=metadataChanged)
    def isUploaded(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("uploaded", False)

    @Property(str, notify=metadataChanged)
    def uploadedDate(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("uploaded_date", "")

    @Property(bool, notify=metadataChanged)
    def isTodo(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("todo", False)

    @Property(str, notify=metadataChanged)
    def todoDate(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("todo_date", "")

    @Property(str, notify=metadataChanged)
    def batchInfoText(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("batch_info_text", "")

    @Property(bool, notify=metadataChanged)
    def isEdited(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("edited", False)

    @Property(str, notify=metadataChanged)
    def editedDate(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("edited_date", "")

    @Property(bool, notify=metadataChanged)
    def isFavorite(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("favorite", False)

    @Property(bool, notify=metadataChanged)
    def isRestacked(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("restacked", False)

    @Property(str, notify=metadataChanged)
    def restackedDate(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("restacked_date", "")

    # --- RAW / True Headroom Support ---

    @Property(bool, notify=metadataChanged)
    def hasRaw(self):
        files = self.app_controller.image_files
        idx = self.app_controller.current_index
        if not files or not (0 <= idx < len(files)):
            return False
        return files[idx].has_raw

    @Property(bool, notify=metadataChanged)
    def hasWorkingTif(self):
        files = self.app_controller.image_files
        idx = self.app_controller.current_index
        if not files or not (0 <= idx < len(files)):
            return False
        return files[idx].has_working_tif

    @Slot()
    def enableRawEditing(self):
        """Switches to RAW editing mode."""
        if hasattr(self.app_controller, "enable_raw_editing"):
            self.app_controller.enable_raw_editing()

    @Property(bool, notify=editSourceModeChanged)
    def isRawActive(self):
        """Returns True if the editor is in RAW source mode."""
        if hasattr(self.app_controller, "current_edit_source_mode"):
            return self.app_controller.current_edit_source_mode == "raw"
        return False

    @Property(bool, notify=rawDevelopmentStateChanged)
    def isRawDeveloping(self):
        if hasattr(self.app_controller, "is_raw_developing_current"):
            return self.app_controller.is_raw_developing_current()
        return False

    @Slot(result=bool)
    def load_image_for_editing(self):
        """Loads the currently viewed image into the editor."""
        return self.app_controller.load_image_for_editing()

    @Slot()
    def developRaw(self):
        # Legacy support
        self.app_controller.develop_raw_for_current_image()

    @Property(str, notify=stackSummaryChanged)
    def stackSummary(self):
        if not self.app_controller.stacks:
            return "No stacks defined."
        lines = [f"Found {len(self.app_controller.stacks)} stacks:", ""]
        for i, (start, end) in enumerate(self.app_controller.stacks):
            count = end - start + 1
            lines.append(f"Stack {i + 1}: {count} photos (indices {start}-{end})")
        return "\n".join(lines) + "\n"

    @Property(str, notify=saveBehaviorMessageChanged)
    def saveBehaviorMessage(self):
        """Returns a string describing what files will be affected by saving."""
        if not hasattr(self.app_controller, "current_edit_source_mode"):
            return ""

        if getattr(self.app_controller, "view_override_kind", None) == "developed":
            return "Editing: developed JPG (saves in-place to the developed file)"

        if self.app_controller.current_edit_source_mode == "raw":
            if self.isRawDeveloping:
                return "Editing: RAW (developing working .tif...)"
            if self.hasWorkingTif:
                return "Editing: RAW (writes working .tif + creates -developed.jpg; original JPG untouched)"
            return "Editing: RAW selected (develop RAW before saving)"
        return "Editing: JPEG (will overwrite JPG)"

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self):
        return self._status_message

    @statusMessage.setter
    def statusMessage(self, value: str):
        if self._status_message != value:
            self._status_message = value
            self.statusMessageChanged.emit()

    @Property(str, notify=statusMessageColorChanged)
    def statusMessageColor(self):
        return self._status_message_color

    @statusMessageColor.setter
    def statusMessageColor(self, value: str):
        if self._status_message_color != value:
            self._status_message_color = value
            self.statusMessageColorChanged.emit()

    @Property(str, notify=variantSaveHintChanged)
    def variantSaveHint(self):
        """Returns a hint message when saving from a variant."""
        if hasattr(self.app_controller, "get_variant_save_hint"):
            return self.app_controller.get_variant_save_hint()
        return ""

    @Property(str, notify=filterStringChanged)
    def filterString(self):
        """Returns the current filter string (empty if no filter active)."""
        return self.app_controller.get_filter_string()

    @Property("QVariantList", notify=filterStringChanged)
    def filterFlags(self):
        """The list of currently active flag filters (e.g. ["favorite"])."""
        return list(self.app_controller.get_filter_flags())

    @Property(str, notify=colorModeChanged)
    def colorMode(self):
        """Returns the current color mode."""
        return self.app_controller.get_color_mode()

    @Property(str, notify=sortModeChanged)
    def sortMode(self):
        """Returns the current sort mode."""
        return self.app_controller.get_sort_mode()

    @Property(float, notify=saturationFactorChanged)
    def saturationFactor(self):
        """Returns the current saturation factor."""
        return self.app_controller.get_saturation_factor()

    @Property(str, notify=awbModeChanged)
    def awbMode(self):
        return self.app_controller.get_awb_mode()

    @awbMode.setter
    def awbMode(self, mode: str):
        if self.app_controller.get_awb_mode() == mode:
            return
        self.app_controller.set_awb_mode(mode)
        self.awbModeChanged.emit()

    @Property(float, notify=awbStrengthChanged)
    def awbStrength(self):
        return self.app_controller.get_awb_strength()

    @awbStrength.setter
    def awbStrength(self, value: float):
        if self.app_controller.get_awb_strength() == value:
            return
        self.app_controller.set_awb_strength(value)
        self.awbStrengthChanged.emit()

    @Property(int, notify=awbWarmBiasChanged)
    def awbWarmBias(self):
        return self.app_controller.get_awb_warm_bias()

    @awbWarmBias.setter
    def awbWarmBias(self, value: int):
        if self.app_controller.get_awb_warm_bias() == value:
            return
        self.app_controller.set_awb_warm_bias(value)
        self.awbWarmBiasChanged.emit()

    @Property(int, notify=awbTintBiasChanged)
    def awbTintBias(self):
        return self.app_controller.get_awb_tint_bias()

    @awbTintBias.setter
    def awbTintBias(self, value: int):
        if self.app_controller.get_awb_tint_bias() == value:
            return
        self.app_controller.set_awb_tint_bias(value)
        self.awbTintBiasChanged.emit()

    @Property(int, notify=awbLumaLowerBoundChanged)
    def awbLumaLowerBound(self):
        return self.app_controller.get_awb_luma_lower_bound()

    @awbLumaLowerBound.setter
    def awbLumaLowerBound(self, value: int):
        if self.app_controller.get_awb_luma_lower_bound() == value:
            return
        self.app_controller.set_awb_luma_lower_bound(value)
        self.awbLumaLowerBoundChanged.emit()

    @Property(int, notify=awbLumaUpperBoundChanged)
    def awbLumaUpperBound(self):
        return self.app_controller.get_awb_luma_upper_bound()

    @awbLumaUpperBound.setter
    def awbLumaUpperBound(self, value: int):
        if self.app_controller.get_awb_luma_upper_bound() == value:
            return
        self.app_controller.set_awb_luma_upper_bound(value)
        self.awbLumaUpperBoundChanged.emit()

    @Property(int, notify=awbRgbLowerBoundChanged)
    def awbRgbLowerBound(self):
        return self.app_controller.get_awb_rgb_lower_bound()

    @awbRgbLowerBound.setter
    def awbRgbLowerBound(self, value: int):
        if self.app_controller.get_awb_rgb_lower_bound() == value:
            return
        self.app_controller.set_awb_rgb_lower_bound(value)
        self.awbRgbLowerBoundChanged.emit()

    @Property(int, notify=awbRgbUpperBoundChanged)
    def awbRgbUpperBound(self):
        return self.app_controller.get_awb_rgb_upper_bound()

    @awbRgbUpperBound.setter
    def awbRgbUpperBound(self, value: int):
        if self.app_controller.get_awb_rgb_upper_bound() == value:
            return
        self.app_controller.set_awb_rgb_upper_bound(value)
        self.awbRgbUpperBoundChanged.emit()

    @Property(str, notify=currentDirectoryChanged)
    def currentDirectory(self):
        """Returns the path of the current working directory."""
        return str(self.app_controller.image_dir)

    @Property(bool, notify=stackDirectorySwitchChanged)
    def isInStackInputDirectory(self):
        return self.app_controller.is_in_stack_input_directory()

    @Property(bool, notify=stackDirectorySwitchChanged)
    def stackDirectorySwitchVisible(self):
        return self.app_controller.stack_directory_switch_visible()

    @Property(bool, notify=metadataChanged)
    def isStackedJpg(self):
        """Returns True if the current image is a stacked JPG."""
        return self.currentFilename.lower().endswith(" stacked.jpg")

    # --- Slots for QML to call ---
    @Slot()
    def nextImage(self):
        self.app_controller.next_image()

    @Slot()
    def nextImageBy10(self):
        self.app_controller.next_image_by_10()

    @Slot()
    def prevImage(self):
        self.app_controller.prev_image()

    @Slot()
    def prevImageBy10(self):
        self.app_controller.prev_image_by_10()

    @Slot(bool)
    def launch_helicon(self, use_raw: bool = True):
        self.app_controller.launch_helicon(use_raw)

    @Slot()
    def clear_all_stacks(self):
        self.app_controller.clear_all_stacks()

    @Slot()
    def clear_all_batches(self):
        self.app_controller.clear_all_batches()

    @Slot()
    def addFavoritesToBatch(self):
        self.app_controller.add_favorites_to_batch()

    @Slot()
    def addUploadedToBatch(self):
        self.app_controller.add_uploaded_to_batch()

    @Slot()
    def addEditedToBatch(self):
        self.app_controller.add_edited_to_batch()

    @Slot()
    def jumpToLastUploaded(self):
        self.app_controller.jump_to_last_uploaded()

    @Slot()
    def switchStackInputDirectory(self):
        self.app_controller.switch_stack_input_directory()

    @Slot(result=str)
    def get_helicon_path(self):
        return self.app_controller.get_helicon_path()

    @Slot(str)
    def set_helicon_path(self, path):
        self.app_controller.set_helicon_path(path)

    @Slot(result=str)
    def get_photoshop_path(self):
        return self.app_controller.get_photoshop_path()

    @Slot(str)
    def set_photoshop_path(self, path):
        self.app_controller.set_photoshop_path(path)

    @Slot(result=str)
    def get_rawtherapee_path(self):
        return self.app_controller.get_rawtherapee_path()

    @Slot(str)
    def set_rawtherapee_path(self, path):
        self.app_controller.set_rawtherapee_path(path)

    @Slot(result=str)
    def get_raw_source_dir(self):
        return self.app_controller.get_raw_source_dir()

    @Slot(str)
    def set_raw_source_dir(self, path):
        self.app_controller.set_raw_source_dir(path)
        self.stackDirectorySwitchChanged.emit()

    @Slot(result=str)
    def get_secondary_raw_source_dir(self):
        return self.app_controller.get_secondary_raw_source_dir()

    @Slot(str)
    def set_secondary_raw_source_dir(self, path):
        self.app_controller.set_secondary_raw_source_dir(path)
        self.stackDirectorySwitchChanged.emit()

    @Slot(str, result=str)
    def open_file_dialog(self, current_path):
        return self.app_controller.open_file_dialog(current_path)

    @Slot(str, result=bool)
    def check_executable_path(self, path):
        return self.app_controller.check_executable_path(path)

    @Slot(result=float)
    def get_cache_size(self):
        return self.app_controller.get_cache_size()

    @Slot(result=float)
    def get_cache_usage_gb(self):
        return self.app_controller.get_cache_usage_gb()

    @Slot(float)
    def set_cache_size(self, size):
        self.app_controller.set_cache_size(size)

    @Slot(result=int)
    def get_prefetch_radius(self):
        return self.app_controller.get_prefetch_radius()

    @Slot(int)
    def set_prefetch_radius(self, radius):
        self.app_controller.set_prefetch_radius(radius)

    @Slot(result=int)
    def get_navigation_rate_fps(self):
        return self.app_controller.get_navigation_rate_fps()

    @Slot(int)
    def set_navigation_rate_fps(self, fps):
        self.app_controller.set_navigation_rate_fps(fps)

    @Slot(result=str)
    def get_held_navigation_quality(self):
        return self.app_controller.get_held_navigation_quality()

    @Slot(str)
    def set_held_navigation_quality(self, quality):
        self.app_controller.set_held_navigation_quality(quality)

    @Slot(result=int)
    def get_theme(self):
        # this lets QML ask the controller, but the real binding is uiState.theme
        return self.app_controller.get_theme()

    @Slot(int)
    def set_theme(self, theme_index):
        # delegate to controller so it can save to config
        self.app_controller.set_theme(theme_index)

    @Slot(result=str)
    def get_default_directory(self):
        return self.app_controller.get_default_directory()

    @Slot(str)
    def set_default_directory(self, path):
        self.app_controller.set_default_directory(path)

    @Slot(result=str)
    def get_optimize_for(self):
        return self.app_controller.get_optimize_for()

    @Slot(str)
    def set_optimize_for(self, optimize_for):
        self.app_controller.set_optimize_for(optimize_for)

    @Slot(str, result=str)
    def open_directory_dialog(self, current_path):
        return self.app_controller.open_directory_dialog(current_path)

    @Slot(result=str)
    def get_current_version(self):
        return self.app_controller.get_current_version()

    @Slot(result=str)
    def get_readme_text(self):
        return self.app_controller.get_readme_text()

    @Slot(result=bool)
    def get_update_check_enabled(self):
        return self.app_controller.get_update_check_enabled()

    @Slot(bool)
    def set_update_check_enabled(self, enabled):
        self.app_controller.set_update_check_enabled(enabled)

    @Slot(result=bool)
    def get_auto_update_enabled(self):
        return self.app_controller.get_auto_update_enabled()

    @Slot(bool)
    def set_auto_update_enabled(self, enabled):
        self.app_controller.set_auto_update_enabled(enabled)

    @Slot()
    def check_for_updates(self):
        self.app_controller.check_for_updates(True)

    @Property(float, notify=autoLevelClippingThresholdChanged)
    def autoLevelClippingThreshold(self):
        return self.app_controller.get_auto_level_clipping_threshold()

    @autoLevelClippingThreshold.setter
    def autoLevelClippingThreshold(self, value):
        if self.app_controller.get_auto_level_clipping_threshold() == value:
            return
        self.app_controller.set_auto_level_clipping_threshold(value)
        self.autoLevelClippingThresholdChanged.emit(value)

    @Property(float, notify=autoLevelStrengthChanged)
    def autoLevelStrength(self):
        return self.app_controller.get_auto_level_strength()

    @autoLevelStrength.setter
    def autoLevelStrength(self, value):
        if self.app_controller.get_auto_level_strength() == value:
            return
        self.app_controller.set_auto_level_strength(value)
        self.autoLevelStrengthChanged.emit(value)

    @Property(bool, notify=autoLevelStrengthAutoChanged)
    def autoLevelStrengthAuto(self):
        return self.app_controller.get_auto_level_strength_auto()

    @autoLevelStrengthAuto.setter
    def autoLevelStrengthAuto(self, value):
        if self.app_controller.get_auto_level_strength_auto() == value:
            return
        self.app_controller.set_auto_level_strength_auto(value)
        self.autoLevelStrengthAutoChanged.emit(value)

    @Property(bool, notify=autoVibranceEnabledChanged)
    def autoVibranceEnabled(self):
        return self.app_controller.get_auto_vibrance_enabled()

    @autoVibranceEnabled.setter
    def autoVibranceEnabled(self, value):
        if self.app_controller.get_auto_vibrance_enabled() == value:
            return
        self.app_controller.set_auto_vibrance_enabled(value)
        self.autoVibranceEnabledChanged.emit(value)

    @Property(bool, notify=autoLevelMidtoneChanged)
    def autoLevelMidtone(self):
        return self.app_controller.get_auto_level_midtone()

    @autoLevelMidtone.setter
    def autoLevelMidtone(self, value):
        if self.app_controller.get_auto_level_midtone() == value:
            return
        self.app_controller.set_auto_level_midtone(value)
        self.autoLevelMidtoneChanged.emit(value)

    @Property(float, notify=autoLevelMidtoneTargetChanged)
    def autoLevelMidtoneTarget(self):
        return self.app_controller.get_auto_level_midtone_target()

    @autoLevelMidtoneTarget.setter
    def autoLevelMidtoneTarget(self, value):
        if self.app_controller.get_auto_level_midtone_target() == value:
            return
        self.app_controller.set_auto_level_midtone_target(value)
        self.autoLevelMidtoneTargetChanged.emit(value)

    @Property(float, notify=autoLevelChannelBudgetChanged)
    def autoLevelChannelBudget(self):
        return self.app_controller.get_auto_level_channel_budget()

    @autoLevelChannelBudget.setter
    def autoLevelChannelBudget(self, value):
        if self.app_controller.get_auto_level_channel_budget() == value:
            return
        self.app_controller.set_auto_level_channel_budget(value)
        self.autoLevelChannelBudgetChanged.emit(value)

    @Property(bool, notify=levelsSoftKneeChanged)
    def levelsSoftKnee(self):
        return self.app_controller.get_levels_soft_knee()

    @levelsSoftKnee.setter
    def levelsSoftKnee(self, value):
        if self.app_controller.get_levels_soft_knee() == value:
            return
        self.app_controller.set_levels_soft_knee(value)
        self.levelsSoftKneeChanged.emit(value)

    @Property(bool, notify=exportDitherChanged)
    def exportDither(self):
        return self.app_controller.get_export_dither()

    @exportDither.setter
    def exportDither(self, value):
        if self.app_controller.get_export_dither() == value:
            return
        self.app_controller.set_export_dither(value)
        self.exportDitherChanged.emit(value)

    @Property(float, notify=awbTintDampChanged)
    def awbTintDamp(self):
        return self.app_controller.get_awb_tint_damp()

    @awbTintDamp.setter
    def awbTintDamp(self, value):
        if self.app_controller.get_awb_tint_damp() == value:
            return
        self.app_controller.set_awb_tint_damp(value)
        self.awbTintDampChanged.emit(value)

    @Slot()
    def open_folder(self):
        self.app_controller.open_folder()

    @Slot()
    def preloadAllImages(self):
        self.app_controller.preload_all_images()

    @Slot()
    def stack_source_raws(self):
        self.app_controller.stack_source_raws()

    @Slot(str, "QVariantList")
    def applyFilter(self, filter_string: str, filter_flags=None):
        """Applies a filter string and/or flag filters to the image list."""
        flags = list(filter_flags) if filter_flags else []
        self.app_controller.apply_filter(filter_string, filter_flags=flags)

    @Slot(str)
    def toggleFilterFlag(self, flag: str):
        """Toggle a single flag in the "Show Only" view filter.

        Preserves any active filter string and other flag filters; only the
        given *flag* is added or removed.
        """
        flags = list(self.app_controller.get_filter_flags())
        filter_string = self.app_controller.get_filter_string()
        if flag in flags:
            flags.remove(flag)
        else:
            flags.append(flag)
        self.app_controller.apply_filter(filter_string, filter_flags=flags)

    @Slot(int, int)
    def onDisplaySizeChanged(self, width: int, height: int):
        self.app_controller.on_display_size_changed(width, height)

    @Slot()
    def resetZoomPan(self):
        """Triggers a reset of zoom and pan in QML."""
        self.resetZoomPanRequested.emit()

    # --- Image Editor Properties ---

    @Property(bool, notify=is_editor_open_changed)
    def isEditorOpen(self) -> bool:
        return self._is_editor_open

    @isEditorOpen.setter
    def isEditorOpen(self, new_value: bool):
        if self._is_editor_open != new_value:
            self._is_editor_open = new_value
            self.is_editor_open_changed.emit(new_value)

    @Property(bool, notify=is_editor_expanded_changed)
    def isEditorExpanded(self) -> bool:
        return self._is_editor_expanded

    @isEditorExpanded.setter
    def isEditorExpanded(self, new_value: bool):
        if self._is_editor_expanded != new_value:
            self._is_editor_expanded = new_value
            self.is_editor_expanded_changed.emit(new_value)

    @Property(bool, notify=originalCompareActiveChanged)
    def originalCompareActive(self) -> bool:
        return bool(
            getattr(
                self.app_controller,
                "_original_compare_active",
                self._original_compare_active,
            )
        )

    @originalCompareActive.setter
    def originalCompareActive(self, new_value: bool):
        active = bool(new_value)
        if self._original_compare_active != active:
            self._original_compare_active = active
            self.originalCompareActiveChanged.emit(active)

    @Property(str, notify=editorImageChanged)
    def editorFilename(self) -> str:
        """Returns the filename of the image currently being edited (may be .tif for developed RAW)."""
        editor = self.app_controller.image_editor
        fp = getattr(editor, "current_filepath", None) if editor else None
        if not fp:
            return ""
        try:
            return Path(fp).name
        except Exception:
            return ""

    @Property(int, notify=editorImageChanged)
    def editorBitDepth(self) -> int:
        """Returns the bit depth (8 or 16) of the image currently being edited."""
        editor = self.app_controller.image_editor
        if editor:
            return editor.bit_depth
        return 8

    @Property(bool, notify=isDialogOpenChanged)
    def isDialogOpen(self) -> bool:
        return self._is_dialog_open

    @isDialogOpen.setter
    def isDialogOpen(self, new_value: bool):
        if self._is_dialog_open != new_value:
            self._is_dialog_open = new_value
            self.isDialogOpenChanged.emit(new_value)

    @Property(bool, notify=isSavingChanged)
    def isSaving(self) -> bool:
        return self._is_saving

    @isSaving.setter
    def isSaving(self, new_value: bool):
        if self._is_saving != new_value:
            self._is_saving = new_value
            self.isSavingChanged.emit(new_value)

    # --- Batch Auto Levels ---

    @Property(bool, notify=batchAutoLevelsActiveChanged)
    def batchAutoLevelsActive(self) -> bool:
        return self._batch_al_active

    @Property(int, notify=batchAutoLevelsProgressChanged)
    def batchAutoLevelsCurrent(self) -> int:
        return self._batch_al_current

    @Property(int, notify=batchAutoLevelsProgressChanged)
    def batchAutoLevelsTotal(self) -> int:
        return self._batch_al_total

    @Slot()
    def batchAutoLevels(self):
        self.app_controller.batch_auto_levels()

    @Slot()
    def cancelBatchAutoLevels(self):
        self.app_controller.cancel_batch_auto_levels()

    @Property(bool, notify=is_cropping_changed)
    def isCropping(self) -> bool:
        return self._is_cropping

    @isCropping.setter
    def isCropping(self, new_value: bool):
        if self._is_cropping != new_value:
            self._is_cropping = new_value
            self.is_cropping_changed.emit(new_value)

    @Property(bool, notify=is_crop_rotating_changed)
    def isCropRotating(self) -> bool:
        return self._is_crop_rotating

    @isCropRotating.setter
    def isCropRotating(self, new_value: bool):
        new_value = bool(new_value)
        if self._is_crop_rotating != new_value:
            self._is_crop_rotating = new_value
            self.is_crop_rotating_changed.emit(new_value)

    @Property(bool, notify=is_histogram_visible_changed)
    def isHistogramVisible(self) -> bool:
        return self._is_histogram_visible

    @isHistogramVisible.setter
    def isHistogramVisible(self, new_value: bool):
        if self._is_histogram_visible != new_value:
            self._is_histogram_visible = new_value
            self.is_histogram_visible_changed.emit(new_value)
            if new_value:
                # Update histogram when opened
                try:
                    self.app_controller.update_histogram()
                except Exception as e:
                    log.warning(f"Failed to update histogram: {e}")

    @Slot()
    def reset_editor_state(self):
        """Resets all editor-related properties to their default values."""
        self.brightness = 0.0
        self.contrast = 0.0
        self.saturation = 0.0
        self.white_balance_by = 0.0
        self.white_balance_mg = 0.0
        self.sharpness = 0.0
        self.rotation = 0
        self.exposure = 0.0
        self.highlights = 0.0
        self.shadows = 0.0
        self.vibrance = 0.0
        self.vignette = 0.0
        self.blacks = 0.0
        self.whites = 0.0
        self.clarity = 0.0
        self.texture = 0.0
        self.color_sat_red = 0.0
        self.color_sat_orange = 0.0
        self.color_sat_yellow = 0.0
        self.color_sat_green = 0.0
        self.color_sat_aqua = 0.0
        self.color_sat_blue = 0.0
        self.color_sat_purple = 0.0
        self.color_sat_magenta = 0.0
        self.cropRotation = 0.0
        self.currentCropBox = (0, 0, 1000, 1000)
        self.currentAspectRatioIndex = 0
        # Darken tool — use property setters so QML bindings update
        self.isDarkening = False
        self.darkenOverlayVisible = True
        self.darkenAmount = 0.5
        self.darkenEdgeProtection = 0.5
        self.darkenSubjectProtection = 0.5
        self.darkenFeather = 0.5
        self.darkenDarkRange = 0.5
        self.darkenNeutrality = 0.5
        self.darkenExpandContract = 0.0
        self.darkenAutoEdges = 0.0
        self.darkenMode = "assisted"
        self.darkenBrushRadius = 0.03

    @Property("QVariantMap", notify=histogram_data_changed)
    def histogramData(self) -> dict:
        """Returns histogram data as a dict with 'r', 'g', 'b' keys, each containing a list of 256 values.

        Note: declared as QVariantMap (not QVariant) so QML JavaScript receives a real
        Object. Under PySide6 6.11+, returning a dict through Property("QVariant") produces
        a QJSValue wrapper that JS cannot index by key.
        """
        return self._histogram_data if self._histogram_data is not None else {}

    @histogramData.setter
    def histogramData(self, new_value):
        if self._histogram_data != new_value:
            self._histogram_data = new_value
            self.histogram_data_changed.emit()

    @Property("QVariantMap", notify=highlightStateChanged)
    def highlightState(self) -> dict:
        """Returns highlight analysis state for UI display.

        Returns dict with:
        - headroom_pct: Fraction of pixels with recoverable data above 1.0 (16-bit sources)
        - source_clipped_pct: Fraction of pixels clipped in the SOURCE image (JPEG flat-top @ 254+)
        - current_nearwhite_pct: Fraction of pixels currently near white in the processed state.

        Note: declared as QVariantMap (not QVariant) so QML JavaScript receives a real
        Object. Under PySide6 6.11+, returning a dict through Property("QVariant") produces
        a QJSValue wrapper that JS cannot index by key.
        """
        editor = self.app_controller.image_editor
        state = {}
        if (
            editor
            and hasattr(editor, "_last_highlight_state")
            and editor._last_highlight_state
        ):
            # Quick copy under lock to minimize contention
            # Using the editor's lock ensures we don't read while it's being written
            with editor._lock:
                state = dict(editor._last_highlight_state)

        # Normalize for QML robustness: ensure stable keys exist regardless of internal naming
        return {
            "headroom_pct": state.get("headroom_pct", 0.0),
            "source_clipped_pct": state.get("source_clipped_pct", 0.0),
            "current_nearwhite_pct": state.get("current_nearwhite_pct", 0.0),
        }

    @Property(float, notify=brightness_changed)
    def brightness(self) -> float:
        return self._brightness

    @brightness.setter
    def brightness(self, new_value: float):
        if self._brightness != new_value:
            self._brightness = new_value
            self.brightness_changed.emit(new_value)

    @Property(float, notify=contrast_changed)
    def contrast(self) -> float:
        return self._contrast

    @contrast.setter
    def contrast(self, new_value: float):
        if self._contrast != new_value:
            self._contrast = new_value
            self.contrast_changed.emit(new_value)

    @Property(float, notify=saturation_changed)
    def saturation(self) -> float:
        return self._saturation

    @saturation.setter
    def saturation(self, new_value: float):
        if self._saturation != new_value:
            self._saturation = new_value
            self.saturation_changed.emit(new_value)

    @Property(float, notify=white_balance_by_changed)
    def white_balance_by(self) -> float:
        return self._white_balance_by

    @white_balance_by.setter
    def white_balance_by(self, new_value: float):
        if self._white_balance_by != new_value:
            self._white_balance_by = new_value
            self.white_balance_by_changed.emit(new_value)

    @Property(float, notify=white_balance_mg_changed)
    def white_balance_mg(self) -> float:
        return self._white_balance_mg

    @white_balance_mg.setter
    def white_balance_mg(self, new_value: float):
        if self._white_balance_mg != new_value:
            self._white_balance_mg = new_value
            self.white_balance_mg_changed.emit(new_value)

    @Property("QVariantList", notify=aspect_ratio_names_changed)
    def aspectRatioNames(self) -> list:
        return self._aspect_ratio_names

    @aspectRatioNames.setter
    def aspectRatioNames(self, new_value: list):
        if self._aspect_ratio_names != new_value:
            self._aspect_ratio_names = new_value
            self.aspect_ratio_names_changed.emit(new_value)

    @Property(int, notify=current_aspect_ratio_index_changed)
    def currentAspectRatioIndex(self) -> int:
        return self._current_aspect_ratio_index

    @currentAspectRatioIndex.setter
    def currentAspectRatioIndex(self, new_value: int):
        if self._current_aspect_ratio_index != new_value:
            self._current_aspect_ratio_index = new_value
            self.current_aspect_ratio_index_changed.emit(new_value)

    @Property("QVariantList", notify=current_crop_box_changed)
    def currentCropBox(self) -> list:
        # Return a plain list so QML JavaScript receives a real Array.
        # Under PySide6 6.11+, returning a tuple through Property("QVariant")
        # produces a QJSValue wrapper that JS cannot index.
        return (
            list(self._current_crop_box) if self._current_crop_box is not None else []
        )

    def _normalize_crop_box_value(self, new_value):
        # Convert QJSValue or list to tuple if needed
        original_value = new_value
        try:
            if hasattr(new_value, "toVariant"):
                # It's a QJSValue, convert to tuple
                variant = new_value.toVariant()
                if isinstance(variant, (list, tuple)):
                    new_value = tuple(variant)
                else:
                    # Try to access elements directly
                    new_value = (variant[0], variant[1], variant[2], variant[3])
            elif isinstance(new_value, list):
                new_value = tuple(new_value)
            elif not isinstance(new_value, tuple):
                # Try to convert to tuple
                new_value = tuple(new_value)
        except (TypeError, IndexError, AttributeError) as e:
            log.warning(
                "UIState.currentCropBox: failed to normalize value %r (type %s): %s",
                original_value,
                type(original_value),
                e,
            )
            return None

        if not isinstance(new_value, tuple) or len(new_value) != 4:
            log.warning(
                "UIState.currentCropBox: ignoring invalid crop box %r", new_value
            )
            return None

        if not all(isinstance(v, Real) and not isinstance(v, bool) for v in new_value):
            log.warning(
                "UIState.currentCropBox: ignoring non-numeric crop box %r", new_value
            )
            return None

        try:
            left, top, right, bottom = new_value
            finite_values = tuple(float(v) for v in new_value)
        except (TypeError, ValueError) as e:
            log.warning(
                "UIState.currentCropBox: failed to validate crop box %r: %s",
                new_value,
                e,
            )
            return None

        if not all(math.isfinite(v) for v in finite_values):
            log.warning(
                "UIState.currentCropBox: ignoring non-finite crop box %r", new_value
            )
            return None

        if not all(0.0 <= v <= 1000.0 for v in finite_values):
            log.warning(
                "UIState.currentCropBox: ignoring out-of-range crop box %r", new_value
            )
            return None

        if not (left < right and top < bottom):
            # Transient during drag: QML can briefly emit zero-size or
            # inverted boxes when the user reverses direction. Reject
            # silently to avoid log spam.
            log.debug(
                "UIState.currentCropBox: ignoring inverted or zero-size crop box %r",
                new_value,
            )
            return None

        return new_value

    def _set_current_crop_box_value(self, new_value) -> bool:
        if self._current_crop_box == new_value:
            return False
        self._current_crop_box = new_value
        self.current_crop_box_changed.emit(new_value)
        return True

    def set_current_crop_box_visual_only(self, new_value) -> bool:
        """Update the crop overlay without mutating the editor session."""
        new_value = self._normalize_crop_box_value(new_value)
        if new_value is None:
            return False
        return self._set_current_crop_box_value(new_value)

    @currentCropBox.setter
    def currentCropBox(self, new_value):
        new_value = self._normalize_crop_box_value(new_value)
        if new_value is None:
            return
        if self._set_current_crop_box_value(new_value):
            # During crop mode this is draft overlay state only; the committed
            # crop box is applied explicitly on Enter.
            if self._is_cropping:
                try:
                    left, top, right, bottom = new_value
                    if (right - left) < 20 or (bottom - top) < 20:
                        return
                except (TypeError, ValueError):
                    return
                kick_preview = getattr(
                    self.app_controller, "_kick_preview_worker", None
                )
                if callable(kick_preview):
                    kick_preview()
                return
            # Sync with ImageEditor outside crop mode.
            if (
                hasattr(self.app_controller, "image_editor")
                and self.app_controller.image_editor
            ):
                self.app_controller.image_editor.set_crop_box(new_value)

    @Property(float, notify=crop_rotation_changed)
    def cropRotation(self) -> float:
        return self._crop_rotation

    @cropRotation.setter
    def cropRotation(self, new_value: float):
        if self._crop_rotation != new_value:
            self._crop_rotation = new_value
            self.crop_rotation_changed.emit(new_value)

    # --- New Properties ---
    @Property(float, notify=sharpness_changed)
    def sharpness(self) -> float:
        return self._sharpness

    @sharpness.setter
    def sharpness(self, new_value: float):
        if self._sharpness != new_value:
            self._sharpness = new_value
            self.sharpness_changed.emit(new_value)

    @Property(int, notify=rotation_changed)
    def rotation(self) -> int:
        return self._rotation

    @rotation.setter
    def rotation(self, new_value: int):
        if self._rotation != new_value:
            self._rotation = new_value
            self.rotation_changed.emit(new_value)

    @Property(float, notify=exposure_changed)
    def exposure(self) -> float:
        return self._exposure

    @exposure.setter
    def exposure(self, new_value: float):
        if self._exposure != new_value:
            self._exposure = new_value
            self.exposure_changed.emit(new_value)

    @Property(float, notify=highlights_changed)
    def highlights(self) -> float:
        return self._highlights

    @highlights.setter
    def highlights(self, new_value: float):
        if self._highlights != new_value:
            self._highlights = new_value
            self.highlights_changed.emit(new_value)

    @Property(float, notify=shadows_changed)
    def shadows(self) -> float:
        return self._shadows

    @shadows.setter
    def shadows(self, new_value: float):
        if self._shadows != new_value:
            self._shadows = new_value
            self.shadows_changed.emit(new_value)

    @Property(float, notify=vibrance_changed)
    def vibrance(self) -> float:
        return self._vibrance

    @vibrance.setter
    def vibrance(self, new_value: float):
        if self._vibrance != new_value:
            self._vibrance = new_value
            self.vibrance_changed.emit(new_value)

    # --- Per-hue saturation (color mix) bank ---
    @Property(float, notify=color_sat_red_changed)
    def color_sat_red(self) -> float:
        return self._color_sat_red

    @color_sat_red.setter
    def color_sat_red(self, new_value: float):
        if self._color_sat_red != new_value:
            self._color_sat_red = new_value
            self.color_sat_red_changed.emit(new_value)

    @Property(float, notify=color_sat_orange_changed)
    def color_sat_orange(self) -> float:
        return self._color_sat_orange

    @color_sat_orange.setter
    def color_sat_orange(self, new_value: float):
        if self._color_sat_orange != new_value:
            self._color_sat_orange = new_value
            self.color_sat_orange_changed.emit(new_value)

    @Property(float, notify=color_sat_yellow_changed)
    def color_sat_yellow(self) -> float:
        return self._color_sat_yellow

    @color_sat_yellow.setter
    def color_sat_yellow(self, new_value: float):
        if self._color_sat_yellow != new_value:
            self._color_sat_yellow = new_value
            self.color_sat_yellow_changed.emit(new_value)

    @Property(float, notify=color_sat_green_changed)
    def color_sat_green(self) -> float:
        return self._color_sat_green

    @color_sat_green.setter
    def color_sat_green(self, new_value: float):
        if self._color_sat_green != new_value:
            self._color_sat_green = new_value
            self.color_sat_green_changed.emit(new_value)

    @Property(float, notify=color_sat_aqua_changed)
    def color_sat_aqua(self) -> float:
        return self._color_sat_aqua

    @color_sat_aqua.setter
    def color_sat_aqua(self, new_value: float):
        if self._color_sat_aqua != new_value:
            self._color_sat_aqua = new_value
            self.color_sat_aqua_changed.emit(new_value)

    @Property(float, notify=color_sat_blue_changed)
    def color_sat_blue(self) -> float:
        return self._color_sat_blue

    @color_sat_blue.setter
    def color_sat_blue(self, new_value: float):
        if self._color_sat_blue != new_value:
            self._color_sat_blue = new_value
            self.color_sat_blue_changed.emit(new_value)

    @Property(float, notify=color_sat_purple_changed)
    def color_sat_purple(self) -> float:
        return self._color_sat_purple

    @color_sat_purple.setter
    def color_sat_purple(self, new_value: float):
        if self._color_sat_purple != new_value:
            self._color_sat_purple = new_value
            self.color_sat_purple_changed.emit(new_value)

    @Property(float, notify=color_sat_magenta_changed)
    def color_sat_magenta(self) -> float:
        return self._color_sat_magenta

    @color_sat_magenta.setter
    def color_sat_magenta(self, new_value: float):
        if self._color_sat_magenta != new_value:
            self._color_sat_magenta = new_value
            self.color_sat_magenta_changed.emit(new_value)

    @Property(float, notify=vignette_changed)
    def vignette(self) -> float:
        return self._vignette

    @vignette.setter
    def vignette(self, new_value: float):
        if self._vignette != new_value:
            self._vignette = new_value
            self.vignette_changed.emit(new_value)

    @Property(float, notify=blacks_changed)
    def blacks(self) -> float:
        return self._blacks

    @blacks.setter
    def blacks(self, new_value: float):
        if self._blacks != new_value:
            self._blacks = new_value
            self.blacks_changed.emit(new_value)

    @Property(float, notify=whites_changed)
    def whites(self) -> float:
        return self._whites

    @whites.setter
    def whites(self, new_value: float):
        if self._whites != new_value:
            self._whites = new_value
            self.whites_changed.emit(new_value)

    @Property(float, notify=clarity_changed)
    def clarity(self) -> float:
        return self._clarity

    @clarity.setter
    def clarity(self, new_value: float):
        if self._clarity != new_value:
            self._clarity = new_value
            self.clarity_changed.emit(new_value)

    @Property(float, notify=texture_changed)
    def texture(self) -> float:
        return self._texture

    @texture.setter
    def texture(self, new_value: float):
        if self._texture != new_value:
            self._texture = new_value
            self.texture_changed.emit(new_value)

    # --- Background Darkening Properties ---

    @Property(bool, notify=is_darkening_changed)
    def isDarkening(self) -> bool:
        return self._is_darkening

    @isDarkening.setter
    def isDarkening(self, new_value: bool):
        if self._is_darkening != new_value:
            self._is_darkening = new_value
            self.is_darkening_changed.emit(new_value)

    @Property(bool, notify=darken_overlay_visible_changed)
    def darkenOverlayVisible(self) -> bool:
        return self._darken_overlay_visible

    @darkenOverlayVisible.setter
    def darkenOverlayVisible(self, new_value: bool):
        if self._darken_overlay_visible != new_value:
            self._darken_overlay_visible = new_value
            self.darken_overlay_visible_changed.emit(new_value)

    @Property(int, notify=darken_overlay_generation_changed)
    def darkenOverlayGeneration(self) -> int:
        return self._darken_overlay_generation

    @Property(float, notify=darken_amount_changed)
    def darkenAmount(self) -> float:
        return self._darken_amount

    @darkenAmount.setter
    def darkenAmount(self, new_value: float):
        if self._darken_amount != new_value:
            self._darken_amount = new_value
            self.darken_amount_changed.emit(new_value)

    @Property(float, notify=darken_edge_protection_changed)
    def darkenEdgeProtection(self) -> float:
        return self._darken_edge_protection

    @darkenEdgeProtection.setter
    def darkenEdgeProtection(self, new_value: float):
        if self._darken_edge_protection != new_value:
            self._darken_edge_protection = new_value
            self.darken_edge_protection_changed.emit(new_value)

    @Property(float, notify=darken_subject_protection_changed)
    def darkenSubjectProtection(self) -> float:
        return self._darken_subject_protection

    @darkenSubjectProtection.setter
    def darkenSubjectProtection(self, new_value: float):
        if self._darken_subject_protection != new_value:
            self._darken_subject_protection = new_value
            self.darken_subject_protection_changed.emit(new_value)

    @Property(float, notify=darken_feather_changed)
    def darkenFeather(self) -> float:
        return self._darken_feather

    @darkenFeather.setter
    def darkenFeather(self, new_value: float):
        if self._darken_feather != new_value:
            self._darken_feather = new_value
            self.darken_feather_changed.emit(new_value)

    @Property(float, notify=darken_dark_range_changed)
    def darkenDarkRange(self) -> float:
        return self._darken_dark_range

    @darkenDarkRange.setter
    def darkenDarkRange(self, new_value: float):
        if self._darken_dark_range != new_value:
            self._darken_dark_range = new_value
            self.darken_dark_range_changed.emit(new_value)

    @Property(float, notify=darken_neutrality_changed)
    def darkenNeutrality(self) -> float:
        return self._darken_neutrality

    @darkenNeutrality.setter
    def darkenNeutrality(self, new_value: float):
        if self._darken_neutrality != new_value:
            self._darken_neutrality = new_value
            self.darken_neutrality_changed.emit(new_value)

    @Property(float, notify=darken_expand_contract_changed)
    def darkenExpandContract(self) -> float:
        return self._darken_expand_contract

    @darkenExpandContract.setter
    def darkenExpandContract(self, new_value: float):
        if self._darken_expand_contract != new_value:
            self._darken_expand_contract = new_value
            self.darken_expand_contract_changed.emit(new_value)

    @Property(float, notify=darken_auto_edges_changed)
    def darkenAutoEdges(self) -> float:
        return self._darken_auto_edges

    @darkenAutoEdges.setter
    def darkenAutoEdges(self, new_value: float):
        if self._darken_auto_edges != new_value:
            self._darken_auto_edges = new_value
            self.darken_auto_edges_changed.emit(new_value)

    @Property(str, notify=darken_mode_changed)
    def darkenMode(self) -> str:
        return self._darken_mode

    @darkenMode.setter
    def darkenMode(self, new_value: str):
        if self._darken_mode != new_value:
            self._darken_mode = new_value
            self.darken_mode_changed.emit(new_value)

    @Property(float, notify=darken_brush_radius_changed)
    def darkenBrushRadius(self) -> float:
        return self._darken_brush_radius

    @darkenBrushRadius.setter
    def darkenBrushRadius(self, new_value: float):
        if self._darken_brush_radius != new_value:
            self._darken_brush_radius = new_value
            self.darken_brush_radius_changed.emit(new_value)

    # --- Debug Cache Properties ---

    @Property(bool, notify=debugCacheChanged)
    def debugCache(self) -> bool:
        return self._debug_cache

    @debugCache.setter
    def debugCache(self, value: bool):
        if self._debug_cache != value:
            self._debug_cache = value
            self.debugCacheChanged.emit(value)

    @Property(str, notify=cacheStatsChanged)
    def cacheStats(self) -> str:
        return self._cache_stats

    @cacheStats.setter
    def cacheStats(self, value: str):
        if self._cache_stats != value:
            self._cache_stats = value
            self.cacheStatsChanged.emit(value)

    @Property(bool, notify=isDecodingChanged)
    def isDecoding(self) -> bool:
        return self._is_decoding

    @isDecoding.setter
    def isDecoding(self, value: bool):
        if self._is_decoding != value:
            self._is_decoding = value
            self.isDecodingChanged.emit(value)

    @Property(bool, notify=debugModeChanged)
    def debugMode(self) -> bool:
        return self._debug_mode

    @debugMode.setter
    def debugMode(self, value: bool):
        if self._debug_mode != value:
            self._debug_mode = value
            self.debugModeChanged.emit(value)

    @Property(bool, notify=debugThumbTimingChanged)
    def debugThumbTiming(self) -> bool:
        return self._debug_thumb_timing

    @debugThumbTiming.setter
    def debugThumbTiming(self, value: bool):
        if self._debug_thumb_timing != value:
            self._debug_thumb_timing = value
            self.debugThumbTimingChanged.emit(value)

    @Property(bool, notify=autoAddEditedToBatchChanged)
    def autoAddEditedToBatch(self) -> bool:
        return self._auto_add_edited_to_batch

    @autoAddEditedToBatch.setter
    def autoAddEditedToBatch(self, value: bool):
        if self._auto_add_edited_to_batch != value:
            self._auto_add_edited_to_batch = value
            self.autoAddEditedToBatchChanged.emit()
            if hasattr(self.app_controller, "save_config"):
                self.app_controller.save_config()

    # --- RAW / Editor Source Logic ---

    # --- Variant Badge Properties ---

    @Property(list, notify=variantBadgesChanged)
    def variantBadges(self) -> list:
        """Returns the badge list for the current image's variant group."""
        if hasattr(self.app_controller, "get_variant_badges"):
            return self.app_controller.get_variant_badges()
        return []

    @Property(str, notify=variantBadgesChanged)
    def activeVariantKind(self) -> str:
        """Returns 'main', 'developed', 'backup', or '' for current view."""
        kind = getattr(self.app_controller, "view_override_kind", None)
        return kind if kind else "main"

    @Slot(str)
    def setVariantOverride(self, path_str: str):
        """Switch loupe view to a different variant file."""
        if hasattr(self.app_controller, "set_variant_override"):
            self.app_controller.set_variant_override(path_str)

    # --- Grid View Properties ---

    # Signals for grid view
    isGridViewActiveChanged = Signal(bool)
    gridDirectoryChanged = Signal(str)
    gridSelectedCountChanged = Signal()  # No args - QML property notify pattern
    gridScrollToIndex = Signal(int)  # Scroll grid view to show this index
    gridCanGoBackChanged = Signal()  # Emitted when back history changes
    isFolderLoadedChanged = Signal()  # Emitted when the current folder finishes loading

    @Property(bool, notify=isFolderLoadedChanged)
    def isFolderLoaded(self) -> bool:
        """Returns True after the current folder scan has completed.

        Used by QML to avoid treating a directory switch as an empty folder
        before the new folder has finished loading.
        """
        return getattr(self.app_controller, "_folder_loaded", False)

    @Property(bool, notify=isGridViewActiveChanged)
    def isGridViewActive(self) -> bool:
        """Returns True if grid view is active, False for loupe view."""
        return getattr(self.app_controller, "_is_grid_view_active", False)

    @isGridViewActive.setter
    def isGridViewActive(self, value: bool):
        # Use controller method to ensure side effects (model refresh, resolver update) are applied
        if hasattr(self.app_controller, "_set_grid_view_active"):
            self.app_controller._set_grid_view_active(value)

    @Property(str, notify=gridDirectoryChanged)
    def gridDirectory(self) -> str:
        """Returns the current directory shown in grid view."""
        if (
            hasattr(self.app_controller, "_thumbnail_model")
            and self.app_controller._thumbnail_model
        ):
            return str(self.app_controller._thumbnail_model.current_directory)
        return str(self.app_controller.image_dir)

    @Property(int, notify=gridSelectedCountChanged)
    def gridSelectedCount(self) -> int:
        """Returns count of selected items in grid view (efficient, no list copy)."""
        if (
            hasattr(self.app_controller, "_thumbnail_model")
            and self.app_controller._thumbnail_model
        ):
            return self.app_controller._thumbnail_model.selected_count
        return 0

    @Slot()
    def toggleGridView(self):
        """Toggle between grid view and loupe view."""
        if hasattr(self.app_controller, "toggle_grid_view"):
            self.app_controller.toggle_grid_view()

    @Slot(int)
    def gridOpenIndex(self, index: int):
        """Open an image from grid view in loupe view."""
        if hasattr(self.app_controller, "grid_open_index"):
            self.app_controller.grid_open_index(index)

    @Slot(str)
    def gridNavigateTo(self, path: str):
        """Navigate to a folder in grid view."""
        if hasattr(self.app_controller, "grid_navigate_to"):
            self.app_controller.grid_navigate_to(path)

    @Slot()
    def gridClearSelection(self):
        """Clear all selections in grid view."""
        if (
            hasattr(self.app_controller, "_thumbnail_model")
            and self.app_controller._thumbnail_model
        ):
            self.app_controller._thumbnail_model.clear_selection()

    @Slot(int, bool, bool)
    def gridSelectIndex(self, index: int, shift: bool, ctrl: bool):
        """Handle selection at index with modifier keys."""
        if (
            hasattr(self.app_controller, "_thumbnail_model")
            and self.app_controller._thumbnail_model
        ):
            self.app_controller._thumbnail_model.select_index(index, shift, ctrl)

    @Slot(result="QVariantList")
    def gridGetSelectedPaths(self) -> list:
        """Get list of selected image paths in grid view."""
        if (
            hasattr(self.app_controller, "_thumbnail_model")
            and self.app_controller._thumbnail_model
        ):
            return [
                str(p)
                for p in self.app_controller._thumbnail_model.get_selected_paths()
            ]
        return []

    @Slot()
    def gridRefresh(self):
        """Refresh the grid view."""
        self.app_controller.refresh_grid()

    @Property(bool, notify=gridCanGoBackChanged)
    def gridCanGoBack(self) -> bool:
        """Returns True if there's navigation history to go back to."""
        if hasattr(self.app_controller, "_grid_nav_history"):
            return len(self.app_controller._grid_nav_history) > 0
        return False

    @Slot()
    def gridGoBack(self):
        """Navigate back to the previous directory in grid view."""
        if hasattr(self.app_controller, "grid_go_back"):
            self.app_controller.grid_go_back()

    @Slot()
    def gridAddSelectionToBatch(self):
        """Add grid-selected images to batch."""
        if hasattr(self.app_controller, "grid_add_selection_to_batch"):
            self.app_controller.grid_add_selection_to_batch()

    @Slot(int)
    def gridDeleteAtCursor(self, cursorIndex: int):
        """Delete image(s) from grid view - selection or cursor image."""
        if hasattr(self.app_controller, "grid_delete_at_cursor"):
            self.app_controller.grid_delete_at_cursor(cursorIndex)

    @Slot()
    def cancelThumbnailPrefetch(self):
        """Cancels all pending thumbnail prefetch requests."""
        if (
            hasattr(self.app_controller, "_thumbnail_prefetcher")
            and self.app_controller._thumbnail_prefetcher
        ):
            self.app_controller._thumbnail_prefetcher.cancel_all()

    @Slot(int, int, int, int, int)
    def gridPrefetchRange(
        self,
        startIndex: int,
        endIndex: int,
        maxCount: int = 800,
        visibleStartIndex: int = -1,
        visibleEndIndex: int = -1,
    ):
        """Prefetch thumbnails, prioritizing the current visible subrange."""
        if (
            not hasattr(self.app_controller, "_thumbnail_model")
            or not self.app_controller._thumbnail_model
        ):
            return
        if (
            not hasattr(self.app_controller, "_thumbnail_prefetcher")
            or not self.app_controller._thumbnail_prefetcher
        ):
            return

        model = self.app_controller._thumbnail_model
        prefetcher = self.app_controller._thumbnail_prefetcher

        # 1. Index Validation
        rowCount = model.rowCount()
        if rowCount <= 0:
            return

        # Clamp indices to valid boundaries
        startIndex = max(0, min(startIndex, rowCount - 1))
        endIndex = max(0, min(endIndex, rowCount - 1))

        if startIndex > endIndex:
            return

        has_visible_range = visibleStartIndex >= 0 and visibleEndIndex >= 0
        if has_visible_range:
            visibleStartIndex = max(0, min(visibleStartIndex, rowCount - 1))
            visibleEndIndex = max(0, min(visibleEndIndex, rowCount - 1))
            visibleStartIndex = max(startIndex, visibleStartIndex)
            visibleEndIndex = min(endIndex, visibleEndIndex)
            has_visible_range = visibleStartIndex <= visibleEndIndex

        # 2. Duplicate Suppression
        now = self._clock()
        current_req = (
            startIndex,
            endIndex,
            maxCount,
            visibleStartIndex if has_visible_range else -1,
            visibleEndIndex if has_visible_range else -1,
        )
        if (
            current_req == self._last_prefetch_data
            and (now - self._last_prefetch_time) < 0.030
        ):
            return

        self._last_prefetch_data = current_req
        self._last_prefetch_time = now

        # 3. Budgeting / Hard Cap
        HARD_LIMIT = 800
        budget = max(1, min(maxCount, HARD_LIMIT))

        # Defensive fallback if thumbnail_size is refactored away
        size = getattr(model, "thumbnail_size", None) or getattr(
            prefetcher, "_target_size", None
        )
        high_priority = getattr(prefetcher, "PRIO_HIGH", 0)
        medium_priority = getattr(prefetcher, "PRIO_MED", 1)

        def submit_in_lifo_order(indices: list[int], priority: int) -> None:
            # PriorityExecutor is LIFO within a priority. Submit in reverse of
            # desired display order so the first visible cell is decoded first.
            for i in reversed(indices):
                entry = model.get_entry(i)
                if entry and not entry.is_folder:
                    prefetcher.submit(
                        entry.path,
                        entry.mtime_ns,
                        size=size,
                        priority=priority,
                    )

        if has_visible_range:
            visible_indices = list(range(visibleStartIndex, visibleEndIndex + 1))
            submit_in_lifo_order(visible_indices, high_priority)

            remaining_budget = max(0, budget - len(visible_indices))
            if remaining_budget <= 0:
                return

            below_indices = list(range(visibleEndIndex + 1, endIndex + 1))
            above_indices = list(range(visibleStartIndex - 1, startIndex - 1, -1))
            medium_indices = (below_indices + above_indices)[:remaining_budget]
            submit_in_lifo_order(medium_indices, medium_priority)
            return

        indices = list(range(startIndex, min(endIndex, startIndex + budget - 1) + 1))
        submit_in_lifo_order(indices, medium_priority)

    @Property(str, notify=recycleBinStatsTextChanged)
    def recycleBinStatsText(self):
        """Returns a formatted summary of recycle bin contents."""
        info = self.app_controller.get_per_bin_restore_info()
        if not info:
            return ""

        total_files = sum(b["total_files"] for b in info)
        n_bins = len(info)
        unavailable = [b for b in info if b["status"] == "unavailable"]

        summary = (
            f"{total_files} file{'s' if total_files != 1 else ''} "
            f"in {n_bins} recycle bin{'s' if n_bins != 1 else ''}."
        )
        if unavailable:
            n_un = len(unavailable)
            summary += (
                f"\n{n_un} bin{'s' if n_un != 1 else ''} "
                f"contain{'s' if n_un == 1 else ''} only legacy files "
                f"and cannot be restored automatically."
            )
        return summary

    @Property(str, notify=recycleBinDetailedTextChanged)
    def recycleBinDetailedText(self):
        """Returns a detailed list of all file paths in recycle bins."""
        stats = self.app_controller.get_recycle_bin_stats()
        if not stats:
            log.debug("recycleBinDetailedText: No recycle bin stats found")
            return ""

        lines = []
        for item in stats:
            lines.append(f"Directory: {item['path']}")
            for fname in item.get("file_paths", []):
                lines.append(f"  - {fname}")
            lines.append("")

        result = "\n".join(lines)
        log.debug("recycleBinDetailedText: Returning %d lines of details", len(lines))
        return result

    @Property(bool, notify=hasRecycleBinItemsChanged)
    def hasRecycleBinItems(self):
        """Returns True if there are items in any recycle bin."""
        stats = self.app_controller.get_recycle_bin_stats()
        return len(stats) > 0

    @Slot()
    def refreshRecycleBinStats(self):
        """Notify QML that recycle-bin properties should be re-read."""
        self.recycleBinStatsTextChanged.emit()
        self.recycleBinDetailedTextChanged.emit()
        self.hasRecycleBinItemsChanged.emit()

    @Slot()
    def cleanupRecycleBins(self):
        """Deletes all tracked recycle bins."""
        self.app_controller.cleanup_recycle_bins()
        self.refreshRecycleBinStats()

    @Slot(result="QVariantList")
    def getPerBinRestoreInfo(self):
        """Returns per-bin restore info as a list of JS-compatible dicts.

        Each entry has: bin_id, bin_path, dest_dir, label, status,
        jpg_count, raw_count, other_count, total_restorable,
        total_files, legacy_count.
        """
        return self.app_controller.get_per_bin_restore_info()

    @Slot(str, result=str)
    def restoreSingleBin(self, bin_path: str) -> str:
        """Restore files from a single recycle bin.

        Returns a user-facing status message string.
        """
        result = self.app_controller.restore_single_bin(bin_path)
        self.refreshRecycleBinStats()

        restored = result["restored_count"]
        skipped = result["skipped_count"]
        legacy = result["legacy_remaining_count"]
        dest = result["dest_dir"]

        # Build context-aware feedback message
        parts = []
        if restored > 0:
            parts.append(
                f"Restored {restored} file{'s' if restored != 1 else ''} to {dest}"
            )
        if skipped > 0:
            parts.append(
                f"{skipped} skipped (already exist{'s' if skipped == 1 else ''})"
            )

        msg = ", ".join(parts) if parts else "Nothing to restore"

        if legacy > 0:
            msg += (
                f"; {legacy} legacy file{'s' if legacy != 1 else ''} "
                f"remain{'s' if legacy == 1 else ''} in recycle bin"
            )

        log.info("Restore result: %s", msg)
        self.app_controller.update_status_message(msg, timeout=5000)
        return msg
