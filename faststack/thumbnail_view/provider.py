"""QML Image Provider for thumbnail grid view."""

import logging
import time
from urllib.parse import unquote
from pathlib import Path
from typing import TYPE_CHECKING, Optional, NamedTuple

import faststack.util.thumb_debug as thumb_debug

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QColor
from PySide6.QtQuick import QQuickImageProvider

from faststack.io.utils import compute_path_hash

if TYPE_CHECKING:
    from faststack.thumbnail_view.model import ThumbnailModel
    from faststack.thumbnail_view.prefetcher import ThumbnailPrefetcher, ThumbnailCache

log = logging.getLogger(__name__)

# Placeholder colors
PLACEHOLDER_COLOR = QColor(60, 60, 60)  # Neutral gray for loading
FOLDER_COLOR = QColor(80, 80, 80)  # Slightly different for folders
ERROR_COLOR = QColor(80, 40, 40)  # Dark red for errors


class ParsedId(NamedTuple):
    """Container for parsed thumbnail ID fields."""

    id_clean: str
    parts: list[str]
    thumb_size: Optional[int]
    path_hash: Optional[str]
    mtime_ns: Optional[int]
    reason: str
    is_folder: bool
    is_valid: bool


class ThumbnailProvider(QQuickImageProvider):
    """QML Image Provider for thumbnails.

    Non-blocking O(1) implementation:
    - Returns cached QImage if available
    - Returns placeholder immediately if not cached
    - Schedules decode via prefetcher (does NOT decode inline)

    URL format:
    - Files: image://thumbnail/{size}/{path_hash}/{mtime_ns}?r={rev}
    - Folders: image://thumbnail/folder/{path_hash}/{mtime_ns}?r={rev}
    """

    def __init__(
        self,
        cache: "ThumbnailCache",
        prefetcher: "ThumbnailPrefetcher",
        path_resolver: callable = None,
        default_size: int = 200,
        debug_timing: bool = False,
        debug_trace: bool = False,
    ):
        """Initialize the provider.

        Args:
            cache: Thumbnail cache to read from
            prefetcher: Prefetcher to schedule decodes
            path_resolver: Function to resolve path_hash to actual Path
            default_size: Default thumbnail size
            debug_timing: Enable [THUMB-TIMING] log lines
            debug_trace: Enable verbose trace logs
        """
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._cache = cache
        self._prefetcher = prefetcher
        self._path_resolver = path_resolver
        self._default_size = default_size
        self._debug_timing = debug_timing
        self._debug_trace = debug_trace

        # Pre-create placeholder images
        self._placeholder = self._create_placeholder(default_size, PLACEHOLDER_COLOR)
        self._folder_placeholder = self._create_folder_placeholder(default_size)
        self._error_placeholder = self._create_placeholder(default_size, ERROR_COLOR)

        # Timing stats for requestImage
        self._first_request_time: Optional[float] = None
        self._request_count = 0
        self._first_second_logged = False
        self._first_hit_logged = False

        log.debug("ThumbnailProvider initialized with default size %d", default_size)

    def _create_placeholder(self, size: int, color: QColor) -> QImage:
        """Create a solid color placeholder image."""
        image = QImage(size, size, QImage.Format.Format_RGB888)
        image.fill(color)
        return image

    def _create_folder_placeholder(self, size: int) -> QImage:
        """Create a folder icon placeholder."""
        from PySide6.QtGui import QPainter, QPen, QBrush

        image = QImage(size, size, QImage.Format.Format_RGB888)
        image.fill(FOLDER_COLOR)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw a simple folder shape
        pen = QPen(QColor(150, 150, 150))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(100, 100, 100)))

        # Folder body
        margin = size // 6
        tab_width = size // 3
        tab_height = size // 8

        # Tab at top
        painter.drawRect(
            margin,
            margin + tab_height,
            size - 2 * margin,
            size - 2 * margin - tab_height,
        )

        # Tab extension
        painter.fillRect(
            margin, margin, tab_width, tab_height + 2, QColor(100, 100, 100)
        )

        painter.end()
        return image

    def _parse_id(self, id_str: str) -> ParsedId:
        """Parse the thumbnail ID string.

        Format: {size}/{path_hash}/{mtime_ns}?r={rev}
        Or: folder/{path_hash}/{mtime_ns}?r={rev}

        Returns:
            ParsedId named tuple with extracted fields.
        """
        # Split query params
        parts_query = id_str.split("?")
        id_clean = parts_query[0]
        reason = "unknown"

        if len(parts_query) > 1:
            query = parts_query[1]
            for param in query.split("&"):
                if param.startswith("reason="):
                    # Robust parsing with split(..., 1) and URL decoding
                    reason = unquote(param.split("=", 1)[1])

        parts = id_clean.split("/")
        if len(parts) < 3:
            return ParsedId(id_clean, parts, None, None, None, reason, False, False)

        is_folder = parts[0] == "folder"
        try:
            # If folder, we don't have a thumb_size in the first part,
            # but we need path_hash and mtime_ns.
            if is_folder:
                thumb_size = self._default_size
                path_hash = parts[1]
                mtime_ns = int(parts[2])
            else:
                thumb_size = int(parts[0])
                path_hash = parts[1]
                mtime_ns = int(parts[2])
            return ParsedId(
                id_clean,
                parts,
                thumb_size,
                path_hash,
                mtime_ns,
                reason,
                is_folder,
                True,
            )
        except (ValueError, IndexError):
            return ParsedId(id_clean, parts, None, None, None, reason, is_folder, False)

    def requestImage(self, id_str: str, size: QSize, _requestedSize: QSize) -> QImage:
        """Request an image for the given ID.

        This method is O(1) - returns immediately with cached data or placeholder.

        Args:
            id_str: URL path after "image://thumbnail/"
            size: Output size reference (set by us)
            _requestedSize: Requested size from QML (unused)

        Returns:
            QImage of the thumbnail or placeholder
        """
        # Parse the ID
        parsed = self._parse_id(id_str)

        if not parsed.is_valid:
            log.debug("Invalid thumbnail ID: %s", id_str)
            size.setWidth(self._error_placeholder.width())
            size.setHeight(self._error_placeholder.height())
            return self._error_placeholder

        if parsed.is_folder:
            size.setWidth(self._folder_placeholder.width())
            size.setHeight(self._folder_placeholder.height())
            return self._folder_placeholder

        # Track total requests if logging enabled
        if thumb_debug.timing_enabled or thumb_debug.trace_enabled:
            thumb_debug.inc_request_count()

        # Deferred logging setup
        timer = None
        cache_key = parsed.id_clean

        # Resolve path - we already have path_hash and mtime_ns
        path = self._path_resolver(parsed.path_hash) if self._path_resolver else None

        if thumb_debug.timing_enabled or thumb_debug.trace_enabled:
            timer = thumb_debug.ThumbTimer(
                key=cache_key, path=path, reason=parsed.reason
            )
            thumb_debug.log_trace(
                "requested",
                rid=timer.rid,
                key=timer.key,
                src=timer.src,
                reason=parsed.reason,
            )

        # Check cache (O(1) lookup)
        t_cache_get_start = time.perf_counter() if timer else 0
        cached_bytes = self._cache.get(cache_key)
        dt_cache_get = (time.perf_counter() - t_cache_get_start) * 1000 if timer else 0

        if cached_bytes:
            if timer:
                thumb_debug.inc("req_cache_hit")
                thumb_debug.log_trace(
                    "cache_hit", rid=timer.rid, ms=f"{dt_cache_get:.3f}"
                )

            # Decode JPEG bytes to QImage
            t_decode_start = time.perf_counter() if timer else 0
            image = self._bytes_to_image(cached_bytes)
            dt_decode = (time.perf_counter() - t_decode_start) * 1000 if timer else 0

            if image is not None and not image.isNull():
                if timer:
                    thumb_debug.log_trace(
                        "delivered", rid=timer.rid, decode_ms=f"{dt_decode:.3f}"
                    )
                    timer.log_timing(
                        cache="hit",
                        cache_get_ms=f"{dt_cache_get:.3f}",
                        pixmap_ms=f"{dt_decode:.3f}",  # keep tag for consistency in logs
                    )
                size.setWidth(image.width())
                size.setHeight(image.height())
                return image
            else:
                # Decode of cached bytes failed — evict the bad entry so
                # the prefetcher can re-decode on the next request.
                if self._cache.discard(cache_key):
                    log.warning("Evicted bad cache entry: %s", cache_key)

        if timer:
            thumb_debug.inc("req_cache_miss")
            thumb_debug.log_trace("cache_miss", rid=timer.rid, ms=f"{dt_cache_get:.3f}")

        if path:
            self._prefetcher.submit(
                path,
                parsed.mtime_ns,
                parsed.thumb_size,
                priority=self._prefetcher.PRIO_HIGH,
                timer=timer,
            )

        # Return placeholder immediately (non-blocking)
        size.setWidth(self._placeholder.width())
        size.setHeight(self._placeholder.height())
        return self._placeholder

    def _bytes_to_image(self, jpeg_bytes: bytes) -> Optional[QImage]:
        """Convert JPEG bytes to QImage.

        Returns:
            QImage of the decoded bytes, or None on failure.
        """
        try:
            image = QImage()
            if image.loadFromData(jpeg_bytes, "JPEG"):
                return image
            log.warning("JPEG decode failed for cached bytes")
        except Exception as e:
            # Guard against Qt/C++ interop runtime errors or other failures during decode
            log.warning("Exception during JPEG decode from cache: %s", e, exc_info=True)
        return None


class PathResolver:
    """Resolves path hashes back to actual paths.

    Maintains a mapping from hash -> path for the current directory.
    """

    def __init__(self):
        self._hash_to_path: dict = {}

    def register(self, path: Path, path_hash: str):
        """Register a path with its hash."""
        self._hash_to_path[path_hash] = path

    def resolve(self, path_hash: str) -> Optional[Path]:
        """Resolve a hash to its path."""
        return self._hash_to_path.get(path_hash)

    def clear(self):
        """Clear all registered paths."""
        self._hash_to_path.clear()

    def update_from_model(self, model: "ThumbnailModel"):
        """Update registrations from a ThumbnailModel."""
        self.clear()

        t0 = time.perf_counter()

        # Optimized update using fast string hashing (no filesystem calls)
        for i in range(model.rowCount()):
            entry = model.get_entry(i)
            if entry and not entry.is_folder:
                # Use centralized hash helper to ensure match with ThumbnailModel
                path_hash = compute_path_hash(entry.path)
                self._hash_to_path[path_hash] = entry.path

        dt = time.perf_counter() - t0
        log.debug(
            "PathResolver update took %.2fms for %d items",
            dt * 1000,
            model.rowCount(),
        )
