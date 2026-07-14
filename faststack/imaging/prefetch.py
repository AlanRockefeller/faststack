"""Handles prefetching and decoding of adjacent images in a background thread pool."""

import hashlib
import io
import logging
import mmap
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import numpy as np
from PIL import Image as PILImage
from PIL import ImageCms

try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None

from faststack.config import config
from faststack.imaging.cache import build_cache_key
from faststack.imaging.jpeg import decode_jpeg_resized, decode_jpeg_rgb
from faststack.imaging.orientation import apply_orientation_to_np
from faststack.io.utils import normalize_path_key
from faststack.models import DecodedImage, ImageFile
from faststack.util.executors import (
    create_daemon_threadpool_executor,
    create_priority_executor,
)

log = logging.getLogger(__name__)

DecodeQuality = Literal["fast", "cover"]

_PRIORITY_DEMAND = 0
_PRIORITY_COVER = 5
_PRIORITY_PREFETCH_BASE = 10
_PRIORITY_PREWARM = 100
_QUEUE_GROUP_INTERACTIVE = 0
_QUEUE_GROUP_PRELOAD = 1

# Held-key browsing deliberately uses a much smaller source than the physical
# viewport. The settled cover decode replaces it visually after navigation
# stops, while the small buffer remains cached for the next browsing burst.
DEFAULT_HELD_NAVIGATION_QUALITY = "balanced"
HELD_NAVIGATION_QUALITY_DIMENSIONS = {
    "performance": 800,
    "balanced": 1600,
    "detailed": 2400,
    "highest": 3200,
}
_MIN_DIRECTION_TAIL = 4


class _DecodeTaskFuture(Future):
    """Stable logical future whose physical executor work may be replaced.

    Callers and Prefetcher bookkeeping retain this future for the lifetime of a
    logical decode. A queued speculative execution can therefore be cancelled
    and moved to the reserved demand executor without cancelling callbacks or
    leaving a waiter attached to the obsolete queue entry.
    """

    def __init__(self, worker: Callable[..., Any], worker_args: tuple[Any, ...]):
        super().__init__()
        self._worker = worker
        self._worker_args = worker_args
        self._execution_lock = threading.RLock()
        self._execution_future: Optional[Future] = None
        self._execution_executor: object = None
        self._completion_claimed = False

    def submit_execution(
        self,
        executor,
        *,
        priority: int,
        queue_group: int = _QUEUE_GROUP_INTERACTIVE,
        queue_order: Optional[int] = None,
    ) -> None:
        """Bind the initial physical execution to this logical future."""
        execution = executor.submit(
            self._worker,
            *self._worker_args,
            priority=priority,
            queue_group=queue_group,
            queue_order=queue_order,
        )
        with self._execution_lock:
            if self.done() or self._completion_claimed:
                execution.cancel()
                return
            self._execution_future = execution
            self._execution_executor = executor
        # A completed Future invokes callbacks synchronously. Register outside
        # _execution_lock so logical completion callbacks are never run while
        # holding the migration lock.
        execution.add_done_callback(self._execution_done)

    def migrate_queued_execution(
        self,
        source_executor,
        target_executor,
        *,
        priority: int,
    ) -> str:
        """Move queued work between executors without changing logical identity.

        Future.cancel() and Future.set_running_or_notify_cancel() serialize the
        queued-versus-running race. A failed cancellation means the source work
        has started (or just completed) and remains the sole accepted execution.
        """
        replacement = None
        completed_execution = None
        submit_error = None
        with self._execution_lock:
            if self.done() or self._completion_claimed:
                return "complete"
            execution = self._execution_future
            if execution is None or self._execution_executor is not source_executor:
                return "not-source"

            # Detach before cancel(): cancellation invokes callbacks
            # synchronously. The obsolete execution callback must see that it no
            # longer owns this logical future and do nothing.
            self._execution_future = None
            self._execution_executor = None
            if not execution.cancel():
                # The worker won the race. Restore ownership and, if it also
                # completed while detached, transfer its result explicitly.
                self._execution_future = execution
                self._execution_executor = source_executor
                if execution.done():
                    completed_execution = execution
                    state = "complete"
                else:
                    state = "running"
            else:
                try:
                    replacement = target_executor.submit(
                        self._worker,
                        *self._worker_args,
                        priority=priority,
                        queue_group=_QUEUE_GROUP_INTERACTIVE,
                    )
                except BaseException as exc:
                    self._completion_claimed = True
                    submit_error = exc
                    state = "failed"
                else:
                    self._execution_future = replacement
                    self._execution_executor = target_executor
                    state = "migrated"

        if completed_execution is not None:
            self._execution_done(completed_execution)
        if replacement is not None:
            replacement.add_done_callback(self._execution_done)
        if submit_error is not None:
            self.set_exception(submit_error)
        return state

    def execution_for(self, executor) -> Optional[Future]:
        """Return the currently bound physical future for an executor."""
        with self._execution_lock:
            if self._execution_executor is executor:
                return self._execution_future
            return None

    def running(self) -> bool:
        """Mirror the physical execution's running state."""
        with self._execution_lock:
            if self.done():
                return False
            if self._completion_claimed:
                return True
            if self._execution_future is None:
                return False
            return self._execution_future.running()

    def cancel(self) -> bool:
        """Cancel queued physical work and this logical future together."""
        completed_execution = None
        with self._execution_lock:
            if self.done() or self._completion_claimed:
                return False
            execution = self._execution_future
            executor = self._execution_executor
            if execution is not None:
                self._execution_future = None
                self._execution_executor = None
                if not execution.cancel():
                    self._execution_future = execution
                    self._execution_executor = executor
                    if execution.done():
                        completed_execution = execution
                    else:
                        return False
        if completed_execution is not None:
            self._execution_done(completed_execution)
            return False
        return super().cancel()

    def _execution_done(self, execution: Future) -> None:
        with self._execution_lock:
            if (
                execution is not self._execution_future
                or self.done()
                or self._completion_claimed
            ):
                return
            # Claim terminal ownership before releasing the lock. cancel() and
            # migration must now reuse this result rather than racing its
            # transfer. Future callbacks run only after the lock is released,
            # avoiding an execution-lock -> Prefetcher-lock inversion.
            self._completion_claimed = True
            self._execution_future = None
            self._execution_executor = None
        if execution.cancelled():
            super().cancel()
            return
        try:
            result = execution.result()
        except BaseException as exc:
            self.set_exception(exc)
        else:
            self.set_result(result)


# RAW extensions that Pillow typically cannot decode (no embedded JPEG preview).
# When decode fails for these, we generate a placeholder instead of returning None.
_RAW_EXTENSIONS = frozenset(
    {".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng"}
)


def normalize_held_navigation_quality(value: object) -> str:
    """Return a supported held-navigation quality name."""
    normalized = str(value or "").strip().lower()
    if normalized in HELD_NAVIGATION_QUALITY_DIMENSIONS:
        return normalized
    return DEFAULT_HELD_NAVIGATION_QUALITY


def held_navigation_quality_max_dimension(value: object) -> int:
    """Resolve a held-navigation quality name to its decode-size ceiling."""
    return HELD_NAVIGATION_QUALITY_DIMENSIONS[normalize_held_navigation_quality(value)]


def _make_raw_placeholder(width: int, height: int) -> np.ndarray:
    """Generate a themed 'Preview unavailable' placeholder for undecodable RAW files.

    Draws a circle-with-slash icon and centered text so the placeholder is
    visually distinct from actual image content.  Theme-aware via config.
    """
    if width <= 0 or height <= 0:
        width, height = 256, 256

    # Theme-aware palette
    theme = config.get("core", "theme", fallback="dark")
    if theme == "dark":
        bg_color = (30, 30, 30)
        text_color = (120, 120, 120)
        icon_color = (80, 80, 80)
    else:
        bg_color = (240, 240, 240)
        text_color = (140, 140, 140)
        icon_color = (180, 180, 180)

    from PIL import ImageDraw, ImageFont

    img = PILImage.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    cx, cy = width // 2, height // 2
    short = min(width, height)
    icon_r = max(10, short // 8)

    # Circle-with-slash "no preview" icon
    if icon_r >= 10:
        stroke = max(2, icon_r // 10)
        icon_cy = cy - icon_r  # icon above center
        draw.ellipse(
            [cx - icon_r, icon_cy - icon_r, cx + icon_r, icon_cy + icon_r],
            outline=icon_color,
            width=stroke,
        )
        draw.line(
            [cx - icon_r, icon_cy + icon_r, cx + icon_r, icon_cy - icon_r],
            fill=icon_color,
            width=stroke,
        )

    # "Preview unavailable" text below icon
    text = "Preview unavailable"
    font_size = max(12, short // 20)
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:
        # Older Pillow without size= parameter
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    text_y = cy + (icon_r // 2 if icon_r >= 10 else 0)
    draw.text(
        ((width - tw) // 2, text_y),
        text,
        fill=text_color,
        font=font,
    )

    return np.array(img)


# ---- Option C: ICC Color Management Setup ----
SRGB_PROFILE = ImageCms.createProfile("sRGB")

# Cache for monitor ICC profile to avoid reloading on every decode
_monitor_profile_cache: Dict[str, Optional[ImageCms.ImageCmsProfile]] = {}
_monitor_profile_warning_logged = False

# Cache for ICC transforms to avoid rebuilding on every image
_icc_transform_cache: Dict[tuple, ImageCms.ImageCmsTransform] = {}
_ICC_CACHE_MAX_ENTRIES = 32

# Cache parsed source ICC profiles by digest so we do not rebuild the same
# source profile object for every preview render.
_source_profile_cache: Dict[str, ImageCms.ImageCmsProfile] = {}

# Thread lock for all ICC caches
_icc_cache_lock = threading.Lock()

_saturation_factor_warning_logged = False
_saturation_factor_warning_lock = threading.Lock()


def get_icc_transform(
    src_profile: ImageCms.ImageCmsProfile,
    monitor_profile: ImageCms.ImageCmsProfile,
    src_profile_key: str,
    monitor_profile_path: str,
) -> ImageCms.ImageCmsTransform:
    """Get or create a cached ICC transform.

    Building transforms is expensive, so we cache them by stable keys:
    - src_profile_key: SHA-256 digest of the embedded ICC bytes
    - monitor_profile_path: file path to the monitor ICC profile
    """
    key = (src_profile_key, monitor_profile_path)
    with _icc_cache_lock:
        cached = _icc_transform_cache.get(key)
        if cached is not None:
            return cached

    transform = ImageCms.buildTransform(src_profile, monitor_profile, "RGB", "RGB")

    with _icc_cache_lock:
        cached = _icc_transform_cache.get(key)
        if cached is not None:
            return cached
        if len(_icc_transform_cache) >= _ICC_CACHE_MAX_ENTRIES:
            _icc_transform_cache.pop(next(iter(_icc_transform_cache)))
        _icc_transform_cache[key] = transform

    log.debug(
        "Built new ICC transform for profile pair (src=%s, monitor=%s)",
        src_profile_key[:16],
        monitor_profile_path,
    )
    return transform


def clear_icc_caches():
    """Clear all ICC-related caches (profiles and transforms)."""
    global _monitor_profile_warning_logged
    with _icc_cache_lock:
        _monitor_profile_cache.clear()
        _icc_transform_cache.clear()
        _source_profile_cache.clear()
        _monitor_profile_warning_logged = False
        log.info("Cleared ICC profile and transform caches")


def _get_source_profile(
    icc_bytes: Optional[bytes],
) -> tuple[ImageCms.ImageCmsProfile, str]:
    """Return a cached source ICC profile and stable cache key."""
    if not icc_bytes:
        return SRGB_PROFILE, "srgb_builtin"

    src_profile_key = hashlib.sha256(icc_bytes).hexdigest()
    with _icc_cache_lock:
        cached = _source_profile_cache.get(src_profile_key)
        if cached is not None:
            return cached, src_profile_key

    try:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
    except Exception as e:
        log.warning("Failed to parse ICC profile: %s", e)
        return SRGB_PROFILE, "srgb_builtin"

    with _icc_cache_lock:
        if len(_source_profile_cache) >= _ICC_CACHE_MAX_ENTRIES:
            _source_profile_cache.pop(next(iter(_source_profile_cache)))
        _source_profile_cache[src_profile_key] = src_profile

    return src_profile, src_profile_key


def get_monitor_profile() -> Optional[ImageCms.ImageCmsProfile]:
    """Dynamically load monitor ICC profile based on current config.

    Caches the profile by path to reduce overhead and log spam.
    """
    global _monitor_profile_warning_logged

    monitor_icc_path = config.get("color", "monitor_icc_path", fallback="").strip()

    with _icc_cache_lock:
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
        except (OSError, ImageCms.PyCMSError) as e:
            log.warning(
                "Failed to load monitor ICC profile from %s: %s", monitor_icc_path, e
            )
            _monitor_profile_cache[monitor_icc_path] = None

        return _monitor_profile_cache[monitor_icc_path]


def _prewarm_common_icc_transform() -> None:
    """Build the common sRGB-to-monitor transform before the first image."""
    if config.get("color", "mode", fallback="none").lower() != "icc":
        return
    monitor_profile = get_monitor_profile()
    if monitor_profile is None:
        return
    monitor_icc_path = config.get("color", "monitor_icc_path", fallback="").strip()
    try:
        get_icc_transform(
            SRGB_PROFILE,
            monitor_profile,
            "srgb_builtin",
            monitor_icc_path,
        )
    except Exception:
        log.debug("Could not prewarm the display ICC transform", exc_info=True)


# Strip-parallel ICC apply. An lcms transform is immutable once built, so the
# same transform can convert independent row bands concurrently; measured
# byte-identical to the single-shot path and ~3x faster on a 6MP cover
# (tools/bench_decode.py). Small (fast-tier) frames stay single-shot: the
# fan-out overhead and contention with busy decode workers outweigh the win,
# and their latency is hidden by the look-ahead buffer anyway.
_ICC_STRIP_MIN_PIXELS = 2_000_000
_ICC_STRIP_COUNT = 4
_icc_strip_pool = None
_icc_strip_pool_lock = threading.Lock()


def _get_icc_strip_pool():
    global _icc_strip_pool
    if _icc_strip_pool is None:
        with _icc_strip_pool_lock:
            if _icc_strip_pool is None:
                _icc_strip_pool = create_daemon_threadpool_executor(
                    max_workers=_ICC_STRIP_COUNT,
                    thread_name_prefix="IccStrip",
                )
    return _icc_strip_pool


def apply_icc_transform_to_buffer(
    buffer: np.ndarray,
    transform: ImageCms.ImageCmsTransform,
) -> np.ndarray:
    """Convert RGB pixels to monitor space, strip-parallel for large frames."""
    height = int(buffer.shape[0])
    pixels = height * int(buffer.shape[1])
    if pixels < _ICC_STRIP_MIN_PIXELS or height < _ICC_STRIP_COUNT:
        img = PILImage.fromarray(buffer)
        ImageCms.applyTransform(img, transform, inPlace=True)
        return np.array(img, dtype=np.uint8)

    bounds = np.linspace(0, height, _ICC_STRIP_COUNT + 1).astype(int)

    def convert(start: int, stop: int) -> np.ndarray:
        strip = PILImage.fromarray(np.ascontiguousarray(buffer[start:stop]))
        ImageCms.applyTransform(strip, transform, inPlace=True)
        return np.asarray(strip, dtype=np.uint8)

    pool = _get_icc_strip_pool()
    futures = [
        pool.submit(convert, int(start), int(stop))
        for start, stop in zip(bounds[:-1], bounds[1:])
    ]
    return np.vstack([future.result() for future in futures])


def get_icc_profile_description(profile: ImageCms.ImageCmsProfile) -> str:
    """Extract a human-readable description from an ICC profile object."""
    try:
        desc = ImageCms.getProfileDescription(profile)
        if desc and desc.strip():
            return desc.strip()
    except Exception as e:
        log.debug("getProfileDescription failed: %s", e)
    try:
        name = ImageCms.getProfileName(profile)
        if name and name.strip():
            return name.strip()
    except Exception as e:
        log.debug("getProfileName failed: %s", e)
    return "(unknown)"


def get_icc_profile_details(profile: ImageCms.ImageCmsProfile) -> Dict[str, str]:
    """Extract detailed metadata from an ICC profile for diagnostic display."""
    details: Dict[str, str] = {}

    details["Description"] = get_icc_profile_description(profile)

    for label, func in [
        ("Copyright", ImageCms.getProfileCopyright),
        ("Manufacturer", ImageCms.getProfileManufacturer),
        ("Model", ImageCms.getProfileModel),
    ]:
        try:
            val = func(profile)
            if val and val.strip():
                details[label] = val.strip()
        except Exception as e:
            log.debug("Failed to extract ICC field %s: %s", label, e)

    try:
        inner = profile.profile
        device_classes = {
            "mntr": "Display (monitor)",
            "scnr": "Input (scanner)",
            "prtr": "Output (printer)",
            "link": "Device link",
            "spac": "Color space conversion",
            "abst": "Abstract",
            "nmcl": "Named color",
        }
        dc = getattr(inner, "device_class", None)
        if dc:
            details["Profile class"] = device_classes.get(dc, dc)

        xs = getattr(inner, "xcolor_space", None)
        if xs and xs.strip():
            details["Color space"] = xs.strip()

        cs = getattr(inner, "connection_space", None)
        if cs and cs.strip():
            details["PCS"] = cs.strip()

        ver = getattr(inner, "version", None)
        if ver:
            details["ICC version"] = str(ver)

        intents = {
            0: "Perceptual",
            1: "Relative colorimetric",
            2: "Saturation",
            3: "Absolute colorimetric",
        }
        ri = getattr(inner, "rendering_intent", None)
        if ri is not None:
            details["Rendering intent"] = intents.get(ri, str(ri))
    except Exception as e:
        log.debug("Failed to read profile.profile attributes: %s", e)

    return details


_EXIF_ORIENTATION_TAG = 274

# apply_orientation_to_np imported from orientation.py


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


def _get_saturation_factor() -> float:
    val = config.get("color", "saturation_factor", fallback="1.0")
    if val is None:
        return 1.0

    try:
        return float(val)
    except (TypeError, ValueError):
        global _saturation_factor_warning_logged
        with _saturation_factor_warning_lock:
            if not _saturation_factor_warning_logged:
                log.warning(
                    "Invalid saturation_factor config value %r; falling back to 1.0",
                    val,
                )
                _saturation_factor_warning_logged = True
        return 1.0


def _apply_configured_saturation(buffer: np.ndarray) -> None:
    saturation_factor = _get_saturation_factor()
    if saturation_factor == 1.0:
        return

    apply_saturation_compensation(
        buffer.ravel(),
        buffer.shape[1],
        buffer.shape[0],
        buffer.strides[0],
        saturation_factor,
    )


def apply_loupe_color_correction(
    buffer: np.ndarray,
    *,
    icc_bytes: Optional[bytes] = None,
    color_mode: Optional[str] = None,
) -> np.ndarray:
    """Apply the same display-only color correction used by the loupe decode path."""
    corrected = np.ascontiguousarray(buffer)
    mode = (
        color_mode
        if color_mode is not None
        else config.get("color", "mode", fallback="none")
    ).lower()

    if mode == "icc":
        monitor_profile = get_monitor_profile()
        monitor_icc_path = config.get("color", "monitor_icc_path", fallback="").strip()
        if monitor_profile is None:
            return corrected

        src_profile, src_profile_key = _get_source_profile(icc_bytes)

        try:
            img = PILImage.fromarray(corrected)
            transform = get_icc_transform(
                src_profile,
                monitor_profile,
                src_profile_key,
                monitor_icc_path,
            )
            ImageCms.applyTransform(img, transform, inPlace=True)
            return np.ascontiguousarray(np.array(img, dtype=np.uint8))
        except Exception as e:
            log.warning("ICC conversion failed: %s", e)
            return corrected

    if mode == "saturation":
        _apply_configured_saturation(corrected)

    return corrected


def _decode_buffer(
    target_path: Path,
    display_width: int,
    display_height: int,
    use_resized: bool,
    should_resize: bool,
    fast_dct: bool,
    want_icc: bool,
    decode_quality: DecodeQuality,
    index: int,
    stats: Optional[dict] = None,
) -> tuple[Optional[np.ndarray], int, Optional[bytes], int, int, Optional[str]]:
    """Decode an image to RGB pixels, returning EXIF orientation and optional ICC.

    When ``stats`` is provided it is populated in-place with decode/resize and
    metadata timing for the navigation trace.
    """
    buffer = None
    icc_bytes = None
    orientation = 1
    native_width = 0
    native_height = 0
    placeholder_reason = None
    is_jpeg = target_path.suffix.lower() in {".jpg", ".jpeg", ".jpe"}

    if is_jpeg:
        icc_metadata_read = False
        try:
            _t_read = time.perf_counter() if stats is not None else None
            with open(target_path, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                    if stats is not None and _t_read is not None:
                        # Diagnostic-only (nav trace active): fault every page
                        # in sequentially so file I/O and antivirus scan cost
                        # land in read_ms instead of inflating jpeg_ms through
                        # page faults taken inside the decoder.
                        _faulted = mmapped[:: mmap.PAGESIZE]
                        stats["read_ms"] = (time.perf_counter() - _t_read) * 1000.0
                    if use_resized and should_resize:
                        buffer = decode_jpeg_resized(
                            mmapped,
                            display_width,
                            display_height,
                            fast_dct=fast_dct,
                            source_path=str(target_path),
                            mode=decode_quality,
                            stats=stats,
                        )
                    else:
                        _t_decode = time.perf_counter() if stats is not None else None
                        buffer = decode_jpeg_rgb(
                            mmapped,
                            fast_dct=fast_dct,
                            source_path=str(target_path),
                            stats=stats,
                        )
                        if stats is not None and _t_decode is not None:
                            stats["jpeg_ms"] = (
                                time.perf_counter() - _t_decode
                            ) * 1000.0
                        if buffer is not None and should_resize:
                            _t_resize = (
                                time.perf_counter() if stats is not None else None
                            )
                            img = PILImage.fromarray(buffer)
                            img.thumbnail(
                                (display_width, display_height),
                                PILImage.Resampling.LANCZOS,
                            )
                            buffer = np.array(img)
                            if stats is not None and _t_resize is not None:
                                stats["resize_ms"] = (
                                    time.perf_counter() - _t_resize
                                ) * 1000.0

                    if buffer is not None:
                        try:
                            _t_meta = time.perf_counter() if stats is not None else None
                            mmapped.seek(0)
                            with PILImage.open(mmapped) as pil_img:
                                native_width, native_height = pil_img.size
                                if want_icc:
                                    icc_bytes = pil_img.info.get("icc_profile")
                                orientation = pil_img.getexif().get(
                                    _EXIF_ORIENTATION_TAG, 1
                                )
                                icc_metadata_read = want_icc
                            if stats is not None and _t_meta is not None:
                                stats["metadata_ms"] = (
                                    time.perf_counter() - _t_meta
                                ) * 1000.0
                        except Exception:
                            log.debug(
                                "Failed to read EXIF from mmap for %s",
                                target_path,
                                exc_info=True,
                            )
        except Exception as e:
            log.warning(
                "Decode failed (%s) index=%d path=%s: %s",
                "ICC path" if want_icc else "mmap path",
                index,
                target_path,
                e,
            )
            buffer = None

        if buffer is not None and want_icc and not icc_metadata_read:
            try:
                with PILImage.open(target_path) as orig:
                    native_width, native_height = orig.size
                    icc_bytes = orig.info.get("icc_profile")
                    if orientation == 1:
                        orientation = orig.getexif().get(_EXIF_ORIENTATION_TAG, 1)
            except Exception as e:
                log.warning("Failed to read metadata from %s: %s", target_path, e)

    if buffer is None:
        try:
            with PILImage.open(target_path) as img:
                native_width, native_height = img.size
                orientation = img.getexif().get(_EXIF_ORIENTATION_TAG, 1)
                if want_icc:
                    icc_bytes = img.info.get("icc_profile")
                img = img.convert("RGB")
                if should_resize:
                    img.thumbnail(
                        (display_width, display_height),
                        PILImage.Resampling.LANCZOS,
                    )
                buffer = np.array(img)
        except Exception as e:
            log.warning("Decode failed index=%d path=%s: %s", index, target_path, e)
            buffer = _make_raw_placeholder(display_width, display_height)
            placeholder_reason = (
                "RAW preview could not be decoded"
                if target_path.suffix.lower() in _RAW_EXTENSIONS
                else "Image could not be decoded"
            )
            if stats is not None:
                stats["placeholder_reason"] = placeholder_reason

    if buffer is not None and (native_width <= 0 or native_height <= 0):
        native_height, native_width = buffer.shape[:2]
    return (
        buffer,
        orientation,
        icc_bytes,
        native_width,
        native_height,
        placeholder_reason,
    )


class Prefetcher:
    def __init__(
        self,
        image_files: List[ImageFile],
        cache_put: Callable,
        prefetch_radius: int,
        get_display_info: Callable,
        debug: bool = False,
        cache_contains: Optional[Callable[[str], bool]] = None,
        cache_get_quality: Optional[Callable[[str], Optional[str]]] = None,
        quality_is_current: Optional[Callable[..., bool]] = None,
        nav_trace: Optional[Callable[[dict], None]] = None,
        fast_decode_max_dimension: int = HELD_NAVIGATION_QUALITY_DIMENSIONS[
            DEFAULT_HELD_NAVIGATION_QUALITY
        ],
        configured_workers: int = 0,
    ):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.get_display_info = get_display_info
        self.debug = debug
        # Optional per-decode telemetry sink (navigation trace). Called from a
        # worker thread with a plain dict of stage timings + decode details;
        # must not touch Qt. See AppController._record_nav_worker_timing.
        self.nav_trace = nav_trace
        # Optional fast-path: lets _decode_and_cache short-circuit when the
        # target cache key is already present, so a resubmit that races a
        # completed decode (or a resubmit after unschedule_path()) is cheap
        # instead of re-decoding the file.
        self.cache_contains = cache_contains
        self.cache_get_quality = cache_get_quality
        self.quality_is_current = quality_is_current
        self.fast_decode_max_dimension = max(1, int(fast_decode_max_dimension))
        # Reserve one worker for an exact demand miss. Speculative TurboJPEG,
        # resize, and ICC work otherwise occupies every worker and a priority
        # bump cannot preempt a decode that has already started.
        # Auto-size the total to ~3/4 of the logical cores, clamped to
        # [2, 16]: decode throughput still scales at 11 speculative workers
        # on a 16-thread machine (tools/bench_decode.py), while the previous
        # flat cap of 8 both left fast machines idle and oversubscribed
        # 4-thread ones, inflating the latency of the one demand decode the
        # UI may be waiting on. core.prefetch_workers overrides the total
        # (speculative + the one reserved demand worker); 0 keeps auto.
        optimal_workers = min(16, max(2, ((os.cpu_count() or 4) * 3) // 4))
        if configured_workers > 0:
            optimal_workers = max(2, min(int(configured_workers), 32))
        self.total_decode_workers = optimal_workers
        speculative_workers = max(1, optimal_workers - 1)

        self.executor = create_priority_executor(
            max_workers=speculative_workers,
            thread_name_prefix="Prefetcher",
        )
        self.demand_executor = create_priority_executor(
            max_workers=1,
            thread_name_prefix="DemandDecoder",
        )
        # The configured monitor profile is known before QML finishes loading.
        # Build its overwhelmingly common sRGB transform during that otherwise
        # idle time instead of adding ~200 ms to the first visible image.
        self.executor.submit(
            _prewarm_common_icc_transform,
            priority=_PRIORITY_PREWARM,
        )
        self._futures_lock = threading.RLock()
        self.futures: Dict[int, Future] = {}
        self.future_paths: Dict[int, Path] = {}
        self.future_quality: Dict[int, DecodeQuality] = {}
        self._demand_indices: set[int] = set()
        self.generation = 0
        self._scheduled: Dict[int, set] = {}  # generation -> set of scheduled indices
        self._active_window: set[int] = set()
        self._next_trace_task_id = 0

        # Cooperative cancellation flag for shutdown
        self._stop_event = threading.Event()

        # The display-size gate already prevents premature work. Warm the full
        # configured window while the user is idle instead of waiting until the
        # held-key burst has already begun.
        self._initial_radius = self.prefetch_radius
        self._navigation_count = 0  # Track how many times user has navigated
        self._radius_expanded = True

        # Directional prefetching
        self._last_navigation_direction: int = 1  # 1 = forward, -1 = backward

    def set_image_files(self, image_files: List[ImageFile]):
        with self._futures_lock:
            if self.image_files == image_files:
                return
            old = self.image_files
            self.image_files = image_files

            # A save changes one entry's metadata (timestamp/backup flag);
            # cancelling the whole generation would force the entire prefetch
            # window to re-decode. When the list shape is unchanged and only a
            # few entries differ, invalidate just those indices.
            if len(old) == len(image_files):
                changed = [
                    i for i, (a, b) in enumerate(zip(old, image_files)) if a != b
                ]
                if len(changed) <= 8:
                    for i in changed:
                        fut = self.futures.get(i)
                        if fut is not None:
                            fut.cancel()
                        self.futures.pop(i, None)
                        self.future_paths.pop(i, None)
                        self.future_quality.pop(i, None)
                        self._demand_indices.discard(i)
                        for scheduled in self._scheduled.values():
                            scheduled.discard(i)
                    return

            self._cancel_all_locked()

    @staticmethod
    def _bump_executor_priority(executor, future: Future, priority: int) -> bool:
        """Raise a logical task's queued physical execution priority."""
        execution = (
            future.execution_for(executor)
            if isinstance(future, _DecodeTaskFuture)
            else future
        )
        if execution is None:
            return False
        return bool(
            executor.bump_priority(
                execution,
                priority,
                queue_group=_QUEUE_GROUP_INTERACTIVE,
            )
        )

    def update_prefetch(
        self,
        current_index: int,
        is_navigation: bool = False,
        direction: Optional[int] = None,
    ):
        """Updates the prefetching queue based on the current image index.

        Args:
            current_index: The index to prefetch around
            is_navigation: True if this is from user navigation (arrow keys, etc.)
            direction: 1 for forward, -1 for backward, None to use last direction
        """
        if self.debug:
            _t_start = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} update_prefetch: START index={current_index} dir={direction}"
            )

        # NOTE: Generation is NOT incremented here. It only changes when display size,
        # zoom state, or color mode changes - events that actually invalidate cached images.
        # Navigation just shifts which indices to prefetch.

        # Track navigation direction
        if direction is not None:
            self._last_navigation_direction = direction

        # Track navigation to expand radius after user starts moving
        if is_navigation and not self._radius_expanded:
            self._navigation_count += 1
            if self._navigation_count >= 2:
                self._radius_expanded = True
                log.info(
                    "Expanding prefetch radius from %d to %d after user navigation",
                    self._initial_radius,
                    self.prefetch_radius,
                )

        # Use a smaller radius initially to reduce cache thrash before display
        # size is stable, but never exceed a user-configured smaller radius.
        effective_radius = (
            min(self._initial_radius, self.prefetch_radius)
            if not self._radius_expanded
            else self.prefetch_radius
        )

        if self.debug:
            log.info(
                "Prefetch radius: initial=%d, configured=%d, effective=%d",
                self._initial_radius,
                self.prefetch_radius,
                effective_radius,
            )

        # Directional prefetching favors travel direction while retaining a
        # small tail behind the current image for short backtracking.
        if effective_radius <= 0:
            behind = 0
            ahead = 0
        elif self._last_navigation_direction > 0:  # Moving forward
            behind = min(
                effective_radius,
                max(_MIN_DIRECTION_TAIL, 1),
            )
            ahead = effective_radius
        else:  # Moving backward
            ahead = min(
                effective_radius,
                max(_MIN_DIRECTION_TAIL, 1),
            )
            behind = effective_radius

        # Invariant: All reads/writes of self.futures, self._scheduled, self.generation,
        # and self.image_files that participate in scheduling or cancellation MUST
        # happen under _futures_lock.

        tasks_submitted = 0

        # Snapshotting and range computation inside lock
        with self._futures_lock:
            image_files = self.image_files
            n = len(image_files)
            if n == 0:
                if self.debug:
                    _t_end = time.perf_counter()
                    log.info(
                        f"[DBGCACHE] {_t_end*1000:.3f} update_prefetch: DONE submitted=0 empty=True total={(_t_end - _t_start)*1000:.2f}ms"
                    )
                return
            # Ensure current_index is clamped
            safe_current = max(0, min(n - 1, current_index))

            start = max(0, safe_current - behind)
            end = min(n, safe_current + ahead + 1)
            self._active_window = set(range(start, end))

            log.debug(
                "Prefetch range: [%d, %d) for index %d (direction=%d, behind=%d, ahead=%d)",
                start,
                end,
                safe_current,
                self._last_navigation_direction,
                behind,
                ahead,
            )

            # Build priority order: current first, then in direction of travel
            priority_order = [safe_current]
            if self._last_navigation_direction > 0:
                priority_order.extend(range(safe_current + 1, end))
                priority_order.extend(range(safe_current - 1, start - 1, -1))
            else:
                priority_order.extend(range(safe_current - 1, start - 1, -1))
                priority_order.extend(range(safe_current + 1, end))

            # Clean up old generation entries to prevent memory leak
            old_generations = [
                g for g in list(self._scheduled.keys()) if g < self.generation
            ]
            for g in old_generations:
                self._scheduled.pop(g, None)

            # Get scheduled set for current generation
            scheduled = self._scheduled.setdefault(self.generation, set())

            for index, future in list(self.futures.items()):
                if index < start or index >= end:
                    task_quality = self.future_quality.get(index)
                    if future.cancel():
                        if task_quality == "cover":
                            self._report_queued_cancellation(
                                index,
                                future,
                                "outside-prefetch-window",
                            )
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                        self._demand_indices.discard(index)
                        scheduled.discard(index)

            for rank, i in enumerate(priority_order):
                if i < 0 or i >= n:
                    continue
                desired_priority = _PRIORITY_PREFETCH_BASE + rank
                future = self.futures.get(i)
                if (
                    future is not None
                    and not future.running()
                    and not future.done()
                    and self.future_quality.get(i) == "fast"
                    and self._bump_executor_priority(
                        self.executor,
                        future,
                        desired_priority,
                    )
                ):
                    # A far-edge task starts at low urgency, but becomes one of
                    # the next contiguous frames as navigation advances. Raise
                    # its queued priority now so newer far-edge submissions do
                    # not jump ahead through the executor's LIFO tie ordering.
                    trace_meta = getattr(future, "_faststack_trace_meta", None)
                    if isinstance(trace_meta, dict):
                        trace_meta["priority"] = desired_priority
                        trace_meta["queue_group"] = _QUEUE_GROUP_INTERACTIVE
                elif i not in scheduled and future is None:
                    self.submit_task(
                        i,
                        self.generation,
                        quality="fast",
                        queue_priority=desired_priority,
                        window_bound=True,
                    )
                    scheduled.add(i)
                    tasks_submitted += 1

        if self.debug:
            _t_end = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_end*1000:.3f} update_prefetch: DONE submitted={tasks_submitted} total={(_t_end - _t_start)*1000:.2f}ms"
            )

    def submit_task(
        self,
        index: int,
        generation: int,
        priority: bool = False,
        override_path: Optional[Path] = None,
        quality: DecodeQuality = "fast",
        quality_token: Optional[int] = None,
        quality_index: Optional[int] = None,
        queue_priority: Optional[int] = None,
        queue_group: int = _QUEUE_GROUP_INTERACTIVE,
        queue_order: Optional[int] = None,
        preload_order: Optional[int] = None,
        nav_seq: Optional[int] = None,
        window_bound: bool = False,
    ) -> Optional[Future]:
        """Submits a decoding task for a given index."""
        if self._stop_event.is_set():
            return None

        if self.debug and priority:
            _t_start = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} submit_task: PRIORITY index={index} gen={generation} override={override_path} quality={quality}"
            )

        with self._futures_lock:
            # Bounds check must happen inside the lock to stay consistent
            # with self.image_files (which set_image_files can replace under lock).
            if index < 0 or index >= len(self.image_files):
                return None

            requested_path = (
                override_path
                if override_path is not None
                else self.image_files[index].path
            )
            if preload_order is not None:
                queue_group = _QUEUE_GROUP_PRELOAD
                queue_order = preload_order

            existing_future = self.futures.get(index)
            existing_same_path_future = None
            decode_after_future = None

            # We track by index. If we already have a job for this index, we
            # must cancel it if the requested path is different (e.g. switching
            # between main and variants). Same-path priority requests still run
            # the cancellation pass below before reusing the existing future.
            if existing_future is not None and not existing_future.done():
                current_path = self.future_paths.get(index)
                current_quality = self.future_quality.get(index, "fast")
                if current_path != requested_path:
                    # Force cancel the old one to switch paths
                    canceled = existing_future.cancel()
                    if canceled and current_quality == "cover":
                        self._report_queued_cancellation(
                            index,
                            existing_future,
                            "path-superseded",
                        )
                    self.futures.pop(index, None)
                    self.future_paths.pop(index, None)
                    self.future_quality.pop(index, None)
                    self._demand_indices.discard(index)
                elif quality == "cover" and current_quality == "fast":
                    if existing_future.cancel():
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                        self._demand_indices.discard(index)
                    else:
                        # The fast decode is already running. Chain the
                        # settled cover decode after it so a running fast
                        # future cannot cause the quality upgrade to be
                        # skipped, while keeping one tracked future per index.
                        decode_after_future = existing_future
                elif quality == "fast" and current_quality == "cover":
                    if existing_future.cancel():
                        self._report_queued_cancellation(
                            index,
                            existing_future,
                            "demand-fast-superseded-cover",
                        )
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                        self._demand_indices.discard(index)
                    else:
                        # The cover decode is already running. Stop tracking it
                        # so this index can submit and wait on a fresh fast
                        # decode; _cleanup_future uses identity checks, so the
                        # orphaned cover callback will not remove the fast one.
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                        self._demand_indices.discard(index)
                else:
                    existing_same_path_future = existing_future

            if priority:
                cancelled_count = 0
                safe_radius = 2
                direction = self._last_navigation_direction

                for task_index, future in list(self.futures.items()):
                    if task_index == index or abs(task_index - index) <= safe_radius:
                        continue

                    # Don't cancel tasks ahead of travel direction — we'll need them
                    if direction > 0 and task_index > index:
                        continue
                    if direction < 0 and task_index < index:
                        continue

                    task_quality = self.future_quality.get(task_index)
                    if not future.done() and future.cancel():
                        if task_quality == "cover":
                            self._report_queued_cancellation(
                                task_index,
                                future,
                                "demand-priority",
                            )
                        cancelled_count += 1
                        self.futures.pop(task_index, None)
                        self.future_paths.pop(task_index, None)
                        self.future_quality.pop(task_index, None)
                        self._demand_indices.discard(task_index)
                        # This index is no longer decoded/queued -- allow it
                        # to be resubmitted, mirroring update_prefetch()'s
                        # own out-of-window cancel loop.
                        for scheduled in self._scheduled.values():
                            scheduled.discard(task_index)
                if cancelled_count > 0:
                    log.debug(
                        "Cancelled %d pending prefetch tasks to prioritize index %d",
                        cancelled_count,
                        index,
                    )

            if existing_same_path_future is not None:
                # A navigation can adopt a queued/running look-ahead decode.
                # Update the mutable logical-task metadata so its eventual
                # pixels retain the demand seq and the wait experienced *after*
                # the user targeted the image.
                existing_meta = getattr(
                    existing_same_path_future,
                    "_faststack_trace_meta",
                    None,
                )
                prior_seq = (
                    existing_meta.get("seq")
                    if isinstance(existing_meta, dict)
                    else None
                )
                migration_state = "reused"
                priority_bumped = False
                if priority and quality == "fast":
                    demand_t = time.perf_counter()
                    self._demand_indices.add(index)
                    if isinstance(existing_meta, dict):
                        if nav_seq is not None:
                            existing_meta["seq"] = nav_seq
                        existing_meta["demand_t"] = demand_t
                        existing_meta["role"] = "demand-reused"
                        worker_start_t = existing_meta.get("worker_start_t")
                        if isinstance(worker_start_t, (int, float)):
                            existing_meta["queue_ms"] = max(
                                0.0,
                                (worker_start_t - demand_t) * 1000.0,
                            )
                            existing_meta["running_before_demand_ms"] = max(
                                0.0,
                                (demand_t - worker_start_t) * 1000.0,
                            )

                    if isinstance(existing_same_path_future, _DecodeTaskFuture):
                        demand_queue_depth = self.demand_executor.qsize()
                        migration_state = (
                            existing_same_path_future.migrate_queued_execution(
                                self.executor,
                                self.demand_executor,
                                priority=_PRIORITY_DEMAND,
                            )
                        )
                        if migration_state == "migrated":
                            if isinstance(existing_meta, dict):
                                existing_meta["role"] = "demand-migrated"
                                existing_meta["priority"] = _PRIORITY_DEMAND
                                existing_meta["queue_group"] = _QUEUE_GROUP_INTERACTIVE
                                existing_meta["migrated_to_demand"] = True
                                existing_meta["migration_t"] = demand_t
                                existing_meta["prefetch_queue_depth"] = (
                                    existing_meta.get("queue_depth")
                                )
                                existing_meta["queue_depth"] = demand_queue_depth
                            log.debug(
                                "Migrated queued speculative decode to reserved "
                                "demand executor: index=%d path=%s",
                                index,
                                requested_path,
                            )
                    else:
                        priority_bumped = self._bump_executor_priority(
                            self.executor,
                            existing_same_path_future,
                            _PRIORITY_DEMAND,
                        )

                    if isinstance(existing_meta, dict) and priority_bumped:
                        existing_meta["priority"] = _PRIORITY_DEMAND
                        existing_meta["queue_group"] = _QUEUE_GROUP_INTERACTIVE
                        existing_meta["priority_bumped"] = True

                elif isinstance(existing_meta, dict) and nav_seq is not None:
                    existing_meta["seq"] = nav_seq

                if (
                    self.nav_trace is not None
                    and isinstance(existing_meta, dict)
                    and (nav_seq is not None or priority)
                ):
                    try:
                        self.nav_trace(
                            {
                                "event": (
                                    "migrated"
                                    if migration_state == "migrated"
                                    else "adopted"
                                ),
                                "task_id": existing_meta.get("task_id"),
                                "index": index,
                                "source_path": existing_meta.get("source_path"),
                                "prior_seq": prior_seq,
                                "seq": existing_meta.get("seq"),
                                "state": migration_state,
                                "priority": existing_meta.get("priority"),
                                "priority_bumped": bool(
                                    existing_meta.get("priority_bumped")
                                ),
                                "migrated_to_demand": bool(
                                    existing_meta.get("migrated_to_demand")
                                ),
                            }
                        )
                    except Exception:
                        log.debug("nav_trace adoption sink raised", exc_info=True)
                return existing_same_path_future

            if queue_priority is None:
                if priority and quality == "fast":
                    queue_priority = _PRIORITY_DEMAND
                elif quality == "cover":
                    queue_priority = _PRIORITY_COVER
                else:
                    queue_priority = _PRIORITY_PREFETCH_BASE

            image_file = self.image_files[index]
            display_width, display_height, display_generation = self.get_display_info()

            # Submit-time metadata: the nav seq the demand belongs to, the queue
            # priority actually used, the enqueue timestamp (for queue_wait), and
            # the queue depth / in-flight count AT SUBMIT (not after decoding).
            target_executor = (
                self.demand_executor
                if priority and quality == "fast"
                else self.executor
            )

            role = (
                "cover"
                if quality == "cover"
                else (
                    "demand"
                    if priority
                    else "preload" if preload_order is not None else "prefetch"
                )
            )
            if self.nav_trace is not None:
                self._next_trace_task_id += 1
                enqueue_t = time.perf_counter()
                submit_meta = {
                    "task_id": self._next_trace_task_id,
                    "seq": nav_seq,
                    "role": role,
                    "quality": quality,
                    "priority": queue_priority,
                    "queue_group": queue_group,
                    "queue_order": queue_order,
                    "source_path": str(requested_path),
                    "display_generation": display_generation,
                    "enqueue_t": enqueue_t,
                    "demand_t": enqueue_t if nav_seq is not None else None,
                    "queue_depth": target_executor.qsize(),
                    "inflight": len(self.futures),
                    "window_bound": window_bound,
                }
            else:
                # Role is correctness metadata as well as telemetry: an
                # adopted window-bound prefetch must remain eligible after it
                # becomes exact demand, even when tracing is disabled.
                submit_meta = {
                    "seq": nav_seq,
                    "role": role,
                    "quality": quality,
                    "source_path": str(requested_path),
                    "window_bound": window_bound,
                }

            decode_args = (
                image_file,
                index,
                generation,
                display_width,
                display_height,
                display_generation,
                override_path,
                quality,
                quality_token,
                quality_index if quality_index is not None else index,
                window_bound,
                submit_meta,
            )
            if decode_after_future is not None:
                worker = self._decode_after_future
                worker_args = (decode_after_future, *decode_args)
            else:
                worker = self._decode_and_cache
                worker_args = decode_args
            future = _DecodeTaskFuture(worker, worker_args)
            # Future safely carries mutable diagnostics shared with the worker.
            # This is also the stable identity returned to every caller.
            setattr(future, "_faststack_trace_meta", submit_meta)
            try:
                future.submit_execution(
                    target_executor,
                    priority=queue_priority,
                    queue_group=(
                        queue_group
                        if target_executor is self.executor
                        else _QUEUE_GROUP_INTERACTIVE
                    ),
                    queue_order=(
                        queue_order if target_executor is self.executor else None
                    ),
                )
            except RuntimeError:
                return None
            self.futures[index] = future
            self.future_paths[index] = requested_path
            self.future_quality[index] = quality
            if priority and quality == "fast":
                self._demand_indices.add(index)
            future.add_done_callback(lambda f, idx=index: self._cleanup_future(idx, f))
            return future

    def _decode_after_future(
        self,
        previous_future: Future,
        image_file: ImageFile,
        index: int,
        generation: int,
        display_width: int,
        display_height: int,
        display_generation: int,
        override_path: Optional[Path],
        quality: DecodeQuality,
        quality_token: Optional[int],
        quality_index: int,
        window_bound: bool,
        submit_meta: Optional[dict] = None,
    ) -> Optional[tuple[Path, int]]:
        """Run a decode after a same-index lower-quality decode has finished."""
        dependency_started = time.perf_counter() if submit_meta else None
        try:
            previous_future.result()
        except Exception:
            pass
        chained_meta = dict(submit_meta or {})
        if dependency_started is not None:
            chained_meta["dependency_ms"] = (
                time.perf_counter() - dependency_started
            ) * 1000.0
        # The cover decode itself starts now. Preserve dependency_ms separately
        # rather than pretending that dependency time was executor queue time.
        if chained_meta:
            chained_meta["enqueue_t"] = time.perf_counter()
        return self._decode_and_cache(
            image_file,
            index,
            generation,
            display_width,
            display_height,
            display_generation,
            override_path,
            quality,
            quality_token,
            quality_index,
            window_bound,
            submit_meta=chained_meta,
        )

    def _decode_and_cache(
        self,
        image_file: ImageFile,
        index: int,
        generation: int,
        display_width: int,
        display_height: int,
        display_generation: int,
        override_path: Optional[Path] = None,
        quality: DecodeQuality = "fast",
        quality_token: Optional[int] = None,
        quality_index: int = -1,
        window_bound: bool = False,
        submit_meta: Optional[dict] = None,
    ) -> Optional[tuple[Path, int]]:
        """The actual work done by the thread pool."""
        if generation != self.generation or self._stop_event.is_set():
            return None

        # Navigation trace is intentionally zero-cost outside diagnostic runs.
        _t_worker_start = time.perf_counter() if self.nav_trace is not None else None
        _meta = submit_meta or {}
        if self.nav_trace is not None and _t_worker_start is not None:
            _meta["worker_start_t"] = _t_worker_start
        enqueue_t = _meta.get("enqueue_t")
        # In diagnostic runs the decoded buffer retains this exact mutable
        # dictionary. If a demand adopts a running prefetch, submit_task updates
        # seq/role/priority in-place and the pixels keep that exact history.
        _stats: dict = _meta if self.nav_trace is not None else {}

        def quality_work_is_current() -> bool:
            if (
                quality != "cover"
                or quality_token is None
                or self.quality_is_current is None
            ):
                return True
            return bool(
                self.quality_is_current(
                    index=quality_index,
                    display_generation=display_generation,
                    token=quality_token,
                )
            )

        def fast_work_is_relevant() -> bool:
            if quality != "fast" or not window_bound:
                return True
            # A queued/running look-ahead task can be adopted by an exact
            # demand request. Its mutable trace role changes at adoption, so it
            # must finish even if navigation has already shifted the window.
            if _meta.get("role") in {
                "demand",
                "demand-reused",
                "demand-migrated",
            }:
                return True
            with self._futures_lock:
                return index in self._active_window or index in self._demand_indices

        def bail_stale(stage: str) -> None:
            """Report a stale-cover cancellation (stage + wasted work), return None."""
            if self.nav_trace is not None and _t_worker_start is not None:
                try:
                    self.nav_trace(
                        {
                            "canceled": True,
                            "stage": stage,
                            "wasted_ms": (time.perf_counter() - _t_worker_start)
                            * 1000.0,
                            "index": index,
                            "source_path": _meta.get("source_path"),
                            "quality": quality,
                            "seq": _meta.get("seq"),
                            "task_id": _meta.get("task_id"),
                            "role": _meta.get("role"),
                            "priority": _meta.get("priority"),
                        }
                    )
                except Exception:
                    log.debug("nav_trace sink raised", exc_info=True)
            return None

        # Drop stale cover work and out-of-window speculative work before
        # touching the file. Running jobs check again between expensive stages.
        if not quality_work_is_current():
            return bail_stale("queued")
        if not fast_work_is_relevant():
            return bail_stale("queued-outside-window")

        # Use override path if provided, otherwise default to image_file.path
        target_path = override_path if override_path is not None else image_file.path

        cache_quality: DecodeQuality = (
            quality if display_width > 0 and display_height > 0 else "cover"
        )
        cache_key = build_cache_key(
            target_path,
            display_generation,
            cache_quality,
        )
        if self.cache_get_quality is not None:
            cached_quality = self.cache_get_quality(cache_key)
            if cached_quality == "cover" or (
                cached_quality == "fast" and quality == "fast"
            ):
                return (target_path, display_generation)
        elif self.cache_contains is not None:
            if self.cache_contains(cache_key):
                # Already cached (e.g. this resubmit raced a decode that just
                # completed, or update_prefetch()/unschedule_path() rescheduled
                # an index whose entry was never actually evicted). The
                # contract of the return value is "this path/generation is
                # now in the cache", which still holds -- skip the redundant
                # decode.
                return (target_path, display_generation)

        # Captured BEFORE the file is read: cache_put consumers compare this
        # against per-path invalidation epochs to reject decodes whose source
        # file was replaced (saved) while the decode was in flight.
        decode_started = time.monotonic()

        try:
            source_bytes = os.path.getsize(target_path)
            if source_bytes == 0:
                log.warning(
                    "Empty image file will display a decode placeholder: %s",
                    target_path,
                )
            if self.nav_trace is not None:
                _stats["source_bytes"] = source_bytes

            color_mode = config.get("color", "mode", fallback="none").lower()
            optimize_for = config.get("core", "optimize_for", fallback="speed").lower()
            fast_dct = optimize_for == "speed" and quality == "fast"
            use_resized = optimize_for == "speed"
            if quality == "fast":
                # Held-key quality controls the decode size independently of
                # the global decoder preference. ``optimize_for`` may still
                # select fast-vs-accurate DCT, but it must not silently turn a
                # configured browsing tier into a full-resolution decode.
                use_resized = True
            should_resize = display_width > 0 and display_height > 0
            decode_width = display_width
            decode_height = display_height
            if quality == "fast" and should_resize:
                decode_width = min(display_width, self.fast_decode_max_dimension)
                decode_height = min(display_height, self.fast_decode_max_dimension)

            monitor_profile = None
            monitor_icc_path = ""
            want_icc = color_mode == "icc"
            if want_icc:
                monitor_profile = get_monitor_profile()
                monitor_icc_path = config.get(
                    "color", "monitor_icc_path", fallback=""
                ).strip()
                want_icc = monitor_profile is not None

            _t_decode_buffer = (
                time.perf_counter() if self.nav_trace is not None else None
            )
            (
                buffer,
                orientation,
                icc_bytes,
                native_width,
                native_height,
                placeholder_reason,
            ) = _decode_buffer(
                target_path,
                decode_width,
                decode_height,
                use_resized,
                should_resize,
                fast_dct,
                want_icc,
                quality,
                index,
                stats=_stats if self.nav_trace is not None else None,
            )
            if _t_decode_buffer is not None:
                _stats["decode_ms"] = (time.perf_counter() - _t_decode_buffer) * 1000.0

            if buffer is None:
                return None

            if not quality_work_is_current():
                return bail_stale("post-decode")
            if not fast_work_is_relevant():
                return bail_stale("post-decode-outside-window")

            _t_icc = time.perf_counter() if self.nav_trace is not None else None
            if want_icc and monitor_profile is not None:
                src_profile, src_profile_key = _get_source_profile(icc_bytes)

                try:
                    transform = get_icc_transform(
                        src_profile,
                        monitor_profile,
                        src_profile_key,
                        monitor_icc_path,
                    )
                    buffer = apply_icc_transform_to_buffer(buffer, transform)
                except Exception as e:
                    log.warning("ICC conversion failed: %s", e)
            if _t_icc is not None:
                _stats["icc_ms"] = (time.perf_counter() - _t_icc) * 1000.0

            if not quality_work_is_current():
                return bail_stale("post-icc")
            if not fast_work_is_relevant():
                return bail_stale("post-icc-outside-window")

            buffer = np.ascontiguousarray(buffer)

            # Apply captured orientation
            try:
                if orientation > 1:
                    buffer = apply_orientation_to_np(buffer, orientation)
                if orientation in (5, 6, 7, 8):
                    native_width, native_height = native_height, native_width
            except Exception as e:
                log.warning("Failed to apply EXIF orientation: %s", e)

            bytes_per_line = buffer.strides[0]

            if color_mode == "saturation":
                _apply_configured_saturation(buffer)
                bytes_per_line = buffer.strides[0]

            decoded_quality = quality if should_resize else "cover"
            if self.nav_trace is not None:
                _stats.setdefault("source", (native_width, native_height))
                _stats.setdefault("output", (buffer.shape[1], buffer.shape[0]))
                demand_t = _meta.get("demand_t")
                if isinstance(demand_t, (int, float)):
                    # An adopted task that was already running had no executor
                    # queue wait after the user targeted it.
                    demand_queue_ms = max(
                        0.0,
                        (_t_worker_start - demand_t) * 1000.0,
                    )
                    running_before_demand_ms = max(
                        0.0,
                        (demand_t - _t_worker_start) * 1000.0,
                    )
                else:
                    demand_queue_ms = (
                        (_t_worker_start - enqueue_t) * 1000.0
                        if isinstance(enqueue_t, (int, float))
                        else None
                    )
                    running_before_demand_ms = 0.0
                _stats.update(
                    {
                        "task_id": _meta.get("task_id"),
                        "seq": _meta.get("seq"),
                        "role": _meta.get("role"),
                        "index": index,
                        "display_generation": display_generation,
                        "quality": decoded_quality,
                        "priority": _meta.get("priority"),
                        "priority_bumped": bool(_meta.get("priority_bumped")),
                        "queue_ms": demand_queue_ms,
                        "prequeue_ms": (
                            (_t_worker_start - enqueue_t) * 1000.0
                            if isinstance(enqueue_t, (int, float))
                            else None
                        ),
                        "running_before_demand_ms": running_before_demand_ms,
                        "dependency_ms": _meta.get("dependency_ms"),
                        "native": (native_width, native_height),
                        "target": (display_width, display_height),
                        "decode_target": (decode_width, decode_height),
                        "queue_depth": _meta.get("queue_depth"),
                        "inflight": _meta.get("inflight"),
                        "source_path": str(target_path),
                        "cache_key": cache_key,
                    }
                )
            mv = memoryview(buffer).cast("B")
            decoded = DecodedImage(
                buffer=mv,
                width=buffer.shape[1],
                height=buffer.shape[0],
                bytes_per_line=bytes_per_line,
                format=QImage.Format.Format_RGB888 if QImage else None,
                quality=decoded_quality,
                native_width=native_width,
                native_height=native_height,
                source_path=str(target_path),
                cache_key=cache_key,
                decode_trace=_stats if self.nav_trace is not None else None,
                is_placeholder=placeholder_reason is not None,
                placeholder_reason=placeholder_reason,
            )

            if generation != self.generation or self._stop_event.is_set():
                return None

            _t_cache = time.perf_counter() if self.nav_trace is not None else None
            cache_result = self.cache_put(
                cache_key,
                decoded,
                target_path,
                decode_started,
                quality=decoded_quality,
                quality_token=quality_token,
                quality_index=quality_index,
                quality_display_generation=display_generation,
            )
            if _t_cache is not None:
                _stats["cache_ms"] = (time.perf_counter() - _t_cache) * 1000.0
                _stats["cache_accepted"] = cache_result is not False

            if self.nav_trace is not None:
                now = time.perf_counter()
                _stats["total_ms"] = (now - _t_worker_start) * 1000.0
                _stats["completed_t"] = now
                try:
                    self.nav_trace(_stats)
                except Exception:
                    log.debug("nav_trace sink raised", exc_info=True)

            return (target_path, display_generation)

        except Exception as e:
            # Downgraded from ERROR to prevent log noise on bad files
            log.warning("Error in _decode_and_cache: %s", e, exc_info=True)
            return None

    def has_active_future(
        self,
        index: int,
        expected_path: Optional[Path | str] = None,
    ) -> bool:
        """True if the exact requested image still has queued/running work."""
        with self._futures_lock:
            fut = self.futures.get(index)
            if fut is None or fut.done():
                return False
            if expected_path is None:
                return True
            active_path = self.future_paths.get(index)
            if active_path is None:
                return False
            return normalize_path_key(active_path) == normalize_path_key(expected_path)

    def _report_queued_cancellation(
        self,
        index: int,
        future: Future,
        reason: str,
    ) -> None:
        if self.nav_trace is None:
            return
        meta = getattr(future, "_faststack_trace_meta", None)
        if not isinstance(meta, dict):
            meta = {}
        try:
            self.nav_trace(
                {
                    "canceled": True,
                    "stage": f"queued:{reason}",
                    "wasted_ms": 0.0,
                    "index": index,
                    "source_path": meta.get("source_path"),
                    "quality": meta.get("quality") or self.future_quality.get(index),
                    "seq": meta.get("seq"),
                    "task_id": meta.get("task_id"),
                    "role": meta.get("role"),
                    "priority": meta.get("priority"),
                }
            )
        except Exception:
            log.debug("nav_trace cancellation sink raised", exc_info=True)

    def _cleanup_future(self, index: int, future: Future):
        """Removes the future from the tracking dictionary upon completion."""
        failed = future.cancelled()
        if not failed:
            try:
                failed = future.exception() is not None or future.result() is None
            except Exception:
                failed = True

        with self._futures_lock:
            # Only remove if it's the specific future we're tracking
            # (to avoid race if a new task for the same index was submitted)
            if self.futures.get(index) is future:
                self.futures.pop(index, None)
                self.future_paths.pop(index, None)
                self.future_quality.pop(index, None)
                self._demand_indices.discard(index)
                # Self-heal: if this future was cancelled by ANY cancellation
                # path, make sure the index isn't left permanently
                # "scheduled" with nothing actually decoded/cached for it.
                # Idempotent -- harmless if another cancel loop already
                # discarded it.
                if failed:
                    for scheduled in self._scheduled.values():
                        scheduled.discard(index)

    def invalidate_path(self, path: Path | str):
        """Targeted invalidation for one file (e.g. after it was re-saved).

        Cancels any in-flight decode for ``path`` and removes its index from
        the scheduled sets so the next update_prefetch() re-submits it. Unlike
        cancel_all(), this does not touch the generation counter, so decodes
        of OTHER paths stay valid.
        """
        path_str = Path(path).as_posix()
        with self._futures_lock:
            for idx, p in list(self.future_paths.items()):
                if Path(p).as_posix() == path_str:
                    fut = self.futures.get(idx)
                    if fut is not None:
                        fut.cancel()
                    self.futures.pop(idx, None)
                    self.future_paths.pop(idx, None)
                    self.future_quality.pop(idx, None)
                    self._demand_indices.discard(idx)
            for i, image_file in enumerate(self.image_files):
                if Path(image_file.path).as_posix() == path_str:
                    for scheduled in self._scheduled.values():
                        scheduled.discard(i)
                    break

    def unschedule_path(self, path) -> None:
        """Allow a path to be prefetched again after its cache entry was evicted.

        The ByteLRUCache evicts entries under memory pressure without any
        knowledge of the prefetcher's `_scheduled` bookkeeping. Without this,
        an index whose cached bytes were evicted stays marked "scheduled"
        for the rest of the generation and is never prefetched again, so in
        folders larger than the cache budget, navigating back to an
        already-evicted image is always a blocking decode.

        Unlike invalidate_path(), this does NOT cancel any in-flight future
        for the path (a decode already in progress will legitimately
        repopulate the cache) and does NOT touch the generation counter.

        Accepts str or Path and compares via as_posix() so it tolerates
        either -- the cache key itself is built from as_posix().
        """
        path_str = Path(path).as_posix()
        with self._futures_lock:
            for i, image_file in enumerate(self.image_files):
                if Path(image_file.path).as_posix() == path_str:
                    for scheduled in self._scheduled.values():
                        scheduled.discard(i)

    def cancel_pending_cover_tasks(self) -> None:
        """Cancel queued cover-quality work without invalidating the generation."""
        with self._futures_lock:
            for index, quality in list(self.future_quality.items()):
                if quality != "cover":
                    continue
                future = self.futures.get(index)
                if future is None:
                    continue
                if future.cancel():
                    self._report_queued_cancellation(
                        index,
                        future,
                        "quality-superseded",
                    )
                    self.futures.pop(index, None)
                    self.future_paths.pop(index, None)
                    self.future_quality.pop(index, None)
                    self._demand_indices.discard(index)
                    for scheduled in self._scheduled.values():
                        scheduled.discard(index)

    def _cancel_all_locked(self):
        """Internal helper to cancel all pending prefetching tasks.
        Assumes _futures_lock is already held.
        """
        self.generation += 1  # Invalidate in-flight tasks
        # Snapshot values before cancelling
        all_futures = list(self.futures.values())
        for future in all_futures:
            future.cancel()
        self.futures.clear()
        self.future_paths.clear()
        self.future_quality.clear()
        self._demand_indices.clear()
        self._active_window.clear()
        self._scheduled.clear()

    def cancel_all(self):
        """Cancels all pending prefetching tasks."""
        with self._futures_lock:
            self._cancel_all_locked()

    def shutdown(self):
        """Initiates a clean shutdown of the prefetcher."""
        log.info("Shutting down Prefetcher...")
        self._stop_event.set()
        self.cancel_all()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.demand_executor.shutdown(wait=False, cancel_futures=True)
