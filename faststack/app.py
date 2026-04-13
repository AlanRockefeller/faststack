"""Main application entry point for FastStack."""

import logging
import sys
import math
import struct
import shlex
import time
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set
from datetime import date, datetime
import os
import re
import shutil
import uuid
import functools
from collections import deque
from itertools import pairwise

# Must set before importing PySide6
os.environ["QT_LOGGING_RULES"] = (
    "qt.qpa.mime.warning=false;" "qt.qpa.fonts.warning=false"
)

import concurrent.futures
import threading
import subprocess
from faststack.ui.provider import ImageProvider, UIState
import PySide6
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtCore import (
    QUrl,
    QTimer,
    QObject,
    QEvent,
    QMetaObject,
    Signal,
    Slot,
    QMimeData,
    Qt,
    QPoint,
    QCoreApplication,  # noqa: F401 — patched by tests
)
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from PySide6.QtQml import QQmlApplicationEngine
from PIL import Image

Image.MAX_IMAGE_PIXELS = 200_000_000  # 200 megapixels, enough for most photos
# ⬇️ these are the ones that went missing
from faststack.config import config
from faststack.logging_setup import setup_logging
from faststack.models import ImageFile, DecodedImage, EntryMetadata
from faststack.io.indexer import (
    find_images,
    find_images_with_variants,
)  # noqa: F401 — find_images patched by tests
from faststack.io.variants import (
    VariantGroup,
    build_badge_list,
    get_group_key_for_path,
    norm_path,
)
from faststack.io.sidecar import SidecarManager
from faststack.io.watcher import Watcher
from faststack.io.helicon import launch_helicon_focus
from faststack.io.executable_validator import validate_executable_path
from faststack.io.utils import normalize_path_key
from faststack.imaging.cache import (
    ByteLRUCache,
    get_decoded_image_size,
    build_cache_key,
)
from faststack.imaging.prefetch import Prefetcher, clear_icc_caches
from faststack.ui.keystrokes import Keybinder
from faststack.imaging.editor import ImageEditor, ASPECT_RATIOS
from faststack.imaging.mask import DarkenSettings, MaskData, MaskStroke
from faststack.imaging.mask_engine import inverse_transform
from faststack.imaging.metadata import get_exif_data
from faststack.thumbnail_view import (
    ThumbnailModel,
    ThumbnailPrefetcher,
    ThumbnailCache,
    ThumbnailProvider,
    PathResolver,
)
from faststack.thumbnail_view.folder_stats import (
    clear_raw_count_cache,
    get_file_counts_by_extension,
)
import numpy as np
from faststack.io.indexer import RAW_EXTENSIONS, JPG_EXTENSIONS
from faststack.io.deletion import (
    confirm_permanent_delete,
    confirm_batch_permanent_delete,
    permanently_delete_image_files,
)
from faststack.deletion_types import (
    DeleteJob,
    DeleteResult,
    DeleteRecord,
    DeletionErrorCodes,
    UIStateRestoration,
)

# AWB thresholds on the -1..+1 normalized slider range.
# NOOP: skip corrections that are effectively imperceptible in the current gain model.
# LABEL: below this the direction word becomes "neutral" in the status message.
_AWB_NOOP_EPS = 0.005
_AWB_LABEL_EPS = 0.002


def _awb_direction(value: float, pos_label: str, neg_label: str) -> str:
    """Return a human-readable direction label for an AWB shift value."""
    if abs(value) < _AWB_LABEL_EPS:
        return "neutral"
    return pos_label if value > 0 else neg_label


def make_hdrop(paths):
    """
    Build a real CF_HDROP (DROPFILES) payload for Windows drag-and-drop.
    paths: list[str]
    """
    files_part = ("\0".join(paths) + "\0\0").encode("utf-16le")

    # DROPFILES header (20 bytes): <IiiII
    pFiles = 20
    pt_x = 0
    pt_y = 0
    fNC = 0
    fWide = 1  # wide chars
    header = struct.pack("<IiiII", pFiles, pt_x, pt_y, fNC, fWide)
    return header + files_part


log = logging.getLogger(__name__)

# Global flags for debug modes - set by main()
_debug_mode = False
_debug_thumb_timing = False
_debug_thumb_trace = False

# Cache Thrashing Detection Constants
CACHE_THRASH_WINDOW_SECS = 2.0
CACHE_THRASH_THRESHOLD = 5
CACHE_WARNING_COOLDOWN_SECS = 300


from faststack.util.executors import create_daemon_threadpool_executor


class AppController(QObject):
    dataChanged = Signal()  # New signal for general data changes
    is_zoomed_changed = Signal(bool)  # Signal for zoom state changes
    histogramReady = Signal(object)  # Signal for off-thread histogram result
    previewReady = Signal(object)  # Signal for off-thread preview result
    dialogStateChanged = Signal(bool)  # Signal for dialog open/close state
    # Thread-safe signal for thumbnail ready (emitted from worker thread, received on GUI thread)
    _thumbnailReadySignal = Signal(str)

    MAX_FAILED_RESTORATIONS_TO_LOG = 10

    @staticmethod
    def _key(p: Optional[Path]) -> Optional[str]:
        """Normalize path for consistent comparison without slow resolve()."""
        if p is None:
            return None
        return normalize_path_key(p)

    class ProgressReporter(QObject):
        progress_updated = Signal(int)
        finished = Signal()

    editSourceModeChanged = Signal(str)  # Notify when JPEG/RAW mode changes
    _saveFinished = Signal(
        object
    )  # Signal for save completion (result or error from background)
    _deleteFinished = Signal(
        object
    )  # Signal for async delete completion (result dict from worker)
    _exifBriefReady = Signal(str, str)  # (path_str, brief) from background thread

    def __init__(
        self,
        image_dir: Path,
        engine: QQmlApplicationEngine,
        debug_cache: bool = False,
        debug_thumb_timing: bool = False,
        debug_thumb_trace: bool = False,
    ):
        super().__init__()
        self.debug_thumb_timing = debug_thumb_timing
        self.debug_thumb_trace = debug_thumb_trace

        import faststack.util.thumb_debug as thumb_debug

        thumb_debug.init(timing=self.debug_thumb_timing, trace=self.debug_thumb_trace)
        # Histogram Offloading Setup
        self._hist_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="Histogram"
        )
        self._hist_inflight = False
        self._hist_inflight_since: float = 0.0  # monotonic time when worker started
        self._hist_inflight_token: int = 0  # token of the currently inflight job
        self._hist_inflight_future: Optional[concurrent.futures.Future] = None
        self._hist_pending = None
        self._hist_token = 0
        self._hist_null_retries: int = 0  # consecutive None-result retries
        self._hist_last_args = (1.0, 0.0, 0.0, 1.0)  # last zoom/pan/scale args used
        self._hist_lock = threading.Lock()
        self.histogramReady.connect(self._apply_histogram_result)
        self.previewReady.connect(self._apply_preview_result)

        # Save Offloading Setup (runs save_image in background thread)
        # ⚠️ NON-DAEMON: We must ensure saving finishes to avoid data loss on exit.
        self._save_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="Save"
        )
        self._saveFinished.connect(self._on_save_finished)

        # Delete Offloading Setup (runs recycle/delete I/O in background thread)
        # ⚠️ NON-DAEMON: Ensure delete/recycle operations complete.
        self._delete_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="Deleter"
        )
        self._deleteFinished.connect(self._on_delete_finished)
        self._pending_delete_jobs: Dict[int, DeleteJob] = {}  # job_id -> DeleteJob
        self._next_delete_job_id = 0

        # Preview Offloading Setup
        self._preview_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="Preview"
        )
        # EXIF Brief Offloading Setup (dedicated executor to avoid blocking histograms)
        self._exif_executor = create_daemon_threadpool_executor(
            max_workers=2, thread_name_prefix="EXIF"
        )
        self._preview_inflight = False
        self._preview_pending = False
        self._preview_token = 0
        self._preview_lock = threading.Lock()
        self._last_rendered_preview = None  # Store latest valid render
        self._shutting_down = False  # Flag to gate async callbacks during shutdown
        self._refresh_scheduled = False  # Coalesce guard for deferred disk refresh
        self._opencv_warning_shown = False  # Only show OpenCV warning once per session
        self._last_auto_levels_msg: str = (
            ""  # Detail message from last auto_levels() call
        )

        # Deferred-init state: set to safe defaults, populated later by their methods
        self._saves_in_flight: set = (
            set()
        )  # canonical target paths currently being saved
        self._saving_keys: set = set()  # keys of images with active saves
        self._batch_indices_cache: set = set()
        self._batch_indices_cache_key: Optional[tuple] = None
        self.recycle_bin_dir: Optional[Path] = None
        self.reporter: Optional[AppController.ProgressReporter] = None
        self._last_rendered_preview_index: int = -1
        self._last_rendered_preview_gen: int = -1
        self._batch_al_indices: list = []
        self._batch_al_pos: int = 0
        self._batch_al_processed: int = 0
        self._batch_al_cancelled: bool = False
        self._batch_al_t_start: float = 0.0

        # Variant state
        self._variant_map: Dict[str, VariantGroup] = {}
        self.view_override_path: Optional[str] = None  # normalized absolute string
        self.view_override_kind: Optional[str] = None  # "main"|"developed"|"backup"

        self.image_dir = image_dir
        self.image_files: List[ImageFile] = []  # Filtered list for display
        self._all_images: List[ImageFile] = []  # Cached full list from disk
        self._path_to_index: Dict[Path, int] = (
            {}
        )  # Resolved path -> index for O(1) lookup
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None
        self.engine = engine
        self.debug_cache = debug_cache  # New debug_cache flag

        # Shutdown is handled in main() via aboutToQuit connection

        self.display_width = 0
        self.display_height = 0
        self.display_generation = 0
        self._display_lock = threading.Lock()
        self._is_decoding = False

        # Cache Warning State
        self._last_cache_warning_time = 0
        self._eviction_lock = threading.Lock()
        self._eviction_timestamps: deque[float] = (
            deque()
        )  # Rolling window for rate detection
        self.display_ready = False  # Track if display size has been reported
        self.pending_prefetch_index: Optional[int] = None  # Deferred prefetch index

        # Edit Source Mode State
        # "jpeg" (default) or "raw"
        self.current_edit_source_mode: str = "jpeg"

        # -- Backend Components --
        self.watcher = Watcher(self.image_dir, self._request_watcher_refresh)
        self._suppressed_paths: Dict[str, float] = {}  # key -> monotonic expiry time
        self._suppressed_paths_lock = threading.Lock()  # guards cross-thread access
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
        self.image_editor = ImageEditor()  # Initialize the editor
        self._dialog_open_count = 0  # Track nested dialogs
        self._temp_files_to_clean: List[Path] = (
            []
        )  # Track temp files for cleanup on shutdown

        # -- Caching & Prefetching --
        cache_size_gb = config.getfloat("core", "cache_size_gb", 1.5)
        cache_size_bytes = int(cache_size_gb * 1024**3)
        self._has_warned_cache_full = False
        self.image_cache = ByteLRUCache(
            max_bytes=cache_size_bytes,
            size_of=get_decoded_image_size,
            on_evict=self._on_cache_evict,
        )
        self.image_cache.hits = 0  # Initialize cache hit counter
        self.image_cache.misses = 0  # Initialize cache miss counter
        self.prefetcher = Prefetcher(
            image_files=self.image_files,
            cache_put=self.image_cache.__setitem__,
            prefetch_radius=config.getint("core", "prefetch_radius", 12),
            get_display_info=self.get_display_info,
            debug=_debug_mode,
        )
        self.last_displayed_image: Optional[DecodedImage] = (
            None  # Cache last image to avoid grey squares
        )
        self._last_image_lock = (
            threading.Lock()
        )  # Protect last_displayed_image from race conditions

        # -- Grid View (Thumbnail) Infrastructure --
        self._is_grid_view_active = True  # Default to grid view on startup
        self._grid_nav_history: list[Path] = (
            []
        )  # Stack of previous directories for back navigation

        # -- Optimization & Instrumentation --
        self._scan_count_variant = 0
        self._grid_refreshes = 0
        self._grid_model_dirty = True  # Start dirty to ensure initial load
        self._folder_loaded = False  # Track whether the current folder scan is complete
        self._thumbnail_cache = ThumbnailCache(
            max_bytes=256 * 1024 * 1024,  # 256 MB
            max_items=5000,
        )
        self._path_resolver = PathResolver()
        self._thumbnail_prefetcher = ThumbnailPrefetcher(
            cache=self._thumbnail_cache,
            on_ready_callback=self._on_thumbnail_ready,
            target_size=200,
            debug_timing=self.debug_thumb_timing,
            debug_trace=self.debug_thumb_trace,
        )
        self._thumbnail_model = ThumbnailModel(
            base_directory=self.image_dir,
            current_directory=self.image_dir,
            get_metadata_callback=self._get_metadata_dict,
            get_batch_indices_callback=self._get_batch_indices,
            get_current_index_callback=self._get_current_loupe_index,
            metadata_key_fn=lambda p: self.sidecar.metadata_key_for_path(p),
            thumbnail_size=200,
            parent=self,  # Ensure proper Qt ownership to prevent GC issues
        )
        self._thumbnail_provider = ThumbnailProvider(
            cache=self._thumbnail_cache,
            prefetcher=self._thumbnail_prefetcher,
            path_resolver=self._path_resolver.resolve,
            default_size=200,
            debug_timing=self.debug_thumb_timing,
            debug_trace=self.debug_thumb_trace,
        )
        # Connect thread-safe thumbnail ready signal to GUI thread handler
        # The callback is invoked from worker threads, so we use a signal to hop to GUI thread
        # Explicit QueuedConnection ensures cross-thread safety
        self._thumbnailReadySignal.connect(
            self._on_thumbnail_ready_gui, Qt.QueuedConnection
        )

        # -- UI State --
        self.ui_state = UIState(self)
        self.ui_state.theme = self.get_theme()
        self.ui_state.debugCache = self.debug_cache
        self.ui_state.debugMode = _debug_mode  # Set debug mode from global
        self.ui_state.debugThumbTiming = self.debug_thumb_timing
        self.keybinder = Keybinder(self)
        self.ui_state.debugCache = self.debug_cache  # Pass debug_cache state to UI
        self.ui_state.isDecoding = False  # Initialize decoding indicator
        self.is_zoomed = False  # Track zoom state for high-res loading logic

        # Connect model selection changes to UIState for QML property notification
        # Must connect to .emit (not the signal itself) for signal-to-signal forwarding
        self._thumbnail_model.selectionChanged.connect(
            self.ui_state.gridSelectedCountChanged.emit
        )

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stacks: List[List[int]] = []

        # -- Batch Selection State (for drag-and-drop) --
        self.batch_start_index: Optional[int] = None
        self.batches: List[List[int]] = []  # List of [start, end] ranges

        self._filter_string: str = ""  # Default filter
        self._filter_flags: list = (
            []
        )  # Active flag filters (e.g. ["uploaded", "stacked"])
        self._filter_enabled: bool = False
        self.sort_mode: str = "default"

        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        self._exif_brief_cache: dict = {}  # normalized path key → formatted EXIF string
        self._exif_pending_path: Optional[str] = (
            None  # path currently awaiting EXIF read
        )
        with self._last_image_lock:
            self.last_displayed_image = None
        self._logged_empty_metadata = False

        # -- Delete/Undo State --
        self.active_recycle_bins: Set[Path] = (
            set()
        )  # Track all recycle bins created/used
        self.delete_history: List[DeleteRecord] = (
            []
        )  # [((jpg_src, jpg_bin), (raw_src, raw_bin)), ...]

        # Track all undoable actions with timestamps
        # [(action_type, action_data, timestamp)]
        self.undo_history: List[Tuple[str, Any, float]] = []

        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self._handle_resize)
        self.pending_width = None
        self.pending_height = None

        # Histogram Throttle Timer
        self.histogram_timer = QTimer(self)
        self.histogram_timer.setSingleShot(True)
        self.histogram_timer.setInterval(50)  # 50ms throttle (max 20fps)
        self.histogram_timer.timeout.connect(self._kick_histogram_worker)

        # Preview refresh uses a gate pattern instead of a timer:
        # - _kick_preview_worker() runs immediately if not inflight
        # - If inflight, it sets _preview_pending and returns
        # - When render completes, _apply_preview_result chains immediately if pending
        # This removes the extra 16ms delay in the fast-render case by chaining
        # immediately on completion. QML's 16ms slider timer remains the fps cap.

        # Debounce timer for filesystem watcher refresh.
        # Coalesces bursts (backup-create + atomic-replace delete + move)
        # into a single refresh on the UI thread.
        self._watcher_debounce_timer = QTimer(self)
        self._watcher_debounce_timer.setSingleShot(True)
        self._watcher_debounce_timer.setInterval(200)  # 200ms debounce
        self._watcher_debounce_timer.timeout.connect(self._on_watcher_refresh)

        # Periodic summary for thumbnail debug logging
        if self.debug_thumb_timing or self.debug_thumb_trace:
            self._thumb_summary_timer = QTimer(self)
            self._thumb_summary_timer.setInterval(1000)  # Check every second
            self._thumb_summary_timer.timeout.connect(
                thumb_debug.check_periodic_summary
            )
            self._thumb_summary_timer.start()

        # Debounce timer for metadata/highlight signals during rapid navigation
        # Only emits these signals once user stops navigating (16ms = 1 frame debounce)
        self._metadata_debounce_timer = QTimer(self)
        self._metadata_debounce_timer.setSingleShot(True)
        self._metadata_debounce_timer.setInterval(16)  # 16ms debounce (1 frame)
        self._metadata_debounce_timer.timeout.connect(
            self._emit_debounced_metadata_signals
        )

        # Debounce timer for EXIF reads — only fires after user stops scrolling
        self._exif_debounce_timer = QTimer(self)
        self._exif_debounce_timer.setSingleShot(True)
        self._exif_debounce_timer.setInterval(150)  # 150ms debounce
        self._exif_debounce_timer.timeout.connect(self._read_exif_deferred)
        self._exifBriefReady.connect(self._on_exif_brief_ready)

        # Track if any dialog is open to disable keybindings
        self._dialog_open = False

        self.auto_level_threshold = config.getfloat("core", "auto_level_threshold", 0.1)
        self.auto_level_strength = config.getfloat("core", "auto_level_strength", 1.0)
        self.auto_level_strength_auto = config.getboolean(
            "core", "auto_level_strength_auto", False
        )

        # Connect editor open/close signal for memory cleanup
        self.ui_state.is_editor_open_changed.connect(self._on_editor_open_changed)

    # _move_to_recycle robust version is defined below in the deletion section
    @Slot(bool)
    def _on_editor_open_changed(self, is_open: bool):
        """Handle necessary setup/cleanup when editor opens or closes."""
        if is_open:
            # Warn once if OpenCV is not available (detail sliders will be slower)
            if not self._opencv_warning_shown:
                from faststack.imaging.optional_deps import HAS_OPENCV

                if not HAS_OPENCV:
                    self._opencv_warning_shown = True
                    log.warning(
                        "OpenCV not available - detail sliders (clarity/texture/sharpness) will be slower"
                    )
                    self.update_status_message(
                        "OpenCV not installed - editor performance reduced. Install opencv-python for faster editing.",
                        timeout=8000,
                    )
        else:
            # Cleanup large memory buffers when editor closes
            if self.image_editor:
                # If a save is active for this session, preserve the memory
                # so the user can re-open/retry if the background task fails.
                current_key = (
                    self._key(self.image_editor.current_filepath)
                    if self.image_editor.current_filepath
                    else None
                )
                if current_key and current_key in self._saving_keys:
                    log.debug(
                        "Editor closed but save in progress for %s; keeping session memory",
                        current_key,
                    )
                else:
                    log.debug("Editor closed, clearing editor memory buffers")
                    self.image_editor.clear()

            # Also clear the cached preview rendering
            with self._preview_lock:
                self._last_rendered_preview = None

    def is_valid_working_tif(self, path: Path) -> bool:
        """Checks if a working TIFF path is valid for editing."""
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    def get_active_edit_path(self, index: int) -> Path:
        """
        Determines the correct file path to use for editing/exporting based on current mode.

        Rules:
        1. If index invalid, raise IndexError or return None (caller handles).
        2. If image is RAW-only (no paired JPEG and path is RAW ext), force "raw" mode functionality.
           (Note: ImageFile.path is usually the JPEG if it exists. If it's a RAW file, it means orphaned RAW).
        3. If mode is "jpeg": return jpg_path (visual/original).
        4. If mode is "raw":
           - Check for valid developed TIFF. If yes, return it.
           - If no TIFF, return the RAW path itself (RawTherapee will need to develop it,
             or we load it if we support direct RAW - here we likely return raw_path so
             load_image_for_editing can decide to develop it).
        """
        if index < 0 or index >= len(self.image_files):
            raise IndexError("Invalid image index")

        img = self.image_files[index]

        # Check if we are strictly RAW-only (orphaned RAW or just RAW opened)
        # ImageFile.path is the main file. ImageFile.raw_pair is the sidecar RAW.
        # If raw_pair is None but path is a RAW extension, it's RAW-only.
        is_raw_only = False
        if img.raw_pair is None and img.path.suffix.lower() in RAW_EXTENSIONS:
            is_raw_only = True

        mode = self.current_edit_source_mode
        if is_raw_only:
            mode = "raw"

        if mode == "jpeg":
            return img.path

        # Mode is RAW
        if img.has_working_tif and self.is_valid_working_tif(img.working_tif_path):
            return img.working_tif_path

        if img.raw_pair:
            return img.raw_pair

        # Fallback for RAW-only case where path is the RAW
        return img.path

    @Slot(str, "QVariantList")
    def apply_filter(self, filter_string: str, filter_flags: list):
        filter_string = filter_string.strip()
        flags = list(filter_flags or [])

        if not filter_string and not flags:
            self.clear_filter()
            return

        self._filter_string = filter_string
        self._filter_flags = flags
        self._filter_enabled = True
        self._apply_filter_to_cached_list()  # Fast in-memory filtering
        self._bump_display_generation()
        self.dataChanged.emit()
        self.ui_state.filterStringChanged.emit()  # Notify UI of filter change

        # Sync filter to grid view model;
        # cancel stale thumbnail jobs so the filtered view's thumbnails load quickly
        if self._is_grid_view_active:
            self._thumbnail_prefetcher.cancel_all()
        # Silent updates - we will refresh manually via refresh_from_controller
        if self._thumbnail_model:
            self._thumbnail_model.set_filter(filter_string, refresh=False)
            self._thumbnail_model.set_filter_flags(flags, refresh=False)

        if self._is_grid_view_active and self._thumbnail_model:
            self._grid_refreshes += 1
            self._thumbnail_model.refresh_from_controller(self.image_files)
            self._path_resolver.update_from_model(self._thumbnail_model)
            self._grid_model_dirty = False
        else:
            self._grid_model_dirty = True

        # reset to start of filtered list
        self.current_index = 0
        self.sync_ui_state()
        self._do_prefetch(self.current_index)

    @Slot(result=str)
    def get_filter_string(self):
        # return current string, or "" if filter off
        return self._filter_string

    @Slot(result="QVariantList")
    def get_filter_flags(self):
        """Return current flag filters (e.g. ["uploaded", "stacked"]) for dialog restoration."""
        return list(self._filter_flags)

    @Slot()
    def clear_filter(self):
        if (
            not self._filter_enabled
            and not self._filter_string
            and not self._filter_flags
        ):
            return
        self._filter_enabled = False
        self._filter_string = ""
        self._filter_flags = []
        self._apply_filter_to_cached_list()  # Fast in-memory filtering
        self._bump_display_generation()
        self.dataChanged.emit()
        self.ui_state.filterStringChanged.emit()  # Notify UI of filter change

        # Sync filter to grid view model;
        # cancel stale thumbnail jobs so the new view's thumbnails load quickly
        if self._is_grid_view_active:
            self._thumbnail_prefetcher.cancel_all()
        # Silent updates - we will refresh manually via refresh_from_controller
        if self._thumbnail_model:
            self._thumbnail_model.set_filter("", refresh=False)
            self._thumbnail_model.set_filter_flags([], refresh=False)

        if self._is_grid_view_active and self._thumbnail_model:
            self._grid_refreshes += 1
            self._thumbnail_model.refresh_from_controller(self.image_files)
            self._path_resolver.update_from_model(self._thumbnail_model)
            self._grid_model_dirty = False
        else:
            self._grid_model_dirty = True

        self.current_index = min(self.current_index, max(0, len(self.image_files) - 1))
        self.sync_ui_state()
        self._do_prefetch(self.current_index)

    @Slot(result=str)
    def get_sort_mode(self):
        return self.sort_mode

    @Slot(str)
    def set_sort_mode(self, mode: str):
        if mode not in ("default", "filename", "date"):
            return
        if self.sort_mode == mode:
            return

        # --- Preflight: simulate the new order WITHOUT mutating state ---
        have_stacks = bool(self.stacks)
        clear_stacks = False

        if have_stacks:
            new_order = self._simulate_sorted_list(mode)
            new_path_to_idx = {
                self._key(img.path): i for i, img in enumerate(new_order)
            }
            if not self._stacks_stay_contiguous(new_path_to_idx):
                if not self._confirm_clear_stacks_for_sort():
                    return  # user cancelled — no state changed
                clear_stacks = True

        # --- Past this point we are committed to the sort ---
        self.sort_mode = mode

        preserved_path = None
        if self.image_files and 0 <= self.current_index < len(self.image_files):
            preserved_path = self.image_files[self.current_index].path

        # Snapshot paths referenced by batches (and stacks if preserving)
        old_batch_paths = self._resolve_ranges_to_paths(self.batches)
        old_stack_paths = (
            self._resolve_ranges_to_paths(self.stacks) if not clear_stacks else []
        )
        old_stack_start_path = (
            self.image_files[self.stack_start_index].path
            if not clear_stacks
            and self.stack_start_index is not None
            and 0 <= self.stack_start_index < len(self.image_files)
            else None
        )
        old_batch_start_path = (
            self.image_files[self.batch_start_index].path
            if self.batch_start_index is not None
            and 0 <= self.batch_start_index < len(self.image_files)
            else None
        )

        self._apply_filter_to_cached_list()
        self._bump_display_generation()

        # Remap batches (splitting is acceptable)
        self.batches = self._rebuild_ranges_from_paths(old_batch_paths)
        self._invalidate_batch_cache()
        if old_batch_start_path:
            key = self._key(old_batch_start_path)
            self.batch_start_index = self._path_to_index.get(key)

        # Handle stacks
        if clear_stacks:
            self.stacks = []
            self.stack_start_index = None
            self.sidecar.data.stacks = []
            self.sidecar.save()
        elif have_stacks:
            self.stacks = self._rebuild_ranges_from_paths(old_stack_paths)
            # Only persist to sidecar when no filter is active — filtered
            # image_files may hide stack members, producing incomplete ranges.
            if not self._filter_enabled:
                self.sidecar.data.stacks = self.stacks
                self.sidecar.save()

        # Remap pending stack start marker (even when no completed stacks exist)
        if not clear_stacks and old_stack_start_path:
            key = self._key(old_stack_start_path)
            self.stack_start_index = self._path_to_index.get(key)

        if self.image_files and preserved_path:
            target_key = self._key(preserved_path)
            new_idx = self._path_to_index.get(target_key)
            if new_idx is not None:
                self.current_index = new_idx
            else:
                self._clear_variant_override()
                self.current_index = 0
        else:
            self._clear_variant_override()
            self.current_index = 0

        if self._is_grid_view_active:
            self._thumbnail_prefetcher.cancel_all()

        if self._is_grid_view_active and self._thumbnail_model:
            self._grid_refreshes += 1
            self._thumbnail_model.refresh_from_controller(self.image_files)
            self._path_resolver.update_from_model(self._thumbnail_model)
            self._grid_model_dirty = False
        else:
            self._grid_model_dirty = True

        self.sync_ui_state()
        self._do_prefetch(self.current_index)
        self.dataChanged.emit()
        if hasattr(self, "ui_state") and self.ui_state:
            self.ui_state.sortModeChanged.emit()

    def _filtered_sorted_copy(self, mode: str) -> list:
        """Return a filtered and sorted copy of ``_all_images``.

        Shared implementation for ``_simulate_sorted_list`` (read-only preview)
        and ``_apply_filter_to_cached_list`` (mutating).
        """
        if self._filter_enabled and self._filter_string:
            needle = self._filter_string.lower()
            result = [
                img for img in self._all_images if needle in img.path.stem.lower()
            ]
        else:
            result = list(self._all_images)

        # Apply flag-based filtering (AND logic: image must have ALL checked flags)
        if self._filter_enabled and self._filter_flags:
            flags = self._filter_flags
            result = [
                img
                for img in result
                if (
                    (meta := self.sidecar.get_metadata(img.path, create=False))
                    and all(getattr(meta, f, False) for f in flags)
                )
            ]

        if mode == "filename":
            result.sort(key=lambda img: img.path.name.lower())
        elif mode == "date":
            # Use the timestamp captured at scan time (ImageFile.timestamp)
            # so we avoid live filesystem calls during sort.  Tiebreak on
            # lowercase filename for determinism when mtimes are equal.
            result.sort(key=lambda img: (-img.timestamp, img.path.name.lower()))
        return result

    def _simulate_sorted_list(self, mode: str) -> list:
        """Return the image list as it would appear under *mode*, without
        mutating any controller state.
        """
        return self._filtered_sorted_copy(mode)

    def _stacks_stay_contiguous(self, new_path_to_idx: dict) -> bool:
        """Return True if every current stack would be contiguous under the
        index mapping *new_path_to_idx* (keyed by ``_key(path)``).
        """
        n = len(self.image_files)
        for start, end in self.stacks:
            indices = []
            for i in range(start, min(end, n - 1) + 1):
                path = self.image_files[i].path
                new_idx = new_path_to_idx.get(self._key(path))
                if new_idx is not None:
                    indices.append(new_idx)
            if len(indices) <= 1:
                continue
            indices.sort()
            for a, b in pairwise(indices):
                if b != a + 1:
                    return False
        return True

    def _confirm_clear_stacks_for_sort(self) -> bool:
        """Show a dialog asking whether to clear stacks to allow resorting.
        Returns True if the user chose to clear and continue.
        """
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setWindowTitle("Stacks Must Be Cleared")
        msg_box.setText(
            "The new sort order would break existing focus stacks.\n\n"
            "Stacks are defined by contiguous image ranges. Resorting "
            "would scatter stack members, so stacks must be cleared "
            "before changing the sort order."
        )
        clear_btn = msg_box.addButton(
            "Clear Stacks and Resort", QMessageBox.ButtonRole.AcceptRole
        )
        cancel_btn = msg_box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg_box.setDefaultButton(cancel_btn)
        msg_box.exec()
        return msg_box.clickedButton() == clear_btn

    def get_display_info(self):
        with self._display_lock:
            if self.is_zoomed:
                return 0, 0, self.display_generation
            return self.display_width, self.display_height, self.display_generation

    def _bump_display_generation(self):
        """Increment display_generation under _display_lock."""
        with self._display_lock:
            self.display_generation += 1

    def on_display_size_changed(self, width: int, height: int):
        """Debounces display size change events to prevent spamming resizes."""
        log.debug(
            "on_display_size_changed called with %dx%d. Current: %dx%d",
            width,
            height,
            self.display_width,
            self.display_height,
        )
        if width <= 0 or height <= 0:
            log.debug("Ignoring invalid resize event")
            return

        # Debounce resize events
        self.pending_width = width
        self.pending_height = height
        self.resize_timer.start(150)  # 150ms debounce

    def _handle_resize(self):
        """Actual resize handler, called after debounce period."""
        log.info(
            "Display size changed to: %dx%d (physical pixels)",
            self.pending_width,
            self.pending_height,
        )
        with self._display_lock:
            self.display_width = self.pending_width
            self.display_height = self.pending_height
            self.display_generation += 1  # Invalidates old entries via cache key

        # Mark display as ready after first size report
        is_first_resize = not self.display_ready
        if is_first_resize:
            self.display_ready = True
            log.info("Display size now stable, enabling prefetch")

        self.prefetcher.cancel_all()  # Cancel stale tasks to avoid wasted work

        if self._loupe_decode_allowed():
            # On first resize, execute deferred prefetch; on subsequent resizes, do normal prefetch
            if is_first_resize and self.pending_prefetch_index is not None:
                self.prefetcher.update_prefetch(self.pending_prefetch_index)
                self.pending_prefetch_index = None
            else:
                self.prefetcher.update_prefetch(self.current_index)

        self.sync_ui_state()  # To refresh the image

    @Slot(bool)
    def set_zoomed(self, zoomed: bool):
        with self._display_lock:
            if self.is_zoomed == zoomed:
                return  # No-op: avoid unnecessary cache invalidation
            if _debug_mode:
                log.info("AppController.set_zoomed: %s -> %s", self.is_zoomed, zoomed)
            self.is_zoomed = zoomed
            self.display_generation += 1  # Invalidates old entries via cache key
        self.is_zoomed_changed.emit(zoomed)
        log.info("Zoom state changed to: %s", zoomed)

        # Cancel stale prefetch tasks and restart at the new resolution,
        # mirroring what _handle_resize() does.
        self.prefetcher.cancel_all()
        if self._loupe_decode_allowed():
            self.prefetcher.update_prefetch(self.current_index)

        # Force QML to reload the image at the new resolution
        if self.image_files and self.main_window:
            self.ui_refresh_generation += 1
            self.ui_state.currentImageSourceChanged.emit()
            self.main_window.update()  # Force repaint

    # -- Zoom Shortcuts --
    def zoom_100(self):
        log.info("Zoom 100% requested")
        self.ui_state.request_absolute_zoom(1.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic

    def zoom_200(self):
        log.info("Zoom 200% requested")
        self.ui_state.request_absolute_zoom(2.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic

    def zoom_300(self):
        log.info("Zoom 300% requested")
        self.ui_state.request_absolute_zoom(3.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic

    def zoom_400(self):
        log.info("Zoom 400% requested")
        self.ui_state.request_absolute_zoom(4.0)
        # self.set_zoomed(True) - Handled by QML smart zoom logic
        # NOTE: We don't clear the cache here. The generation increment is enough.
        # Cache keys include display_generation, so zoomed/unzoomed images become
        # naturally unreachable and LRU will evict them. This lets us instantly
        # reuse cached images if user toggles zoom on/off repeatedly.
        self.prefetcher.cancel_all()  # Cancel stale tasks to avoid wasted work
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()
        self.ui_state.isZoomedChanged.emit()

    @Slot(int, int, str)
    def handle_key_from_histogram(self, key: int, modifiers: int, text: str):
        """Forward key presses from the histogram window through eventFilter."""
        from PySide6.QtGui import QKeyEvent

        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier(modifiers), text
        )
        self.eventFilter(self.main_window, event)

    def eventFilter(self, watched, event) -> bool:
        # Don't handle key events when a dialog is open
        if self._dialog_open:
            return False

        if watched == self.main_window and event.type() == QEvent.Type.KeyPress:
            # QML handles Crop Enter/Esc keys now.
            # We defer to QML to avoid double-triggering or focus conflicts.
            # handled = self.keybinder.handle_key_press(event) ...

            # Esc closes histogram if visible (priority: before editor/grid handling)
            # This works in both grid and loupe view
            if event.key() == Qt.Key_Escape and self.ui_state.isHistogramVisible:
                self.ui_state.isHistogramVisible = False
                return True  # Consume event, histogram closed

            # Esc cancels crop mode if active (priority: before grid handling)
            # Handled here because QML focus issues can prevent Keys.onEscapePressed from firing
            if event.key() == Qt.Key_Escape and getattr(
                self.ui_state, "isCropping", False
            ):
                self.cancel_crop_mode()
                return True  # Consume event, crop mode cancelled

            # When editing, let QML handle Enter/Esc and related keys.
            # Otherwise keybinder can swallow them before QML sees them.
            if getattr(self.ui_state, "isEditorOpen", False):
                return False

            # When cropping, let QML handle Enter/Return for crop execution
            if getattr(self.ui_state, "isCropping", False):
                key = event.key()
                if key in (Qt.Key_Enter, Qt.Key_Return):
                    return False  # Let QML handle crop execution

            # When in grid view, let QML handle navigation and action keys
            if self._is_grid_view_active:
                key = event.key()
                grid_keys = {
                    Qt.Key_Left,
                    Qt.Key_Right,
                    Qt.Key_Up,
                    Qt.Key_Down,
                    Qt.Key_Return,
                    Qt.Key_Enter,
                    Qt.Key_Space,
                    Qt.Key_B,
                    Qt.Key_Escape,
                    Qt.Key_Delete,
                    Qt.Key_Backspace,  # Delete handled by QML with cursor context
                }
                if key in grid_keys:
                    return False  # Let QML handle it

            handled = self.keybinder.handle_key_press(event)
            if handled:
                return True
        return super().eventFilter(watched, event)

    def _do_prefetch(
        self,
        index: int,
        is_navigation: bool = False,
        direction: Optional[int] = None,
        override_path: Optional[Path] = None,
    ):
        """Helper to defer prefetch until display size is stable.

        Args:
            index: The index to prefetch around
            is_navigation: True if called from user navigation (arrow keys, etc.)
            direction: 1 for forward, -1 for backward, None to use last direction
            override_path: Optional explicit path to decode for the current index.
        """
        if not self._loupe_decode_allowed():
            return

        # If navigation occurs during resize debounce, cancel timer and apply resize immediately
        # to ensure prefetch uses correct dimensions
        if is_navigation and self.resize_timer.isActive():
            self.resize_timer.stop()
            self._handle_resize()

        if not self.display_ready:
            log.debug("Display not ready, deferring prefetch for index %d", index)
            self.pending_prefetch_index = index
            return

        # If override_path is provided, it's a priority request for the current image
        if override_path is not None:
            self.prefetcher.submit_task(
                index,
                self.prefetcher.generation,
                priority=True,
                override_path=override_path,
            )
            return  # Skip adjacent prefetch when viewing a variant override

        self.prefetcher.update_prefetch(
            index, is_navigation=is_navigation, direction=direction
        )

    def load(self, skip_thumbnail_refresh: bool = False):
        """Loads images, sidecar data, and starts services."""
        # Reset instrumentation for this load operation
        self._scan_count_variant = 0
        self._grid_refreshes = 0
        self._grid_model_dirty = True

        self.refresh_image_list()  # Initial scan from disk
        if not self.image_files:
            self.current_index = 0
        else:
            self.current_index = max(
                0, min(self.sidecar.data.last_index, len(self.image_files) - 1)
            )
        self.stacks = self.sidecar.data.stacks  # Load stacks from sidecar
        self.dataChanged.emit()  # Emit after stacks are loaded
        self.watcher.start()
        self._do_prefetch(self.current_index)

        # Defer initial UI sync until after images are loaded
        self.sync_ui_state()

        if self._is_grid_view_active and not skip_thumbnail_refresh:

            # Ensure grid model is populated if starting in grid mode
            if (
                self._thumbnail_model
                and self._grid_model_dirty
                and self._thumbnail_model.rowCount() == 0
            ):
                self._grid_refreshes += 1
                self._thumbnail_model.refresh_from_controller(self.image_files)
                self._path_resolver.update_from_model(self._thumbnail_model)
                self._grid_model_dirty = False

        self._set_folder_loaded(True)

        log.info(
            "Load summary: scans=variant:%d grid_refreshes:%d",
            self._scan_count_variant,
            self._grid_refreshes,
        )

    def _request_watcher_refresh(self, path=None):
        """Thread-safe entry point for the filesystem watcher.

        Called from the watchdog thread.  Uses QMetaObject.invokeMethod with
        QueuedConnection to safely restart the debounce QTimer on the UI
        thread, so bursts of events (backup-create, atomic-replace delete,
        move) are coalesced into a single ``refresh_image_list`` call.
        """
        if path:
            # Defensive handling: watchdog sends str, but direct calls might send Path
            p = path if isinstance(path, Path) else Path(path)
            key = self._key(p)
            now = time.monotonic()
            with self._suppressed_paths_lock:
                expiry = self._suppressed_paths.get(key)
                if expiry:
                    if now < expiry:
                        if _debug_mode:
                            log.debug(
                                "Suppressing watcher refresh for recently deleted path: %s",
                                path,
                            )
                        return
                    # Cleanup expired entry
                    del self._suppressed_paths[key]

        try:
            QMetaObject.invokeMethod(
                self, "_start_watcher_debounce_timer", Qt.QueuedConnection
            )
        except RuntimeError:
            pass  # QObject already deleted during shutdown

    @Slot()
    def _start_watcher_debounce_timer(self) -> None:
        """Non-overloaded slot to restart the watcher debounce timer.

        QTimer.start is overloaded (start() / start(int)), which can cause
        ambiguity with QMetaObject.invokeMethod in some PySide versions.
        """
        self._watcher_debounce_timer.start()

    def refresh_image_list(self):
        """Rescans the directory for images from disk and updates cache.

        This does a full disk scan and should only be called when:
        - Application starts (load())
        - Directory watcher detects file changes
        - User explicitly refreshes

        For filtering, use _apply_filter_to_cached_list() instead.
        """
        # Clear folder stats cache so subfolder counts are fresh
        clear_raw_count_cache()

        self._scan_count_variant += 1
        images, variant_map = find_images_with_variants(self.image_dir)
        self._all_images = images
        self._variant_map = variant_map
        self._apply_filter_to_cached_list()

        # Mark model as dirty since the underlying directory was rescanned
        self._grid_model_dirty = True

        # Refresh thumbnail model if it exists (for external file changes or startup)
        if self._thumbnail_model and self._is_grid_view_active:
            self._grid_refreshes += 1
            self._thumbnail_model.refresh_from_controller(self.image_files)
            self._path_resolver.update_from_model(self._thumbnail_model)
            self._grid_model_dirty = False

    @Slot()
    def _on_watcher_refresh(self):
        """Watcher debounce handler: rescan disk and keep UI consistent.

        Unlike bare refresh_image_list(), this clamps current_index,
        updates the prefetcher, and syncs the UI so the display never
        references an out-of-bounds index.
        """
        # Remember which image we were viewing so we can stay on it
        preserved_path = None
        if self.image_files and 0 <= self.current_index < len(self.image_files):
            preserved_path = self.image_files[self.current_index].path

        self.refresh_image_list()

        # Bust decode cache so modified-on-disk files are re-decoded
        self._bump_display_generation()

        # Re-select the same image if it still exists, otherwise clamp
        if self.image_files and preserved_path:
            target_key = self._key(preserved_path)
            new_idx = self._path_to_index.get(target_key)
            if new_idx is not None:
                self.current_index = new_idx
            else:
                # Image gone — clear stale variant override
                self._clear_variant_override()
                self.current_index = min(self.current_index, len(self.image_files) - 1)
        else:
            # List empty or no preserved path — clear stale variant override
            self._clear_variant_override()
            self.current_index = (
                max(0, min(self.current_index, len(self.image_files) - 1))
                if self.image_files
                else 0
            )

        self._do_prefetch(self.current_index)
        self.sync_ui_state()

    def _apply_filter_to_cached_list(self):
        """Applies current filter to cached image list without disk I/O."""
        self.image_files = self._filtered_sorted_copy(self.sort_mode)
        self._rebuild_path_to_index()
        self.prefetcher.set_image_files(self.image_files)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.ui_state.imageCountChanged.emit()

    def _rebuild_path_to_index(self):
        """Rebuild path-to-index dict for O(1) lookup in grid_open_index.

        Call this whenever self.image_files is mutated (filter, sort, directory change).
        """
        self._path_to_index = {
            self._key(img.path): i for i, img in enumerate(self.image_files)
        }

    def _resolve_ranges_to_paths(self, ranges: List[List[int]]) -> List[List[Path]]:
        """Convert index ranges to lists of paths for remap across reorder."""
        result = []
        n = len(self.image_files)
        for start, end in ranges:
            paths = []
            for i in range(start, min(end, n - 1) + 1):
                paths.append(self.image_files[i].path)
            if paths:
                result.append(paths)
        return result

    def _rebuild_ranges_from_paths(self, groups: List[List[Path]]) -> List[List[int]]:
        """Rebuild index ranges from path groups after reorder."""
        ranges = []
        for paths in groups:
            indices = []
            for p in paths:
                idx = self._path_to_index.get(self._key(p))
                if idx is not None:
                    indices.append(idx)
            if not indices:
                continue
            indices.sort()
            # Merge into contiguous runs
            run_start = indices[0]
            run_end = indices[0]
            for idx in indices[1:]:
                if idx == run_end + 1:
                    run_end = idx
                else:
                    ranges.append([run_start, run_end])
                    run_start = idx
                    run_end = idx
            ranges.append([run_start, run_end])
        ranges.sort()
        return ranges

    def _reindex_after_save(self, saved_path: str) -> bool:
        """Re-derive current_index to point at *saved_path* after a save.

        Backup files are excluded from the visible image list (the indexer
        skips ``-backup`` stems), so the list itself is unchanged.  We just
        need to make sure current_index still points at the right entry.

        Returns True if saved_path was found.
        """
        cp = Path(saved_path)

        # Fast path: normalized key lookup (must match _rebuild_path_to_index format)
        path_key = self._key(cp)
        new_idx = self._path_to_index.get(path_key)
        if new_idx is not None:
            self.current_index = new_idx
            return True

        # Name-based fallback (drive letter / symlink mismatches)
        target_name = cp.name
        for i, img_file in enumerate(self.image_files):
            if img_file.path.name == target_name:
                self.current_index = i
                return True

        log.warning("_reindex_after_save: could not find %s in list", saved_path)
        return False

    # --- Variant Badge Logic ---

    def get_variant_badges(self) -> list:
        """Return badge list for the current image's variant group."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return []
        img = self.image_files[self.current_index]
        key_cf = get_group_key_for_path(img.path, self._variant_map)
        if key_cf is None:
            return []
        group = self._variant_map[key_cf]
        if len(group.all_files) <= 1:
            return []
        badges = build_badge_list(group)

        # Determine which badge is active
        if self.view_override_path:
            active_norm = self.view_override_path
        else:
            active_norm = norm_path(img.path)

        # Create a new list with COPIED dicts to avoid mutating the cached result from build_badge_list
        result_badges = []
        for badge in badges:
            b_copy = badge.copy()
            b_copy["active"] = badge["path"] == active_norm
            result_badges.append(b_copy)
        return result_badges

    def set_variant_override(self, path_str: str):
        """Switch loupe view to a different variant file."""
        norm = norm_path(Path(path_str))

        # If selecting main, clear override
        if self.image_files and self.current_index < len(self.image_files):
            main_norm = norm_path(self.image_files[self.current_index].path)
            if norm == main_norm:
                self.view_override_path = None
                self.view_override_kind = None
            else:
                self.view_override_path = norm
                # Determine kind
                img = self.image_files[self.current_index]
                key_cf = get_group_key_for_path(img.path, self._variant_map)
                if key_cf and key_cf in self._variant_map:
                    group = self._variant_map[key_cf]
                    if group.developed_path and norm_path(group.developed_path) == norm:
                        self.view_override_kind = "developed"
                    else:
                        self.view_override_kind = "backup"
                else:
                    self.view_override_kind = "backup"

        # Bump generation to bust cache, trigger re-render
        self.ui_refresh_generation += 1

        # Trigger decode for the new variant
        override_p = Path(self.view_override_path) if self.view_override_path else None
        self._maybe_decode_current_image(
            reason="variant-switch", override_path=override_p
        )

        if self.ui_state:
            self.ui_state.variantBadgesChanged.emit()
            self.ui_state.variantSaveHintChanged.emit()
            self.ui_state.currentImageSourceChanged.emit()

    def _clear_variant_override(self):
        """Clear variant override state (called on navigation)."""
        if self.view_override_path is not None:
            self.view_override_path = None
            self.view_override_kind = None
            if self.ui_state:
                self.ui_state.variantBadgesChanged.emit()
                self.ui_state.variantSaveHintChanged.emit()

    def _loupe_decode_allowed(self) -> bool:
        """Predicate to check if full-resolution decoding is allowed."""
        # Only decode full-res when we are in loupe mode and the folder is loaded.
        return (not self._is_grid_view_active) and self._folder_loaded

    def _maybe_decode_current_image(
        self, reason: str = "", override_path: Optional[Path] = None
    ) -> None:
        """Trigger decode of current image if allowed."""
        if not self._loupe_decode_allowed():
            return

        # Log reason for debugging
        if _debug_mode and reason:
            log.debug("Triggering decode: %s", reason)

        # Trigger prefetch/decode for current index
        self._do_prefetch(self.current_index, override_path=override_path)

    def get_decoded_image(self, index: int) -> Optional[DecodedImage]:
        """Retrieves a decoded image, blocking until ready to ensure correct display.

        This blocks the UI thread on cache miss, but that's acceptable for an image viewer
        where users expect to see the correct image immediately. The prefetcher minimizes
        cache misses by decoding adjacent images in advance.
        """
        if not self._loupe_decode_allowed():
            if _debug_mode:
                log.debug(
                    "get_decoded_image called but loupe decode not allowed (index=%d)",
                    index,
                )
            return None

        if self.debug_cache:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} get_decoded_image: START index={index}"
            )

        if not self.image_files or index < 0 or index >= len(self.image_files):
            log.warning(
                "get_decoded_image called with empty image_files or out of bounds index."
            )
            return None

        # Debug preview condition
        if self.ui_state.isEditorOpen or self.ui_state.isCropping:
            # Robust path comparison
            editor_path = self.image_editor.current_filepath
            file_path = self.image_files[index].path

            match = False
            if editor_path and file_path:
                try:
                    match = Path(editor_path).resolve() == Path(file_path).resolve()
                except (OSError, ValueError):
                    match = str(editor_path) == str(file_path)

            if not match:
                # Debug log if mismatch
                log.debug(
                    "Path mismatch in preview. Editor: %s, File: %s",
                    editor_path,
                    file_path,
                )

            # Return background-rendered preview if Editor is open OR Cropping is active
            if match and self.image_editor.original_image:
                if self._last_rendered_preview:
                    return self._last_rendered_preview

        _, _, display_gen = self.get_display_info()

        # Variant override: use override path for current index
        current_override = None
        if self.view_override_path and index == self.current_index:
            if _debug_mode:
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
        cache_key = build_cache_key(image_path, display_gen)

        # Check cache
        if cache_key in self.image_cache:
            self.image_cache.hits += 1  # Increment hit counter
            self._update_cache_stats()  # Update UI with new stats
            decoded = self.image_cache[cache_key]
            with self._last_image_lock:
                self.last_displayed_image = decoded

            if self.debug_cache:
                _t_end = time.perf_counter()
                print(
                    f"[DBGCACHE] {_t_end*1000:.3f} get_decoded_image: CACHE HIT index={index} total={(_t_end - _t_start)*1000:.2f}ms"
                )

            return decoded

        self.image_cache.misses += 1  # Increment miss counter
        self._update_cache_stats()  # Update UI with new stats
        if self.debug_cache:
            prefix = f"{path_str}::"
            cached_gens = [
                key.split("::", 1)[1]
                for key in self.image_cache.keys()
                if key.startswith(prefix)
            ]
            cache_usage_gb = self.image_cache.currsize / (1024**3)
            _t_miss = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_miss*1000:.3f} get_decoded_image: CACHE MISS index={index} gen={display_gen} (after {(_t_miss - _t_start)*1000:.2f}ms)"
            )
            log.info(
                "Cache miss for %s (index=%d gen=%d). Cached gens: %s. Cache usage=%.2fGB entries=%d",
                image_path.name,
                index,
                display_gen,
                cached_gens or "none",
                cache_usage_gb,
                len(self.image_cache),
            )

        # Cache miss: need to decode synchronously to ensure correct image displays
        if _debug_mode:
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
            # Submit with priority=True to cancel pending prefetch tasks and free up workers
            future = self.prefetcher.submit_task(
                index,
                self.prefetcher.generation,
                priority=True,
                override_path=current_override,
            )
            if not future:
                with self._last_image_lock:
                    return self.last_displayed_image

            try:
                # Wait for decode to complete (blocking but fast for JPEGs)
                result = future.result(timeout=5.0)  # 5 second timeout as safety
            except concurrent.futures.TimeoutError:
                log.warning("Timeout decoding image at index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
            except concurrent.futures.CancelledError:
                log.debug("Decode cancelled for index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image
            except Exception:
                log.exception("Error decoding image at index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image

            if not result:
                if _debug_mode:
                    log.debug("Decode returned no result for index %d", index)
                with self._last_image_lock:
                    return self.last_displayed_image

            decoded_path, decoded_display_gen = result
            cache_key = build_cache_key(decoded_path, decoded_display_gen)
            if cache_key in self.image_cache:
                decoded = self.image_cache[cache_key]
                with self._last_image_lock:
                    self.last_displayed_image = decoded
                if _debug_mode:
                    elapsed = time.perf_counter() - decode_start
                    log.info("Decoded image %d in %.3fs", index, elapsed)
                if self.debug_cache:
                    _t_decoded = time.perf_counter()
                    print(
                        f"[DBGCACHE] {_t_decoded*1000:.3f} get_decoded_image: DECODED index={index} total={(_t_decoded - _t_start)*1000:.2f}ms"
                    )
                return decoded
            else:
                if _debug_mode:
                    log.debug(
                        "Decode finished but cache_key missing (index=%d, key=%s)",
                        index,
                        cache_key,
                    )
                with self._last_image_lock:
                    return self.last_displayed_image
        finally:
            # Hide decoding indicator
            if self.debug_cache:
                self.ui_state.isDecoding = False

        with self._last_image_lock:
            return self.last_displayed_image

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

        cache_key = build_cache_key(image_path, display_gen)

        # Check cache (thread-safe read)
        if cache_key in self.image_cache:
            # We don't update stats/hits here to avoid race conditions on those counters
            return self.image_cache[cache_key]

        # Cache miss: decode synchronously (in this worker thread)
        try:
            # Submit with priority=True
            # Note: prefetcher.submit_task logic needs to be thread-safe.
            # Assuming futures dict access in submit_task handles strict GIL/thread safety or we might need locks there.
            # But usually submitting to Executor is thread safe.
            # The danger is 'self.futures' management in Prefetcher.
            future = self.prefetcher.submit_task(
                index, self.prefetcher.generation, priority=True
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
                    cache_key = build_cache_key(decoded_path, decoded_display_gen)
                    if cache_key in self.image_cache:
                        return self.image_cache[cache_key]
        except Exception:
            log.exception("_get_decoded_image_safe failed for index %d", index)

        return None

    def sync_ui_state(self):
        """Forces the UI to update by emitting all state change signals.

        Essential signals (currentIndexChanged, currentImageSourceChanged) are emitted
        immediately. Non-essential signals (highlightStateChanged, metadataChanged) are
        debounced to reduce overhead during rapid navigation.
        """
        if self.debug_cache:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} sync_ui_state: START gen={self.ui_refresh_generation + 1}"
            )

        self.ui_refresh_generation += 1
        self._metadata_cache_index = (-1, -1)  # Invalidate cache

        # Essential signals - emit immediately for responsive image display
        self.ui_state.currentIndexChanged.emit()
        self.ui_state.currentImageSourceChanged.emit()

        # Debounce non-essential signals during rapid navigation
        # These will emit once after user stops navigating (16ms)
        self._metadata_debounce_timer.start()

        if self.debug_cache:
            _t_end = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_end*1000:.3f} sync_ui_state: DONE signals emitted, total={(_t_end - _t_start)*1000:.2f}ms"
            )

        log.debug(
            "UI State Synced: Index=%d, Count=%d",
            self.ui_state.currentIndex,
            self.ui_state.imageCount,
        )

    def _emit_debounced_metadata_signals(self):
        """Emit deferred metadata/highlight signals after navigation stops."""
        if not self.ui_state:
            return

        if self.debug_cache:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} _emit_debounced_metadata_signals: emitting deferred signals"
            )

        self.ui_state.highlightStateChanged.emit()
        self.ui_state.metadataChanged.emit()
        self.ui_state.variantBadgesChanged.emit()
        self.ui_state.variantSaveHintChanged.emit()

        if self.debug_cache:
            _t_end = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_end*1000:.3f} _emit_debounced_metadata_signals: DONE total={(_t_end - _t_start)*1000:.2f}ms"
            )

        log.debug(
            "Metadata Synced: Filename=%s, Uploaded=%s, StackInfo='%s', BatchInfo='%s'",
            self.ui_state.currentFilename,
            self.ui_state.isUploaded,
            self.ui_state.stackInfoText,
            self.ui_state.batchInfoText,
        )

    def get_variant_save_hint(self) -> str:
        """Returns a string describing the save behavior when viewing a variant."""
        if self.view_override_path and self._get_save_target_path_for_current_view():
            return "Saving will restore to main image (backup will be created)."
        return ""

    # --- Image Editor Integration ---

    def _get_save_target_path_for_current_view(self) -> Optional[Path]:
        """Determine the target path for saving edits based on current view.

        If we are viewing a variant (backup or developed) via override, we want
        to save changes "as" the main image so that a NEW backup of the main
        image is created (Policy A).
        """
        if not self.view_override_path:
            return None

        # Policy Change: When editing a "developed" variant (e.g. from RawTherapee),
        # we want to save IN-PLACE (overwrite the developed file) rather than
        # overwriting the Main source file. This prevents accidental data loss/confusion.
        # Editing a "backup" still targets the Main file (restore behavior).
        if self.view_override_kind == "developed":
            return None

        if self.current_index is not None and 0 <= self.current_index < len(
            self.image_files
        ):
            return self.image_files[self.current_index].path
        return None

    @Slot()
    def save_edited_image(self):
        """Saves the edited image in a background thread to keep UI responsive.

        All export-critical state is captured as an immutable snapshot on the
        main thread BEFORE the editor is closed or the background worker starts.
        The background worker operates only on the snapshot — it never reads
        live editor state for export data.
        """
        if not self.image_editor.original_image:
            return

        # Determine the actual target path for duplicate-save protection.
        # Normalize to a canonical string so Path vs str never causes a miss.
        save_target_path = self._get_save_target_path_for_current_view()
        raw_target = save_target_path or self.image_editor.current_filepath
        effective_target = str(Path(raw_target).resolve()) if raw_target else None
        if effective_target and effective_target in self._saves_in_flight:
            self.update_status_message(
                "This image is still saving. Please wait a moment.", timeout=3000
            )
            return

        # Capture state needed for save before we start
        write_sidecar = self.current_edit_source_mode == "raw"
        dev_path = None
        if write_sidecar and 0 <= self.current_index < len(self.image_files):
            dev_path = self.image_files[self.current_index].developed_jpg_path

        # --- CRITICAL: Snapshot export state BEFORE closing editor or submitting ---
        # This runs on the main thread and captures an immutable copy of everything
        # needed for the export: source image, edits, darken settings, mask data, EXIF.
        try:
            export_snapshot = self.image_editor.snapshot_for_export(
                write_developed_jpg=write_sidecar,
                developed_path=dev_path,
                save_target_path=save_target_path,
            )
        except RuntimeError as e:
            self.update_status_message(str(e))
            return

        # Capture save context NOW — these are frozen into the result dict so
        # _on_save_finished can make cleanup decisions without reading mutable
        # controller/editor fields that may change during the background save.
        editor_was_open = self.ui_state.isEditorOpen
        save_image_key = (
            self._key(self.image_files[self.current_index].path)
            if 0 <= self.current_index < len(self.image_files)
            else None
        )
        session_token = (
            save_image_key,
            getattr(self, "view_override_kind", None),
            self.image_editor.session_id if self.image_editor else None,
            # Include edit revision so _on_save_finished does not clear state
            # if the user continued editing after save was submitted.
            getattr(self.image_editor, "_edits_rev", None),
        )
        save_metadata_path = (
            self.image_files[self.current_index].path
            if 0 <= self.current_index < len(self.image_files)
            else save_target_path or self.image_editor.current_filepath
        )

        if save_image_key and save_image_key in self._saving_keys:
            self.update_status_message(
                "This image is still saving. Please wait a moment.", timeout=3000
            )
            return

        # Track in-flight save by target path
        if effective_target:
            self._saves_in_flight.add(effective_target)
        if save_image_key:
            self._saving_keys.add(save_image_key)

        # Show saving indicator (stays until save finishes — no auto-clear timeout)
        self.ui_state.isSaving = True
        self.ui_state.statusMessage = "Saving..."

        # Compute restore-override flag
        # We are restoring if we have an override path AND kind is NOT developed (i.e. it's a backup)
        started_from_restore_override = (
            bool(self.view_override_path)
            and getattr(self, "view_override_kind", None) != "developed"
        )

        # Build the base context that every result dict carries
        _ctx = {
            "target": effective_target,
            "editor_was_open": editor_was_open,
            "save_image_key": save_image_key,
            "session_token": session_token,
            "save_directory_key": self._key(self.image_dir),
            "save_metadata_path": (
                str(save_metadata_path) if save_metadata_path else None
            ),
            "started_from_restore_override": started_from_restore_override,
            # Keep metadata writes bound to the sidecar that owned the save
            # request. The user can navigate to another folder before the
            # background save completes, and self.sidecar may change.
            "save_sidecar": self.sidecar,
        }

        # Submit save work to background thread — operates only on the snapshot
        def do_save():
            """Worker function that runs in background thread."""
            try:
                result = self.image_editor.save_from_snapshot(export_snapshot)
                return {"success": True, "result": result, **_ctx}
            except RuntimeError as e:
                return {"success": False, "error": str(e), **_ctx}
            except Exception as e:
                log.exception("Unexpected error during save: %s", e)
                return {
                    "success": False,
                    "error": "Failed to save image",
                    **_ctx,
                }

        def on_done(future):
            """Callback when background save completes - emits signal to hop to main thread."""
            if self._shutting_down:
                return
            try:
                result = future.result()
            except Exception as e:
                result = {"success": False, "error": str(e), **_ctx}
            self._saveFinished.emit(result)

        try:
            future = self._save_executor.submit(do_save)
        except Exception as e:
            log.error("Failed to submit save to background executor: %s", e)
            # Rollback save bookkeeping: submission failed, save never started.
            if effective_target:
                self._saves_in_flight.discard(effective_target)
            if save_image_key:
                self._saving_keys.discard(save_image_key)
            self.ui_state.isSaving = False
            self.update_status_message(f"Failed to start background save: {e}")
            # Do NOT close editor if submission failed, as the save never started.
            # Return early to avoid the isEditorOpen = False block below.
            return
        try:
            future.add_done_callback(on_done)
        except Exception as e:
            # Submission succeeded, so the save IS running — do not roll back
            # _saves_in_flight / _saving_keys.  Instead, spin up a minimal
            # daemon thread to await the future and deliver the result via
            # on_done(), so _saveFinished is still emitted and cleanup runs.
            log.error(
                "Failed to register save callback; using fallback watcher thread: %s", e
            )

            def _fallback_watcher(fut=future):
                concurrent.futures.wait([fut])
                on_done(fut)

            t = threading.Thread(
                target=_fallback_watcher, daemon=True, name="SaveCallbackFallback"
            )
            t.start()

        # Close editor UI immediately to allow the user to continue working.
        # The background worker uses the frozen export_snapshot, so it doesn't
        # need the live editor UI to remain open.
        # If the save fails later, memory is preserved due to the guard in
        # _on_editor_open_changed, allowing the same session to be re-opened.
        if self.ui_state.isEditorOpen:
            self.ui_state.isEditorOpen = False

    @Slot(object)
    def _on_save_finished(self, save_result: dict):
        """Handle save completion on main thread (called via signal from background)."""
        # Guard against callbacks during/after shutdown
        if self._shutting_down:
            return

        # Remove completed target from in-flight set, then clear the saving
        # indicator only when no exports remain in progress.
        target = save_result.get("target")
        if target:
            self._saves_in_flight.discard(target)

        save_key = save_result.get("save_image_key")
        if save_key:
            self._saving_keys.discard(save_key)
        if not self._saves_in_flight:
            self.ui_state.isSaving = False

        if not save_result.get("success"):
            error_msg = save_result.get("error", "Save failed")
            self.update_status_message(f"Save failed: {error_msg}", timeout=5000)
            return

        result = save_result.get("result")
        if isinstance(result, tuple) and len(result) == 2:
            saved_path, backup_path = result

            # --- Post-Save Cleanup ---

            # Read frozen save context — these were captured at save-initiation
            # time and are immune to editor/navigation changes during the save.
            editor_was_open = save_result.get("editor_was_open", False)
            save_session_token = save_result.get("session_token")

            # Check whether the user is still viewing the identical session
            # they saved (same image, same variant, same editor underlying data)
            current_image_key = (
                self._key(self.image_files[self.current_index].path)
                if 0 <= self.current_index < len(self.image_files)
                else None
            )
            current_session_token = (
                current_image_key,
                getattr(self, "view_override_kind", None),
                self.image_editor.session_id if self.image_editor else None,
                getattr(self.image_editor, "_edits_rev", None),
            )

            # Check whether the user is still viewing the same image/session
            # (image key, variant kind, and session_id matching)
            still_on_same_session = (
                save_session_token is not None
                and current_session_token is not None
                and len(save_session_token) >= 3
                and current_session_token[:3] == save_session_token[:3]
            )

            # Check if it is the EXACT identical revision (no user edits since save started)
            still_on_identical_revision = (
                still_on_same_session
                and len(save_session_token) >= 4
                and current_session_token[3] == save_session_token[3]
            )

            if still_on_same_session:
                # 1. Editor Cleanup (only if revision is unchanged)
                if still_on_identical_revision:
                    if editor_was_open:
                        if self.ui_state.isEditorOpen:
                            self.ui_state.isEditorOpen = False
                        # Closing triggers _on_editor_open_changed -> image_editor.clear()
                        # but we call it explicitly here just in case they closed it manually.
                        self.image_editor.clear()

                    # Also clear variant override if we started from one
                    if save_result.get("started_from_restore_override"):
                        self._clear_variant_override()

            # Record current path to stay on it after refresh (since index may shift)
            preserved_path = None
            if 0 <= self.current_index < len(self.image_files):
                preserved_path = self.image_files[self.current_index].path

            # 1. Update sidecar metadata FIRST so all following refreshes see it
            if saved_path:
                save_sidecar = save_result.get("save_sidecar") or self.sidecar
                metadata_path = (
                    Path(save_result["save_metadata_path"])
                    if save_result.get("save_metadata_path")
                    else saved_path
                )
                metadata_before = self._mark_image_edited_in_sidecar(
                    save_sidecar, metadata_path
                )
                save_directory_key = save_result.get("save_directory_key")
                current_directory_key = self._key(self.image_dir)
                should_record_undo = backup_path and (
                    save_directory_key is None
                    or save_directory_key == current_directory_key
                )
                if should_record_undo:
                    self.undo_history.append(
                        (
                            "save_edit",
                            self._build_edit_undo_data(
                                saved_path,
                                backup_path,
                                metadata_path=metadata_path,
                                metadata_before=metadata_before,
                                sidecar=save_sidecar,
                            ),
                            time.time(),
                        )
                    )
                elif backup_path:
                    log.info(
                        "Skipping save_edit undo for %s after directory change: %s -> %s",
                        saved_path,
                        save_directory_key,
                        current_directory_key,
                    )

            # 2. Update variants and re-select index
            self.refresh_image_list()

            if still_on_same_session:
                # Still viewing the saved image — pin index and force sync
                self._reindex_after_save(saved_path)
                self.image_cache.clear()
                self.prefetcher.cancel_all()
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()
            else:
                # User navigated away — re-find new index for preserved_path and drop stale cache
                if preserved_path:
                    self._reindex_after_save(preserved_path)
                if saved_path:
                    self.image_cache.pop_path(saved_path)

            if self.ui_state:
                self.ui_state.variantBadgesChanged.emit()

            self.update_status_message("Image saved")
        else:
            # Success reported but result shape unexpected
            log.warning("Save finished with unexpected result shape: %r", result)
            self.update_status_message(
                "Save finished, but the result payload was malformed.", timeout=5000
            )

    # --- Actions ---

    def _set_current_index(
        self, index: int, direction: int = 0, is_navigation: bool = True
    ):
        """Centralized method to change current image index and reset state."""
        if self.debug_cache:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} _set_current_index: START index={index} dir={direction}"
            )

        if index < 0 or index >= len(self.image_files):
            return

        # Reset source mode to JPEG unless new image is strictly RAW-only
        # (This implements the "Default state on navigation" requirement)
        img = self.image_files[index]
        is_raw_only = False
        # Robust RAW-only check: Main path is RAW and it's not a JPEG
        is_jpeg_main = img.path.suffix.lower() in JPG_EXTENSIONS
        is_raw_main = img.path.suffix.lower() in RAW_EXTENSIONS
        is_raw_only = is_raw_main and not is_jpeg_main

        new_mode = "raw" if is_raw_only else "jpeg"
        if self.current_edit_source_mode != new_mode:
            self.current_edit_source_mode = new_mode
            self.editSourceModeChanged.emit(new_mode)

        self.current_index = index  # Set index first so signals pick up correct image

        # Clear variant override on navigation
        self._clear_variant_override()

        self._reset_crop_settings()
        self._reset_darken_on_navigation()

        if self.debug_cache:
            _t_prefetch = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_prefetch*1000:.3f} _set_current_index: calling _do_prefetch"
            )

        self._do_prefetch(
            self.current_index, is_navigation=is_navigation, direction=direction
        )

        if self.debug_cache:
            _t_sync = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_sync*1000:.3f} _set_current_index: calling sync_ui_state (prefetch took {(_t_sync - _t_prefetch)*1000:.2f}ms)"
            )

        self.sync_ui_state()

        # Update histogram if visible
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

        if self.debug_cache:
            _t_end = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_end*1000:.3f} _set_current_index: DONE total={(_t_end - _t_start)*1000:.2f}ms"
            )

    def next_image(self):
        if self.debug_cache:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} next_image: START from index={self.current_index}"
            )

        if self.current_index < len(self.image_files) - 1:
            self._set_current_index(self.current_index + 1, direction=1)

        if self.debug_cache:
            _t_end = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_end*1000:.3f} next_image: DONE total={(_t_end - _t_start)*1000:.2f}ms"
            )

    def prev_image(self):
        if self.debug_cache:
            _t_start = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_start*1000:.3f} prev_image: START from index={self.current_index}"
            )

        if self.current_index > 0:
            self._set_current_index(self.current_index - 1, direction=-1)

        if self.debug_cache:
            _t_end = time.perf_counter()
            print(
                f"[DBGCACHE] {_t_end*1000:.3f} prev_image: DONE total={(_t_end - _t_start)*1000:.2f}ms"
            )

    @Slot(int)
    def jump_to_image(self, index: int):
        """Jump to a specific image by index (0-based)."""
        if 0 <= index < len(self.image_files):
            if index == self.current_index:
                self.update_status_message(f"Already at image {index + 1}")
                return
            direction = 1 if index > self.current_index else -1
            self._set_current_index(index, direction=direction)
            self.update_status_message(f"Jumped to image {index + 1}")
        else:
            log.warning("Invalid image index: %d", index)
            self.update_status_message("Invalid image number")

    @Slot()
    def jump_to_last_uploaded(self):
        """Find the uploaded image with the highest index and jump to it."""
        if not self.image_files:
            self.update_status_message("No images in current folder")
            return

        last_uploaded_index = None
        # Optimization: Iterate backwards to find the last uploaded image faster
        # for idx in range(last_index, -1, -1)
        for idx in range(len(self.image_files) - 1, -1, -1):
            img = self.image_files[idx]
            # Dynamic look-up of self.sidecar as requested (important for mocks in tests)
            meta = self.sidecar.get_metadata(img.path, create=False)

            uploaded = meta.uploaded if meta else False

            if uploaded is True:
                last_uploaded_index = idx
                break

        if last_uploaded_index is not None:
            if last_uploaded_index == self.current_index:
                self.update_status_message("Already at last uploaded image")
            else:
                self.jump_to_image(last_uploaded_index)
                # Ensure grid view scrolls if it's active
                ui = getattr(self, "ui_state", None)
                if ui:
                    sig = getattr(ui, "gridScrollToIndex", None)
                    if sig and hasattr(sig, "emit"):
                        sig.emit(last_uploaded_index)
        else:
            self.update_status_message("No uploaded images found in this folder")

    def show_jump_to_image_dialog(self):
        """Shows the jump to image dialog (called from keybinder)."""
        if self.main_window and hasattr(self.main_window, "show_jump_to_image_dialog"):
            self.main_window.show_jump_to_image_dialog()
        else:
            log.warning(
                "Cannot open jump to image dialog: main_window or function not available"
            )

    def show_exif_dialog(self):
        """Shows the EXIF data dialog."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        path = self.image_files[self.current_index].path
        data = get_exif_data(path)

        if self.main_window and hasattr(self.main_window, "openExifDialog"):
            # Pass data as QVariantMap (dict)
            self.main_window.openExifDialog(data)
        else:
            log.warning(
                "Cannot open EXIF dialog: main_window or openExifDialog not available"
            )

    @Slot()
    def dialog_opened(self):
        """Called when any dialog opens to disable global keybindings."""
        self._dialog_open_count += 1
        if self._dialog_open_count == 1:
            self._dialog_open = True
            self.dialogStateChanged.emit(True)
            log.debug("Dialog opened (count=1), disabling global keybindings")

    @Slot()
    def dialog_closed(self):
        """Called when any dialog closes to re-enable global keybindings."""
        prev = self._dialog_open_count
        self._dialog_open_count = max(0, self._dialog_open_count - 1)
        if prev > 0 and self._dialog_open_count == 0:
            self._dialog_open = False
            self.dialogStateChanged.emit(False)
            log.debug("Dialog closed (count=0), re-enabling global keybindings")

    def toggle_grid_view(self):
        """Toggle between grid view and loupe (single image) view."""
        self._set_grid_view_active(not self._is_grid_view_active)

    @Slot()
    def refresh_grid(self):
        """Full manual refresh: Rescans the disk directory and rebuilds the grid view.

        This is a heavy operation that clears caches and rescans the filesystem.
        Use this only when you need to pick up external changes that the watcher
        might have missed.
        """
        log.info("Manual grid refresh requested (Full Rescan)")
        # Ensure all images are rescanned from disk and the grid follows
        self.refresh_image_list()

    def switch_to_grid_view(self):
        """Switch to grid view (from loupe view). Called by Esc key."""
        if not self._is_grid_view_active:
            self._set_grid_view_active(True)

    def _set_grid_view_active(self, active: bool):
        """Set grid view active state and handle side effects."""
        if self._is_grid_view_active == active:
            return

        self._is_grid_view_active = active

        if active:
            # Entering grid view
            self.pending_prefetch_index = None
            # Cancel inflight full-res decode jobs to free up resources
            self.prefetcher.cancel_all()
            # Cancel stale thumbnail jobs (e.g. from transient top-of-list state)
            if hasattr(self, "_thumbnail_prefetcher"):
                self._thumbnail_prefetcher.cancel_all()

            # Refresh the model if dirty or empty
            needs_refresh = (
                self._grid_model_dirty or self._thumbnail_model.rowCount() == 0
            )
            if needs_refresh:
                self._grid_refreshes += 1

                # Always use controller's list, even if empty.
                self._thumbnail_model.refresh_from_controller(self.image_files)

                # Update path resolver for the current directory
                self._path_resolver.update_from_model(self._thumbnail_model)
                self._grid_model_dirty = False

            # Find current loupe image in grid and scroll to it
            if self.image_files and 0 <= self.current_index < len(self.image_files):
                current_path = self.image_files[self.current_index].path
                grid_index = self._thumbnail_model.find_image_index(current_path)
                if grid_index >= 0:
                    # Emit after isGridViewActiveChanged so QML has created the view
                    QTimer.singleShot(
                        0, lambda: self.ui_state.gridScrollToIndex.emit(grid_index)
                    )

            log.info("Switched to grid view")
        else:
            # Entering loupe view
            log.info("Switched to loupe view")
            # Trigger exactly one decode for the current index
            self._maybe_decode_current_image("enter-loupe")

        # Notify UI state via signal
        self.ui_state.isGridViewActiveChanged.emit(active)

    def grid_navigate_to(self, path: str):
        """Navigate to a folder in grid view.

        This updates both the grid view AND the main working directory,
        so loupe view will show images from the new folder.

        When navigating up above the current base directory (e.g., going to
        parent when at the initial launch directory), updates base_directory
        to allow continued navigation.
        """
        if not self._is_grid_view_active:
            return

        folder_path = Path(path).resolve()
        if not folder_path.is_dir():
            log.warning("Cannot navigate to non-directory: %s", path)
            return

        # Push current directory to history before navigating
        current_dir = self.image_dir.resolve()
        if current_dir != folder_path:
            self._grid_nav_history.append(current_dir)
            self.ui_state.gridCanGoBackChanged.emit()

        # Check if we're navigating above the current base directory
        # This happens when user clicks ".." at the initial launch directory
        update_base = False
        if self._thumbnail_model:
            base_dir = self._thumbnail_model.base_directory
            try:
                folder_path.relative_to(base_dir)
            except ValueError:
                # folder_path is outside base_directory - we're going up
                update_base = True
                log.info(
                    "Navigating above base directory: %s -> %s", base_dir, folder_path
                )

        # Use canonical directory switch
        self._switch_to_directory(folder_path, update_base_directory=update_base)
        log.info("Grid view navigated to: %s", folder_path)

    def grid_go_back(self):
        """Navigate back to the previous directory in grid view history."""
        if not self._grid_nav_history:
            return

        # Pop the previous directory from history
        prev_dir = self._grid_nav_history.pop()
        self.ui_state.gridCanGoBackChanged.emit()

        if not prev_dir.is_dir():
            log.warning("Previous directory no longer exists: %s", prev_dir)
            return

        # Navigate without adding to history (this is going back, not forward)
        update_base = False
        if self._thumbnail_model:
            base_dir = self._thumbnail_model.base_directory
            try:
                prev_dir.relative_to(base_dir)
            except ValueError:
                update_base = True

        self._switch_to_directory(prev_dir, update_base_directory=update_base)
        log.info("Grid view went back to: %s", prev_dir)

    def grid_open_index(self, index: int):
        """Open an image from grid view in loupe view."""
        entry = self._thumbnail_model.get_entry(index)
        if not entry:
            log.warning("grid_open_index: no entry at index %d", index)
            return

        if entry.is_folder:
            # Navigate into folder instead of opening
            self.grid_navigate_to(str(entry.path))
            return

        # Find this image in the main image list using O(1) lookup
        path_key = self._key(entry.path)
        loupe_index = self._path_to_index.get(path_key)

        if loupe_index is None:
            # Index might be stale - rebuild and retry once
            self._rebuild_path_to_index()
            loupe_index = self._path_to_index.get(path_key)

        if loupe_index is None:
            log.warning(
                "grid_open_index: image not found in current list: %s", entry.path
            )
            # Image might be in a different directory - don't switch view
            return

        # Switch to loupe view first (avoids transient work while still in grid)
        self._set_grid_view_active(False)

        # Then set index with navigation=True for proper state reset and prefetch
        self._set_current_index(loupe_index, is_navigation=True)

        log.info("Opened image from grid: %s", entry.path)

    @Slot()
    def delete_current_image(self):
        """Standard entry point for Loupe deletion.
        Triggers batch dialog if current image is part of a multi-image batch.
        """
        # 1. Check if we're in a multi-image batch
        batch_count = self.get_batch_count_for_current_image()
        if batch_count > 1 and self.main_window:
            # Trigger QML batch deletion dialog (user confirms there)
            self.main_window.show_delete_batch_dialog(batch_count)
            return

        # 2. Otherwise default to single image deletion
        self._delete_indices([self.current_index], "loupe")

    @Slot(int)
    def grid_delete_at_cursor(self, cursor_index: int):
        """Unified grid deletion entry point.
        Handles both multi-selection and single-cursor deletion.
        """
        if not self._thumbnail_model:
            return

        # 1. Rebuild index mapping once for reliable lookup
        self._rebuild_path_to_index()

        # 2. Prefer selection if it exists
        selected_paths = self._thumbnail_model.get_selected_paths()
        if selected_paths:
            indices = []
            for path in selected_paths:
                idx = self._path_to_index.get(self._key(path))
                if idx is not None:
                    indices.append(idx)

            if not indices:
                self.update_status_message("Selected images not found in current list.")
                return

            summary = self._delete_indices(indices, "grid_selection")
            if summary.get("queued"):
                self._thumbnail_model.clear_selection()
            return

        # 3. Fallback to cursor index if no selection
        if cursor_index >= 0:
            entry = self._thumbnail_model.get_entry(cursor_index)
            if not entry:
                return
            if entry.is_folder:
                self.update_status_message("Cannot delete folders in grid view.")
                return

            idx = self._path_to_index.get(self._key(entry.path))
            if idx is None:
                self.update_status_message("Image not found in current list.")
                return

            self._delete_indices([idx], "grid_cursor")

    def _on_thumbnail_ready(self, thumbnail_id: str):
        """Callback when a thumbnail finishes decoding (called from worker thread).

        This emits a signal to hop to the GUI thread for thread-safe model updates.
        """
        self._thumbnailReadySignal.emit(thumbnail_id)

    @Slot(str)
    def _on_thumbnail_ready_gui(self, thumbnail_id: str):
        """Handle thumbnail ready on GUI thread (thread-safe)."""
        # Guard against callbacks during/after shutdown
        if getattr(self, "_shutting_down", False):
            return
        if self._thumbnail_model:
            self._thumbnail_model.thumbnailReady.emit(thumbnail_id)

    def _get_metadata_dict(self, image_path: Path | str) -> dict:
        """Get metadata for an image path as a dict for thumbnail model."""
        try:
            meta = self.sidecar.get_metadata(image_path, create=False)
            if meta is None:
                return {
                    "stacked": False,
                    "uploaded": False,
                    "edited": False,
                    "restacked": False,
                    "favorite": False,
                    "todo": False,
                }
            return {
                "stacked": meta.stacked,
                "uploaded": meta.uploaded,
                "edited": meta.edited,
                "restacked": meta.restacked,
                "favorite": meta.favorite,
                "todo": meta.todo,
            }
        except Exception as e:  # Broad catch for UI plumbing - don't crash grid view
            log.debug("Failed to get metadata for %s: %s", image_path, e)
            return {
                "stacked": False,
                "uploaded": False,
                "edited": False,
                "restacked": False,
                "favorite": False,
                "todo": False,
            }

    def _get_bulk_metadata_map(self) -> Dict[str, dict]:
        """Get flattened metadata map for all images (for efficient grid refresh)."""
        bulk_map = {}
        try:
            for img in self.image_files:
                key = self.sidecar.metadata_key_for_path(img.path)
                meta = self.sidecar.get_metadata(img.path, create=False)
                if meta is None:
                    continue
                bulk_map[key] = {
                    "stacked": getattr(meta, "stacked", False),
                    "uploaded": getattr(meta, "uploaded", False),
                    "edited": getattr(meta, "edited", False),
                    "restacked": getattr(meta, "restacked", False),
                    "favorite": getattr(meta, "favorite", False),
                    "todo": getattr(meta, "todo", False),
                }
        except Exception as e:
            log.warning("Failed to build bulk metadata map: %s", e)
        return bulk_map

    def _invalidate_batch_cache(self):
        """Clear the batch indices cache. Call after mutating self.batches."""
        self._batch_indices_cache = set()
        self._batch_indices_cache_key = None

    def _get_batch_indices(self) -> Set[int]:
        """Get set of all indices that are in any batch (for thumbnail model).

        Cached to avoid O(batch_span) computation on every delegate paint.
        """
        # Check if cache is valid (batches haven't changed)
        cache_key = tuple(tuple(b) for b in self.batches)
        if (
            hasattr(self, "_batch_indices_cache_key")
            and self._batch_indices_cache_key == cache_key
        ):
            return self._batch_indices_cache

        # Rebuild cache
        indices: Set[int] = set()
        for start, end in self.batches:
            for i in range(start, end + 1):
                indices.add(i)

        self._batch_indices_cache = indices
        self._batch_indices_cache_key = cache_key
        return indices

    def _get_current_loupe_index(self) -> int:
        """Get current loupe view index (for thumbnail model)."""
        return self.current_index

    def toggle_uploaded(self):
        """Toggle uploaded flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        today = datetime.now().strftime("%Y-%m-%d")
        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path)

        meta.uploaded = not meta.uploaded
        if meta.uploaded:
            meta.uploaded_date = today
        else:
            meta.uploaded_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "uploaded" if meta.uploaded else "not uploaded"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled uploaded flag to %s for %s", meta.uploaded, image_path)

    def toggle_todo(self):
        """Toggle todo flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        today = datetime.now().strftime("%Y-%m-%d")
        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path)

        meta.todo = not getattr(meta, "todo", False)
        if meta.todo:
            meta.todo_date = today
        else:
            meta.todo_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "todo" if meta.todo else "not todo"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled todo flag to %s for %s", meta.todo, image_path)

    def toggle_edited(self):
        """Toggle edited flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        today = datetime.now().strftime("%Y-%m-%d")
        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path)

        meta.edited = not meta.edited
        if meta.edited:
            meta.edited_date = today
        else:
            meta.edited_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "edited" if meta.edited else "not edited"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled edited flag to %s for %s", meta.edited, image_path)

    def toggle_restacked(self):
        """Toggle restacked flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        today = datetime.now().strftime("%Y-%m-%d")
        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path)

        meta.restacked = not meta.restacked
        if meta.restacked:
            meta.restacked_date = today
        else:
            meta.restacked_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "restacked" if meta.restacked else "not restacked"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled restacked flag to %s for %s", meta.restacked, image_path)

    def toggle_favorite(self):
        """Toggle favorite flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path)

        meta.favorite = not meta.favorite

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "Favorited" if meta.favorite else "Unfavorited"
        self.update_status_message(status)
        log.info("Toggled favorite flag to %s for %s", meta.favorite, image_path)

    def toggle_stacked(self):
        """Toggle stacked flag for current image."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        today = datetime.now().strftime("%Y-%m-%d")
        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path)

        meta.stacked = not meta.stacked
        if meta.stacked:
            meta.stacked_date = today
        else:
            meta.stacked_date = None

        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        status = "stacked" if meta.stacked else "not stacked"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled stacked flag to %s for %s", meta.stacked, image_path)

    def get_current_metadata(self) -> Dict:
        if not self.image_files or self.current_index >= len(self.image_files):
            if not self._logged_empty_metadata:
                log.debug(
                    "get_current_metadata: image_files is empty or index out of bounds, returning {}."
                )
                self._logged_empty_metadata = True
            return {}
        self._logged_empty_metadata = False

        # Cache hit check
        cache_key = (self.current_index, self.ui_refresh_generation)
        if cache_key == self._metadata_cache_index:
            return self._metadata_cache

        # Compute and cache
        image_path = self.image_files[self.current_index].path
        meta = self.sidecar.get_metadata(image_path, create=False)
        stack_info = self._get_stack_info(self.current_index)
        batch_info = self._get_batch_info(self.current_index)

        filename = self.image_files[self.current_index].path.name
        if self.image_files[self.current_index].has_raw:
            # e.g. "image.JPG + ORF"
            raw_ext = (
                self.image_files[self.current_index].raw_path.suffix.lstrip(".").upper()
            )
            filename += f" + {raw_ext}"

        # EXIF brief: instant from cache, otherwise schedule deferred read.
        # Always read EXIF from the variant group's main image (not backup/developed).
        exif_key = self._exif_source_key(self.current_index)
        exif_brief = self._exif_brief_cache.get(exif_key, None)
        if exif_brief is None:
            exif_brief = ""
            # Only restart timer when the pending path actually changes,
            # so repeated QML property evaluations don't keep resetting it.
            if self._exif_pending_path != exif_key:
                self._exif_pending_path = exif_key
                self._exif_debounce_timer.start()

        self._metadata_cache = {
            "filename": filename,
            "exif_brief": exif_brief,
            "stacked": meta.stacked if meta else False,
            "stacked_date": (meta.stacked_date or "") if meta else "",
            "uploaded": meta.uploaded if meta else False,
            "uploaded_date": (meta.uploaded_date or "") if meta else "",
            "edited": meta.edited if meta else False,
            "edited_date": (meta.edited_date or "") if meta else "",
            "restacked": meta.restacked if meta else False,
            "restacked_date": (meta.restacked_date or "") if meta else "",
            "favorite": meta.favorite if meta else False,
            "todo": meta.todo if meta else False,
            "todo_date": (meta.todo_date or "") if meta else "",
            "stack_info_text": stack_info,
            "batch_info_text": batch_info,
        }
        self._metadata_cache_index = cache_key
        return self._metadata_cache

    _EXIF_SUFFIXES = frozenset(
        {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".heif", ".heic"}
    )

    def _exif_source_key(self, index: int) -> str:
        """Return a normalized cache key for the EXIF source of image at *index*.

        Resolves to the variant group's main image path when available,
        so backups/developed files use the same EXIF as the original.
        Uses ``normalize_path_key`` for Windows case-insensitivity.
        """
        img = self.image_files[index]
        source_path = img.path
        key_cf = get_group_key_for_path(img.path, self._variant_map)
        if key_cf is not None:
            group = self._variant_map[key_cf]
            if group.main_path is not None:
                source_path = group.main_path
        return normalize_path_key(source_path)

    def _read_exif_deferred(self):
        """Called after 150ms of no navigation. Submits EXIF read to background."""
        if getattr(self, "_shutting_down", False):
            return
        if not self.image_files or self.current_index >= len(self.image_files):
            return
        exif_key = self._exif_source_key(self.current_index)
        if self._exif_pending_path is not None and self._exif_pending_path != exif_key:
            self._exif_pending_path = None
            return  # Different image path pending, or we aren't tracking this key.

        if exif_key in self._exif_brief_cache:
            self._exif_pending_path = None
            return  # already cached
        # Resolve the actual file path for the EXIF source
        img = self.image_files[self.current_index]
        source_path = img.path
        key_cf = get_group_key_for_path(img.path, self._variant_map)
        if key_cf is not None:
            group = self._variant_map[key_cf]
            if group.main_path is not None:
                source_path = group.main_path
        # Early return for formats without EXIF support
        if source_path.suffix.lower() not in self._EXIF_SUFFIXES:
            self._exif_brief_cache[exif_key] = ""
            self._exif_pending_path = None
            return
        # Submit to dedicated EXIF executor to avoid blocking histograms
        signal = self._exifBriefReady

        def _worker(key=exif_key, p=str(source_path)):
            from faststack.imaging.metadata import get_exif_brief

            try:
                brief = get_exif_brief(p)
            except Exception as e:
                log.error("Failed to get EXIF brief for %s: %s", p, e, exc_info=True)
                brief = ""
            signal.emit(key, brief)

        try:
            self._exif_executor.submit(_worker)
        except RuntimeError:
            pass  # executor shut down, ignore

    def _on_exif_brief_ready(self, exif_key: str, brief: str):
        """Slot called on main thread when background EXIF read completes."""
        if getattr(self, "_shutting_down", False) or not self.ui_state:
            return
        self._exif_brief_cache[exif_key] = brief
        if self._exif_pending_path == exif_key:
            self._exif_pending_path = None
        # Only refresh UI if we're still looking at this image
        if (
            self.image_files
            and self.current_index < len(self.image_files)
            and self._exif_source_key(self.current_index) == exif_key
        ):
            self._metadata_cache_index = (-1, -1)
            if self.ui_state:
                self.ui_state.metadataChanged.emit()

    def begin_new_stack(self):
        self.stack_start_index = self.current_index
        log.info("Stack start marked at index %d", self.stack_start_index)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()  # Update UI to show start marker
        self.sync_ui_state()

    def end_current_stack(self):
        log.info(
            "end_current_stack called. stack_start_index: %s", self.stack_start_index
        )
        if self.stack_start_index is not None:
            start = min(self.stack_start_index, self.current_index)
            end = max(self.stack_start_index, self.current_index)
            self.stacks.append([start, end])
            self.stacks.sort()  # Keep stacks sorted by start index
            self.sidecar.data.stacks = self.stacks
            self.sidecar.save()
            log.info("Defined new stack: [%d, %d]", start, end)
            self.stack_start_index = None
            self._metadata_cache_index = (-1, -1)  # Invalidate cache
            self.dataChanged.emit()  # Notify QML of data change
            self.ui_state.stackSummaryChanged.emit()  # Update stack summary in dialog
            self.sync_ui_state()
        else:
            log.warning("No stack start marked. Press '[' first.")

    def begin_new_batch(self):
        """Mark the start of a new batch for drag-and-drop."""
        self.batch_start_index = self.current_index
        log.info("Batch start marked at index %d", self.batch_start_index)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("Batch start marked")

    def end_current_batch(self):
        """End the current batch and save the range."""
        log.info(
            "end_current_batch called. batch_start_index: %s", self.batch_start_index
        )
        if self.batch_start_index is not None:
            start = min(self.batch_start_index, self.current_index)
            end = max(self.batch_start_index, self.current_index)
            self.batches.append([start, end])
            self.batches.sort()  # Keep batches sorted by start index
            self._invalidate_batch_cache()
            log.info("Defined new batch: [%d, %d]", start, end)
            self.batch_start_index = None
            self._metadata_cache_index = (-1, -1)  # Invalidate cache
            self.dataChanged.emit()
            self.sync_ui_state()
            count = end - start + 1
            self.update_status_message(f"Batch defined: {count} images")
        else:
            log.warning("No batch start marked. Press '{' first.")
            self.update_status_message("No batch start marked")

    def grid_add_selection_to_batch(self):
        """Add grid-selected images to batch."""
        if not self._thumbnail_model:
            return

        selected_paths = self._thumbnail_model.get_selected_paths()
        if not selected_paths:
            self.update_status_message("No images selected in grid.")
            return

        # Build path -> index map for the main image list
        # 1. Rebuild index mapping
        self._rebuild_path_to_index()

        # 2. Find indices for selected paths
        indices_to_add = []
        for path in selected_paths:
            idx = self._path_to_index.get(self._key(path))
            if idx is not None:
                indices_to_add.append(idx)

        if not indices_to_add:
            self.update_status_message("Selected images not found in current list.")
            return

        # Sort indices and create batch ranges (merge consecutive)
        indices_to_add.sort()
        added_count = 0

        for idx in indices_to_add:
            # Check if already in a batch
            in_batch = False
            for start, end in self.batches:
                if start <= idx <= end:
                    in_batch = True
                    break

            if not in_batch:
                # Add as single-item batch (will be merged below)
                self.batches.append([idx, idx])
                added_count += 1

        if added_count > 0:
            # Sort and merge overlapping/adjacent batches
            self.batches.sort()
            merged_batches = [self.batches[0]] if self.batches else []
            for i in range(1, len(self.batches)):
                last_start, last_end = merged_batches[-1]
                current_start, current_end = self.batches[i]
                if current_start <= last_end + 1:
                    merged_batches[-1] = [last_start, max(last_end, current_end)]
                else:
                    merged_batches.append([current_start, current_end])
            self.batches = merged_batches

            self._invalidate_batch_cache()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            # Refresh grid to show batch badges
            self._thumbnail_model.refresh()

            self.update_status_message(f"Added {added_count} image(s) to batch")
            log.info("Added %d image(s) to batch from grid selection", added_count)
        else:
            self.update_status_message("All selected images already in batch.")

    def add_favorites_to_batch(self):
        """Add all favorite-flagged images in the current directory to the batch."""
        if not self.image_files:
            self.update_status_message("No images loaded.")
            return

        # Find indices of all favorited images
        indices_to_add = []
        for i, img in enumerate(self.image_files):
            meta = self.sidecar.get_metadata(img.path, create=False)
            if meta and meta.favorite:
                indices_to_add.append(i)

        if not indices_to_add:
            self.update_status_message("No favorites found.")
            return

        # Add each to batch (skip if already in a batch)
        added_count = 0
        for idx in indices_to_add:
            in_batch = any(start <= idx <= end for start, end in self.batches)
            if not in_batch:
                self.batches.append([idx, idx])
                added_count += 1

        if added_count > 0:
            # Sort and merge overlapping/adjacent batches
            self.batches.sort()
            merged_batches = [self.batches[0]] if self.batches else []
            for i in range(1, len(self.batches)):
                last_start, last_end = merged_batches[-1]
                current_start, current_end = self.batches[i]
                if current_start <= last_end + 1:
                    merged_batches[-1] = [last_start, max(last_end, current_end)]
                else:
                    merged_batches.append([current_start, current_end])
            self.batches = merged_batches

            self._invalidate_batch_cache()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            if hasattr(self, "_thumbnail_model") and self._thumbnail_model:
                self._thumbnail_model.refresh()

            self.update_status_message(
                f"Added {added_count} favorite(s) to batch ({len(indices_to_add)} total favorites)"
            )
            log.info("Added %d favorite(s) to batch", added_count)
        else:
            self.update_status_message(
                f"All {len(indices_to_add)} favorite(s) already in batch."
            )

    def add_uploaded_to_batch(self):
        """Add all uploaded-flagged images in the current directory to the batch."""
        if not self.image_files:
            self.update_status_message("No images loaded.")
            return

        # Find indices of all uploaded images
        indices_to_add = []
        for i, img in enumerate(self.image_files):
            meta = self.sidecar.get_metadata(img.path, create=False)
            if meta and meta.uploaded:
                indices_to_add.append(i)

        if not indices_to_add:
            self.update_status_message("No uploaded images found.")
            return

        # Add each to batch (skip if already in a batch)
        added_count = 0
        for idx in indices_to_add:
            in_batch = any(start <= idx <= end for start, end in self.batches)
            if not in_batch:
                self.batches.append([idx, idx])
                added_count += 1

        if added_count > 0:
            # Sort and merge overlapping/adjacent batches
            self.batches.sort()
            merged_batches = [self.batches[0]] if self.batches else []
            for i in range(1, len(self.batches)):
                last_start, last_end = merged_batches[-1]
                current_start, current_end = self.batches[i]
                if current_start <= last_end + 1:
                    merged_batches[-1] = [last_start, max(last_end, current_end)]
                else:
                    merged_batches.append([current_start, current_end])
            self.batches = merged_batches

            self._invalidate_batch_cache()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            if hasattr(self, "_thumbnail_model") and self._thumbnail_model:
                self._thumbnail_model.refresh()

            self.update_status_message(
                f"Added {added_count} uploaded image(s) to batch ({len(indices_to_add)} total uploaded)"
            )
            log.info("Added %d uploaded image(s) to batch", added_count)
        else:
            self.update_status_message(
                f"All {len(indices_to_add)} uploaded image(s) already in batch."
            )

    def remove_from_batch_or_stack(self):
        """Remove current image from any batch or stack it's in."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        removed = False

        # Check and remove from batches
        new_batches = []
        batch_modified = False
        for start, end in self.batches:
            if not batch_modified and start <= self.current_index <= end:
                # This is the batch to modify.

                # Single image batch - remove entirely by not adding anything.
                if start == end:
                    pass
                # Remove from beginning - shift start forward
                elif self.current_index == start:
                    new_batches.append([start + 1, end])
                # Remove from end - shift end backward
                elif self.current_index == end:
                    new_batches.append([start, end - 1])
                # Remove from middle - split into two ranges
                else:
                    new_batches.append([start, self.current_index - 1])
                    new_batches.append([self.current_index + 1, end])

                log.info(
                    "Removed index %d from batch [%d, %d]",
                    self.current_index,
                    start,
                    end,
                )
                self.update_status_message("Removed from batch")
                removed = True
                batch_modified = True
            else:
                new_batches.append([start, end])

        if batch_modified:
            self.batches = new_batches
            self._invalidate_batch_cache()

        # Check and remove from stacks
        # Check and remove from stacks
        if not removed:
            new_stacks = []
            stack_modified = False
            for start, end in self.stacks:
                if not stack_modified and start <= self.current_index <= end:
                    # This is the stack to modify.

                    # Single image stack - remove entirely.
                    if start == end:
                        pass
                    # Remove from beginning
                    elif self.current_index == start:
                        new_stacks.append([start + 1, end])
                    # Remove from end
                    elif self.current_index == end:
                        new_stacks.append([start, end - 1])
                    # Remove from middle
                    else:
                        new_stacks.append([start, self.current_index - 1])
                        new_stacks.append([self.current_index + 1, end])

                    log.info(
                        "Removed index %d from stack [%d, %d]",
                        self.current_index,
                        start,
                        end,
                    )
                    self.update_status_message("Removed from stack")
                    removed = True
                    stack_modified = True
                else:
                    new_stacks.append([start, end])

            if stack_modified:
                self.stacks = new_stacks
                self.sidecar.data.stacks = self.stacks
                self.sidecar.save()
        if removed:
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.ui_state.stackSummaryChanged.emit()
            self.sync_ui_state()
        else:
            self.update_status_message("Not in any batch or stack")

    def toggle_batch_membership(self):
        """Toggles the current image's inclusion in a batch."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        index_to_toggle = self.current_index

        # Check if the image is already in a batch
        in_batch = False
        for start, end in self.batches:
            if start <= index_to_toggle <= end:
                in_batch = True
                break

        new_batches = []
        if in_batch:
            # Remove from batch
            item_removed = False
            for start, end in self.batches:
                if not item_removed and start <= index_to_toggle <= end:
                    if start < index_to_toggle:
                        new_batches.append([start, index_to_toggle - 1])
                    if index_to_toggle < end:
                        new_batches.append([index_to_toggle + 1, end])
                    item_removed = True
                else:
                    new_batches.append([start, end])
            self.batches = new_batches
            self.update_status_message("Removed image from batch")
            log.info("Removed index %d from a batch.", index_to_toggle)
        else:
            # Add to batch - merge with adjacent batches if possible
            if not self.batches:
                self.batches.append([index_to_toggle, index_to_toggle])
                self.update_status_message("Created new batch with current image.")
                log.info(
                    "No existing batches. Created new batch for index %d.",
                    index_to_toggle,
                )
            else:
                # Check if adjacent to any existing batch
                merged = False
                for i, (start, end) in enumerate(self.batches):
                    # Adjacent to start of batch
                    if index_to_toggle == start - 1:
                        self.batches[i] = [index_to_toggle, end]
                        merged = True
                        break
                    # Adjacent to end of batch
                    elif index_to_toggle == end + 1:
                        self.batches[i] = [start, index_to_toggle]
                        merged = True
                        break

                if not merged:
                    # Not adjacent to any batch, create new one
                    self.batches.append([index_to_toggle, index_to_toggle])

                # Sort and merge any overlapping batches
                self.batches.sort()
                merged_batches = [self.batches[0]] if self.batches else []
                for i in range(1, len(self.batches)):
                    last_start, last_end = merged_batches[-1]
                    current_start, current_end = self.batches[i]
                    if current_start <= last_end + 1:
                        merged_batches[-1] = [last_start, max(last_end, current_end)]
                    else:
                        merged_batches.append([current_start, current_end])
                self.batches = merged_batches

                self.update_status_message("Added image to batch")
                log.info("Added index %d to batch.", index_to_toggle)

        self._invalidate_batch_cache()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()

    def toggle_stack_membership(self):
        """Toggles the current image's inclusion in a stack."""
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        index_to_toggle = self.current_index

        # Check if the image is already in a stack
        stack_to_modify_idx = -1
        for i, (start, end) in enumerate(self.stacks):
            if start <= index_to_toggle <= end:
                stack_to_modify_idx = i
                break

        if stack_to_modify_idx != -1:
            # --- Remove from existing stack ---
            new_stacks = []
            item_removed = False
            for i, (start, end) in enumerate(self.stacks):
                if not item_removed and i == stack_to_modify_idx:
                    if start < index_to_toggle:
                        new_stacks.append([start, index_to_toggle - 1])
                    if index_to_toggle < end:
                        new_stacks.append([index_to_toggle + 1, end])
                    item_removed = True
                else:
                    new_stacks.append([start, end])
            self.stacks = new_stacks
            self.update_status_message("Removed image from stack")
            log.info(
                "Removed index %d from stack #%d.",
                index_to_toggle,
                stack_to_modify_idx + 1,
            )

        else:
            # --- Add to nearest stack ---
            if not self.stacks:
                self.stacks.append([index_to_toggle, index_to_toggle])
                self.update_status_message("Created new stack with current image.")
                log.info(
                    "No existing stacks. Created new stack for index %d.",
                    index_to_toggle,
                )
            else:
                # Find closest stack
                dist_backward = float("inf")
                stack_idx_backward = -1
                for i in range(index_to_toggle - 1, -1, -1):
                    for j, (start, end) in enumerate(self.stacks):
                        if start <= i <= end:
                            dist_backward = index_to_toggle - i
                            stack_idx_backward = j
                            break
                    if stack_idx_backward != -1:
                        break

                dist_forward = float("inf")
                stack_idx_forward = -1
                for i in range(index_to_toggle + 1, len(self.image_files)):
                    for j, (start, end) in enumerate(self.stacks):
                        if start <= i <= end:
                            dist_forward = i - index_to_toggle
                            stack_idx_forward = j
                            break
                    if stack_idx_forward != -1:
                        break

                if stack_idx_backward == -1 and stack_idx_forward == -1:
                    # This case should not be reached if `if not self.stacks` handles it.
                    self.stacks.append([index_to_toggle, index_to_toggle])
                    self.update_status_message("Created new stack with current image.")
                    log.info(
                        "No stacks found nearby. Created new stack for index %d.",
                        index_to_toggle,
                    )
                else:
                    if dist_backward <= dist_forward:
                        stack_to_join_idx = stack_idx_backward
                    else:
                        stack_to_join_idx = stack_idx_forward

                    start, end = self.stacks[stack_to_join_idx]
                    self.stacks[stack_to_join_idx] = [
                        min(start, index_to_toggle),
                        max(end, index_to_toggle),
                    ]

                    # Merge overlapping stacks
                    self.stacks.sort()
                    merged_stacks = [self.stacks[0]] if self.stacks else []
                    for i in range(1, len(self.stacks)):
                        last_start, last_end = merged_stacks[-1]
                        current_start, current_end = self.stacks[i]
                        if current_start <= last_end + 1:
                            merged_stacks[-1] = [last_start, max(last_end, current_end)]
                        else:
                            merged_stacks.append([current_start, current_end])
                    self.stacks = merged_stacks

                    # Find the new stack index for the status message
                    new_stack_idx = -1
                    for i, (start, end) in enumerate(self.stacks):
                        if start <= index_to_toggle <= end:
                            new_stack_idx = i
                            break

                    self.update_status_message(
                        f"Added image to Stack #{new_stack_idx + 1}"
                    )
                    log.info(
                        "Added index %d to stack #%d.",
                        index_to_toggle,
                        new_stack_idx + 1,
                    )

        self.sidecar.data.stacks = self.stacks
        self.sidecar.save()
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()

    def _reset_crop_only(self):
        """Resets crop settings (crop box and straighten) to default and exits crop mode, PRESERVING 90-deg rotation."""
        if self.ui_state.isCropping:
            self.ui_state.isCropping = False
            self.update_status_message("Crop mode exited")
        self.ui_state.currentCropBox = (0, 0, 1000, 1000)
        # Also clear any editor-side crop box in case it's not fully synced yet
        self.image_editor.set_crop_box((0, 0, 1000, 1000))
        # Reset straighten angle since it is considered a crop-specific state
        self.image_editor.set_edit_param("straighten_angle", 0.0)
        if hasattr(self.ui_state, "cropRotation"):
            self.ui_state.cropRotation = 0.0
        if "straighten_angle" in self.image_editor.current_edits:
            self.image_editor.current_edits["straighten_angle"] = 0.0

    def _reset_crop_settings(self):
        """Resets crop settings (using _reset_crop_only) AND resets 90-degree rotation."""
        self._reset_crop_only()
        # Reset 90-degree rotation
        self.image_editor.set_edit_param("rotation", 0)
        # Also update UI state for rotation values if they are exposed
        if hasattr(self.ui_state, "rotation"):
            self.ui_state.rotation = 0

    @Slot()
    def launch_helicon_default(self):
        """Slot for QML/Keys that cannot pass arguments. Defaults to use_raw=True."""
        self.launch_helicon(use_raw=True)

    @Slot(bool)
    def launch_helicon(self, use_raw: bool = True):
        """Launches Helicon with selected files (RAW preferred if use_raw=True, else JPG) or stacks."""
        if self.stacks:
            log.info(
                "Launching Helicon for %d defined stacks (use_raw=%s).",
                len(self.stacks),
                use_raw,
            )
            any_success = False
            for start, end in self.stacks:
                files_to_process = []
                for idx in range(start, end + 1):
                    if idx < len(self.image_files):
                        img_file = self.image_files[idx]
                        # Use RAW if available and requested, otherwise use JPG
                        if use_raw and img_file.raw_pair:
                            file_to_use = img_file.raw_pair
                        else:
                            file_to_use = img_file.path
                        files_to_process.append(file_to_use)

                if files_to_process:
                    success = self._launch_helicon_with_files(files_to_process)
                    if success:
                        any_success = True
                else:
                    log.warning("No valid files found for stack [%d, %d].", start, end)

            # Only clear stacks if at least one launch succeeded
            if any_success:
                self.clear_all_stacks()

        else:
            log.warning("No selection or stacks defined to launch Helicon Focus.")
            return

        self.sync_ui_state()

    def _launch_helicon_with_files(self, files: List[Path]) -> bool:
        """Helper to launch Helicon with a specific list of files (RAW or JPG).

        Returns:
            True if Helicon was successfully launched, False otherwise.
        """
        log.info("Launching Helicon Focus with %d files.", len(files))
        unique_files = sorted(list(set(files)))
        success, tmp_path = launch_helicon_focus(unique_files)
        if success and tmp_path:
            # Defer deletion until shutdown to avoid race condition with Helicon Focus
            self._temp_files_to_clean.append(tmp_path)

            # Record stacking metadata
            today = date.today().isoformat()
            for file_path in unique_files:
                # Find the corresponding image file to get the stem
                for img_file in self.image_files:
                    # Match by either RAW pair or JPG path
                    if img_file.raw_pair == file_path or img_file.path == file_path:
                        meta = self.sidecar.get_metadata(img_file.path)
                        meta.stacked = True
                        meta.stacked_date = today
                        break
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)  # Invalidate cache

        return success

    def clear_all_stacks(self):
        log.info("Clearing all defined stacks.")
        self.stacks = []
        self.stack_start_index = None
        # Do NOT clear batches here

        self.sidecar.data.stacks = self.stacks
        self.sidecar.save()

        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()
        self.update_status_message("All stacks cleared")

    def clear_all_batches(self):
        """Clear all defined batches."""
        log.info("Clearing all defined batches.")
        self.batches = []
        self.batch_start_index = None
        self._invalidate_batch_cache()

        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("All batches cleared")

    def get_helicon_path(self):
        return config.get("helicon", "exe")

    def set_helicon_path(self, path):
        config.set("helicon", "exe", path)
        config.save()

    def get_photoshop_path(self):
        return config.get("photoshop", "exe")

    def set_photoshop_path(self, path):
        config.set("photoshop", "exe", path)
        config.save()

    def get_rawtherapee_path(self):
        return config.get("rawtherapee", "exe")

    def set_rawtherapee_path(self, path):
        config.set("rawtherapee", "exe", path)
        config.save()

    def open_file_dialog(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setNameFilter("Executables (*.exe)")
        if dialog.exec():
            return dialog.selectedFiles()[0]
        return ""

    def check_path_exists(self, path):
        return os.path.exists(path)

    def get_cache_size(self):
        return config.getfloat("core", "cache_size_gb")

    def get_cache_usage_gb(self):
        """Returns current cache usage in GB."""
        return self.image_cache.currsize / (1024**3)

    def set_cache_size(self, size):
        """Update cache size at runtime and persist to config."""
        size = max(0.5, min(size, 16.0))  # enforce sane bounds
        config.set("core", "cache_size_gb", size)
        config.save()

        old_max_bytes = self.image_cache.max_bytes
        new_max_bytes = int(size * 1024**3)
        if old_max_bytes == new_max_bytes:
            return

        log.info(
            "Resizing decoded image cache from %.2f GB to %.2f GB",
            old_max_bytes / (1024**3),
            size,
        )
        self.image_cache.max_bytes = new_max_bytes

        # If the new size is smaller than current usage, evict until under limit
        while self.image_cache.currsize > new_max_bytes and len(self.image_cache) > 0:
            try:
                self.image_cache.popitem()
            except KeyError:
                break

        # Allow future warnings after expanding the cache
        if new_max_bytes > old_max_bytes:
            self._has_warned_cache_full = False

    def get_prefetch_radius(self):
        return config.getint("core", "prefetch_radius")

    def set_prefetch_radius(self, radius):
        config.set("core", "prefetch_radius", radius)
        config.save()
        self.prefetcher.prefetch_radius = radius
        self.prefetcher.update_prefetch(self.current_index)

    def get_theme(self):
        return 0 if config.get("core", "theme") == "dark" else 1

    def set_theme(self, theme_index):
        # update Python-side state
        self.ui_state.theme = theme_index

        # persist it
        theme = "dark" if theme_index == 0 else "light"
        config.set("core", "theme", theme)
        config.save()

        # tell QML it changed (once is enough)
        self.ui_state.themeChanged.emit()

    @Slot(result=str)
    def get_color_mode(self):
        """Returns current color management mode: 'none', 'saturation', or 'icc'."""
        return config.get("color", "mode", fallback="none")

    @Slot(str)
    def set_color_mode(self, mode: str):
        """Sets color management mode and clears cache to force re-decode."""
        mode = mode.lower()
        if mode not in ["none", "saturation", "icc"]:
            log.error("Invalid color mode: %s", mode)
            return

        log.info("Setting color mode to: %s", mode)
        config.set("color", "mode", mode)
        config.save()

        # Clear ICC caches when color mode changes
        clear_icc_caches()

        # Clear cache and restart prefetcher to apply new color mode
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self._bump_display_generation()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()

        # Notify QML that color mode changed
        self.ui_state.colorModeChanged.emit()

        # Update status message
        mode_names = {
            "none": "Original Colors",
            "saturation": "Saturation Compensation",
            "icc": "Full ICC Profile",
        }
        self.update_status_message(f"Color mode: {mode_names.get(mode, mode)}")

    @Slot(result=float)
    def get_saturation_factor(self):
        """Returns current saturation factor (0.0-1.0)."""
        return config.getfloat("color", "saturation_factor", fallback=0.85)

    @Slot(float)
    def set_saturation_factor(self, factor: float):
        """Sets saturation factor and refreshes images."""
        factor = max(0.0, min(1.0, factor))  # Clamp to 0-1
        log.info("Setting saturation factor to: %.2f", factor)
        config.set("color", "saturation_factor", str(factor))
        config.save()

        # Only refresh if in saturation mode
        if self.get_color_mode() == "saturation":
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self._bump_display_generation()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

        # Notify QML
        self.ui_state.saturationFactorChanged.emit()

    @Slot(result=str)
    def get_awb_mode(self):
        return config.get("awb", "mode")

    @Slot(str)
    def set_awb_mode(self, mode):
        config.set("awb", "mode", mode)
        config.save()

    @Slot(result=float)
    def get_awb_strength(self):
        return config.getfloat("awb", "strength")

    @Slot(float)
    def set_awb_strength(self, value):
        config.set("awb", "strength", value)
        config.save()

        # Refresh if AWB was recently applied
        if self.get_color_mode() in ["saturation", "icc"]:
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self._bump_display_generation()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

    @Slot(float)
    @Slot(float, float)
    def set_straighten_angle(self, angle: float, target_aspect_ratio: float = -1.0):
        """Sets the straighten angle for the image editor and updates current view."""
        if not (self.ui_state.isEditorOpen or self.ui_state.isCropping):
            return

        # Optimization: Assume image is loaded by toggle_crop_mode or open_editor.
        # Avoid disk I/O here to prevent stutter during drag.
        if not self.image_editor.original_image:
            return

        # log.info(f"AppController.set_straighten_angle: {angle}, AR: {target_aspect_ratio}")

        # Update Aspect Ratio Compensation for Crop Box
        # If we have a target aspect ratio, we need to adjust the normalized crop box
        # because the underlying canvas aspect ratio changes with rotation (expand=True).
        if target_aspect_ratio > 0 and self.ui_state.currentCropBox:
            left, top, right, bottom = self.ui_state.currentCropBox
            w_norm = right - left
            h_norm = bottom - top

            if w_norm > 0 and h_norm > 0:
                # Calculate new canvas dimensions
                # PIL expand=True logic:
                im_w, im_h = self.image_editor.original_image.size
                # math imported at top level
                rad = math.radians(abs(angle))
                # New dimensions
                new_w = abs(im_w * math.cos(rad)) + abs(im_h * math.sin(rad))
                new_h = abs(im_w * math.sin(rad)) + abs(im_h * math.cos(rad))

                if new_w > 0 and new_h > 0:
                    canvas_aspect = new_w / new_h

                    # We want PixelAspect = (w_norm * new_w/1000) / (h_norm * new_h/1000) = target_aspect
                    # (w_norm / h_norm) * (new_w / new_h) = target_aspect
                    # w_norm / h_norm = target_aspect / canvas_aspect

                    target_norm_ratio = target_aspect_ratio / canvas_aspect

                    # Adjust dimensions to match target_norm_ratio
                    # Simple: Preserve Width, adjust Height.

                    new_h_norm = w_norm / target_norm_ratio

                    # If new height exceeds bounds (1000), constrain and adjust width instead
                    if new_h_norm > 1000:
                        new_h_norm = 1000
                        w_norm = new_h_norm * target_norm_ratio
                    # Recenter height
                    cy = (top + bottom) / 2
                    top = cy - new_h_norm / 2
                    bottom = cy + new_h_norm / 2

                    # Clamp vertical
                    if top < 0:
                        bottom -= top  # shift down
                        top = 0
                    if bottom > 1000:
                        top -= bottom - 1000  # shift up
                        bottom = 1000
                        top = max(0, top)  # double clamp

                    # Recenter width (if changed)
                    cx = (left + right) / 2
                    left = cx - w_norm / 2
                    right = cx + w_norm / 2

                    # Clamp horizontal
                    if left < 0:
                        right -= left
                        left = 0
                    if right > 1000:
                        left -= right - 1000
                        right = 1000
                        left = max(0, left)

                    self.ui_state.currentCropBox = (left, top, right, bottom)
                    self.image_editor.set_crop_box((left, top, right, bottom))

        log.debug("AppController.set_straighten_angle: %s", angle)
        # Pass the angle as-is (degrees CW).
        # QML rotation is CW-positive.
        # ImageEditor expects CW-positive and handles the inversion for PIL internally.
        self.image_editor.set_edit_param("straighten_angle", angle)

        # Trigger refresh. Since we are editing, we are viewing the preview.
        # Incrementing display generation invalidates cache, but for preview it just ensures freshness if logic depends on it.
        # Crucially, sync_ui_state emits currentImageSourceChanged, forcing QML to reload.
        # self.display_generation += 1
        # self.sync_ui_state() # DISABLE TO PREVENT FLASHING - QML handles preview live

    @Slot(result=int)
    def get_awb_warm_bias(self):
        return config.getint("awb", "warm_bias")

    @Slot(int)
    def set_awb_warm_bias(self, value):
        config.set("awb", "warm_bias", value)
        config.save()

    @Slot(result=int)
    def get_awb_tint_bias(self):
        return config.getint("awb", "tint_bias", fallback=0)

    @Slot(int)
    def set_awb_tint_bias(self, value):
        config.set("awb", "tint_bias", value)
        config.save()

    @Slot(result=int)
    def get_awb_luma_lower_bound(self):
        return config.getint("awb", "luma_lower_bound")

    @Slot(int)
    def set_awb_luma_lower_bound(self, value):
        config.set("awb", "luma_lower_bound", value)
        config.save()

    @Slot(result=int)
    def get_awb_luma_upper_bound(self):
        return config.getint("awb", "luma_upper_bound")

    @Slot(int)
    def set_awb_luma_upper_bound(self, value):
        config.set("awb", "luma_upper_bound", value)
        config.save()

    @Slot(result=int)
    def get_awb_rgb_lower_bound(self):
        return config.getint("awb", "rgb_lower_bound")

    @Slot(int)
    def set_awb_rgb_lower_bound(self, value):
        config.set("awb", "rgb_lower_bound", value)
        config.save()

    @Slot(result=int)
    def get_awb_rgb_upper_bound(self):
        return config.getint("awb", "rgb_upper_bound")

    @Slot(int)
    def set_awb_rgb_upper_bound(self, value):
        config.set("awb", "rgb_upper_bound", value)
        config.save()

    def get_default_directory(self):
        return config.get("core", "default_directory")

    def set_default_directory(self, path):
        config.set("core", "default_directory", path)
        config.save()

    def get_optimize_for(self):
        return config.get("core", "optimize_for", fallback="speed")

    def set_optimize_for(self, optimize_for):
        old_value = config.get("core", "optimize_for", fallback="speed")
        config.set("core", "optimize_for", optimize_for)
        config.save()

        # If the setting changed, clear cache and redraw current image
        if old_value != optimize_for:
            log.info(
                f"Optimize for changed from {old_value} to {optimize_for}, clearing cache and redrawing"
            )
            self.image_cache.clear()
            # Force redraw of current image
            if self.current_index >= 0 and self.current_index < len(self.image_files):
                self.ui_state.currentImageSourceChanged.emit()

    @Slot(result=float)
    def get_auto_level_clipping_threshold(self):
        return self.auto_level_threshold

    @Slot(float)
    def set_auto_level_clipping_threshold(self, value):
        # Clamp to 0-1 range for safety
        value = max(0.0, min(1.0, value))
        self.auto_level_threshold = value
        # Store as formatted string to avoid scientific notation weirdness or precision issues
        config.set("core", "auto_level_threshold", f"{value:.6g}")
        config.save()

    @Slot(result=float)
    def get_auto_level_strength(self):
        return self.auto_level_strength

    @Slot(float)
    def set_auto_level_strength(self, value):
        # Clamp to 0-1 range
        value = max(0.0, min(1.0, value))
        self.auto_level_strength = value
        config.set("core", "auto_level_strength", f"{value:.6g}")
        config.save()

    @Slot(result=bool)
    def get_auto_level_strength_auto(self):
        return self.auto_level_strength_auto

    @Slot(bool)
    def set_auto_level_strength_auto(self, value):
        self.auto_level_strength_auto = value
        # Store as canonical lowercase string
        config.set("core", "auto_level_strength_auto", "true" if value else "false")
        config.save()

    def open_directory_dialog(self):
        dialog = QFileDialog()
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        if dialog.exec():
            return dialog.selectedFiles()[0]
        return ""

    def _switch_to_directory(
        self, folder_path: Path, update_base_directory: bool = True
    ):
        """Canonical directory switch - used by both open_folder() and grid_navigate_to().

        Args:
            folder_path: The directory to switch to.
            update_base_directory: If True, also updates the thumbnail model's base directory
                                   (for File -> Open Folder). If False, keeps existing base
                                   (for grid navigation within current base).
        """
        # Stop the old watcher
        if self.watcher:
            self.watcher.stop()

        # Update the directory path
        self.image_dir = folder_path

        # Reinitialize directory-bound components
        self.watcher = Watcher(self.image_dir, self._request_watcher_refresh)
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)

        # Only update recycle bin when switching base directories (not subfolder navigation)
        # This ensures all deleted files go to the same recycle bin
        if update_base_directory:
            self.recycle_bin_dir = self.image_dir / "image recycle bin"
            # Only clear history when switching base directories
            self.delete_history = []
            self.undo_history = []
            # Clear grid navigation history (don't allow "back" to previous base folder)
            self._grid_nav_history.clear()
            self.ui_state.gridCanGoBackChanged.emit()

        # Clear directory-specific state
        self.stacks = []
        self.batches = []
        self.batch_start_index = None
        self.stack_start_index = None

        # Clear caches since they reference old directory's images
        with self._last_image_lock:
            self.last_displayed_image = None
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self._bump_display_generation()
        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        self._exif_brief_cache.clear()
        self._exif_pending_path = None

        # Clear batch indices cache (avoids stale batch membership checks)
        if hasattr(self, "_batch_indices_cache"):
            self._batch_indices_cache = set()
            self._batch_indices_cache_key = None

        # Clear editor state if open
        self.image_editor.clear()

        # Clear thumbnail cache BEFORE refresh to avoid stale thumbs
        if self._thumbnail_cache:
            self._thumbnail_cache.clear()

        self._set_folder_loaded(False)

        # Update thumbnail view infrastructure
        if self._thumbnail_model:
            if update_base_directory:
                self._thumbnail_model.set_directories(self.image_dir, self.image_dir)
                self._thumbnail_model.refresh()  # set_directories doesn't refresh
            else:
                # navigate_to() already calls refresh() internally
                self._thumbnail_model.navigate_to(self.image_dir)
            self._path_resolver.update_from_model(self._thumbnail_model)
            self.ui_state.gridDirectoryChanged.emit(str(self.image_dir))

        # Notify that the current directory changed (for window title)
        self.ui_state.currentDirectoryChanged.emit()

        # Load images from new directory (thumbnail model already refreshed above)
        self.load(skip_thumbnail_refresh=True)

    def _set_folder_loaded(self, loaded: bool) -> None:
        """Update the current-folder load state and notify QML when it changes."""
        if self._folder_loaded == loaded:
            return

        self._folder_loaded = loaded
        # Defensive for initialization and tests that may not have bound UIState yet.
        if hasattr(self, "ui_state") and self.ui_state:
            self.ui_state.isFolderLoadedChanged.emit()

    @Slot()
    def open_folder(self):
        """Opens a directory dialog and reloads the application with the selected folder."""
        path = self.open_directory_dialog()
        if path:
            self._switch_to_directory(Path(path), update_base_directory=True)

    def preload_all_images(self):
        if self.ui_state.isPreloading:
            log.info("Preloading is already in progress.")
            return

        log.info("Starting to preload all images, skipping cached.")
        self.ui_state.isPreloading = True
        self.ui_state.preloadProgress = 0

        self.reporter = self.ProgressReporter()
        self.reporter.progress_updated.connect(self._update_preload_progress)
        self.reporter.finished.connect(self._finish_preloading)

        total_images = len(self.image_files)
        if total_images == 0:
            log.info("No images to preload.")
            self.ui_state.isPreloading = False
            self.ui_state.preloadProgress = 0
            return

        # --- Check for cached images ---
        images_to_preload = []
        already_cached_count = 0
        _, _, display_gen = self.get_display_info()

        # We want to load images furthest from the current index FIRST,
        # and images closest to the current index LAST.
        # This ensures that the images the user is currently looking at (and their neighbors)
        # are the most recently added to the LRU cache, so they won't be evicted.

        # Calculate distance for all images
        # (index, distance_from_current)
        all_images_with_dist = []
        for i in range(total_images):
            dist = abs(i - self.current_index)
            all_images_with_dist.append((i, dist))

        # Sort by distance descending (furthest first)
        all_images_with_dist.sort(key=lambda x: x[1], reverse=True)

        # Determine which images are "nearby" (e.g. within prefetch radius * 2)
        # We will FORCE these to be re-cached even if they are already in cache,
        # to ensure they are moved to the front of the LRU queue.
        nearby_radius = self.prefetcher.prefetch_radius * 2

        for i, dist in all_images_with_dist:
            if i >= len(self.image_files):
                continue
            image_path = self.image_files[i].path
            cache_key = build_cache_key(image_path, display_gen)
            is_cached = cache_key in self.image_cache
            is_nearby = dist <= nearby_radius

            if is_cached and not is_nearby:
                already_cached_count += 1
            else:
                # Add to preload list if it's not cached OR if it's nearby (to refresh LRU)
                images_to_preload.append(i)

        log.info(
            f"Found {already_cached_count} cached images (skipping). Preloading {len(images_to_preload)} images (including nearby refreshes)."
        )

        if not images_to_preload:
            log.info("All images are already cached.")
            self._update_preload_progress(100)
            self._finish_preloading()
            return

        # --- Setup progress tracking ---
        # `completed` starts at the number of images already cached (that we are skipping).
        completed = already_cached_count

        # Update initial progress
        initial_progress = int((completed / total_images) * 100)
        self._update_preload_progress(initial_progress)

        def _on_done(_future):
            nonlocal completed
            completed += 1
            progress = int((completed / total_images) * 100)
            self.reporter.progress_updated.emit(progress)
            # Check if all images (including cached ones) are accounted for
            if completed == total_images:
                self.reporter.finished.emit()

        # --- Submit tasks ---
        # images_to_preload is already sorted furthest -> nearest
        for i in images_to_preload:
            # For nearby images that we are forcing to re-cache, we might need to remove them first
            # to ensure the cache actually updates the LRU position (depending on cache implementation).
            # ByteLRUCache (cachetools) updates LRU on access (get/set), so just overwriting is fine.
            # But we need to make sure we don't skip the task in prefetcher if it thinks it's already done.
            # The prefetcher checks self.futures, but we are submitting new ones.

            future = self.prefetcher.submit_task(i, self.prefetcher.generation)
            if future:
                future.add_done_callback(_on_done)

    def _update_preload_progress(self, progress: int):
        log.debug("Updating preload progress in UI: %d%%", progress)
        self.ui_state.preloadProgress = progress

    def _finish_preloading(self):
        self.ui_state.isPreloading = False
        self.ui_state.preloadProgress = 0
        log.info("Finished preloading all images.")

    @Slot(result=int)
    def get_batch_count_for_current_image(self) -> int:
        """Get the count of images in the batch that contains the current image."""
        if not self.image_files:
            return 0

        # Check if current image is in any batch
        for start, end in self.batches:
            if start <= self.current_index <= end:
                # Calculate total count across all batches
                total_count = sum(end - start + 1 for start, end in self.batches)
                return total_count

        return 0

    @staticmethod
    def _move_to_recycle(
        src: Path,
        _created_bins: set | None = None,
        unique_tag: str | None = None,
    ) -> Optional[Path]:
        """Moves a file to the recycle bin safely. Thread-safe, no Qt access.

        Uses uuid-based destination names to avoid collision checks.
        Tries fast os.replace first (same-filesystem), falls back to shutil.move.

        Args:
            src: Source file path.
            _created_bins: Optional set of already-created recycle bin dirs (cache).
            unique_tag: Optional shared UUID tag. When recycling paired files
                (JPG + RAW), pass the same tag so stems still match in the
                recycle bin and find_images() can re-pair them.

        Returns:
            Destination path in recycle bin, or None on failure.
        """
        if not src.exists() or not src.is_file():
            return None

        recycle_bin = src.parent / "image recycle bin"

        # Create recycle bin dir (cached per parent to skip redundant mkdirs)
        if _created_bins is None or recycle_bin not in _created_bins:
            try:
                recycle_bin.mkdir(parents=True, exist_ok=True)
                if _created_bins is not None:
                    _created_bins.add(recycle_bin)
            except OSError as e:
                log.error("Failed to create recycle bin: %s", e)
                return None

        # Use uuid suffix to guarantee unique name without existence checks.
        # Paired files (JPG + RAW) share the same tag so their stems match
        # in the recycle bin and find_images() can re-pair them.
        if unique_tag is None:
            unique_tag = uuid.uuid4().hex[:8]
        dest = recycle_bin / f"{src.stem}._fs_{unique_tag}{src.suffix}"

        try:
            # Fast path: rename within same filesystem (no data copy)
            os.replace(str(src), str(dest))
            log.info("Moved %s to recycle bin: %s (rename)", src.name, dest.name)
            return dest
        except OSError:
            pass  # Cross-device or permission issue, fall back to shutil

        try:
            shutil.move(str(src), str(dest))
            log.info("Moved %s to recycle bin: %s (copy)", src.name, dest.name)
            return dest
        except OSError as e:
            log.error("Failed to recycle %s: %s", src.name, e)
            return None

    @staticmethod
    def _perm_delete_worker(
        job_id: int,
        items: list,  # List of (original_index, ImageFile)
    ) -> dict:
        """Background worker: performs permanent deletion. No Qt access."""
        perm_success = []
        perm_fail = []

        for idx, img in items:
            try:
                # permanently_delete_image_files is imported from faststack.io.deletion
                if permanently_delete_image_files(img):
                    perm_success.append((idx, img))
                else:
                    perm_fail.append((idx, img))
            except Exception as e:
                log.error("Perm delete failed for %s: %s", img.path, e)
                perm_fail.append((idx, img))

        return {
            "job_id": job_id,
            "_perm_result": True,
            "perm_success": perm_success,
            "perm_fail": perm_fail,
        }

    @staticmethod
    def _delete_worker(
        job_id: int,
        images_to_delete: list,
        cancel_event: threading.Event,
    ) -> dict:
        """Background worker: performs file I/O for deletion. No Qt access.

        Args:
            job_id: Unique job identifier.
            images_to_delete: List of (jpg_path, raw_path) tuples.
            cancel_event: threading.Event; if set, abort early.

        Returns:
            dict with:
                job_id: int
                status: str ("completed")
                successes: list of {"jpg": Path, "recycled_jpg": Path, "raw": Path|None, "recycled_raw": Path|None}
                warnings: list of {"jpg": Path, "raw": Path, "message": str} (RAW move failed)
                failures: list of {"jpg": Path, "raw": Path|None, "code": str} (JPG move failed or cancelled)
                cancelled: bool
        """
        successes = []
        warnings = []
        failures = []
        created_bins: set = set()
        processed_count = 0
        did_cancel = False
        cancel_index = -1

        for i, item in enumerate(images_to_delete):
            if cancel_event.is_set():
                log.info("Delete job %d cancelled mid-flight", job_id)
                did_cancel = True
                cancel_index = i
                break

            # Sanity Check for Problem A (AttributeError):
            # images_to_delete MUST be List[Tuple[Path, Optional[Path]]]
            # If item is (0, (path, raw)), it's a nested structure from incorrect calling code.
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                log.error(
                    "CRITICAL: _delete_worker received invalid item format: %r", item
                )
                failures.append(
                    {
                        "jpg": None,
                        "raw": None,
                        "code": DeletionErrorCodes.INVALID_WORK_ITEM.value,
                    }
                )
                continue

            jpg_path, raw_path = item

            # Robustness: if raw_path is a tuple/list, we have a nested structure error.
            # This is a hard error — record failure and skip rather than silently recovering,
            # which would mask upstream bugs.
            if isinstance(raw_path, (tuple, list)):
                log.error(
                    "CRITICAL: _delete_worker received nested tuple item: %r", item
                )
                failures.append(
                    {
                        "jpg": str(jpg_path) if jpg_path else None,
                        "raw": None,
                        "code": DeletionErrorCodes.INVALID_WORK_ITEM.value,
                    }
                )
                continue

            processed_count += 1
            actual_raw_exists = bool(raw_path and raw_path.exists())

            try:
                # Share the same UUID tag for JPG + RAW so their stems
                # still match in the recycle bin (enables re-pairing).
                shared_tag = uuid.uuid4().hex[:8]
                recycled_jpg = AppController._move_to_recycle(
                    jpg_path, created_bins, unique_tag=shared_tag
                )
                if not recycled_jpg:
                    failures.append(
                        {
                            "jpg": jpg_path,
                            "raw": raw_path,
                            "code": DeletionErrorCodes.RECYCLE_FAILED.value,
                        }
                    )
                    continue

                recycled_raw = None
                if actual_raw_exists:
                    try:
                        recycled_raw = AppController._move_to_recycle(
                            raw_path, created_bins, unique_tag=shared_tag
                        )
                        if not recycled_raw:
                            raise OSError("RAW move failed")
                    except OSError as e:
                        log.warning("RAW recycle failed for %s: %s", raw_path.name, e)
                        warnings.append(
                            {"jpg": jpg_path, "raw": raw_path, "message": str(e)}
                        )

                successes.append(
                    {
                        "jpg": jpg_path,
                        "recycled_jpg": recycled_jpg,
                        "raw": raw_path,
                        "recycled_raw": recycled_raw,
                    }
                )

            except PermissionError:
                log.warning("Permission denied deleting %s", jpg_path.name)
                failures.append(
                    {
                        "jpg": jpg_path,
                        "raw": raw_path,
                        "code": DeletionErrorCodes.PERMISSION_DENIED.value,
                    }
                )
            except OSError as e:
                # Check for "trash full" or similar OS errors if distinguishable,
                # otherwise treat as generic recycle failure or unknown.
                # Windows "trash full" is hard to detect reliably without win32 api,
                # but we can at least capture the message.
                log.warning("OSError deleting %s: %s", jpg_path.name, e)
                failures.append(
                    {
                        "jpg": jpg_path,
                        "raw": raw_path,
                        "code": DeletionErrorCodes.RECYCLE_FAILED.value,  # Fallback to recycle failed
                        "message": str(e),
                    }
                )
            except Exception as e:
                log.warning("Recycle exception for %s: %s", jpg_path.name, e)
                failures.append(
                    {
                        "jpg": jpg_path,
                        "raw": raw_path,
                        "code": DeletionErrorCodes.UNKNOWN.value,
                        "message": str(e),
                    }
                )

        # Record unprocessed items (skipped due to cancellation)
        if did_cancel and cancel_index >= 0:
            remaining = images_to_delete[cancel_index:]
            for item in remaining:
                # Re-validate shape to prevent crashes on invalid items
                if not isinstance(item, (tuple, list)) or len(item) != 2:
                    continue

                jpg_path, raw_path = item
                failures.append(
                    {
                        "jpg": jpg_path,
                        "raw": raw_path,
                        "code": DeletionErrorCodes.CANCELLED.value,
                    }
                )

        # Convert all Path objects to str before crossing signal boundary.
        # _normalize_worker_results converts back to Path on the UI thread.
        for lst in (successes, warnings, failures):
            for d in lst:
                for k, v in d.items():
                    if isinstance(v, Path):
                        d[k] = str(v)

        return {
            "job_id": job_id,
            "status": "completed",
            "successes": successes,
            "warnings": warnings,
            "failures": failures,
            "cancelled": did_cancel,
        }

    def _on_delete_finished(self, result_dict: dict) -> None:
        """Main-thread completion handler for async delete worker.

        Refactored to 3-phase flow with typed data structures.
        """
        if self._shutting_down:
            return

        # --- Phase 1: Resolve Job & Result ---
        # Convert raw dict to typed result immediately
        result = DeleteResult.from_worker_dict(result_dict)

        # Retrieve job context
        job = self._pending_delete_jobs.pop(result.job_id, None)

        if job:
            # Remove pending_delete placeholders from undo history
            self.undo_history = [
                entry
                for entry in self.undo_history
                if not (entry[0] == "pending_delete" and entry[1] == job.job_id)
            ]
        else:
            # Job might have been popped by undo_delete logic already?
            # Or this is a stray signal.
            log.warning(
                "Delete job %d completed but not found in pending jobs", result.job_id
            )
            return

        # --- Phase 1.5: Handle Perm Delete Result ---
        if result.is_perm_result:
            if result.perm_success:
                self.update_status_message(
                    f"Permanently deleted {len(result.perm_success)} images"
                )

                # Update suppression for permanent deletes (prevent watcher re-scans)
                ttl = 2.0
                now = time.monotonic()
                with self._suppressed_paths_lock:
                    for _, img in result.perm_success:
                        if img.path:
                            self._suppressed_paths[self._key(img.path)] = now + ttl
                        if img.raw_pair:
                            self._suppressed_paths[self._key(img.raw_pair)] = now + ttl

            if result.perm_fail:
                # Rollback failures (they have original indices)
                # Note: job context is required for rollback (restores index/batches/focus)
                self._rollback_ui_items(result.perm_fail, job)

            self._rebuild_path_to_index()
            self.sync_ui_state()
            self._schedule_delete_refresh()
            return

        # --- Phase 2: Apply Results ---

        # 2a. Update suppression (prevent watcher loops for moved files)
        # Opportunistic cleanup of expired suppression entries
        ttl = 2.0
        now = time.monotonic()
        with self._suppressed_paths_lock:
            # Prune expired
            expired_keys = [k for k, t in self._suppressed_paths.items() if t < now]
            for k in expired_keys:
                del self._suppressed_paths[k]

            # Add new
            for s in result.successes:
                if s.jpg:
                    self._suppressed_paths[self._key(s.jpg)] = now + ttl
                if s.raw:
                    self._suppressed_paths[self._key(s.raw)] = now + ttl

        # 2b. Handle Policy 1: Auto-Restore if Undo Requested
        # If user hit Undo while worker was running, we must restore any moved files immediately.
        if job.undo_requested:
            if result.successes:
                log.info(
                    "Job %d was undone mid-flight; auto-restoring %d moved files",
                    job.job_id,
                    len(result.successes),
                )
                self._auto_restore_moved_files(result.successes)
            # Do NOT record history.
            # Failures/Cancelled items are already handled by the undo_delete logic (restored to UI)
            # or simply ignored because they never moved.

            # Update status
            self.update_status_message("Deletion cancelled (files restored)")
            self._schedule_delete_refresh()
            return

        # 2c. Normal Completion: Record History & Handle Failures

        # Track recycle bins
        for s in result.successes:
            if s.recycled_jpg:
                self.active_recycle_bins.add(s.recycled_jpg.parent)

        # Log warnings
        for w in result.warnings:
            log.warning("Partial delete warning for %s: %s", w.jpg, w.message)

        # Add to undo history
        for s in result.successes:
            # Store tuple of tuples: ((jpg, recycled_jpg), (raw, recycled_raw))
            record = ((s.jpg, s.recycled_jpg), (s.raw, s.recycled_raw))
            self.delete_history.append(record)
            self.undo_history.append(("delete", record, job.timestamp))

        # Handle Failures / Rollback UI
        # Only failed items need to be restored to UI.
        # Check for permanent delete candidates (recycle bin failures).
        self._handle_delete_failures(result, job)

        # --- Phase 3: Post Actions ---

        # Status Message
        count = len(result.successes)
        if count > 0:
            msg = f"Deleted {count} images"
            if result.warnings:
                msg += " (some RAW moves failed)"
            elif count == 1:
                msg = "Image moved to recycle bin"
            self.update_status_message(msg)
        elif result.failures:
            self.update_status_message(
                "Deletion cancelled" if result.cancelled else "Delete failed"
            )

        self._schedule_delete_refresh()

    def _auto_restore_moved_files(self, successes: List[DeleteRecord]) -> None:
        """Policy 1: Automatically move files back from recycle bin if undo was requested."""
        restored = 0
        for s in successes:
            # Restore JPG
            if s.jpg and s.recycled_jpg:
                ok, reason = self._restore_from_recycle_bin_safe(s.jpg, s.recycled_jpg)
                if ok:
                    restored += 1
                else:
                    log.error("Failed to auto-restore JPG %s: %s", s.jpg, reason)

            # Restore RAW
            if s.raw and s.recycled_raw:
                ok, reason = self._restore_from_recycle_bin_safe(s.raw, s.recycled_raw)
                if not ok:
                    log.error("Failed to auto-restore RAW %s: %s", s.raw, reason)

    def _handle_delete_failures(self, result: DeleteResult, job: DeleteJob) -> None:
        """Handle items that failed to delete. Rollback UI or prompt for perm delete."""
        if not result.failures:
            return

        # Identify which UI items failed (map back using paths)
        # Note: We use the _key() mapping to ensure we match robustly
        failed_keys = {self._key(f.jpg) for f in result.failures if f.jpg}

        failed_indices_and_imgs = []
        for idx, img in job.removed_items:
            if self._key(img.path) in failed_keys:
                failed_indices_and_imgs.append((idx, img))

        if not failed_indices_and_imgs:
            return

        # Check if we should offer permanent delete (recycle bin error)
        perm_candidates = []  # List of (idx, img)

        # Helper to find if a specific failure code warrants perm delete
        recycle_codes = {
            DeletionErrorCodes.RECYCLE_FAILED.value,
            DeletionErrorCodes.PERMISSION_DENIED.value,
            DeletionErrorCodes.TRASH_FULL.value,
        }

        # Map failure code by key for easy lookup
        failure_map = {self._key(f.jpg): f for f in result.failures if f.jpg}

        for idx, img in failed_indices_and_imgs:
            f = failure_map.get(self._key(img.path))
            if f and f.code in recycle_codes:
                perm_candidates.append((idx, img))

        if perm_candidates:
            # Prompt user for permanent delete

            # 1. Rollback non-candidates first
            candidate_keys = {self._key(img.path) for _, img in perm_candidates}
            to_rollback = [
                (i, img)
                for i, img in failed_indices_and_imgs
                if self._key(img.path) not in candidate_keys
            ]

            if to_rollback:
                self._rollback_ui_items(to_rollback, job)

            # 2. Ask user
            candidate_imgs = [img for _, img in perm_candidates]

            reason = "Recycle bin failure"
            confirmed = False
            if len(candidate_imgs) == 1:
                confirmed = confirm_permanent_delete(candidate_imgs[0], reason=reason)
            else:
                confirmed = confirm_batch_permanent_delete(
                    candidate_imgs, reason=reason
                )

            if confirmed:
                # ASYNC permanent delete
                # Put job back in pending map so _on_delete_finished can find it again
                self._pending_delete_jobs[job.job_id] = job

                # Define callback to bridge back to main thread
                def _on_perm_done(future):
                    try:
                        res = future.result()
                        # Emit on main thread via signal
                        self._deleteFinished.emit(res)
                    except Exception as e:
                        log.error("Perm delete worker exception: %s", e)

                fut = self._delete_executor.submit(
                    self._perm_delete_worker, job.job_id, perm_candidates
                )
                fut.add_done_callback(_on_perm_done)

                self.update_status_message("Permanently deleting files...")
                # Return EARLY so we don't rebuild index/sync UI yet
                return

            else:
                # User said NO, rollback candidates too
                self._rollback_ui_items(perm_candidates, job)

        else:
            # Just rollback everything
            self._rollback_ui_items(failed_indices_and_imgs, job)

        self._rebuild_path_to_index()
        self.sync_ui_state()

    @staticmethod
    def _recompute_batches_after_deletions(
        saved_batches: List[List[int]], still_deleted: List[int]
    ) -> List[List[int]]:
        """Return a copy of saved_batches with index spans adjusted for still_deleted.

        Used during partial rollbacks: start from the pre-delete snapshot and
        re-apply only the deletions that were not reversed.
        """
        if not still_deleted:
            return [b[:] for b in saved_batches]

        deleted_set = set(still_deleted)

        def _shift(orig_idx: int) -> int:
            return orig_idx - sum(1 for d in still_deleted if d < orig_idx)

        new_batches = []
        for b_start, b_end in saved_batches:
            first_ok = next(
                (i for i in range(b_start, b_end + 1) if i not in deleted_set),
                None,
            )
            if first_ok is None:
                continue  # whole batch still deleted — drop it
            last_ok = next(
                (i for i in range(b_end, b_start - 1, -1) if i not in deleted_set),
                None,
            )
            ns = _shift(first_ok)
            ne = _shift(last_ok)
            if ns <= ne:
                new_batches.append([ns, ne])
        return new_batches

    @staticmethod
    def _shift_start_index(
        saved_start: Optional[int], still_deleted: List[int]
    ) -> Optional[int]:
        """Shift a saved start index past still-deleted positions."""
        if saved_start is None:
            return None
        if saved_start in still_deleted:
            return None
        return saved_start - sum(1 for d in still_deleted if d < saved_start)

    def _rollback_ui_items(self, items: List[Tuple[int, Any]], job: DeleteJob) -> None:
        """Restore items to the UI list in correct order."""
        # Insert in ascending index order so each insertion shifts subsequent
        # indices correctly, restoring the original list positions.
        for idx, img in sorted(items, key=lambda x: x[0]):
            self.image_files.insert(min(idx, len(self.image_files)), img)

        # Restore selection/focus (approximated)
        self.current_index = min(job.previous_index, len(self.image_files) - 1)
        self._bump_display_generation()

        # Targeted cache invalidation instead of full clear
        if self.image_cache is not None:
            paths_to_invalidate = []
            for _, img in items:
                paths_to_invalidate.append(img.path)
                if img.raw_pair:
                    paths_to_invalidate.append(img.raw_pair)
            self.image_cache.evict_paths(paths_to_invalidate)

        if self._thumbnail_model:
            # Restore model rows (simple refresh for correctness)
            self._thumbnail_model.refresh()

        if self.image_files:
            self.prefetcher.update_prefetch(self.current_index)

        # Restore saved batch state if present
        ui = job.ui_state
        if ui is not None and ui.saved_batches is not None and items:
            original = {idx for idx, _ in job.removed_items}
            restored = {idx for idx, _ in items}
            if restored == original:
                # Full rollback: restore pre-delete snapshot directly
                self.batches = [b[:] for b in ui.saved_batches]
                self.batch_start_index = ui.saved_batch_start_index
            else:
                # Partial rollback: re-apply the deletions that were not reversed
                still_deleted = sorted(original - restored)
                self.batches = self._recompute_batches_after_deletions(
                    ui.saved_batches, still_deleted
                )
                self.batch_start_index = self._shift_start_index(
                    ui.saved_batch_start_index, still_deleted
                )
            self._invalidate_batch_cache()

        # Restore saved stack state if present
        if ui is not None and ui.saved_stacks is not None and items:
            original = {idx for idx, _ in job.removed_items}
            restored = {idx for idx, _ in items}
            if restored == original:
                self.stacks = [s[:] for s in ui.saved_stacks]
                self.stack_start_index = ui.saved_stack_start_index
            else:
                still_deleted = sorted(original - restored)
                self.stacks = self._recompute_batches_after_deletions(
                    ui.saved_stacks, still_deleted
                )
                self.stack_start_index = self._shift_start_index(
                    ui.saved_stack_start_index, still_deleted
                )
            self.sidecar.data.stacks = self.stacks
            self._metadata_cache_index = (-1, -1)

    def _schedule_delete_refresh(self) -> None:
        """Debounce post-delete refresh: coalesce rapid deletes into one refresh."""
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        QTimer.singleShot(200, self._fire_delete_refresh)

    def _fire_delete_refresh(self) -> None:
        """Called by QTimer after debounce delay."""
        self._refresh_scheduled = False
        self._do_delete_refresh()

    def _do_delete_refresh(self) -> None:
        """Perform user-interface refresh (debounce ended).

        Optimized: No longer performs a full disk scan (refresh_image_list).
        Relies on optimistic UI updates already performed in _delete_indices.
        Watcher events handle any true drift (external changes).
        """
        t_start = time.perf_counter()

        # Coalesce with watcher: if we are doing a delete refresh, we don't
        # need a separate watcher refresh immediately after.
        self._watcher_debounce_timer.stop()

        clear_raw_count_cache()
        self._rebuild_path_to_index()

        # Update the path resolver to reflect current model state
        if self._thumbnail_model and hasattr(self, "_path_resolver"):
            self._path_resolver.update_from_model(self._thumbnail_model)

        dt = time.perf_counter() - t_start
        if _debug_mode:
            log.info(
                "delete_refresh took %.4fs for %d images", dt, len(self.image_files)
            )

    def _delete_indices(self, indices: List[int], action_type: str) -> dict:
        """Unified core deletion engine for FastStack.

        Uses optimistic UI pattern: updates in-memory list and UI immediately
        for instant visual feedback, then enqueues file I/O to a background
        worker thread. Rollback or undo is handled by the completion handler.

        Args:
            indices: List of indices into self.image_files to delete.
            action_type: String for logging (e.g. 'loupe', 'grid_selection', 'grid_cursor', 'batch').

        Returns:
            dict with "requested_count", "queued" (bool),
            and "job_id" for the async delete job.
        """
        summary = {
            "total_deleted": 0,
            "recycled": 0,
            "permanent": 0,
            "failed_recycles": [],
            "cancelled": False,
            "requested_count": 0,
            "queued": False,
        }

        if not self.image_files or not indices:
            log.debug("[_delete_indices] Nothing to delete: action=%s", action_type)
            return summary

        # 1. Collect ImageFile objects and sort indices in reverse to prevent shifting
        sorted_indices = sorted(list(set(indices)), reverse=True)
        images_to_delete = []
        for idx in sorted_indices:
            if 0 <= idx < len(self.image_files):
                images_to_delete.append(self.image_files[idx])

        if not images_to_delete:
            log.warning("[_delete_indices] No valid indices found in %s", indices)
            return summary

        if self._block_if_saving(*[img.path for img in images_to_delete]):
            return summary

        summary["requested_count"] = len(images_to_delete)

        # --- PHASE 1: OPTIMISTIC UI UPDATE (instant, no I/O) ---
        # Snapshot for potential rollback (store in ascending order for proper restoration)
        removed_items = [
            (idx, self.image_files[idx])
            for idx in sorted(sorted_indices)
            if 0 <= idx < len(self.image_files)
        ]
        original_count = len(self.image_files)
        previous_index = self.current_index

        # Remove from in-memory list immediately for instant visual feedback
        for idx in sorted_indices:
            if 0 <= idx < len(self.image_files):
                del self.image_files[idx]

        # Reposition current_index immediately (fast, in-memory only)
        validated_sorted = sorted(i for i in sorted_indices if 0 <= i < original_count)
        deleted_set = set(validated_sorted)
        if not self.image_files:
            self.current_index = 0
        elif previous_index in deleted_set:
            # Current image was deleted → stay at same position (shows next image) or clamp
            self.current_index = min(previous_index, len(self.image_files) - 1)
        else:
            # Current image survived → shift index down for each deletion before it
            shift = sum(1 for d in validated_sorted if d < previous_index)
            self.current_index = max(
                0, min(previous_index - shift, len(self.image_files) - 1)
            )

        # Save batch/stack state before mutation so rollback can restore it.
        pre_batch_snapshot = [b[:] for b in self.batches]
        pre_batch_start_snapshot = self.batch_start_index
        pre_stack_snapshot = [s[:] for s in self.stacks]
        pre_stack_start_snapshot = self.stack_start_index

        # Shared helper: compute post-deletion index for a surviving index.
        deleted_ascending = sorted(validated_sorted)

        def _shift(orig_idx: int) -> int:
            return orig_idx - sum(1 for d in deleted_ascending if d < orig_idx)

        # Adjust batch index ranges to account for removed entries.
        # Deleting index d shifts every index > d down by one.  Without this,
        # batches that sit above any deleted image reference the wrong files.
        if self.batches:
            new_batches = []
            for b_start, b_end in self.batches:
                # Anchor on the first and last *surviving* indices in the range.
                # If every image in the batch was deleted, discard it entirely so
                # it cannot migrate onto unrelated images.
                first_ok = next(
                    (i for i in range(b_start, b_end + 1) if i not in deleted_set),
                    None,
                )
                if first_ok is None:
                    continue  # whole batch deleted — drop it
                last_ok = next(
                    (i for i in range(b_end, b_start - 1, -1) if i not in deleted_set),
                    None,
                )
                ns = _shift(first_ok)
                ne = _shift(last_ok)
                if ns <= ne:
                    new_batches.append([ns, ne])
            if new_batches != self.batches:
                self.batches = new_batches
                self._invalidate_batch_cache()

            # Adjust batch_start_index for removed entries
            if pre_batch_start_snapshot is not None:
                if pre_batch_start_snapshot in deleted_set:
                    self.batch_start_index = None
                else:
                    shifted = _shift(pre_batch_start_snapshot)
                    if shifted != self.batch_start_index:
                        self.batch_start_index = shifted
                        self._invalidate_batch_cache()

        # Adjust stack index ranges (same algorithm as batches).
        if self.stacks:
            new_stacks = []
            for s_start, s_end in self.stacks:
                first_ok = next(
                    (i for i in range(s_start, s_end + 1) if i not in deleted_set),
                    None,
                )
                if first_ok is None:
                    continue  # whole stack deleted — drop it
                last_ok = next(
                    (i for i in range(s_end, s_start - 1, -1) if i not in deleted_set),
                    None,
                )
                ns = _shift(first_ok)
                ne = _shift(last_ok)
                if ns <= ne:
                    new_stacks.append([ns, ne])
            if new_stacks != self.stacks:
                self.stacks = new_stacks
                self.sidecar.data.stacks = self.stacks
                self._metadata_cache_index = (-1, -1)

        # Adjust stack_start_index for removed entries
        if pre_stack_start_snapshot is not None:
            if pre_stack_start_snapshot in deleted_set:
                self.stack_start_index = None
            else:
                self.stack_start_index = _shift(pre_stack_start_snapshot)

        # Update UI immediately - this is fast since it just reads from memory
        # Check for existence, not truthiness (empty cache is falsy)
        if self.image_cache is not None:
            # Targeted eviction: remove only deleted images and their raw pairs
            # This preserves the cache for remaining images (huge perf win)
            paths_to_evict = []
            for img in images_to_delete:
                paths_to_evict.append(img.path)
                if img.raw_pair:
                    paths_to_evict.append(img.raw_pair)

            # Use new targeted eviction with tombstones
            self.image_cache.evict_paths(paths_to_evict)

        # Sync prefetcher's image list and cancel pending tasks
        if self.prefetcher:
            self.prefetcher.set_image_files(self.image_files)
            self.prefetcher.cancel_all()

        # Update ID mapping (now fast due to string hashing)
        self._rebuild_path_to_index()

        # SNAPPY: Tell the thumbnail model to remove these rows individually
        # instead of a full reset. This provides instant visual feedback in grid.
        if self._thumbnail_model:
            del_paths = [img.path for img in images_to_delete]
            self._thumbnail_model.remove_rows_by_path(del_paths)

            # Diagnostic: check synchronization between controller and model
            if _debug_mode:
                img_count = len(self.image_files)
                model_rows = self._thumbnail_model.rowCount()
                folder_count = getattr(self._thumbnail_model, "folder_count", 0)

                log.debug(
                    "Sync Check (delete): controller=%d, model=%d",
                    img_count,
                    model_rows,
                )
                log.debug(
                    "Sync Breakdown: images=%d, folders=%d, model_rows=%d",
                    img_count,
                    folder_count,
                    model_rows,
                )

        # Pre-suppress watcher events for these soon-to-be-moved/deleted paths.
        # Must happen BEFORE the worker starts I/O, because watchdog events can arrive immediately.
        ttl = (
            2.0  # seconds; plenty to cover os.replace/shutil.move and watchdog delivery
        )
        now = time.monotonic()
        with self._suppressed_paths_lock:
            for img in images_to_delete:
                self._suppressed_paths[self._key(img.path)] = now + ttl
                if img.raw_pair:
                    self._suppressed_paths[self._key(img.raw_pair)] = now + ttl

        self.sync_ui_state()

        # snapshot for worker: just paths. Worker checks existence dynamically.
        worker_items = [(img.path, img.raw_pair) for img in images_to_delete]

        # Create job record for tracking/undo
        job_id = self._next_delete_job_id
        self._next_delete_job_id += 1
        cancel_event = threading.Event()
        timestamp = time.time()

        self._pending_delete_jobs[job_id] = DeleteJob(
            job_id=job_id,
            removed_items=removed_items,
            action_type=action_type,
            timestamp=timestamp,
            cancel_event=cancel_event,
            previous_index=previous_index,
            images_to_delete=images_to_delete,
            ui_state=UIStateRestoration(
                saved_batches=pre_batch_snapshot,
                saved_batch_start_index=pre_batch_start_snapshot,
                saved_stacks=pre_stack_snapshot,
                saved_stack_start_index=pre_stack_start_snapshot,
            ),
        )

        # Add single placeholder undo entry per job
        self.undo_history.append(("pending_delete", job_id, timestamp))

        log.info(
            "Delete enqueued: job_id=%d, type='%s', count=%d",
            job_id,
            action_type,
            len(images_to_delete),
        )

        # Submit to background executor
        def _on_worker_done(fut):
            try:
                # Thread-safe signal emission from worker thread
                self._deleteFinished.emit(fut.result())
            except Exception as e:
                log.error("Delete worker failed: %s", e)
                # Emit a failure result so completion handler can rollback
                self._deleteFinished.emit(
                    {
                        "job_id": job_id,
                        "successes": [],
                        "failures": [
                            {
                                "jpg": str(p) if p else None,
                                "raw": str(r) if r else None,
                                "code": str(e),
                            }
                            for p, r in worker_items
                        ],
                        "cancelled": False,
                    }
                )

        fut = self._delete_executor.submit(
            self._delete_worker,
            job_id,
            worker_items,
            cancel_event,
        )
        fut.add_done_callback(_on_worker_done)

        summary["queued"] = True
        summary["job_id"] = job_id
        summary["requested_count"] = len(images_to_delete)
        return summary

    def _reposition_after_delete(
        self, preserved_path: Optional[Path], previous_index: int
    ):
        """Reposition current_index after the image list refreshed post-deletion."""
        if not self.image_files:
            self.current_index = 0
            return

        if preserved_path:
            for i, img_file in enumerate(self.image_files):
                if img_file.path == preserved_path:
                    self.current_index = i
                    return

        self.current_index = min(previous_index, len(self.image_files) - 1)

    @Slot()
    def delete_current_image_only(self):
        """Delete only the current image, ignoring batch selection."""
        self._delete_indices([self.current_index], "loupe_single_only")

    @Slot()
    def delete_batch_images(self):
        """Standard entry point for batch deletion.
        Deletes all images currently in batches.
        """
        if not self.batches:
            self.update_status_message("No images in batch to delete.")
            return

        # 1. Collect all indices from batches (filter to valid range)
        max_index = len(self.image_files) - 1
        indices_to_delete = set()
        for start, end in self.batches:
            for i in range(start, end + 1):
                if 0 <= i <= max_index:
                    indices_to_delete.add(i)

        # 2. Save batch state for rollback, then clear optimistically
        saved_batches = [b[:] for b in self.batches]
        saved_batch_start = self.batch_start_index

        # 3. Call unified engine
        summary = self._delete_indices(list(indices_to_delete), "batch")

        if not summary.get("queued"):
            # Nothing was enqueued (empty/invalid indices)
            return

        # 4. Clear batches optimistically; save state in job for rollback
        job_id = summary["job_id"]
        if job_id in self._pending_delete_jobs:
            job = self._pending_delete_jobs[job_id]
            if job.ui_state is None:
                job.ui_state = UIStateRestoration()
            job.ui_state.saved_batches = saved_batches
            job.ui_state.saved_batch_start_index = saved_batch_start

        self.batches = []
        self.batch_start_index = None
        self._invalidate_batch_cache()
        log.info("Batch state cleared optimistically for delete job %d.", job_id)

    def _restore_backup_safe(self, saved_path_str: str, backup_path_str: str) -> bool:
        """
        Robustly restores a backup file to its original location, handling
        locking and permission errors using a unique temporary file strategy.
        Verifies success.
        """
        saved_path = Path(saved_path_str)
        backup_path = Path(backup_path_str)

        if not backup_path.exists():
            if saved_path.exists():
                self.update_status_message("Already restored (backup missing)")
                log.warning("Backup %s missing but original exists.", backup_path)
            else:
                self.update_status_message("Backup not found")
                log.warning(
                    "Backup %s disappeared before it could be restored.", backup_path
                )
            return False

        # Generate a unique temporary path to avoid collisions
        temp_path = saved_path.with_suffix(f".{uuid.uuid4().hex}.tmp_restore")

        try:
            # 1. If the target exists, we need to move the backup to the temp location first,
            #    then try to swap. If target is locked, we can't delete it directly.
            if saved_path.exists():
                try:
                    saved_path.unlink()  # Try the easy way first
                except PermissionError as pe:
                    log.warning(
                        "File %s locked, attempting safe restore strategy: %s",
                        saved_path,
                        pe,
                    )

                    # Move backup to temp
                    try:
                        shutil.move(str(backup_path), str(temp_path))
                    except OSError as e:
                        log.error("Failed to move backup to temp: %s", e)
                        raise

                    if not temp_path.exists():
                        log.error("Temp file %s not found after move!", temp_path)
                        raise OSError(f"Failed to create temp file {temp_path}") from pe

                    # Try to force-move the temp file over the target (replace)
                    try:
                        os.replace(str(temp_path), str(saved_path))
                    except OSError:
                        # If replace fails, try to move back
                        log.error("Could not overwrite locked file %s", saved_path)
                        shutil.move(str(temp_path), str(backup_path))
                        raise

            # 2. If target doesn't exist (successfully unlinked or didn't exist), move backup to target
            if not saved_path.exists():
                # If we moved to temp, move temp -> target
                source = temp_path if temp_path.exists() else backup_path
                shutil.move(str(source), str(saved_path))

            # Verify restoration
            if not saved_path.exists():
                raise OSError(
                    f"Restoration failed: {saved_path} does not exist after move."
                )

            if saved_path.stat().st_size == 0:
                log.warning("Restored file %s is 0 bytes!", saved_path)

            log.info("Successfully restored %s from %s", saved_path, backup_path_str)
            return True

        except Exception as e:
            # Attempt cleanup
            if temp_path.exists():
                try:
                    if backup_path.exists():
                        temp_path.unlink()  # Backup still there, just kill temp
                    else:
                        shutil.move(str(temp_path), str(backup_path))  # Put it back
                except OSError:
                    pass
            log.exception("Detailed error in _restore_backup_safe")
            raise e

    @Slot()
    def _restore_from_recycle_bin_safe(
        self, src_path: Path, bin_path: Path
    ) -> Tuple[bool, str]:
        """Restores file from recycle bin safely.

        Returns:
            (success: bool, reason: str)
            Reasons: "ok", "missing_in_bin", "dest_exists", "move_failed"
        """
        if not bin_path.exists():
            return False, "missing_in_bin"
        if src_path.exists():
            return False, "dest_exists"

        try:
            src_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bin_path), str(src_path))
            if src_path.exists():
                return True, "ok"
            else:
                return False, "move_failed"
        except OSError as e:
            log.error("Failed to restore %s: %s", bin_path.name, e)
            return False, "move_failed"

    def _post_undo_refresh_and_select(
        self, target: Path, *, update_hist: bool = False
    ) -> None:
        """Centralized logic for refreshing state after an undo action."""
        self.refresh_image_list()

        # Find index of restored image
        target_resolve = target.resolve()
        for i, img_file in enumerate(self.image_files):
            try:
                if img_file.path.resolve() == target_resolve:
                    self.current_index = i
                    break
            except OSError:
                continue

        self._bump_display_generation()
        self.image_cache.clear()
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state()

        if update_hist and self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def undo_delete(self):
        """Unified undo that handles delete, pending_delete, and edit operations."""
        if not self.undo_history:
            self.update_status_message("Nothing to undo.")
            return

        # Get the most recent action
        action_type, action_data, timestamp = self.undo_history.pop()

        # --- PENDING DELETE: cancel in-flight and restore UI immediately ---
        if action_type == "pending_delete":
            job_id = action_data
            job = self._pending_delete_jobs.get(job_id)

            if job is not None:
                # Cancel the background worker (best-effort)
                job.cancel_event.set()
                # Mark as undo_requested so completion handler automatically restores files (Policy 1)
                job.undo_requested = True
                job.user_undone = True  # Keep for logic that checks if user intervened

                # Restore removed items to in-memory list immediately
                removed_items = job.removed_items
                previous_index = job.previous_index

                # Re-insert in ascending index order so each insertion shifts
                # subsequent indices correctly, restoring original positions.
                for idx, img in sorted(removed_items, key=lambda x: x[0]):
                    insert_idx = min(idx, len(self.image_files))
                    self.image_files.insert(insert_idx, img)

                self.current_index = min(previous_index, len(self.image_files) - 1)
                self._bump_display_generation()
                # Targeted eviction instead of full clear
                if self.image_cache is not None:
                    paths_to_evict = []
                    for _, img in removed_items:
                        paths_to_evict.append(img.path)
                        if img.raw_pair:
                            paths_to_evict.append(img.raw_pair)
                    self.image_cache.evict_paths(paths_to_evict)
                self.prefetcher.set_image_files(self.image_files)
                self.prefetcher.cancel_all()
                if self.image_files:
                    self.prefetcher.update_prefetch(self.current_index)
                self._rebuild_path_to_index()
                # Restore batch state that was shifted during _delete_indices
                ui = job.ui_state
                if ui is not None and ui.saved_batches is not None and removed_items:
                    self.batches = ui.saved_batches
                    self.batch_start_index = ui.saved_batch_start_index
                    self._invalidate_batch_cache()
                # Restore stack state that was shifted during _delete_indices
                if ui is not None and ui.saved_stacks is not None and removed_items:
                    self.stacks = ui.saved_stacks
                    self.stack_start_index = ui.saved_stack_start_index
                    self.sidecar.data.stacks = self.stacks
                    self._metadata_cache_index = (-1, -1)
                self.sync_ui_state()

                count = len(removed_items)
                self.update_status_message(
                    f"Cancel requested... restoring view ({count} item{'s' if count > 1 else ''})"
                )
                log.info(
                    "Undo cancelled pending delete job %d (%d items)", job_id, count
                )
            else:
                # Job already completed — find the corresponding "delete" entries
                # in undo_history and undo the last one
                self.update_status_message("Delete already completed, undoing...")
                # Fall through to try popping the next entry
                if self.undo_history:
                    action_type, action_data, timestamp = self.undo_history.pop()
                else:
                    self.update_status_message("Nothing to undo.")
                    return

        if action_type == "delete":
            try:
                jpg_pair, raw_pair = action_data
                jpg_src, jpg_bin = jpg_pair
                raw_src, raw_bin = raw_pair
            except Exception:
                self.update_status_message("Undo failed: unexpected undo record format")
                log.exception("Unexpected undo record format: %r", action_data)
                return

            # Remove from delete_history only if it matches (prevent duplicates)
            popped_delete_history = False
            if self.delete_history and self.delete_history[-1] == action_data:
                self.delete_history.pop()
                popped_delete_history = True

            restored_files = []
            jpg_res_ok = False

            # --- Jpeg Restore ---
            success, reason = self._restore_from_recycle_bin_safe(jpg_src, jpg_bin)
            if success:
                jpg_res_ok = True
                restored_files.append(jpg_src.name)
                log.info("Restored %s from recycle bin", jpg_src.name)
            elif reason == "dest_exists":
                log.warning(
                    "Restore skipped for %s: destination already exists", jpg_src.name
                )
            else:
                self.update_status_message(f"Undo failed: {reason} for {jpg_src.name}")
                self.undo_history.append(("delete", action_data, timestamp))
                if popped_delete_history:
                    self.delete_history.append(action_data)
                return

            # --- Raw Restore ---
            if raw_src and raw_bin:
                success, reason = self._restore_from_recycle_bin_safe(raw_src, raw_bin)
                if success:
                    restored_files.append(raw_src.name)
                    log.info("Restored %s from recycle bin", raw_src.name)
                elif reason == "dest_exists":
                    log.warning(
                        "Restore skipped for %s: destination already exists",
                        raw_src.name,
                    )
                    restored_files.append(f"{raw_src.name} (existed)")
                else:
                    if jpg_res_ok:
                        log.warning(
                            "RAW restore failed (%s), rolling back JPG for atomicity",
                            reason,
                        )
                        try:
                            shutil.move(str(jpg_src), str(jpg_bin))
                        except OSError as e:
                            log.error("Failed to rollback JPG: %s", e)
                            self.update_status_message(
                                "Partial restore error (manual cleanup needed)"
                            )
                            return

                    self.update_status_message(
                        f"Undo failed: {reason} for {raw_src.name}"
                    )
                    self.undo_history.append(("delete", action_data, timestamp))
                    if popped_delete_history:
                        self.delete_history.append(action_data)
                    return

            # --- Success Path ---
            if restored_files:
                files_str = ", ".join(restored_files)
                self.update_status_message(f"Restored: {files_str}")
            else:
                self.update_status_message("No files restored (destinations existed)")

            self._post_undo_refresh_and_select(jpg_src, update_hist=False)

            if self._thumbnail_model and self._is_grid_view_active:
                self._thumbnail_model.refresh()

        elif action_type in {
            "save_edit",
            "auto_white_balance",
            "auto_levels",
            "crop",
        }:
            try:
                (
                    saved_path,
                    backup_path,
                    metadata_path,
                    metadata_before,
                    metadata_sidecar,
                ) = self._parse_edit_undo_data(action_data)
            except ValueError as e:
                self.update_status_message(f"Undo failed: {e}")
                return

            try:
                if self._restore_backup_safe(saved_path, backup_path):
                    restore_sidecar = metadata_sidecar or self.sidecar
                    restore_metadata_path = (
                        Path(metadata_path) if metadata_path else Path(saved_path)
                    )
                    self._restore_metadata_snapshot(
                        restore_sidecar, restore_metadata_path, metadata_before
                    )
                    self._post_undo_refresh_and_select(
                        Path(saved_path),
                        update_hist=action_type != "crop",
                    )
                    if action_type == "save_edit":
                        self.update_status_message("Undid saved edit")
                    elif action_type == "auto_white_balance":
                        self.update_status_message("Undid auto white balance")
                    elif action_type == "auto_levels":
                        self.update_status_message("Undid auto levels")
                    else:
                        self.update_status_message("Undid crop")
            except (FileNotFoundError, OSError, shutil.Error) as e:
                self.update_status_message(f"Undo failed: {e}")
                if Path(backup_path).exists():
                    self.undo_history.append((action_type, action_data, timestamp))

    def shutdown_qt(self):
        """Shutdown Qt objects only - MUST run on main/Qt thread."""
        self._shutting_down = True  # set EARLY to make all slots no-op
        self._exif_pending_path = None
        log.info("Application shutting down (Qt cleanup).")

        # Stop Qt timers
        try:
            self._metadata_debounce_timer.stop()
            self._exif_debounce_timer.stop()
            self._watcher_debounce_timer.stop()
            self.histogram_timer.stop()
            self.resize_timer.stop()
            if hasattr(self, "_thumb_summary_timer"):
                self._thumb_summary_timer.stop()
        except Exception:
            pass

        # Stop QFileSystemWatcher if it's Qt-based
        try:
            self.watcher.stop()
        except Exception:
            pass

        # Check tracked recycle bins for logging
        bins_to_check = set(self.active_recycle_bins)
        try:
            bins_to_check.add(self.image_dir / "image recycle bin")
        except Exception:
            pass

        total_files = 0
        bin_stats = {}

        for bin_dir in bins_to_check:
            if bin_dir.exists() and bin_dir.is_dir():
                try:
                    stats = get_file_counts_by_extension(bin_dir)
                    count = sum(stats.values())
                    if count > 0:
                        total_files += count
                        bin_stats[bin_dir] = count
                except OSError:
                    pass

        if total_files > 0:
            log.info(
                "Shutdown with %d files in recycle bins: %s",
                total_files,
                list(bin_stats.keys()),
            )

        # Clear QML engine reference (but don't delete - let Qt handle it)
        if self.engine:
            log.info("Detaching QML engine.")
            self.engine = None

    @staticmethod
    def _safe_shutdown_executor(executor, name, *, wait=False, cancel_futures=True):
        """Shut down a single executor, logging and swallowing any error."""
        if executor is None:
            return
        try:
            executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        except Exception as e:
            log.warning("Error shutting down %s executor: %s", name, e, exc_info=True)

    def shutdown_nonqt(self):
        """Shutdown non-Qt resources - safe to run in background thread."""
        log.info("Shutting down background resources.")

        self._shutting_down = True  # gate async callbacks during shutdown_nonqt too
        self._exif_pending_path = None  # optional but consistent with shutdown_qt

        # Clear pending delete jobs and remove associated undo placeholders
        if self._pending_delete_jobs:
            log.info(
                "Clearing %d pending delete jobs on shutdown",
                len(self._pending_delete_jobs),
            )
            pending_ids = set(self._pending_delete_jobs.keys())
            self._pending_delete_jobs.clear()
            self.undo_history = [
                entry
                for entry in self.undo_history
                if not (entry[0] == "pending_delete" and entry[1] in pending_ids)
            ]

        # Shutdown thread pool executors — each isolated so one failure can't
        # prevent the others (especially save/delete) from shutting down.
        log.info("Shutting down background executors...")
        self._safe_shutdown_executor(self._hist_executor, "histogram", wait=False)
        self._safe_shutdown_executor(self._preview_executor, "preview", wait=False)
        self._safe_shutdown_executor(
            getattr(self, "_exif_executor", None),
            "exif",
            wait=False,
        )
        # wait=True ensures pending saves/deletes complete to avoid data loss/corruption
        self._safe_shutdown_executor(
            self._save_executor, "save", wait=True, cancel_futures=False
        )
        self._safe_shutdown_executor(
            self._delete_executor, "delete", wait=True, cancel_futures=False
        )

        # Shutdown prefetcher
        try:
            self.prefetcher.shutdown()
        except Exception as e:
            log.warning("Error shutting down prefetcher: %s", e)

        # Shutdown thumbnail prefetcher
        try:
            if getattr(self, "_thumbnail_prefetcher", None):
                self._thumbnail_prefetcher.shutdown()
        except Exception as e:
            log.warning("Error shutting down thumbnail prefetcher: %s", e)

        # Save sidecar state
        # NOTE: This runs on the main thread during shutdown (via main() -> shutdown_nonqt()).
        # It needs to be robust against file I/O errors to avoid hanging the exit.
        try:
            self.sidecar.set_last_index(self.current_index)
            self.sidecar.save()
        except Exception as e:
            log.warning("Error saving sidecar during shutdown: %s", e)

        # Clean up temporary files (e.g. Helicon Focus lists)
        if self._temp_files_to_clean:
            log.debug(
                "Cleaning up %d temporary files...", len(self._temp_files_to_clean)
            )
            for tmp_path in self._temp_files_to_clean:
                try:
                    tmp_path.unlink(missing_ok=True)
                    log.debug("Deleted temporary file: %s", tmp_path)
                except OSError as e:
                    log.warning("Error deleting temporary file %s: %s", tmp_path, e)
            self._temp_files_to_clean.clear()

        log.info("Background shutdown complete.")

    def shutdown(self):
        """Legacy shutdown method - calls both Qt and non-Qt shutdown."""
        self.shutdown_qt()
        self.shutdown_nonqt()

    def empty_recycle_bin(self):
        """Permanently deletes all files in all tracked recycle bins."""
        # Clean up tracked bins
        bins_to_clean = set(self.active_recycle_bins)
        # Check base bin too
        try:
            bins_to_clean.add(self.image_dir / "image recycle bin")
        except Exception:
            pass

        for bin_path in bins_to_clean:
            if bin_path.exists():
                try:
                    shutil.rmtree(bin_path)
                except OSError:
                    log.exception("Failed to empty recycle bin %s", bin_path)

        self.active_recycle_bins.clear()
        self.delete_history.clear()
        clear_raw_count_cache()
        log.info("Emptied recycle bins and cleared delete history")

    def _on_cache_evict(self, key, value, info):
        """Callback for when the image cache evicts an item.

        Args:
            key: Cache key that was evicted.
            value: Cached value that was evicted.
            info: Dict with eviction context captured at eviction time:
                  reason ("pressure"|"replace"|"manual"), usage_bytes, max_bytes,
                  entry_count, thread_id.
        """
        reason = info.get("reason", "unknown")

        # Only count capacity-pressure evictions toward thrashing detection.
        # Replacements and manual removals (pop_path, popitem resize) are not
        # indicators of cache size being too small.
        if reason != "pressure":
            if self.debug_cache:
                log.debug(
                    "Cache evict (skipped for thrash): reason=%s key=%s",
                    reason,
                    key,
                )
            return

        # Use usage captured at eviction time (inside the lock), not current
        # currsize which may be stale if clear()/evict_paths() ran between
        # the eviction and this callback executing outside the lock.
        eviction_usage = info.get("usage_bytes", 0)
        eviction_max = info.get("max_bytes", 1)

        if self.debug_cache:
            log.debug(
                "Cache evict (pressure): key=%s usage=%.2fMB/%.2fMB "
                "entries=%d thread=%s",
                key,
                eviction_usage / (1024**2),
                eviction_max / (1024**2),
                info.get("entry_count", -1),
                info.get("thread_id", "?"),
            )

        now = time.time()

        with self._eviction_lock:
            # 1. Record eviction timestamp / prune oldest outside window
            self._eviction_timestamps.append(now)
            cutoff = now - CACHE_THRASH_WINDOW_SECS
            while self._eviction_timestamps and self._eviction_timestamps[0] <= cutoff:
                self._eviction_timestamps.popleft()

            # 2. Check for thrashing (e.g., > threshold evictions in window)
            if len(self._eviction_timestamps) > CACHE_THRASH_THRESHOLD:
                # 3. Rate limit the warning
                if now - self._last_cache_warning_time > CACHE_WARNING_COOLDOWN_SECS:
                    self._last_cache_warning_time = now
                    self._has_warned_cache_full = True

                    # Use captured usage from eviction time for accurate reporting
                    used_gb = eviction_usage / (1024**3)
                    max_gb = eviction_max / (1024**3)

                    # Include key/value summary for context
                    val_summary = ""
                    if hasattr(value, "width") and hasattr(value, "height"):
                        val_summary = f" ({value.width}x{value.height})"
                    elif hasattr(value, "__len__"):
                        val_summary = f" (len={len(value)})"

                    key_name = getattr(key, "name", str(key))
                    msg = f"Cache thrashing! {len(self._eviction_timestamps)} evictions in {CACHE_THRASH_WINDOW_SECS}s. Evicted {key_name}{val_summary}. Usage: {used_gb:.1f}GB / {max_gb:.1f}GB."

                    # Schedule UI work safely on main thread
                    # QTimer.singleShot(0, ...) is thread-safe entry to main loop
                    QTimer.singleShot(0, lambda: self.update_status_message(msg))
                    log.warning(msg)

    @Slot()
    def edit_in_photoshop(self):
        if not self.image_files:
            self.update_status_message("No image to edit.")
            return

        current_image_path = self.image_files[self.current_index].path
        if self._block_if_saving(current_image_path):
            return

        # Prefer RAW file if it exists, otherwise use JPG
        image_file = self.image_files[self.current_index]
        jpg_path = image_file.path

        # Handle backup images: strip -backup, -backup2, -backup-1, etc. to find original RAW
        original_stem = jpg_path.stem
        # Remove -backup with optional digits or -backup-digits (handles both formats)
        original_stem = re.sub(r"-backup(-?\d+)?$", "", original_stem)

        # Look for RAW file with the original stem
        raw_path = None
        if image_file.raw_pair and image_file.raw_pair.exists():
            # Use the paired RAW if it exists
            raw_path = image_file.raw_pair
        else:
            # Search for RAW file manually by original stem
            for ext in RAW_EXTENSIONS:
                potential_raw = jpg_path.parent / f"{original_stem}{ext}"
                if potential_raw.exists():
                    raw_path = potential_raw
                    break

        if raw_path and raw_path.exists():
            current_image_path = raw_path
            log.info("Using RAW file for Photoshop: %s", raw_path)
        else:
            current_image_path = jpg_path
            log.info(
                "Using JPG file for Photoshop (no RAW found): %s", current_image_path
            )

        photoshop_exe = config.get("photoshop", "exe")
        photoshop_args = config.get("photoshop", "args")

        # Validate executable path securely
        is_valid, error_msg = validate_executable_path(
            photoshop_exe, app_type="photoshop", allow_custom_paths=True
        )

        if not is_valid:
            self.update_status_message(f"Photoshop validation failed: {error_msg}")
            log.error("Photoshop executable validation failed: %s", error_msg)
            return

        # Validate that the file path exists and is a file
        if not current_image_path.exists() or not current_image_path.is_file():
            self.update_status_message(
                f"Image file not found: {current_image_path.name}"
            )
            log.error("Image file not found or not a file: %s", current_image_path)
            return

        try:
            # Build command list safely
            command = [photoshop_exe]

            # Parse additional args safely using shlex (handles quotes and escapes properly)
            if photoshop_args:
                try:
                    # Use shlex to properly parse arguments with quotes/escapes
                    # On Windows, use posix=False to handle Windows-style paths
                    parsed_args = shlex.split(photoshop_args, posix=(os.name != "nt"))
                    command.extend(parsed_args)
                except ValueError as e:
                    log.error("Invalid photoshop_args format: %s", e)
                    self.update_status_message("Invalid Photoshop arguments configured")
                    return

            # Add the file path as the last argument
            # Convert to string but keep it as a list element (not shell-interpolated)
            command.append(str(current_image_path.resolve()))

            # SECURITY: Explicitly disable shell execution
            subprocess.Popen(
                command,
                shell=False,  # CRITICAL: Never use shell=True with user input
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,  # Close unused file descriptors
            )

            # Mark as edited on successful launch
            today = datetime.now().strftime("%Y-%m-%d")
            meta = self.sidecar.get_metadata(image_file.path)
            meta.edited = True
            meta.edited_date = today
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            self.update_status_message(
                f"Opened {current_image_path.name} in Photoshop."
            )
            log.info("Launched Photoshop with: %s", command)
        except FileNotFoundError as e:
            self.update_status_message(f"Photoshop executable not found: {e}")
            log.exception("Photoshop executable not found")
            # Don't mark as edited if launch failed
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.update_status_message(f"Failed to open in Photoshop: {e}")
            log.exception("Error launching Photoshop")
            # Don't mark as edited if launch failed
            return

    @Slot()
    def copy_path_to_clipboard(self):
        if not self.image_files:
            self.update_status_message("No image path to copy.")
            return

        current_image_path = str(self.image_files[self.current_index].path)
        QApplication.clipboard().setText(current_image_path)
        self.update_status_message(f"Copied: {current_image_path}")
        log.info("Copied path to clipboard: %s", current_image_path)

    @Slot()
    def reset_zoom_pan(self):
        """Resets zoom and pan to fit the image in the window (like Ctrl+0 in Photoshop)."""
        log.info("Resetting zoom and pan to fit window")
        self.ui_state.resetZoomPan()
        self.update_status_message("Reset zoom and pan")

    def update_status_message(self, message: str, timeout: int = 3000):
        """
        Updates the UI status message and clears it after a timeout.
        """

        def clear_message():
            if self.ui_state.statusMessage == message:
                self.ui_state.statusMessage = ""

        self.ui_state.statusMessage = message
        QTimer.singleShot(timeout, clear_message)

    def _capture_metadata_snapshot(
        self, sidecar: SidecarManager, image_path: Path
    ) -> Optional[dict]:
        """Capture the current sidecar metadata for undo/restore."""
        meta = sidecar.get_metadata(image_path, create=False)
        if meta is None:
            return None
        return {
            field_name: getattr(meta, field_name)
            for field_name in EntryMetadata.__dataclass_fields__
        }

    def _mark_image_edited_in_sidecar(
        self, sidecar: SidecarManager, image_path: Path
    ) -> Optional[dict]:
        """Mark an image as edited and return the pre-save metadata snapshot."""
        old_meta = self._capture_metadata_snapshot(sidecar, image_path)
        new_meta = dict(old_meta or {})
        new_meta["edited"] = True
        new_meta["edited_date"] = datetime.now().strftime("%Y-%m-%d")
        sidecar.update_metadata(image_path, new_meta)
        return old_meta

    @staticmethod
    def _build_edit_undo_data(
        saved_path: Path,
        backup_path: Path,
        *,
        metadata_path: Optional[Path] = None,
        metadata_before: Optional[dict] = None,
        sidecar: Optional[SidecarManager] = None,
    ) -> dict:
        """Build a backward-compatible undo payload for saved edit operations."""
        return {
            "saved_path": str(saved_path),
            "backup_path": str(backup_path),
            "metadata_path": str(metadata_path) if metadata_path else None,
            "metadata_before": metadata_before,
            "sidecar": sidecar,
        }

    @staticmethod
    def _parse_edit_undo_data(
        action_data: Any,
    ) -> tuple[str, str, Optional[str], Optional[dict], Any]:
        """Read both legacy tuple undo payloads and new dict payloads."""
        if isinstance(action_data, dict):
            saved_path = action_data.get("saved_path")
            backup_path = action_data.get("backup_path")
            if saved_path and backup_path:
                return (
                    str(saved_path),
                    str(backup_path),
                    action_data.get("metadata_path"),
                    action_data.get("metadata_before"),
                    action_data.get("sidecar"),
                )
        elif isinstance(action_data, (tuple, list)) and len(action_data) >= 2:
            return str(action_data[0]), str(action_data[1]), None, None, None

        raise ValueError(f"Unexpected edit undo payload: {action_data!r}")

    def _restore_metadata_snapshot(
        self, sidecar: SidecarManager, image_path: Path, snapshot: Optional[dict]
    ) -> None:
        """Restore only the metadata fields owned by edit-save actions."""
        stable_key = sidecar.metadata_key_for_path(image_path)
        current_meta = sidecar.data.entries.get(stable_key)
        restored_edited = bool(snapshot.get("edited", False)) if snapshot else False
        restored_edited_date = snapshot.get("edited_date") if snapshot else None
        changed = False

        if current_meta is None:
            if not restored_edited and restored_edited_date is None:
                return
            current_meta = sidecar.get_metadata(image_path, create=True)
            changed = True

        if current_meta.edited != restored_edited:
            current_meta.edited = restored_edited
            changed = True
        if current_meta.edited_date != restored_edited_date:
            current_meta.edited_date = restored_edited_date
            changed = True

        if snapshot is None:
            default_meta = EntryMetadata()
            if current_meta.__dict__ == default_meta.__dict__:
                del sidecar.data.entries[stable_key]
                changed = True

        if changed:
            sidecar.save()

    def _is_image_saving(self, file_path_str: str) -> bool:
        if not file_path_str or not hasattr(self, "_saving_keys"):
            return False
        return self._key(Path(file_path_str)) in self._saving_keys

    def _block_if_saving(self, *paths) -> bool:
        """Helper to block actions if any of the given paths are currently saving."""
        for path in paths:
            if path and self._is_image_saving(str(path)):
                self.update_status_message(
                    "This image is still saving. Please wait a moment.", timeout=3000
                )
                return True
        return False

    @Slot()
    def start_drag_current_image(self):
        if not self.image_files or self.current_index >= len(self.image_files):
            return

        # (Check moved below after batch resolution)
        path_to_check = self.image_files[self.current_index].path
        # We still check the current image early as a fast-fail
        if self._block_if_saving(path_to_check):
            return

        # Collect files to drag: batch files if any batches exist, otherwise current image
        files_to_drag = set()

        # Add all files from defined batches
        for start, end in self.batches:
            for idx in range(start, end + 1):
                if 0 <= idx < len(self.image_files):
                    files_to_drag.add(idx)

        # Only include current image if no batch files were selected
        if not files_to_drag:
            files_to_drag.add(self.current_index)

        # Convert to sorted list and get only existing paths
        file_indices = sorted(files_to_drag)
        existing_indices = [
            idx for idx in file_indices if self.image_files[idx].path.exists()
        ]

        # Check if ANY of the resolved files are currently saving
        for idx in existing_indices:
            if self._block_if_saving(self.image_files[idx].path):
                return

        # Prefer dragging the developed JPG if it exists (for external export),
        # but only when RAW mode is active or we are dragging a developed file itself.
        file_paths = []
        for idx in existing_indices:
            img = self.image_files[idx]

            # Suggestion: only prefer -developed.jpg when RAW mode is active
            # or when the current entry is itself the working/developed artifact.
            is_developed_artifact = img.path.stem.lower().endswith("-developed")
            in_raw_mode = getattr(self, "current_edit_source_mode", "jpeg") == "raw"

            if (
                in_raw_mode or is_developed_artifact
            ) and img.developed_jpg_path.exists():
                file_paths.append(img.developed_jpg_path)
            else:
                file_paths.append(img.path)

        if not file_paths:
            log.error("No valid files to drag")
            return

        if self.main_window is None:
            return

        drag = QDrag(self.main_window)
        mime_data = QMimeData()

        # Use Qt's standard setUrls - it handles both browser and native app compatibility
        urls = [QUrl.fromLocalFile(str(p)) for p in file_paths]
        mime_data.setUrls(urls)

        drag.setMimeData(mime_data)

        # --- thumbnail / drag preview ---
        pix = QPixmap(str(file_paths[0]))
        if not pix.isNull():
            # scale it down so it's not huge
            scaled = pix.scaled(128, 128, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            drag.setPixmap(scaled)
            # hotspot = center of image
            drag.setHotSpot(QPoint(scaled.width() // 2, scaled.height() // 2))

        log.info(
            "Starting drag for %d file(s): %s",
            len(file_paths),
            [str(p) for p in file_paths],
        )
        # Support both Copy and Move actions for browser compatibility
        result = drag.exec(Qt.CopyAction | Qt.MoveAction)
        log.info("Drag completed with result: %s", result)

        # Reset zoom/pan after drag completes (drag can cause unwanted panning)
        self.ui_state.resetZoomPan()

        # Mark all dragged files as uploaded if drag was successful
        if result in (Qt.CopyAction, Qt.MoveAction):
            today = datetime.now().strftime("%Y-%m-%d")

            for idx in existing_indices:
                meta = self.sidecar.get_metadata(self.image_files[idx].path)
                meta.uploaded = True
                meta.uploaded_date = today

            self.sidecar.save()

            # Clear all batches after successful drag (like pressing \)
            self.batches = []
            self.batch_start_index = None
            self._invalidate_batch_cache()

            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()
            log.info(
                "Marked %d file(s) as uploaded on %s. Cleared all batches.",
                len(existing_indices),
                today,
            )

    @Slot()
    def enable_raw_editing(self):
        """Switches the current image to RAW mode (using developed TIFF)."""
        if not self.image_files:
            return

        current_image_path = self.image_files[self.current_index].path
        if self._block_if_saving(current_image_path):
            return

        # 1. Update State
        # 1. Update State
        if self.current_edit_source_mode != "raw":
            self.current_edit_source_mode = "raw"
            self.editSourceModeChanged.emit("raw")
        self.sync_ui_state()

        # 2. Check if we have a valid TIFF ready
        path = self.get_active_edit_path(self.current_index)

        # If the path returned IS the working TIFF (and it exists), we can just load it.
        # Check specific condition:
        image_file = self.image_files[self.current_index]
        if path == image_file.working_tif_path and self.is_valid_working_tif(path):
            log.info("Valid working TIFF exists, switching to RAW mode immediately.")
            self.load_image_for_editing()  # This will now pick up the TIFF via get_active_edit_path
            return

        # 3. If not ready, trigger development
        # (Pass through to existing backend logic)
        self._develop_raw_backend()

    def _develop_raw_backend(self):
        """Internal: Triggers the actual RawTherapee process."""
        if not self.image_files:
            return

        image_file = self.image_files[self.current_index]
        if not image_file.has_raw:
            self.update_status_message("No RAW file available.")
            return

        raw_path = image_file.raw_path
        tif_path = image_file.working_tif_path

        # Resolve RawTherapee Executable
        from faststack.config import config

        rt_exe = config.get("rawtherapee", "exe")
        if not rt_exe or not os.path.exists(rt_exe):
            self.update_status_message("RawTherapee not found. Check settings.")
            log.error("RawTherapee executable not configured or missing: %s", rt_exe)
            return
        self.update_status_message("Developing RAW... please wait.")
        log.info("Starting RAW development: %s -> %s", raw_path, tif_path)

        def worker():
            # Check for optional args in config
            rt_args = config.get("rawtherapee", "args")

            # Build command: rawtherapee-cli -t -Y -o <out.tif> -c <in.raw>
            # -t: TIFF output
            # -b16: 16-bit depth (Critical! Default is often 8-bit)
            # -Y: Overwrite existing
            # -o: Output file
            # -c: Input file (must be last)
            cmd = [rt_exe, "-t", "-b16", "-Y", "-o", str(tif_path)]

            if rt_args:
                try:
                    # Use shlex to properly parse arguments with quotes/escapes
                    # On Windows, use posix=False to handle Windows-style paths
                    parsed_args = shlex.split(rt_args, posix=(os.name != "nt"))
                    cmd.extend(parsed_args)
                except ValueError as e:
                    log.error("Invalid rawtherapee args format: %s", e)

            cmd.extend(["-c", str(raw_path)])
            cmd_str = " ".join(cmd)  # For logging

            # Run process
            run_kwargs = {
                "capture_output": True,
                "text": True,
                "timeout": 60,  # 60 second timeout
            }
            if sys.platform == "win32":
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            try:
                result = subprocess.run(cmd, check=False, **run_kwargs)

                if result.returncode == 0:
                    if tif_path.exists() and tif_path.stat().st_size > 0:
                        log.info("RAW development successful.")
                        # Use partial to bind variable deeply
                        QTimer.singleShot(
                            0, functools.partial(self._on_develop_finished, True, None)
                        )
                        return  # Success path
                    msg = f"RawTherapee exited successfully but output file is missing or empty.\nCommand: {cmd_str}"
                    log.error(msg)
                    QTimer.singleShot(
                        0, functools.partial(self._on_develop_finished, False, msg)
                    )
                else:
                    stderr = result.stderr.strip() if result.stderr else "(no stderr)"
                    stdout = result.stdout.strip() if result.stdout else "(no stdout)"
                    err_msg = f"RawTherapee failed (exit code {result.returncode}):\nCommand: {cmd_str}\nstderr: {stderr}\nstdout: {stdout}"
                    log.error(err_msg)
                    QTimer.singleShot(
                        0, functools.partial(self._on_develop_finished, False, err_msg)
                    )

            except subprocess.TimeoutExpired:
                err_msg = f"RawTherapee timed out after 60 seconds.\nCommand: {cmd_str}"
                log.error(err_msg)
                QTimer.singleShot(
                    0, functools.partial(self._on_develop_finished, False, err_msg)
                )
            except Exception as e:
                err_msg = f"Unexpected error running RawTherapee: {str(e)}"
                log.exception(err_msg)
                QTimer.singleShot(
                    0, functools.partial(self._on_develop_finished, False, err_msg)
                )
            finally:
                # Cleanup if we failed and left a bad file or 0-byte file (unless success logic already returned)
                # Note: success logic returns early. If we are here, we likely failed or fell through (e.g. 0 byte file case did not return)
                # Actually, the 0-byte case calls on_finished but doesn't return, so it falls here.
                # Let's check specifically if we need to cleanup.
                # If we succeeded, we returned.
                if tif_path.exists() and "result" in locals():
                    # Only cleanup if result was assigned (subprocess ran)
                    # If it's 0 bytes or we are in an error state (which implies we didn't return early)
                    try:
                        if tif_path.stat().st_size == 0:
                            tif_path.unlink()
                        elif result.returncode != 0:
                            # If we crashed but left a file, delete it
                            tif_path.unlink()
                    except (OSError, AttributeError):
                        # AttributeError if result is None
                        pass

        threading.Thread(target=worker, daemon=True).start()

    # Preserving legacy slot name for compatibility if QML calls it directly,
    # but QML should call enable_raw_editing now.
    # Actually provider.py calls this. I will update provider.py to call enable_raw_editing.
    # But I'll keep this as a proxy to the new method just in case.
    @Slot()
    def develop_raw_for_current_image(self):
        self.enable_raw_editing()

    # Return sentinels for load_image_for_editing():
    #   True  — a real reload was performed (new file or changed mtime)
    #   _REUSED — the existing editor session was kept (truthy, but ``is True`` is False)
    #   False — load failed or was aborted
    _REUSED = 2  # truthy int so QML @Slot(result=bool) coerces to true

    @Slot(result=bool)
    def load_image_for_editing(self):
        """Load the currently viewed image into the editor.

        Returns True on real reload, _REUSED when the existing session
        was kept, or False on failure.  The @Slot annotation coerces
        _REUSED to true for QML callers (none of which inspect the value).
        """
        try:
            if self.view_override_path:
                active_path = Path(self.view_override_path)
            else:
                active_path = self.get_active_edit_path(self.current_index)
            filepath = str(active_path)

            editor_path = getattr(self.image_editor, "current_filepath", None)
            match = False
            if editor_path:
                try:
                    match = Path(editor_path).resolve() == Path(filepath).resolve()
                    if match:
                        mtime = Path(filepath).stat().st_mtime
                        if mtime != getattr(self.image_editor, "current_mtime", 0.0):
                            match = False
                except (OSError, ValueError):
                    pass

            if match:
                # Also require an intact float buffer — a preview_only load leaves
                # current_filepath/mtime set but float_image=None, which breaks
                # crop, darken, and full-editor flows that need the master buffer.
                if getattr(self.image_editor, "float_image", None) is None:
                    match = False

            if match:
                log.debug(
                    "load_image_for_editing: Reusing existing session for %s", filepath
                )
                # Ensure the background renderer is current and notify UI to refresh
                # Also synchronize sliders/crop state to the backend session.
                self._sync_editor_state_from_session()
                return self._REUSED

            # Fetch cached preview if available for faster initial display
            cached_preview = self.get_decoded_image(self.current_index)

            # Determine if we should capture source EXIF (e.g., for RAW mode)
            source_exif = None
            if self.current_edit_source_mode == "raw":
                # Capture EXIF from the original JPEG to preserve in developed JPG
                image_file = self.image_files[self.current_index]
                jpeg_path = image_file.path
                # Only if the main path isn't itself a TIFF (avoid recursion)
                if (
                    jpeg_path.suffix.lower() not in (".tif", ".tiff")
                    and jpeg_path.exists()
                ):
                    try:
                        with Image.open(jpeg_path) as src_im:
                            source_exif = src_im.info.get("exif")
                    except Exception as e:
                        log.warning(
                            "Failed to capture source EXIF from %s: %s", jpeg_path, e
                        )

            # Load into editor
            if self.image_editor.load_image(
                filepath, cached_preview=cached_preview, source_exif=source_exif
            ):
                # Notify UIState to update bindings
                # We do this via signals or by calling the update function on UIState if available
                # But UIState listens to editor signals?
                # Actually, the previous implementation in UIState pushed edits to itself.
                # We need to preserve that behavior.
                # For now, simpler to emit a signal that UIState listens to,
                # OR just manually update UIState here if we have reference.
                if self.ui_state:
                    self._sync_editor_state_from_session()

                return True  # Real reload performed

        except Exception as e:
            log.exception("Failed to load image for editing: %s", e)
            self.update_status_message(f"Error loading editor: {e}")
            if self.ui_state:
                self.ui_state.isEditorOpen = False
            return False

        # load_image returned False
        if self.ui_state:
            self.ui_state.isEditorOpen = False
        return False

    def _sync_editor_state_from_session(self):
        """Helper to push current editor session state (edits, crop) to UIState."""
        edits = self.image_editor.current_edits
        for key, value in edits.items():
            if hasattr(self.ui_state, key):
                setattr(self.ui_state, key, value)

        # Reset visual components
        if hasattr(self.ui_state, "aspectRatioNames"):
            self.ui_state.aspectRatioNames = [r["name"] for r in ASPECT_RATIOS]
            # Pull crop box specifically; use default if None
            crop_box = edits.get("crop_box")
            if crop_box:
                self.ui_state.currentCropBox = crop_box
            else:
                self.ui_state.currentAspectRatioIndex = 0
                self.ui_state.currentCropBox = (0, 0, 1000, 1000)

        # Kick off background render
        self._kick_preview_worker()
        # Notify UI
        self.ui_state.editorImageChanged.emit()

    def _on_develop_finished(self, success: bool, error_msg: Optional[str]):
        """Callback on main thread after RAW development."""
        if success:
            self.update_status_message("RAW Development complete.")
            # Load active path (which should now be the developed TIFF)
            self.load_image_for_editing()
        else:
            self.update_status_message(f"Development failed: {error_msg}")
            # Ensure UI reflects failure (maybe revert mode? or just show error)
            # Staying in RAW mode but failing to load allows user to try again or see error.

    @Slot(result=DecodedImage)
    def get_preview_data(self) -> Optional[DecodedImage]:
        """Gets the preview data of the currently edited image as a DecodedImage."""
        return self.image_editor.get_preview_data()

    @Slot(str, "QVariant")
    def set_edit_parameter(self, key: str, value: Any):
        """Sets an edit parameter and updates the UIState for the slider visual."""
        # Robust guard: only allow edits if the editor is actually holding an image.
        if not self.image_editor:
            return
        if self.image_editor.current_filepath is None:
            return
        # Must have either a float image (working copy) or original loaded
        if (
            self.image_editor.float_image is None
            and self.image_editor.original_image is None
        ):
            return

        try:
            # Update actual edit state (this bumps _edits_rev and invalidates preview cache)
            changed = self.image_editor.set_edit_param(key, value)

            # Sync UI state with backend (e.g., rotation might be rounded)
            final_value = value
            if changed:
                # Use thread-safe accessor to get the actual value applied
                actual = self.image_editor.get_edit_value(key)
                if actual is not None:
                    final_value = actual

            # Update UI state regardless (visual sliders need to match what user dragged, OR the clamped backend value)
            if hasattr(self.ui_state, key):
                setattr(self.ui_state, key, final_value)

            # Trigger a refresh of the image to show the edit, ONLY if something changed
            # Uses gate pattern: runs immediately if not inflight, else queues for next
            if changed:
                self._kick_preview_worker()
        except Exception as e:
            log.error("Error setting edit parameter %s=%s: %s", key, value, e)

    @Slot(int, int, int, int)
    def set_crop_box(self, left: int, top: int, right: int, bottom: int):
        """Sets the normalized crop box (0-1000) in the editor."""
        crop_box: Tuple[int, int, int, int] = (left, top, right, bottom)
        self.image_editor.set_crop_box(crop_box)
        self.ui_state.currentCropBox = crop_box  # Update QML visual (if implemented)

    @Slot()
    def reset_edit_parameters(self):
        """Resets all editing parameters in the editor."""
        self.image_editor.reset_edits()
        if hasattr(self.ui_state, "reset_editor_state"):
            self.ui_state.reset_editor_state()

        self.update_status_message("Edits reset")

        # Trigger a refresh to show the reset image
        self.ui_refresh_generation += 1
        self._kick_preview_worker()

        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    # ---- Background Darkening Tool ----

    def _reset_darken_on_navigation(self):
        """Reset all darken-specific state on image switch.

        Called from _set_current_index() to ensure a clean slate.
        Clears both editor-level mask data AND UI-side panel values,
        because navigation while the editor is open does NOT call
        editor.load_image().
        """
        # Editor-level: clear mask assets, raster cache, and darken settings
        self.image_editor._mask_assets.clear()
        self.image_editor._mask_raster_cache.clear()
        if self.image_editor.current_edits.get("darken_settings") is not None:
            self.image_editor.current_edits["darken_settings"] = None
            self.image_editor._edits_rev += 1
        # In-progress stroke
        self._current_darken_stroke = None
        # Tool mode
        self.ui_state.isDarkening = False
        # Overlay image
        self.ui_state._darken_overlay_image = None
        self.ui_state._darken_overlay_generation += 1
        self.ui_state.darken_overlay_generation_changed.emit()
        # Reset slider / panel values via property setters so QML bindings update
        self.ui_state.darkenOverlayVisible = True
        self.ui_state.darkenAmount = 0.5
        self.ui_state.darkenEdgeProtection = 0.5
        self.ui_state.darkenSubjectProtection = 0.5
        self.ui_state.darkenFeather = 0.5
        self.ui_state.darkenDarkRange = 0.5
        self.ui_state.darkenNeutrality = 0.5
        self.ui_state.darkenExpandContract = 0.0
        self.ui_state.darkenAutoEdges = 0.0
        self.ui_state.darkenMode = "assisted"
        self.ui_state.darkenBrushRadius = 0.03

    def _prepare_darken_image_state(self) -> bool:
        """Helper to ensure the correct image is loaded for darkening."""
        needs_load = (
            self.image_editor.float_image is None
            or self.image_editor.current_filepath is None
        )
        if not needs_load:
            try:
                active = (
                    self.view_override_path
                    if self.view_override_path
                    else self.get_active_edit_path(self.current_index)
                )
                current_p = self.image_editor.current_filepath
                if current_p and active:
                    match = Path(current_p).resolve() == Path(active).resolve()
                    if not match:
                        needs_load = True
                else:
                    needs_load = True
            except (IndexError, TypeError, OSError, ValueError):
                needs_load = True

        if needs_load:
            load_result = self.load_image_for_editing()
            if load_result is False:
                return False  # load failed — abort rather than darken stale data
            # Only reset darken on a real reload, not when reusing the session
            if load_result is True:
                self._reset_darken_on_navigation()
        return True

    @Slot()
    def open_darken_tool(self):
        """Activate the darkening tool, loading the image if needed.

        The darken panel is independent of the editor sidebar — pressing K
        in loupe view opens the darken panel without forcing the editor
        panel open.  The image is silently loaded for editing if it hasn't
        been already.
        """
        if not self._prepare_darken_image_state():
            return
        self._ensure_darken_state()
        self.ui_state.isDarkening = True
        self._kick_preview_worker()
        self._update_darken_overlay()

    @Slot()
    def toggle_darken_mode(self):
        """Toggle the background darkening tool on/off.

        Turning off also disables the darkening effect so the preview
        reverts to the un-darkened image.
        """
        if self.ui_state.isDarkening:
            self.ui_state.isDarkening = False
            ds = self.image_editor.current_edits.get("darken_settings")
            if ds is not None:
                ds.enabled = False
                self.image_editor._edits_rev += 1
                self._kick_preview_worker()
        else:
            if not self._prepare_darken_image_state():
                return
            self._ensure_darken_state()
            self.ui_state.isDarkening = True
            self._kick_preview_worker()
            self._update_darken_overlay()

    def _ensure_darken_state(self):
        """Ensure MaskData and DarkenSettings exist for the darken tool."""
        if "darken" not in self.image_editor._mask_assets:
            self.image_editor._mask_assets["darken"] = MaskData()
        if self.image_editor.current_edits.get("darken_settings") is None:
            ds = DarkenSettings(enabled=True)
            self.image_editor.current_edits["darken_settings"] = ds
            self.image_editor._edits_rev += 1
        else:
            ds = self.image_editor.current_edits["darken_settings"]
            if not ds.enabled:
                ds.enabled = True
                self.image_editor._edits_rev += 1

    @Slot(float, float, str)
    def start_darken_stroke(self, x_norm: float, y_norm: float, stroke_type: str):
        """Begin a new brush stroke. Coords are normalised [0,1] relative to the
        displayed (post-crop, post-straighten) image."""
        edits = self.image_editor.current_edits
        x_base, y_base = inverse_transform(
            x_norm,
            y_norm,
            edits,
            (1, 1),  # display_shape unused for normalised
        )
        brush_r = self.ui_state._darken_brush_radius
        self._current_darken_stroke = {
            "points": [(x_base, y_base)],
            "radius": brush_r,
            "stroke_type": stroke_type,
        }

    @Slot(float, float)
    def continue_darken_stroke(self, x_norm: float, y_norm: float):
        """Add a point to the current brush stroke."""
        stroke = getattr(self, "_current_darken_stroke", None)
        if stroke is None:
            return
        edits = self.image_editor.current_edits
        x_base, y_base = inverse_transform(x_norm, y_norm, edits, (1, 1))
        stroke["points"].append((x_base, y_base))

    @Slot()
    def finish_darken_stroke(self):
        """Commit the current stroke to MaskData."""
        stroke = getattr(self, "_current_darken_stroke", None)
        if stroke is None or not stroke["points"]:
            return
        self._current_darken_stroke = None

        mask_data = self.image_editor._mask_assets.get("darken")
        if mask_data is None:
            return

        ms = MaskStroke(
            points=stroke["points"],
            radius=stroke["radius"],
            stroke_type=stroke["stroke_type"],
        )
        mask_data.add_stroke(ms)

        # Bump editor revision and refresh preview
        self.image_editor._edits_rev += 1
        self._kick_preview_worker()
        self._update_darken_overlay()

    @Slot()
    def undo_darken_stroke(self):
        """Remove the last brush stroke."""
        mask_data = self.image_editor._mask_assets.get("darken")
        if mask_data is None or not mask_data.has_strokes():
            return
        mask_data.undo_last_stroke()
        self.image_editor._edits_rev += 1
        self._kick_preview_worker()
        self._update_darken_overlay()

    @Slot()
    def clear_darken_strokes(self):
        """Clear all brush strokes."""
        mask_data = self.image_editor._mask_assets.get("darken")
        if mask_data is None or not mask_data.has_strokes():
            return
        mask_data.clear_strokes()
        self.image_editor._edits_rev += 1
        self._kick_preview_worker()
        self._update_darken_overlay()

    @Slot(str, float)
    def set_darken_param(self, key: str, value: float):
        """Update a DarkenSettings scalar and refresh."""
        ds = self.image_editor.current_edits.get("darken_settings")
        if ds is None:
            return
        if not hasattr(ds, key):
            log.warning("Unknown darken param: %s", key)
            return
        setattr(ds, key, value)
        # Sync UIState — map DarkenSettings field name → UIState property name
        prop_map = {
            "darken_amount": "darkenAmount",
            "edge_protection": "darkenEdgeProtection",
            "subject_protection": "darkenSubjectProtection",
            "feather": "darkenFeather",
            "dark_range": "darkenDarkRange",
            "neutrality_sensitivity": "darkenNeutrality",
            "expand_contract": "darkenExpandContract",
            "auto_from_edges": "darkenAutoEdges",
            "brush_radius": "darkenBrushRadius",
        }
        ui_prop = prop_map.get(key)
        if ui_prop and hasattr(self.ui_state, ui_prop):
            setattr(self.ui_state, ui_prop, value)

        self.image_editor._edits_rev += 1
        self._kick_preview_worker()
        self._update_darken_overlay()

    @Slot(str)
    def set_darken_mode(self, mode: str):
        """Set the darkening mode."""
        ds = self.image_editor.current_edits.get("darken_settings")
        if ds is None:
            return
        ds.mode = mode
        self.ui_state.darkenMode = mode
        self.image_editor._edits_rev += 1
        self._kick_preview_worker()
        self._update_darken_overlay()

    @Slot(bool)
    def set_darken_overlay_visible(self, visible: bool):
        """Toggle mask overlay visibility."""
        self.ui_state.darkenOverlayVisible = visible

    @Slot(int, int, int)
    def set_darken_overlay_color(self, r: int, g: int, b: int):
        """Set the overlay colour."""
        mask_data = self.image_editor._mask_assets.get("darken")
        if mask_data is not None:
            mask_data.overlay_color = (r, g, b)
        self._update_darken_overlay()

    def _update_darken_overlay(self):
        """Generate the mask overlay QImage for display in QML."""
        try:
            from PySide6.QtGui import QImage

            mask_data = self.image_editor._mask_assets.get("darken")
            ds = self.image_editor.current_edits.get("darken_settings")
            if mask_data is None or ds is None or not mask_data.has_strokes():
                self.ui_state._darken_overlay_image = None
                self.ui_state._darken_overlay_generation += 1
                self.ui_state.darken_overlay_generation_changed.emit()
                return

            # Resolve mask at preview resolution
            preview = self.image_editor.float_preview
            if preview is None:
                return

            from faststack.imaging.mask_engine import resolve_mask

            edits = dict(self.image_editor.current_edits)
            resolved = resolve_mask(
                mask_data,
                ds,
                preview,
                preview.shape[:2],
                edits,
                cache=self.image_editor._mask_raster_cache,
            )

            # Build ARGB32 overlay
            h, w = resolved.shape
            r, g, b = mask_data.overlay_color
            alpha = int(mask_data.overlay_opacity * 255)

            # Create ARGB buffer: (H, W, 4) uint8
            overlay = np.zeros((h, w, 4), dtype=np.uint8)
            mask_u8 = (np.clip(resolved, 0.0, 1.0) * alpha).astype(np.uint8)
            overlay[:, :, 0] = b  # QImage ARGB32 is BGRA in memory on little-endian
            overlay[:, :, 1] = g
            overlay[:, :, 2] = r
            overlay[:, :, 3] = mask_u8

            buf = overlay.tobytes()
            self._darken_overlay_buffer = buf
            qimg = QImage(buf, w, h, w * 4, QImage.Format.Format_ARGB32)

            self.ui_state._darken_overlay_image = qimg
            self.ui_state._darken_overlay_generation += 1
            self.ui_state.darken_overlay_generation_changed.emit()

        except Exception:
            log.exception("Failed to update darken overlay")

    @Slot()
    def rotate_image_cw(self):
        """Rotate the edited image 90 degrees clockwise."""
        current = self.image_editor.current_edits.get("rotation", 0)
        new_rotation = (current - 90) % 360
        self.set_edit_parameter("rotation", new_rotation)
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def rotate_image_ccw(self):
        """Rotate the edited image 90 degrees counter-clockwise."""
        current = self.image_editor.current_edits.get("rotation", 0)
        new_rotation = (current + 90) % 360
        self.set_edit_parameter("rotation", new_rotation)
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

    @Slot()
    def toggle_histogram(self):
        """Toggle histogram window visibility."""
        self.ui_state.isHistogramVisible = not self.ui_state.isHistogramVisible
        if self.ui_state.isHistogramVisible:
            self.update_histogram()
            log.info("Histogram window opened")
        else:
            log.info("Histogram window closed")

    @Slot()
    @Slot(float, float, float, float)  # zoom, panX, panY, imageScale
    def update_histogram(
        self,
        zoom: float = 1.0,
        pan_x: float = 0.0,
        pan_y: float = 0.0,
        image_scale: float = 1.0,
    ):
        """Throttled request to update histogram. Updates continuously but capped at interval.

        Args:
            zoom: Zoom scale factor (1.0 = no zoom)
            pan_x: Pan offset in X direction (in image coordinates)
            pan_y: Pan offset in Y direction (in image coordinates)
            image_scale: Scale factor of displayed image vs original
        """
        # Early guard: don't even schedule if nothing is showing the histogram
        if not (self.ui_state.isHistogramVisible or self.ui_state.isEditorOpen):
            with self._hist_lock:
                self._hist_pending = None
            return

        with self._hist_lock:
            self._hist_pending = (zoom, pan_x, pan_y, image_scale)
            self._hist_null_retries = 0  # Fresh request resets retry counter
            inflight = self._hist_inflight

        if not self.histogram_timer.isActive() and not inflight:
            self.histogram_timer.start()

    def _kick_histogram_worker(self):
        if getattr(self, "_shutting_down", False):
            return

        with self._hist_lock:
            if self._hist_inflight:
                # Safety: if inflight has been stuck for >10s, force-reset it.
                # This is a defensive fallback against unforeseen edge cases
                # where the done-callback or signal delivery fails to clear it.
                if self._hist_inflight_since == 0.0:
                    return  # Job is queued but hasn't started yet
                elapsed = time.monotonic() - self._hist_inflight_since
                if elapsed < 10.0:
                    return
                # Only force-reset if the Future is actually done (callback was
                # lost).  If the worker is still running, resetting would just
                # queue a duplicate behind it on the single-worker executor.
                fut = self._hist_inflight_future
                if fut is not None and not fut.done():
                    return  # Worker is genuinely still running, not lost
                log.warning(
                    "Histogram inflight stuck for %.1fs, force-resetting", elapsed
                )
                self._hist_inflight = False
                self._hist_inflight_future = None
            if self._hist_pending is None:
                return

            args = self._hist_pending
            self._hist_pending = None
            self._hist_last_args = args  # Preserve for retry on None result

            self._hist_token += 1
            token = self._hist_token
            # Mark as inflight while holding the lock to prevent others from entering
            self._hist_inflight = True
            self._hist_inflight_token = token
            self._hist_inflight_since = 0.0  # Set when worker actually starts

        # Snap the currently known preview data to avoid racing with the editor.
        # Only use cached preview if it matches the current image to prevent stale histograms.
        preview_data = self._last_rendered_preview
        if preview_data and self._last_rendered_preview_index != self.current_index:
            preview_data = None
        if not preview_data:
            # Fallback for initial load if no edit preview yet (could use get_decoded_image?)
            # But histogram is mostly for edits. If preview_data is None, we likely can't compute anyway.
            # We can try to peek at the image editor if _last_rendered_preview is unset.
            preview_data = self.image_editor.get_preview_data_cached(
                allow_compute=False
            )

        # Fallback: If still no preview data (e.g. editor not open), we need to fetch the main image.
        # But doing get_decoded_image() here blocks the main thread.
        # Instead, we pass the index to the worker and let it fetch/decode if needed.
        target_index = -1
        if not preview_data and 0 <= self.current_index < len(self.image_files):
            target_index = self.current_index

        # If no preview data AND no valid index, we can't compute.
        if not preview_data and target_index == -1:
            # We must clear inflight if we abort, otherwise we deadlock future updates
            # Keep lock held while modifying shared state AND checking timer to prevent race
            with self._hist_lock:
                self._hist_inflight = False
                # Restore pending args so the next timer tick (or preview completion) retries
                if self._hist_pending is None:
                    self._hist_pending = args
                # Make sure timer is running to retry (check under lock to avoid race)
                should_start_timer = not self.histogram_timer.isActive()

            if should_start_timer:
                self.histogram_timer.start()
            return

        try:
            # Pass simple data + controller reference + target_index
            fut = self._hist_executor.submit(
                self._compute_histogram_worker,
                token,
                args,
                preview_data,
                self,
                target_index,
            )
            self._hist_inflight_future = fut
            fut.add_done_callback(
                functools.partial(self._on_histogram_done, submitted_token=token)
            )
        except Exception as e:
            log.error("Histogram executor failed to submit task: %s", e)
            with self._hist_lock:
                self._hist_inflight = False
                self._hist_inflight_future = None

    @staticmethod
    def _compute_histogram_worker(
        token, args, decoded, controller=None, target_index=-1
    ):
        # IMPORTANT: do not touch QObjects here except thread-safe plain data
        # Record when the worker actually starts executing (not when it was queued)
        if controller:
            with controller._hist_lock:
                controller._hist_inflight_since = time.monotonic()

        zoom, pan_x, pan_y, image_scale = args

        # If data wasn't provided, try to fetch it safely using the controller
        if not decoded and controller and target_index >= 0:
            decoded = controller._get_decoded_image_safe(target_index)

        # Use explicitly passed or fetched decoded data
        if not decoded:
            return token, None

        try:
            # Validate buffer size before reshape to prevent ValueError
            expected_size = decoded.height * decoded.width * 3
            if len(decoded.buffer) != expected_size:
                log.warning(
                    "Histogram: Buffer size mismatch. Expected %d bytes, got %d",
                    expected_size,
                    len(decoded.buffer),
                )
                return token, None

            arr = np.frombuffer(decoded.buffer, dtype=np.uint8).reshape(
                (decoded.height, decoded.width, 3)
            )

            # If zoomed in, calculate visible region and only use that portion
            if zoom > 1.1:
                visible_width = decoded.width / zoom
                visible_height = decoded.height / zoom
                center_x = decoded.width / 2
                center_y = decoded.height / 2
                pan_x_image = pan_x / image_scale if image_scale > 0 else 0
                pan_y_image = pan_y / image_scale if image_scale > 0 else 0
                visible_center_x = center_x - (pan_x_image / zoom)
                visible_center_y = center_y - (pan_y_image / zoom)

                visible_x_start = max(0, int(visible_center_x - visible_width / 2))
                visible_y_start = max(0, int(visible_center_y - visible_height / 2))
                visible_x_end = min(
                    decoded.width, int(visible_center_x + visible_width / 2)
                )
                visible_y_end = min(
                    decoded.height, int(visible_center_y + visible_height / 2)
                )

                if visible_x_end > visible_x_start and visible_y_end > visible_y_start:
                    arr = arr[
                        visible_y_start:visible_y_end, visible_x_start:visible_x_end, :
                    ]

            bins = 256
            value_range = (0, 256)

            r_hist = np.histogram(arr[:, :, 0], bins=bins, range=value_range)[0]
            g_hist = np.histogram(arr[:, :, 1], bins=bins, range=value_range)[0]
            b_hist = np.histogram(arr[:, :, 2], bins=bins, range=value_range)[0]

            r_clip_count = int(r_hist[255])
            g_clip_count = int(g_hist[255])
            b_clip_count = int(b_hist[255])

            r_preclip_count = int(np.sum(r_hist[250:255]))
            g_preclip_count = int(np.sum(g_hist[250:255]))
            b_preclip_count = int(np.sum(b_hist[250:255]))

            log_r_hist = [float(x) for x in np.log1p(r_hist)]
            log_g_hist = [float(x) for x in np.log1p(g_hist)]
            log_b_hist = [float(x) for x in np.log1p(b_hist)]

            hist = {
                "r": log_r_hist,
                "g": log_g_hist,
                "b": log_b_hist,
                "r_clip": r_clip_count,
                "g_clip": g_clip_count,
                "b_clip": b_clip_count,
                "r_preclip": r_preclip_count,
                "g_preclip": g_preclip_count,
                "b_preclip": b_preclip_count,
            }
            return token, hist
        except Exception:
            return token, None

    def _on_histogram_done(self, fut, submitted_token):
        if getattr(self, "_shutting_down", False):
            return

        try:
            token, hist = fut.result()
        except Exception:
            # Use the submitted_token so _apply_histogram_result can still
            # match this completion to the correct inflight job.
            token, hist = submitted_token, None

        # bounce back to UI thread via signal
        self.histogramReady.emit((token, hist))

    @Slot(object)
    def _apply_histogram_result(self, payload):
        if getattr(self, "_shutting_down", False):
            return

        token, hist = payload

        with self._hist_lock:
            # Only clear inflight if this result is from the current job.
            # After a force-reset in _kick_histogram_worker, a stale completion
            # must not clear inflight for a newer job that is already running.
            if token == self._hist_inflight_token:
                self._hist_inflight = False
                self._hist_inflight_future = None

            if hist is not None:
                if token == self._hist_token:
                    self.ui_state.histogramData = hist
                    self.ui_state.highlightStateChanged.emit()
                    self._hist_null_retries = 0

            # If more updates arrived while we computed, run again soon.
            # Also retry (up to 3 times) if the computation returned None
            # (image may not have been cached yet after the 50ms timer).
            # Only retry for the current token — stale completions should not
            # trigger retries or consume the retry budget.
            pending = self._hist_pending is not None
            if (
                not pending
                and hist is None
                and token == self._hist_token
                and self._hist_null_retries < 3
                and (self.ui_state.isHistogramVisible or self.ui_state.isEditorOpen)
            ):
                self._hist_null_retries += 1
                self._hist_pending = self._hist_last_args
                pending = True

        if pending:
            self.histogram_timer.start()

    def _kick_preview_worker(self):
        """Kicks off a background preview render task."""
        if getattr(self, "_shutting_down", False):
            return

        with self._preview_lock:
            if self._preview_inflight:
                self._preview_pending = True
                return

            self._preview_inflight = True
            self._preview_pending = False
            self._preview_token += 1
            token = self._preview_token

        # Submit task to dedicated preview executor
        try:
            fut = self._preview_executor.submit(
                self._render_preview_worker, token, self.image_editor
            )
            fut.add_done_callback(self._on_preview_done)
        except RuntimeError:
            log.warning("Preview executor failed (shutting down?)")
            with self._preview_lock:
                self._preview_inflight = False

    @staticmethod
    def _render_preview_worker(token, image_editor):
        # Heavy work (PIL apply_edits) happens here off-thread
        try:
            # allow_compute=True ensures we actually do the work
            decoded = image_editor.get_preview_data_cached(allow_compute=True)
            return token, decoded
        except Exception:
            log.exception("Preview render failed")
            return token, None

    def _on_preview_done(self, fut):
        if getattr(self, "_shutting_down", False):
            return

        try:
            token, decoded = fut.result()
        except Exception:
            token, decoded = None, None

        # Emit from worker thread; Qt will queue to UI thread
        self.previewReady.emit((token, decoded))

    @Slot(object)
    def _apply_preview_result(self, payload):
        if getattr(self, "_shutting_down", False):
            return

        token, decoded = payload
        should_kick = False
        should_accept = False

        with self._preview_lock:
            self._preview_inflight = False

            # Accept result only if:
            # 1. We got valid decoded data
            # 2. Token matches (not stale from an old request)
            # 3. No pending request waiting (avoid "snap back" stale frame flash)
            if (
                decoded is not None
                and token == self._preview_token
                and not self._preview_pending
            ):
                self._last_rendered_preview = decoded
                self.ui_refresh_generation += 1
                self._last_rendered_preview_index = self.current_index
                self._last_rendered_preview_gen = self.ui_refresh_generation
                should_accept = True

            # Consume pending flag atomically before scheduling
            if self._preview_pending:
                self._preview_pending = False
                should_kick = True

        # Emit outside lock to avoid holding lock during UI work
        if should_accept:
            self.ui_state.currentImageSourceChanged.emit()
            self.ui_state.highlightStateChanged.emit()
            self.update_histogram()
            # Keep mask overlay in sync with the preview whenever it changes
            if self.ui_state._is_darkening:
                self._update_darken_overlay()

        # Call directly (not via singleShot) since we're on the UI thread.
        # This prevents race where a new slider event could interleave between
        # scheduling and execution, causing a spurious extra render.
        if should_kick:
            self._kick_preview_worker()

    @Slot()
    def cancel_crop_mode(self):
        """Cancel crop mode without applying changes."""
        if self.ui_state.isCropping:
            self._reset_crop_only()
            # Notify UI and kick fresh render
            self.ui_refresh_generation += 1
            self._kick_preview_worker()
            self.update_status_message("Crop cancelled")

    @Slot()
    def toggle_crop_mode(self):
        """Toggle crop mode on/off."""
        if self.ui_state.isCropping:
            # Exiting crop mode: reuse the specialized cleanup
            self.cancel_crop_mode()
        else:
            # Entering crop mode requires a loaded image with a valid float buffer.
            if not self.image_files or not (
                0 <= self.current_index < len(self.image_files)
            ):
                self.update_status_message("No image to crop")
                return

            # Block if a save is already in progress for this image.
            current_path = self.image_files[self.current_index].path
            if self._block_if_saving(current_path):
                return

            if not self.load_image_for_editing():
                return

            # Reset to full image defaults (UI and Backend)
            self._reset_crop_only()
            # Set isCropping to True now that reset is done
            self.ui_state.isCropping = True

            self.ui_state.aspectRatioNames = [r["name"] for r in ASPECT_RATIOS]
            self.ui_state.currentAspectRatioIndex = 0

            # Ensure UI and backend are synced and kick render
            self.ui_refresh_generation += 1
            self._kick_preview_worker()
            self.update_status_message("Crop mode: Drag to select area, Enter to crop")

    @Slot()
    def stack_source_raws(self):
        """
        Finds the source RAW files for the current stacked JPG and launches Helicon Focus.
        """
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image selected.")
            return

        current_image_path = self.image_files[self.current_index].path
        filename = current_image_path.name

        # Ensure it's a stacked JPG
        if not filename.lower().endswith(" stacked.jpg"):
            self.update_status_message("Current image is not a stacked JPG.")
            return

        # Extract base name and number, e.g., "PB210633" from "20251121-PB210633 stacked.JPG"
        match = re.search(r"([A-Z]+)(\d+)\s+stacked\.JPG", filename, re.IGNORECASE)
        if not match:
            self.update_status_message("Could not parse stacked JPG filename format.")
            log.error("Could not parse stacked JPG filename: %s", filename)
            return

        base_prefix = match.group(1)  # e.g., "PB"
        base_number_str = match.group(2)  # e.g., "210633"
        base_number = int(base_number_str)

        # Determine the RAW source directory
        raw_source_dir_str = config.get("raw", "source_dir")
        if not raw_source_dir_str:
            self.update_status_message(
                "RAW source directory not configured in settings."
            )
            log.warning("RAW source directory (raw.source_dir) is not set in config.")
            return

        raw_base_dir = Path(raw_source_dir_str)
        if not raw_base_dir.is_dir():
            self.update_status_message(
                f"RAW source directory not found: {raw_base_dir}"
            )
            log.warning(
                "Configured RAW source directory does not exist: %s", raw_base_dir
            )
            return

        # Get the mirror base from config
        mirror_base_str = config.get("raw", "mirror_base")
        if not mirror_base_str:
            self.update_status_message(
                "RAW mirror base directory not configured in settings."
            )
            log.warning("RAW mirror base (raw.mirror_base) is not set in config.")
            return

        mirror_base_dir = Path(mirror_base_str)
        if not mirror_base_dir.is_dir():
            self.update_status_message(
                f"RAW mirror base directory not found: {mirror_base_dir}"
            )
            log.warning(
                "Configured RAW mirror base directory does not exist: %s",
                mirror_base_dir,
            )
            return

        # The date structure in the RAW directory mirrors the structure relative to the mirror_base
        try:
            relative_part = current_image_path.parent.relative_to(mirror_base_dir)
        except ValueError:
            self.update_status_message(
                "Current image is not in the configured mirror base directory."
            )
            log.error(
                "Could not find relative path for '%s' from base '%s'. Check 'mirror_base' config.",
                current_image_path.parent,
                mirror_base_dir,
            )
            return

        raw_search_dir = raw_base_dir / relative_part

        if not raw_search_dir.is_dir():
            self.update_status_message(
                f"RAW directory for this date not found: {raw_search_dir}"
            )
            log.warning("RAW search directory does not exist: %s", raw_search_dir)
            return

        # Find RAW files by decrementing the number
        found_raw_files: List[Path] = []
        # Start one number less than the stacked image number
        current_raw_number = base_number - 1

        # Limit to reasonable number of RAWs to avoid infinite loop or too many files
        max_raw_search = 15  # As per user request, typically between 3 and 15
        search_count = 0

        while current_raw_number >= 0 and search_count < max_raw_search:
            raw_filename_stem = (
                f"{base_prefix}{current_raw_number:06d}"  # e.g., PB210632
            )

            # Look for any of the common RAW extensions
            potential_raw_paths = []
            for ext in RAW_EXTENSIONS:
                potential_raw_paths.append(raw_search_dir / f"{raw_filename_stem}{ext}")

            found_this_number = False
            for p in potential_raw_paths:
                if p.is_file():
                    found_raw_files.append(p)
                    found_this_number = True
                    break

            if not found_this_number:
                # User specified "continue until there is a gap in the numbers"
                # If we don't find any RAW for a number, assume it's a gap and stop
                if (
                    found_raw_files
                ):  # Only break if we've found at least one file before this gap
                    break

            current_raw_number -= 1
            search_count += 1

        if not found_raw_files:
            self.update_status_message(
                f"No source RAW files found in {raw_search_dir} for {filename}."
            )
            log.info("No source RAWs found for %s in %s", filename, raw_search_dir)
            return

        # Sort the files by name to ensure Helicon Focus receives them in sequence
        found_raw_files.sort()

        self.update_status_message(
            f"Launching Helicon Focus with {len(found_raw_files)} RAWs..."
        )
        log.info(
            "Launching Helicon Focus for %s with RAWs: %s",
            filename,
            [str(p) for p in found_raw_files],
        )
        success = self._launch_helicon_with_files(found_raw_files)

        if success:
            # Mark as restacked on success
            today = datetime.now().strftime("%Y-%m-%d")
            meta = self.sidecar.get_metadata(self.image_files[self.current_index].path)
            meta.restacked = True
            meta.restacked_date = today
            self.sidecar.save()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            self.update_status_message("Helicon Focus launched successfully.")
        else:
            self.update_status_message("Failed to launch Helicon Focus.")

    @Slot()
    def execute_crop(self):
        """Execute the crop operation: crop image, save, backup, and refresh."""
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image to crop")
            return

        if not self.ui_state.isCropping:
            return

        # Capture current rotation (straighten_angle) from editor state BEFORE any reload
        # This is the single source of truth since set_straighten_angle updates it live.
        current_rotation = float(
            self.image_editor.current_edits.get("straighten_angle", 0.0)
        )

        crop_box_raw = self.ui_state.currentCropBox

        # Normalize crop_box_raw to a tuple of 4 ints
        try:
            # Handle QJSValue/QVariant wrapper if present
            if hasattr(crop_box_raw, "toVariant"):
                crop_box_raw = crop_box_raw.toVariant()

            # Convert list to tuple if needed
            if isinstance(crop_box_raw, list):
                crop_box_raw = tuple(crop_box_raw)

            if not isinstance(crop_box_raw, tuple) or len(crop_box_raw) != 4:
                raise ValueError(
                    f"Expected 4-item tuple, got {type(crop_box_raw)}: {crop_box_raw}"
                )

            # Coerce elements to int and clamp to [0, 1000]
            left, top, right, bottom = [max(0, min(1000, int(x))) for x in crop_box_raw]

            # Ensure correct order (left <= right, top <= bottom)
            crop_box_raw = (
                min(left, right),
                min(top, bottom),
                max(left, right),
                max(top, bottom),
            )

        except (ValueError, TypeError, AttributeError) as e:
            log.warning("Invalid crop box format: %s", e)
            self.update_status_message("Invalid crop selection")
            return

        if crop_box_raw == (0, 0, 1000, 1000):
            self.update_status_message("No crop area selected")
            return

        # Restoration means viewing a backup; crop should target the main image.
        # We must resolve this BEFORE potentially reloading or saving.
        save_target_path = self._get_save_target_path_for_current_view()
        is_restoring = save_target_path is not None

        # Ensure image is loaded in editor.
        # For crop, we use the CURRENTLY VIEWED file (which might be a variant).
        if self.view_override_path:
            filepath = Path(self.view_override_path)
        else:
            filepath = self.get_active_edit_path(self.current_index)

        # Robust path comparison
        editor_path = self.image_editor.current_filepath
        paths_match = False
        if editor_path:
            try:
                paths_match = Path(editor_path).resolve() == Path(filepath).resolve()
            except (OSError, ValueError):
                paths_match = str(editor_path) == str(filepath)

        if not paths_match:
            log.debug(
                f"execute_crop reloading image due to path mismatch. Editor: {editor_path}, File: {filepath}"
            )
            # get_decoded_image() honors variants/overrides.
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(
                str(filepath), cached_preview=cached_preview
            ):
                self.update_status_message("Failed to load image for cropping")
                return

        self.image_editor.set_crop_box(crop_box_raw)

        # Re-apply the captured rotation.
        # This handles cases where we reloaded the image (resetting edits) or where UI state sync was flaky.
        self.image_editor.set_edit_param("straighten_angle", current_rotation)

        # Save via ImageEditor (passing the resolved target for variant-save policy)
        try:
            save_result = self.image_editor.save_image(
                save_target_path=save_target_path
            )
        except RuntimeError as e:
            log.warning("execute_crop: Save failed: %s", e)
            self.update_status_message(f"Failed to save cropped image: {e}")
            return
        except Exception as e:
            log.exception("execute_crop: Unexpected error during save: %s", e)
            self.update_status_message("Failed to save cropped image")
            return

        if save_result:
            saved_path, backup_path = save_result
            metadata_path = self.image_files[self.current_index].path
            metadata_before = self._mark_image_edited_in_sidecar(
                self.sidecar, metadata_path
            )

            # IF we were restoring from a variant, clear the override now that it's "the truth"
            if is_restoring:
                self._clear_variant_override()

            timestamp = time.time()
            self.undo_history.append(
                (
                    "crop",
                    self._build_edit_undo_data(
                        saved_path,
                        backup_path,
                        metadata_path=metadata_path,
                        metadata_before=metadata_before,
                        sidecar=self.sidecar,
                    ),
                    timestamp,
                )
            )

            # Exit crop mode
            self.ui_state.isCropping = False
            self.ui_state.currentCropBox = (0, 0, 1000, 1000)

            # Refresh the view
            self.refresh_image_list()

            # Find the edited image
            for i, img_file in enumerate(self.image_files):
                if img_file.path == saved_path:
                    self.current_index = i
                    break

            # Invalidate cache and refresh display
            self._bump_display_generation()
            self.image_cache.pop_path(saved_path)
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            # Reset zoom/pan
            self.ui_state.resetZoomPan()

            if self.ui_state.isHistogramVisible:
                self.update_histogram()

            self.update_status_message("Image cropped and saved")
            log.info("Crop operation completed for %s", saved_path)

            # Force reload of editor to ensure subsequent edits operate on the cropped image
            self.image_editor.clear()
            self.reset_edit_parameters()

        else:
            self.update_status_message("Failed to save cropped image")

    @Slot()
    def auto_levels(self):
        """Calculates and applies auto levels (preview only). Returns False if skipped."""
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image to adjust")
            return False

        t_al_start = time.perf_counter()

        image_file = self.image_files[self.current_index]
        filepath = str(image_file.path)

        # Ensure image is loaded in editor
        if (
            not self.image_editor.current_filepath
            or str(self.image_editor.current_filepath) != filepath
        ):
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(
                filepath, cached_preview=cached_preview
            ):
                self.update_status_message("Failed to load image")
                return False
        t_al_load = time.perf_counter()

        # Calculate auto levels - now returns (blacks, whites, p_low, p_high)
        blacks, whites, p_low, p_high = self.image_editor.auto_levels(
            self.auto_level_threshold
        )
        t_al_calc = time.perf_counter()

        # Auto-strength computation using stretch-factor capping
        #
        # Philosophy: threshold_percent defines acceptable clipping (e.g., 0.1% at each end).
        # Auto-strength should NOT prevent that clipping - it's intentional.
        # Instead, auto-strength prevents INSANE levels on low-dynamic-range images.
        #
        # Approach: Cap the stretch factor to a reasonable maximum (e.g., 3-4x).
        # - Full strength: stretch = 255 / (p_high - p_low)
        # - If stretch is reasonable (<= cap), use full strength
        # - If stretch is extreme (> cap), blend to limit effective stretch to cap
        #
        if self.auto_level_strength_auto:
            # Calculate full-strength stretch factor
            dynamic_range = p_high - p_low
            if dynamic_range < 1.0:
                # Degenerate case: nearly flat image
                strength = 0.0
                log.debug(
                    f"Auto levels: degenerate dynamic range ({dynamic_range:.2f}), strength=0"
                )
            else:
                stretch_full = 255.0 / dynamic_range

                # Cap stretch to prevent insane levels
                # E.g., if image spans only 50-200 (range=150), full stretch would be 255/150 = 1.7x (fine)
                # But if image spans 100-110 (range=10), full stretch would be 255/10 = 25.5x (insane!)
                STRETCH_CAP = 4.0  # Maximum allowed stretch factor

                if stretch_full <= STRETCH_CAP:
                    # Reasonable stretch, use full strength
                    strength = 1.0
                else:
                    # Excessive stretch - blend to cap it
                    # effective_stretch = 1 + strength * (stretch_full - 1) = STRETCH_CAP
                    # solving for strength: strength = (STRETCH_CAP - 1) / (stretch_full - 1)
                    strength = (STRETCH_CAP - 1.0) / (stretch_full - 1.0)
                    strength = max(0.0, min(1.0, strength))

                log.debug(
                    f"Auto levels: p_low={p_low:.1f}, p_high={p_high:.1f}, "
                    f"range={dynamic_range:.1f}, stretch_full={stretch_full:.2f}, strength={strength:.3f}"
                )
        else:
            strength = self.auto_level_strength

        # Apply strength scaling to blacks and whites parameters
        blacks *= strength
        whites *= strength

        # Detect no-op before applying: flat image or already full range
        dynamic_range = p_high - p_low
        if dynamic_range < 1.0:
            msg = "Auto levels: no change (flat image)"
            self.update_status_message(f"{msg} (preview only)", timeout=9000)
            self._last_auto_levels_msg = msg
            return False
        if p_low <= 0 and p_high >= 255:
            msg = "Auto levels: no change (already full range)"
            self.update_status_message(f"{msg} (preview only)", timeout=9000)
            self._last_auto_levels_msg = msg
            return False

        # Apply scaled values
        self.image_editor.set_edit_param("blacks", blacks)
        self.image_editor.set_edit_param("whites", whites)

        # Update UI state
        self.ui_state.blacks = blacks
        self.ui_state.whites = whites

        # Trigger preview update
        self.ui_state.currentImageSourceChanged.emit()

        if self.ui_state.isHistogramVisible:
            self.update_histogram()

        # Build detail message
        if p_high >= 255.0:
            msg = (
                f"Auto levels: highlights clipped; shadows only (blacks {blacks:+.1f})"
            )
        elif p_low <= 0.0:
            msg = (
                f"Auto levels: shadows clipped; highlights only (whites {whites:+.1f})"
            )
        else:
            gain = 255.0 / dynamic_range
            msg = (
                f"Auto levels: blacks {blacks:+.1f}, whites {whites:+.1f} "
                f"(range {p_low:.0f}\u2013{p_high:.0f}, gain {gain:.2f})"
            )

        self._kick_preview_worker()

        self.update_status_message(f"{msg} (preview only)", timeout=9000)
        log.info(
            "Auto levels preview applied to %s (clip %.2f%%, str %.2f). Msg: %s",
            filepath,
            self.auto_level_threshold,
            strength,
            msg,
        )
        t_al_end = time.perf_counter()
        log.debug(
            "[AUTO_LEVEL] load=%dms calc=%dms apply+ui=%dms total=%dms  %s",
            int((t_al_load - t_al_start) * 1000),
            int((t_al_calc - t_al_load) * 1000),
            int((t_al_end - t_al_calc) * 1000),
            int((t_al_end - t_al_start) * 1000),
            filepath,
        )
        # Store detail message for quick_auto_levels to pick up
        self._last_auto_levels_msg = msg
        return True

    @Slot()
    def quick_auto_levels(self):
        """Applies auto levels and immediately saves (with undo)."""
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        t_start = time.perf_counter()

        # Pre-load with preview_only for uint8 fast path (skips float32 conversion)
        image_file = self.image_files[self.current_index]
        filepath = str(image_file.path)
        if (
            not self.image_editor.current_filepath
            or str(self.image_editor.current_filepath) != filepath
        ):
            cached_preview = self.get_decoded_image(self.current_index)
            self.image_editor.load_image(
                filepath, cached_preview=cached_preview, preview_only=True
            )

        # Apply the preview first (loads image + sets params)
        self._last_auto_levels_msg = ""
        applied = self.auto_levels()
        t_compute = time.perf_counter()

        # If in auto mode and no changes were made (skipped), don't save
        if self.auto_level_strength_auto and not applied:
            # Status message already set by auto_levels ("No changes made...")
            return

        try:
            # Determine save_target_path for variant saves (Policy A)
            save_target_path = self._get_save_target_path_for_current_view()

            # Try uint8 fast path first, fall back to regular save
            save_result = self.image_editor.save_image_uint8_levels(
                save_target_path=save_target_path
            )
            if save_result is None:
                save_result = self.image_editor.save_image(
                    save_target_path=save_target_path
                )
        except RuntimeError as e:
            log.warning("quick_auto_levels: Save failed: %s", e)
            self.update_status_message(f"Failed to save image: {e}")
            return
        except Exception as e:
            log.exception("quick_auto_levels: Unexpected error during save: %s", e)
            self.update_status_message("Failed to save image")
            return
        t_save = time.perf_counter()

        if save_result:
            saved_path, backup_path = save_result
            metadata_path = self.image_files[self.current_index].path
            metadata_before = self._mark_image_edited_in_sidecar(
                self.sidecar, metadata_path
            )
            timestamp = time.time()
            self.undo_history.append(
                (
                    "auto_levels",
                    self._build_edit_undo_data(
                        saved_path,
                        backup_path,
                        metadata_path=metadata_path,
                        metadata_before=metadata_before,
                        sidecar=self.sidecar,
                    ),
                    timestamp,
                )
            )

            # 2. Update list and model to pick up changes
            self.refresh_image_list()

            # Force reload to ensure disk consistency
            self.image_editor.clear()

            # Re-derive current_index (backup is excluded from visible list)
            self._reindex_after_save(saved_path)
            t_list = time.perf_counter()

            self._bump_display_generation()
            self.image_cache.pop_path(saved_path)
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            if self.ui_state.isHistogramVisible:
                self.update_histogram()

            t_total = time.perf_counter()
            total_ms = int((t_total - t_start) * 1000)
            log.debug(
                "[AUTO_LEVEL] quick: compute=%dms save=%dms list=%dms total=%dms",
                int((t_compute - t_start) * 1000),
                int((t_save - t_compute) * 1000),
                int((t_list - t_save) * 1000),
                total_ms,
            )
            detail = self._last_auto_levels_msg
            saved_msg = (
                f"{detail} \u2014 saved ({total_ms} ms)"
                if detail
                else f"Auto levels applied and saved ({total_ms} ms)"
            )
            self.update_status_message(saved_msg, timeout=9000)
            log.info(
                "Quick auto levels saved for %s. New index: %d",
                saved_path,
                self.current_index,
            )
        else:
            self.update_status_message("Failed to save image")

    def _apply_auto_levels_at_index(self, index: int) -> bool:
        """Apply auto levels and save for image at the given index.

        Returns True if the image was processed and saved, False if skipped/failed.
        Does NOT update UI state or prefetcher — caller is responsible for that.
        """
        if index < 0 or index >= len(self.image_files):
            return False

        image_file = self.image_files[index]
        filepath = str(image_file.path)

        # Load image into editor
        if (
            not self.image_editor.current_filepath
            or str(self.image_editor.current_filepath) != filepath
        ):
            cached_preview = self.get_decoded_image(index)
            self.image_editor.load_image(
                filepath, cached_preview=cached_preview, preview_only=True
            )

        # Save current_index, temporarily set to target index for auto_levels()
        saved_index = self.current_index
        self.current_index = index

        try:
            self._last_auto_levels_msg = ""
            applied = self.auto_levels()

            if self.auto_level_strength_auto and not applied:
                return False

            try:
                save_target_path = self._get_save_target_path_for_current_view()
                save_result = self.image_editor.save_image_uint8_levels(
                    save_target_path=save_target_path
                )
                if save_result is None:
                    save_result = self.image_editor.save_image(
                        save_target_path=save_target_path
                    )
            except Exception as e:
                log.warning("batch auto levels: save failed for %s: %s", filepath, e)
                return False

            if save_result:
                saved_path, backup_path = save_result
                timestamp = time.time()
                metadata_path = image_file.path
                metadata_before = self._mark_image_edited_in_sidecar(
                    self.sidecar, metadata_path
                )

                self.undo_history.append(
                    (
                        "auto_levels",
                        self._build_edit_undo_data(
                            saved_path,
                            backup_path,
                            metadata_path=metadata_path,
                            metadata_before=metadata_before,
                            sidecar=self.sidecar,
                        ),
                        timestamp,
                    )
                )
                self.image_editor.clear()
                self.image_cache.pop_path(saved_path)
                return True

            return False
        finally:
            self.current_index = saved_index

    # --- Batch Auto Levels ---

    batchAutoLevelsProgress = Signal(int, int)  # (current, total)
    batchAutoLevelsFinished = Signal(int, int)  # (processed, total)

    def batch_auto_levels(self):
        """Auto-level every image in the current batch, one at a time via event loop."""
        batch_indices = sorted(self._get_batch_indices())
        if not batch_indices:
            self.update_status_message("No images in batch.")
            return

        self._batch_al_indices = batch_indices
        self._batch_al_pos = 0
        self._batch_al_processed = 0
        self._batch_al_cancelled = False
        self._batch_al_t_start = time.perf_counter()

        self.dialog_opened()
        self.batchAutoLevelsProgress.emit(0, len(batch_indices))
        QTimer.singleShot(0, self._batch_auto_levels_step)

    def cancel_batch_auto_levels(self):
        """Cancel an in-progress batch auto levels operation."""
        self._batch_al_cancelled = True

    def _batch_auto_levels_step(self):
        """Process one image, then schedule the next via QTimer."""
        indices = self._batch_al_indices
        total = len(indices)

        if self._batch_al_cancelled or self._batch_al_pos >= total:
            self._batch_auto_levels_done()
            return

        idx = indices[self._batch_al_pos]
        try:
            if self._apply_auto_levels_at_index(idx):
                self._batch_al_processed += 1
        except Exception as e:
            log.warning("batch auto levels: error on index %d: %s", idx, e)

        self._batch_al_pos += 1
        self.batchAutoLevelsProgress.emit(self._batch_al_pos, total)

        # Schedule next step, yielding to event loop for UI updates
        QTimer.singleShot(0, self._batch_auto_levels_step)

    def _batch_auto_levels_done(self):
        """Finish batch auto levels — refresh state and report."""
        processed = self._batch_al_processed
        total = len(self._batch_al_indices)
        cancelled = self._batch_al_cancelled
        elapsed_ms = int((time.perf_counter() - self._batch_al_t_start) * 1000)

        # Refresh display
        self._bump_display_generation()
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.sync_ui_state()
        if hasattr(self, "_thumbnail_model") and self._thumbnail_model:
            self._thumbnail_model.refresh()

        self.dialog_closed()
        self.batchAutoLevelsFinished.emit(processed, total)

        if cancelled:
            msg = f"Batch auto levels cancelled: {processed}/{total} processed ({elapsed_ms} ms)"
        else:
            msg = f"Batch auto levels complete: {processed}/{total} processed ({elapsed_ms} ms)"
        self.update_status_message(msg)
        log.info(msg)

        # Cleanup
        del self._batch_al_indices
        del self._batch_al_pos
        del self._batch_al_processed
        del self._batch_al_cancelled
        del self._batch_al_t_start

    @Slot()
    def quick_auto_white_balance(self):
        """Quickly apply auto white balance, save the image, and track for undo."""
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        t_start = time.perf_counter()

        image_file = self.image_files[self.current_index]
        if self.view_override_path:
            active_path = Path(self.view_override_path)
        else:
            active_path = self.get_active_edit_path(self.current_index)
        filepath = str(active_path)

        # Ensure image is loaded in editor (skip if already loaded)
        editor_path = self.image_editor.current_filepath
        paths_match = False
        if editor_path:
            try:
                paths_match = Path(editor_path).resolve() == active_path.resolve()
            except (OSError, ValueError):
                paths_match = str(editor_path) == filepath

        if not paths_match:
            cached_preview = self.get_decoded_image(self.current_index)
            if not self.image_editor.load_image(
                filepath, cached_preview=cached_preview, preview_only=True
            ):
                self.update_status_message("Failed to load image")
                return
        t_load = time.perf_counter()

        # Calculate and apply auto white balance
        # Returns detail string if applied, None if no change
        detail_msg = self.auto_white_balance()
        t_compute = time.perf_counter()

        # If no correction was needed, skip saving
        if not detail_msg:
            # Status message already set by auto_white_balance()
            return

        # Save the edited image (this creates a backup automatically)
        try:
            save_target_path = self._get_save_target_path_for_current_view()
            save_result = self.image_editor.save_image_uint8_white_balance(
                save_target_path=save_target_path
            )
            if save_result is None:
                save_result = self.image_editor.save_image(
                    save_target_path=save_target_path
                )
        except RuntimeError as e:
            log.warning("quick_auto_white_balance: Save failed: %s", e)
            self.update_status_message(f"Failed to save image: {e}")
            return
        except Exception as e:
            log.exception(
                "quick_auto_white_balance: Unexpected error during save: %s", e
            )
            self.update_status_message("Failed to save image")
            return
        t_save = time.perf_counter()

        if save_result:
            saved_path, backup_path = save_result
            timestamp = time.time()
            metadata_path = self.image_files[self.current_index].path
            metadata_before = self._mark_image_edited_in_sidecar(
                self.sidecar, metadata_path
            )

            # 2. Update list and model to pick up changes
            self.refresh_image_list()

            # Re-derive current_index
            self._reindex_after_save(saved_path)

            self.undo_history.append(
                (
                    "auto_white_balance",
                    self._build_edit_undo_data(
                        saved_path,
                        backup_path,
                        metadata_path=metadata_path,
                        metadata_before=metadata_before,
                        sidecar=self.sidecar,
                    ),
                    timestamp,
                )
            )

            # Force the image editor to clear its current state so it reloads fresh
            self.image_editor.clear()

            t_list = time.perf_counter()

            # Invalidate cache for the edited image so it's reloaded from disk
            self._bump_display_generation()
            self.image_cache.pop_path(saved_path)
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()

            # Update histogram if visible
            if self.ui_state.isHistogramVisible:
                self.update_histogram()

            t_total = time.perf_counter()
            total_ms = int((t_total - t_start) * 1000)
            log.debug(
                "[AUTO_COLOR] quick: load=%dms compute=%dms save=%dms list=%dms total=%dms",
                int((t_load - t_start) * 1000),
                int((t_compute - t_load) * 1000),
                int((t_save - t_compute) * 1000),
                int((t_list - t_save) * 1000),
                total_ms,
            )
            self.update_status_message(f"{detail_msg} \u2014 saved ({total_ms} ms)")
            log.info("Quick auto white balance applied to %s", filepath)
        else:
            self.update_status_message("Failed to save image")

    @Slot()
    def auto_white_balance(self) -> Optional[str]:
        """
        Dispatcher for auto white balance. Calls the appropriate method based on
        the mode set in the config ('lab' or 'rgb').

        Returns the detail message string if a correction was applied, or None
        if no change / error.
        """
        mode = config.get("awb", "mode", fallback="lab")
        if mode == "lab":
            return self.auto_white_balance_lab()
        elif mode == "rgb":
            return self.auto_white_balance_legacy()
        else:
            log.error("Unknown AWB mode: %s", mode)
            self.update_status_message(f"Error: Unknown AWB mode '{mode}'")
            return None

    def auto_white_balance_legacy(self) -> Optional[str]:
        """
        Calculates and applies auto white balance using the legacy grey world
        assumption on the entire RGB image.

        Returns the detail message string if a correction was applied, or None.
        """
        if not self.image_editor.original_image:
            log.warning("No image loaded in editor for auto white balance")
            return None

        log.info("Applying legacy (RGB Grey World) Auto White Balance")
        t_awb_start = time.perf_counter()

        img = self.image_editor.original_image
        arr = np.array(img, dtype=np.float32)

        r_mean = arr[:, :, 0].mean()
        g_mean = arr[:, :, 1].mean()
        b_mean = arr[:, :, 2].mean()

        grey_target = (r_mean + g_mean + b_mean) / 3.0

        r_diff = r_mean - grey_target
        g_diff = g_mean - grey_target

        by_shift = -(r_diff + g_diff) / 2.0
        mg_shift = -(r_diff - g_diff) / 2.0

        by_value = by_shift / 63.75
        mg_value = mg_shift / 63.75

        by_value = float(np.clip(by_value, -1.0, 1.0))
        mg_value = float(np.clip(mg_value, -1.0, 1.0))

        # No-change detection
        if abs(by_value) < _AWB_NOOP_EPS and abs(mg_value) < _AWB_NOOP_EPS:
            self.update_status_message("AWB: no correction needed (already neutral)")
            return None

        self.image_editor.set_edit_param("white_balance_by", by_value)
        self.image_editor.set_edit_param("white_balance_mg", mg_value)

        self.ui_state.white_balance_by = by_value
        self.ui_state.white_balance_mg = mg_value

        self._kick_preview_worker()

        by_dir = _awb_direction(by_value, "warming", "cooling")
        mg_dir = _awb_direction(mg_value, "magenta", "greener")
        msg = f"AWB (Legacy): B/Y {by_value:+.2f} ({by_dir}), M/G {mg_value:+.2f} ({mg_dir})"
        t_awb_end = time.perf_counter()
        log.debug(
            "[AUTO_COLOR] legacy: total=%dms",
            int((t_awb_end - t_awb_start) * 1000),
        )
        self.update_status_message(msg)
        return msg

    def auto_white_balance_lab(self) -> Optional[str]:
        """
        Calculates and applies auto white balance using a robust preview-sized
        neutral-pixel estimate aligned to the editor's slider model.

        Returns the detail message string if a correction was applied, or None.
        """
        if not self.image_editor.original_image:
            log.warning("No image loaded in editor for auto white balance")
            return None

        t_awb_start = time.perf_counter()
        strength = config.getfloat("awb", "strength", 0.7)
        warm_bias = config.getint("awb", "warm_bias", 6)
        tint_bias = config.getint("awb", "tint_bias", 0)
        rgb_lower_bound = config.getint("awb", "rgb_lower_bound", 5)
        rgb_upper_bound = config.getint("awb", "rgb_upper_bound", 250)
        luma_lower_bound = config.getint("awb", "luma_lower_bound", 30)
        luma_upper_bound = config.getint("awb", "luma_upper_bound", 220)

        estimate = self.image_editor.estimate_auto_white_balance(
            strength=strength,
            warm_bias=warm_bias,
            tint_bias=tint_bias,
            rgb_lower_bound=rgb_lower_bound,
            rgb_upper_bound=rgb_upper_bound,
            luma_lower_bound=luma_lower_bound,
            luma_upper_bound=luma_upper_bound,
        )
        if not estimate:
            self.update_status_message("AWB failed: no valid neutral pixels found")
            return None

        by_value = estimate["by_value"]
        mg_value = estimate["mg_value"]

        # No-change detection — see _AWB_NOOP_EPS definition for rationale
        if abs(by_value) < _AWB_NOOP_EPS and abs(mg_value) < _AWB_NOOP_EPS:
            self.update_status_message("AWB: no correction needed (already neutral)")
            return None

        self.image_editor.set_edit_param("white_balance_by", by_value)
        self.image_editor.set_edit_param("white_balance_mg", mg_value)

        self.ui_state.white_balance_by = by_value
        self.ui_state.white_balance_mg = mg_value

        self._kick_preview_worker()

        by_dir = _awb_direction(by_value, "warming", "cooling")
        mg_dir = _awb_direction(mg_value, "magenta", "greener")
        msg = (
            f"AWB: B/Y {by_value:+.2f} ({by_dir}), M/G {mg_value:+.2f} ({mg_dir})"
            f" \u2014 neutral RGB {estimate['r_mean']:.3f}/"
            f"{estimate['g_mean']:.3f}/{estimate['b_mean']:.3f}"
        )
        t_awb_end = time.perf_counter()
        selected_pixels = int(estimate.get("selected_pixels", 0))
        stride = int(estimate.get("stride", 0))
        neutrality_limit = float(estimate.get("neutrality_limit", 0.0))
        log.debug(
            "[AUTO_COLOR] total=%dms  (selected=%d stride=%d neutral<=%.3f)",
            int((t_awb_end - t_awb_start) * 1000),
            selected_pixels,
            stride,
            neutrality_limit,
        )
        self.update_status_message(msg)
        return msg

    def _get_stack_info(self, index: int) -> str:
        info = ""
        for i, (start, end) in enumerate(self.stacks):
            if start <= index <= end:
                count_in_stack = end - start + 1
                pos_in_stack = index - start + 1
                info = f"Stack {i + 1} ({pos_in_stack}/{count_in_stack})"
                break
        if (
            not info
            and self.stack_start_index is not None
            and self.stack_start_index == index
        ):
            info = "Stack Start Marked"
        log.debug("_get_stack_info for index %d: %s", index, info)
        return info

    def _get_batch_info(self, index: int) -> str:
        """Get batch info for the given index."""
        info = ""
        # Check if current image is in any batch
        in_batch = False
        for start, end in self.batches:
            if start <= index <= end:
                in_batch = True
                break

        if in_batch:
            # Calculate total count across all batches
            total_count = sum(end - start + 1 for start, end in self.batches)
            info = f"{total_count} in Batch"
        elif self.batch_start_index is not None and self.batch_start_index == index:
            info = "Batch Start Marked"

        log.debug("_get_batch_info for index %d: %s", index, info)
        return info

    def get_stack_summary(self) -> str:
        if not self.stacks:
            return "No stacks defined."
        summary = []
        for i, (start, end) in enumerate(self.stacks):
            summary.append(f"Stack {i + 1}: {start}-{end}")
        return "; ".join(summary)

    def is_stacked(self) -> bool:
        if not self.image_files or self.current_index >= len(self.image_files):
            return False
        meta = self.sidecar.get_metadata(
            self.image_files[self.current_index].path, create=False
        )
        return meta.stacked if meta else False

    def _update_cache_stats(self):
        if self.debug_cache:
            hits = self.image_cache.hits
            misses = self.image_cache.misses
            total = hits + misses
            hit_rate = (hits / total * 100) if total > 0 else 0
            size_mb = self.image_cache.currsize / (1024 * 1024)
            self.ui_state.cacheStats = f"Cache: {hits} hits, {misses} misses ({hit_rate:.1f}%), {size_mb:.1f} MB"

    def get_recycle_bin_stats(self) -> List[Dict[str, Any]]:
        """Get stats for all tracked recycle bins.

        Returns:
            List of dicts: [{
                "path": absolute_path,
                "count": total_count,
                "jpg_count": num_jpg,
                "raw_count": num_raw,
                "other_count": num_other,
                "file_paths": [list of file names]
            }, ...]
        """
        stats = []
        active_bins = self._collect_active_bins()
        self.active_recycle_bins = active_bins

        for bin_path in active_bins:
            try:
                jpg_count = 0
                raw_count = 0
                other_count = 0
                file_names = []

                for p in bin_path.iterdir():
                    if p.is_file():
                        file_names.append(p.name)
                        ext = p.suffix.lower()
                        if ext in [".jpg", ".jpeg"]:
                            jpg_count += 1
                        elif ext in RAW_EXTENSIONS:
                            raw_count += 1
                        else:
                            other_count += 1

                if file_names:
                    stats.append(
                        {
                            "path": str(bin_path),
                            "count": len(file_names),
                            "jpg_count": jpg_count,
                            "raw_count": raw_count,
                            "other_count": other_count,
                            "file_paths": sorted(file_names),
                        }
                    )
            except OSError:
                continue

        return stats

    def cleanup_recycle_bins(self):
        """Delete all tracked recycle bins."""
        active_bins = {p for p in self.active_recycle_bins if p.exists() and p.is_dir()}

        for bin_path in active_bins:
            try:
                shutil.rmtree(bin_path)
                log.info("Cleaned up recycle bin: %s", bin_path)
            except OSError as e:
                log.error("Failed to delete recycle bin %s: %s", bin_path, e)

        self.active_recycle_bins.clear()

        # Clear stats cache since we deleted files/folders
        clear_raw_count_cache()

    # ---- regex for reversing UUID-suffixed recycle bin names ----
    # Current format uses ``._fs_`` marker: ``{stem}._fs_{8hex}{suffix}``
    _RECYCLE_FS_RE = re.compile(r"^(.+)\._fs_[0-9a-f]{8}$", re.IGNORECASE)

    @staticmethod
    def _original_name_from_recycled(recycled_path: Path) -> Optional[str]:
        """Derive the original filename from a recycled file's UUID-suffixed name.

        Current format: ``{original_stem}._fs_{8-hex-uuid}{original_suffix}``
        e.g. ``IMG_001._fs_a7c3f2e1.jpg`` → ``IMG_001.jpg``

        Only matches the ``._fs_`` marker to avoid false positives on
        legitimate filenames that happen to end with a dot-8hex segment
        (e.g. ``photo.a1b2c3d4.jpg``).

        Returns:
            The original filename, or None if the name doesn't match the pattern.
        """
        m = AppController._RECYCLE_FS_RE.match(recycled_path.stem)
        if m:
            return m.group(1) + recycled_path.suffix
        return None

    def _collect_active_bins(self) -> set:
        """Return the set of existing recycle bin directories (tracked + local).

        Excludes any bin that still has outstanding pending-delete jobs so that
        the restore UI cannot act on a bin the worker is still writing to.
        """
        # Build set of bin dirs that have in-flight delete jobs.
        pending_bins: set = set()
        for job in self._pending_delete_jobs.values():
            for img in job.images_to_delete:
                pending_bins.add(img.path.parent / "image recycle bin")

        active = {
            p
            for p in self.active_recycle_bins
            if p.exists() and p.is_dir() and p not in pending_bins
        }
        local_bin = self.image_dir / "image recycle bin"
        if local_bin.exists() and local_bin.is_dir() and local_bin not in pending_bins:
            active.add(local_bin)
        return active

    def get_per_bin_restore_info(self) -> List[Dict[str, Any]]:
        """Compute per-bin restore information with classification.

        Returns a list of dicts, one per non-empty bin, sorted deterministically
        (restorable first, then unavailable; within each bucket by dest_dir).

        Each dict contains:
            bin_id, bin_path, dest_dir, label, status,
            jpg_count, raw_count, other_count, total_restorable,
            total_files, legacy_count
        """
        results = []

        for bin_path in self._collect_active_bins():
            dest_dir = bin_path.parent
            jpg_count = 0
            raw_count = 0
            other_count = 0
            legacy_count = 0
            total_files = 0

            try:
                for p in bin_path.iterdir():
                    if not p.is_file():
                        continue
                    total_files += 1
                    original_name = self._original_name_from_recycled(p)
                    if original_name is None:
                        legacy_count += 1
                        continue
                    ext = p.suffix.lower()
                    if ext in (".jpg", ".jpeg", ".jpe"):
                        jpg_count += 1
                    elif ext in RAW_EXTENSIONS:
                        raw_count += 1
                    else:
                        other_count += 1
            except OSError:
                continue

            if total_files == 0:
                continue  # skip truly empty bins

            total_restorable = jpg_count + raw_count + other_count
            dest_dir_str = str(dest_dir)

            results.append(
                {
                    "bin_id": str(bin_path),
                    "bin_path": str(bin_path),
                    "dest_dir": dest_dir_str,
                    "label": dest_dir.name or dest_dir_str,
                    "status": "restorable" if total_restorable > 0 else "unavailable",
                    "jpg_count": jpg_count,
                    "raw_count": raw_count,
                    "other_count": other_count,
                    "total_restorable": total_restorable,
                    "total_files": total_files,
                    "legacy_count": legacy_count,
                }
            )

        # Deterministic sort: restorable first, then by dest_dir within bucket
        status_order = {"restorable": 0, "unavailable": 1}
        results.sort(key=lambda r: (status_order.get(r["status"], 9), r["dest_dir"]))
        return results

    def restore_single_bin(self, bin_path_str: str) -> Dict[str, Any]:
        """Restore all UUID-suffixed files from a single recycle bin.

        Only allows restoring from paths that are currently in the tracked
        active recycle bin set.  Refuses arbitrary paths.

        Args:
            bin_path_str: Absolute path string of the recycle bin directory.

        Returns:
            Dict with restored_count, skipped_count, legacy_remaining_count,
            dest_dir, bin_path.
        """
        bin_path = Path(bin_path_str).resolve()
        dest_dir = bin_path.parent
        dest_dir_str = str(dest_dir)
        result = {
            "restored_count": 0,
            "skipped_count": 0,
            "legacy_remaining_count": 0,
            "dest_dir": dest_dir_str,
            "bin_path": bin_path_str,
        }

        # Verify the requested path is a known active recycle bin.
        active_bins = {p.resolve() for p in self._collect_active_bins()}
        if bin_path not in active_bins:
            log.warning(
                "restore_single_bin: refusing path not in active bins: %s",
                bin_path_str,
            )
            return result

        if not bin_path.exists() or not bin_path.is_dir():
            log.warning("restore_single_bin: bin does not exist: %s", bin_path_str)
            return result

        restored_paths: Set[Path] = set()  # recycled paths successfully moved

        try:
            # Snapshot the listing so moves during iteration can't skip entries.
            entries = list(bin_path.iterdir())
        except OSError:
            log.exception("Failed to iterate recycle bin %s", bin_path)
            entries = []

        for p in entries:
            if not p.is_file():
                continue
            original_name = self._original_name_from_recycled(p)
            if original_name is None:
                result["legacy_remaining_count"] += 1
                continue
            dest = dest_dir / original_name
            if dest.exists():
                log.warning(
                    "Skipping restore of %s — %s already exists",
                    p.name,
                    dest.name,
                )
                result["skipped_count"] += 1
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dest))
                log.info("Restored %s → %s", p.name, dest.name)
                result["restored_count"] += 1
                restored_paths.add(p)
            except OSError as e:
                log.error("Failed to restore %s: %s", p.name, e)
                result["skipped_count"] += 1

        # Prune delete_history and undo_history entries whose recycled
        # paths were just restored, so Ctrl+Z doesn't try to restore
        # files that are already back in place.
        if restored_paths:
            # Resolve all restored paths so comparison works even when
            # delete_history contains relative paths (e.g. app started
            # with a relative image directory).
            resolved_restored = {p.resolve() for p in restored_paths}

            def _record_stale(record: DeleteRecord) -> bool:
                """True if any recycled path in this record was restored."""
                (_, jpg_bin), (_, raw_bin) = record
                return (
                    jpg_bin is not None and jpg_bin.resolve() in resolved_restored
                ) or (raw_bin is not None and raw_bin.resolve() in resolved_restored)

            self.delete_history = [
                r for r in self.delete_history if not _record_stale(r)
            ]
            self.undo_history = [
                (atype, adata, ts)
                for atype, adata, ts in self.undo_history
                if atype != "delete" or not _record_stale(adata)
            ]

        # Clean up empty bin directory
        try:
            remaining = list(bin_path.iterdir())
            if not remaining:
                bin_path.rmdir()
                log.info("Removed empty recycle bin: %s", bin_path)
        except OSError:
            pass

        self.active_recycle_bins = {
            p for p in self.active_recycle_bins if p.exists() and p.is_dir()
        }
        clear_raw_count_cache()
        return result


def main(
    image_dir: Optional[str] = None,
    debug: bool = False,
    debug_cache: bool = False,
    debug_thumb_timing: bool = False,
    debug_thumb_trace: bool = False,
):
    """FastStack Application Entry Point"""
    global _debug_mode, _debug_thumb_timing, _debug_thumb_trace
    _debug_mode = debug
    _debug_thumb_timing = debug_thumb_timing
    _debug_thumb_trace = debug_thumb_trace

    t0 = time.perf_counter()
    setup_logging(debug)
    if debug:
        log.info("Startup: after setup_logging: %.3fs", time.perf_counter() - t0)
    log.info("Starting FastStack")

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"
    os.environ["QML2_IMPORT_PATH"] = os.path.join(os.path.dirname(__file__), "qml")

    app = QApplication(
        sys.argv
    )  # QApplication is correct for desktop apps with widgets

    # Enable Ctrl-C to terminate the application
    import signal

    signal.signal(signal.SIGINT, lambda *args: app.quit())
    # Ensure Python's signal handler runs (Qt blocks main thread)
    timer = QTimer()
    timer.start(500)  # Check for signals every 500ms
    timer.timeout.connect(lambda: None)

    if debug:
        log.info("Startup: after QApplication: %.3fs", time.perf_counter() - t0)

    if not image_dir:
        image_dir_str = config.get("core", "default_directory")
        if not image_dir_str:
            log.warning(
                "No image directory provided and no default directory set. Opening directory selection dialog."
            )
            selected_dir = QFileDialog.getExistingDirectory(
                None, "Select Image Directory"
            )
            if not selected_dir:
                log.error("No image directory selected. Exiting.")
                sys.exit(1)
            image_dir_str = selected_dir
        image_dir_path = Path(image_dir_str)
    else:
        image_dir_path = Path(image_dir)

    if not image_dir_path.is_dir():
        print(f"\nDirectory not found: {image_dir_path}\n")
        # Show which part of the path exists to help the user spot the typo
        check = image_dir_path
        while check != check.parent:
            if check.exists():
                print(f"  Closest existing path: {check}")
                break
            check = check.parent
        print("\nUsage: faststack <directory>")
        sys.exit(1)
    app.setOrganizationName("FastStack")
    app.setOrganizationDomain("faststack.dev")
    app.setApplicationName("FastStack")

    engine = QQmlApplicationEngine()
    engine.addImportPath(os.path.join(os.path.dirname(PySide6.__file__), "qml"))
    engine.addImportPath("qrc:/qt-project.org/imports")
    engine.addImportPath(os.path.join(os.path.dirname(__file__), "qml"))
    # Add the path to Qt5Compat.GraphicalEffects to QML import paths
    engine.addImportPath(
        os.path.join(os.path.dirname(PySide6.__file__), "qml", "Qt5Compat")
    )

    controller = AppController(
        image_dir=image_dir_path,
        engine=engine,
        debug_cache=debug_cache,
        debug_thumb_timing=debug_thumb_timing,
        debug_thumb_trace=debug_thumb_trace,
    )
    if debug:
        log.info("Startup: after AppController: %.3fs", time.perf_counter() - t0)
    image_provider = ImageProvider(controller)
    engine.addImageProvider("provider", image_provider)
    # Register thumbnail provider for grid view
    engine.addImageProvider("thumbnail", controller._thumbnail_provider)

    # Expose controller and UI state to QML
    context = engine.rootContext()
    context.setContextProperty("uiState", controller.ui_state)
    context.setContextProperty("controller", controller)
    context.setContextProperty("thumbnailModel", controller._thumbnail_model)

    qml_file = Path(__file__).parent / "qml" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    if debug:
        log.info("Startup: after engine.load(QML): %.3fs", time.perf_counter() - t0)

    if not engine.rootObjects():
        log.error("Failed to load QML.")
        sys.exit(-1)

    # Connect key events from the main window
    main_window = engine.rootObjects()[0]
    controller.main_window = main_window
    main_window.installEventFilter(controller)

    # Defer heavy loading to after event loop starts so the window appears instantly.
    # controller.load() does disk scanning, image decode, and thumbnail model refresh —
    # all of which can run after the first event loop iteration.
    QTimer.singleShot(0, controller.load)
    if debug:
        log.info(
            "Startup: controller.load() deferred to event loop (%.3fs to window)",
            time.perf_counter() - t0,
        )

    # Graceful shutdown with timeout fallback
    import threading
    import faulthandler

    def _log_live_threads(tag: str):
        """Log non-daemon threads for debugging shutdown hangs."""
        threads = threading.enumerate()
        alive = [
            t
            for t in threads
            if t.is_alive() and not t.daemon and t.name != "MainThread"
        ]
        if not alive:
            return

        log.warning("%s: %d NON-DAEMON threads still alive:", tag, len(alive))
        for t in alive:
            log.warning("  - name=%r ident=%r daemon=%r", t.name, t.ident, t.daemon)

    def _shutdown_with_timeout():
        """Graceful shutdown with Python timer fallback."""
        log.info("aboutToQuit fired")

        # Backstop MUST start first, or it won't run if shutdown blocks.
        # Increased to 7s to ensure pending saves (wait=True) have time to complete.
        killer = threading.Timer(7.0, lambda: os._exit(1))
        killer.daemon = True
        killer.start()

        # After 4s, dump stacks to stderr so we can see what's hung just before the kill.
        faulthandler.dump_traceback_later(4.0, repeat=False)

        try:
            # Stop Qt timers on main thread
            try:
                timer.stop()
            except Exception:
                pass

            # Run Qt cleanup on main thread
            controller.shutdown_qt()

            # Consolidated shutdown for all thread pools and pending jobs
            # This replaces previous ad-hoc shutdown logic
            controller.shutdown_nonqt()

            _log_live_threads("after shutdown_executors")

        finally:
            faulthandler.cancel_dump_traceback_later()
            killer.cancel()  # if we got here, no need to force-kill

    app.aboutToQuit.connect(_shutdown_with_timeout)

    # Ensure closing last window actually quits the app
    app.setQuitOnLastWindowClosed(True)
    app.lastWindowClosed.connect(app.quit)

    sys.exit(app.exec())


def cli():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="FastStack - Ultra-fast JPG Viewer for Focus Stacking Selection"
    )
    parser.add_argument(
        "image_dir", nargs="?", default="", help="Directory of images to view"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging and timing information",
    )
    parser.add_argument(
        "--debugcache", action="store_true", help="Enable debug cache features"
    )
    parser.add_argument(
        "--debug-thumbtiming",
        action="store_true",
        help="Enable thumbnail pipeline timing logs (implies --debug)",
    )
    parser.add_argument(
        "--debug-thumbtrace",
        action="store_true",
        help="Enable thumbnail pipeline trace logs (implies --debug)",
    )
    args = parser.parse_args()
    if args.debug_thumbtiming or args.debug_thumbtrace:
        args.debug = True
    main(
        image_dir=args.image_dir,
        debug=args.debug,
        debug_cache=args.debugcache,
        debug_thumb_timing=args.debug_thumbtiming,
        debug_thumb_trace=args.debug_thumbtrace,
    )


if __name__ == "__main__":
    cli()
