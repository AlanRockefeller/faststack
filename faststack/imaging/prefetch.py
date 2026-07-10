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
from typing import Callable, Dict, List, Literal, Optional

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
from faststack.models import DecodedImage, ImageFile
from faststack.util.executors import create_daemon_threadpool_executor

log = logging.getLogger(__name__)

DecodeQuality = Literal["fast", "cover"]

# RAW extensions that Pillow typically cannot decode (no embedded JPEG preview).
# When decode fails for these, we generate a placeholder instead of returning None.
_RAW_EXTENSIONS = frozenset(
    {".orf", ".rw2", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".dng"}
)


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
) -> tuple[Optional[np.ndarray], int, Optional[bytes]]:
    """Decode an image to RGB pixels, returning EXIF orientation and optional ICC."""
    buffer = None
    icc_bytes = None
    orientation = 1
    is_jpeg = target_path.suffix.lower() in {".jpg", ".jpeg", ".jpe"}

    if is_jpeg:
        icc_metadata_read = False
        try:
            with open(target_path, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mmapped:
                    if use_resized and should_resize:
                        buffer = decode_jpeg_resized(
                            mmapped,
                            display_width,
                            display_height,
                            fast_dct=fast_dct,
                            source_path=str(target_path),
                            mode=decode_quality,
                        )
                    else:
                        buffer = decode_jpeg_rgb(
                            mmapped,
                            fast_dct=fast_dct,
                            source_path=str(target_path),
                        )
                        if buffer is not None and should_resize:
                            img = PILImage.fromarray(buffer)
                            img.thumbnail(
                                (display_width, display_height),
                                PILImage.Resampling.LANCZOS,
                            )
                            buffer = np.array(img)

                    if buffer is not None:
                        try:
                            mmapped.seek(0)
                            with PILImage.open(mmapped) as pil_img:
                                if want_icc:
                                    icc_bytes = pil_img.info.get("icc_profile")
                                orientation = pil_img.getexif().get(
                                    _EXIF_ORIENTATION_TAG, 1
                                )
                                icc_metadata_read = want_icc
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
                    icc_bytes = orig.info.get("icc_profile")
                    if orientation == 1:
                        orientation = orig.getexif().get(_EXIF_ORIENTATION_TAG, 1)
            except Exception as e:
                log.warning("Failed to read metadata from %s: %s", target_path, e)

    if buffer is None:
        try:
            with PILImage.open(target_path) as img:
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
            if target_path.suffix.lower() in _RAW_EXTENSIONS:
                buffer = _make_raw_placeholder(display_width, display_height)
            else:
                return None, orientation, icc_bytes

    return buffer, orientation, icc_bytes


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
    ):
        self.image_files = image_files
        self.cache_put = cache_put
        self.prefetch_radius = prefetch_radius
        self.get_display_info = get_display_info
        self.debug = debug
        # Optional fast-path: lets _decode_and_cache short-circuit when the
        # target cache key is already present, so a resubmit that races a
        # completed decode (or a resubmit after unschedule_path()) is cheap
        # instead of re-decoding the file.
        self.cache_contains = cache_contains
        self.cache_get_quality = cache_get_quality
        # Use CPU count for I/O-bound JPEG decoding
        # Rule of thumb: 2x CPU cores for I/O bound, 1x for CPU bound
        optimal_workers = min(
            (os.cpu_count() or 1) * 2, 8
        )  # Cap at 8 for fast navigation

        self.executor = create_daemon_threadpool_executor(
            max_workers=optimal_workers,
            thread_name_prefix="Prefetcher",
        )
        self._futures_lock = threading.RLock()
        self.futures: Dict[int, Future] = {}
        self.future_paths: Dict[int, Path] = {}
        self.future_quality: Dict[int, DecodeQuality] = {}
        self.generation = 0
        self._scheduled: Dict[int, set] = {}  # generation -> set of scheduled indices

        # Cooperative cancellation flag for shutdown
        self._stop_event = threading.Event()

        # Adaptive prefetch: start with smaller radius, expand after user navigates
        self._initial_radius = 4  # Increased for faster initial responsiveness
        self._navigation_count = 0  # Track how many times user has navigated
        self._radius_expanded = False

        # Directional prefetching
        self._last_navigation_direction: int = 1  # 1 = forward, -1 = backward
        self._direction_bias: float = 0.85  # 85% of radius in travel direction

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
                        for scheduled in self._scheduled.values():
                            scheduled.discard(i)
                    return

            self._cancel_all_locked()

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
            behind = max(1, int(effective_radius * (1 - self._direction_bias)))
            ahead = effective_radius - behind + 1
            if ahead < 1:
                ahead = 1
                behind = effective_radius
        else:  # Moving backward
            ahead = max(1, int(effective_radius * (1 - self._direction_bias)))
            behind = effective_radius - ahead + 1
            if behind < 1:
                behind = 1
                ahead = effective_radius

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
                    if future.cancel():
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                        scheduled.discard(index)

            for i in priority_order:
                if i < 0 or i >= n:
                    continue
                if i not in scheduled and i not in self.futures:
                    self.submit_task(i, self.generation, quality="fast")
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
                    existing_future.cancel()
                    self.futures.pop(index, None)
                    self.future_paths.pop(index, None)
                    self.future_quality.pop(index, None)
                elif quality == "cover" and current_quality == "fast":
                    if existing_future.cancel():
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                    else:
                        # The fast decode is already running. Chain the
                        # settled cover decode after it so a running fast
                        # future cannot cause the quality upgrade to be
                        # skipped, while keeping one tracked future per index.
                        decode_after_future = existing_future
                elif quality == "fast" and current_quality == "cover":
                    if existing_future.cancel():
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
                    else:
                        # The cover decode is already running. Stop tracking it
                        # so this index can submit and wait on a fresh fast
                        # decode; _cleanup_future uses identity checks, so the
                        # orphaned cover callback will not remove the fast one.
                        self.futures.pop(index, None)
                        self.future_paths.pop(index, None)
                        self.future_quality.pop(index, None)
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

                    if not future.done() and future.cancel():
                        cancelled_count += 1
                        self.futures.pop(task_index, None)
                        self.future_paths.pop(task_index, None)
                        self.future_quality.pop(task_index, None)
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
                return existing_same_path_future

            image_file = self.image_files[index]
            display_width, display_height, display_generation = self.get_display_info()

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
            )
            if decode_after_future is not None:
                future = self.executor.submit(
                    self._decode_after_future,
                    decode_after_future,
                    *decode_args,
                )
            else:
                future = self.executor.submit(self._decode_and_cache, *decode_args)
            self.futures[index] = future
            self.future_paths[index] = requested_path
            self.future_quality[index] = quality
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
    ) -> Optional[tuple[Path, int]]:
        """Run a decode after a same-index lower-quality decode has finished."""
        try:
            previous_future.result()
        except Exception:
            pass
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
    ) -> Optional[tuple[Path, int]]:
        """The actual work done by the thread pool."""
        if generation != self.generation or self._stop_event.is_set():
            return None

        # Use override path if provided, otherwise default to image_file.path
        target_path = override_path if override_path is not None else image_file.path

        cache_key = build_cache_key(target_path, display_generation)
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
            if os.path.getsize(target_path) == 0:
                log.warning("Skipping empty image file: %s", target_path)
                return None

            color_mode = config.get("color", "mode", fallback="none").lower()
            optimize_for = config.get("core", "optimize_for", fallback="speed").lower()
            fast_dct = optimize_for == "speed"
            use_resized = optimize_for == "speed"
            should_resize = display_width > 0 and display_height > 0

            monitor_profile = None
            monitor_icc_path = ""
            want_icc = color_mode == "icc"
            if want_icc:
                monitor_profile = get_monitor_profile()
                monitor_icc_path = config.get(
                    "color", "monitor_icc_path", fallback=""
                ).strip()
                want_icc = monitor_profile is not None

            buffer, orientation, icc_bytes = _decode_buffer(
                target_path,
                display_width,
                display_height,
                use_resized,
                should_resize,
                fast_dct,
                want_icc,
                quality,
                index,
            )

            if buffer is None:
                return None

            if want_icc and monitor_profile is not None:
                img = PILImage.fromarray(buffer)
                src_profile, src_profile_key = _get_source_profile(icc_bytes)

                try:
                    transform = get_icc_transform(
                        src_profile,
                        monitor_profile,
                        src_profile_key,
                        monitor_icc_path,
                    )
                    ImageCms.applyTransform(img, transform, inPlace=True)
                    buffer = np.array(img, dtype=np.uint8)
                except Exception as e:
                    log.warning("ICC conversion failed: %s", e)

            buffer = np.ascontiguousarray(buffer)

            # Apply captured orientation
            try:
                if orientation > 1:
                    buffer = apply_orientation_to_np(buffer, orientation)
            except Exception as e:
                log.warning("Failed to apply EXIF orientation: %s", e)

            bytes_per_line = buffer.strides[0]

            if color_mode == "saturation":
                _apply_configured_saturation(buffer)
                bytes_per_line = buffer.strides[0]

            decoded_quality = quality if should_resize else "cover"
            mv = memoryview(buffer).cast("B")
            decoded = DecodedImage(
                buffer=mv,
                width=buffer.shape[1],
                height=buffer.shape[0],
                bytes_per_line=bytes_per_line,
                format=QImage.Format.Format_RGB888 if QImage else None,
                quality=decoded_quality,
            )

            if generation != self.generation or self._stop_event.is_set():
                return None

            self.cache_put(
                cache_key,
                decoded,
                target_path,
                decode_started,
                quality=decoded_quality,
                quality_token=quality_token,
                quality_index=quality_index,
                quality_display_generation=display_generation,
            )
            return (target_path, display_generation)

        except Exception as e:
            # Downgraded from ERROR to prevent log noise on bad files
            log.warning("Error in _decode_and_cache: %s", e, exc_info=True)
            return None

    def _cleanup_future(self, index: int, future: Future):
        """Removes the future from the tracking dictionary upon completion."""
        with self._futures_lock:
            # Only remove if it's the specific future we're tracking
            # (to avoid race if a new task for the same index was submitted)
            if self.futures.get(index) is future:
                self.futures.pop(index, None)
                self.future_paths.pop(index, None)
                self.future_quality.pop(index, None)
                # Self-heal: if this future was cancelled by ANY cancellation
                # path, make sure the index isn't left permanently
                # "scheduled" with nothing actually decoded/cached for it.
                # Idempotent -- harmless if another cancel loop already
                # discarded it.
                if future.cancelled():
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
                    self.futures.pop(index, None)
                    self.future_paths.pop(index, None)
                    self.future_quality.pop(index, None)
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
