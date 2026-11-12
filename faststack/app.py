"""Main application entry point for FastStack."""

import copy
import io
import logging
import sys
import math
import struct
import shlex
import time
import argparse
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set, NamedTuple
from datetime import date, datetime, timezone
import os
import re
import shutil
import uuid
import functools
from contextlib import contextmanager
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, replace as dataclass_replace
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
from PySide6.QtGui import (
    QCursor,
    QDesktopServices,
    QDrag,
    QGuiApplication,
    QImageReader,
    QPixmap,
)
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
    qVersion,
    QCoreApplication,  # noqa: F401 — patched by tests
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtQml import QQmlApplicationEngine
from PIL import Image

Image.MAX_IMAGE_PIXELS = 200_000_000  # 200 megapixels, enough for most photos
# ⬇️ these are the ones that went missing
from faststack.config import DEFAULT_CONFIG, config, detect_image_editor_path
from faststack.logging_setup import setup_logging
from faststack.wayland_drag import install_wayland_drag_monitor
from faststack.models import (
    ImageFile,
    DecodedImage,
    EntryMetadata,
)
from faststack.io.child_process import (
    child_environment,
    launch_with_relayed_output,
    log_tool_output,
)
from faststack.io.folder_delete import (
    delete_inspected_folder,
    inspect_folder_for_delete,
)
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
from faststack.io.session import SessionRegistry, respawn_for_directory
from faststack.io.watcher import Watcher
from faststack.io.helicon import (
    FocusStackerLaunch,
    launch_focus_stacker,
    validate_focus_stacker_path,
)
from faststack.io.focus_order import order_focus_stack_inputs
from faststack.io.arguments import parse_external_arguments
from faststack.inat.controller import INatUploadController
from faststack.inat.providers import (
    CardPhotoProvider,
    ObservationThumbProvider,
    TileImageProvider,
)
from faststack.io.executable_validator import validate_executable_path
from faststack.io.utils import normalize_path_key, resolve_configured_path
from faststack.imaging.cache import (
    ByteLRUCache,
    get_decoded_image_size,
    build_cache_key,
)
from faststack.imaging.prefetch import (
    DEFAULT_HELD_NAVIGATION_QUALITY,
    HELD_NAVIGATION_QUALITY_DIMENSIONS,
    Prefetcher,
    clear_icc_caches,
    get_icc_profile_description,
    get_icc_profile_details,
    get_monitor_profile,
    held_navigation_quality_max_dimension,
    normalize_held_navigation_quality,
)
from faststack.ui.keystrokes import Keybinder
from faststack.imaging.editor import (
    ASPECT_RATIOS,
    DEFAULT_PREVIEW_MASTER_BOX,
    EditRenderCancelled,
    ImageEditor,
    _safe_replace,
    fit_scale_in_box,
    geometry_affine,
    geometry_normalized_affine,
)
from faststack.imaging.local_adjust import (
    LOCAL_ADJUST_KEYS,
    effective_layers,
    normalize_local_layers,
    normalize_recipe,
)
from faststack.imaging.mask import STROKE_ERASE, STROKE_PAINT, MaskData, MaskStroke
from faststack.imaging.mask_engine import (
    LayerDensityCache,
    inverse_transform,
    rasterize_layer_density,
)
from faststack.imaging.metadata import get_exif_data
from faststack.imaging.optional_deps import get_cv2
from faststack.imaging.halo_fix import halo_layers, is_halo_layer
from faststack.imaging.stack_paint import (
    BASE_SPACE_EDITS,
    STACK_OUTPUT_SOURCE,
    is_stack_layer,
    load_stack_source,
    stack_input_label,
    stack_layers,
)
from faststack.resources import (
    faststack_changelog_path,
    faststack_qml_dir,
    faststack_readme_path,
    pyside_qml_dir,
    readme_from_metadata,
)
from faststack.updater import (
    GITHUB_REPOSITORY,
    check_for_update,
    get_current_version,
    is_release_url_allowed,
    normalize_version,
    same_base_version,
    should_check_for_updates,
)
from faststack.thumbnail_view import (
    DEFAULT_THUMBNAIL_CACHE_BYTES,
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
from faststack.util.win_dpi import start_dpi_watchdog
import numpy as np
from faststack.io.indexer import RAW_EXTENSIONS, JPG_EXTENSIONS
from faststack.io.deletion import (
    SourceChangedError,
    capture_file_identity,
    confirm_permanent_delete,
    confirm_batch_permanent_delete,
    permanently_delete_image_files,
    restore_without_replace,
    validate_file_identity,
)
from faststack.deletion_types import (
    DeleteJob,
    DeleteResult,
    DeleteRecord,
    DeleteWorkItem,
    DeletionErrorCodes,
    UIStateRestoration,
)


@dataclass(frozen=True)
class RawDevelopmentResult:
    """Immutable result emitted by a RAW worker across the Qt boundary."""

    operation_id: str
    source_path: Path
    tif_path: Path
    develop_key: str
    success: bool
    error: Optional[str] = None


@dataclass
class RawDevelopmentOperation:
    """Lifecycle-owned RawTherapee process and its temporary output."""

    operation_id: str
    develop_key: str
    source_path: Path
    raw_path: Path
    tif_path: Path
    tmp_tif_path: Path
    process: Optional[subprocess.Popen] = None
    worker: Optional[threading.Thread] = None
    cancel_requested: bool = False


# AWB thresholds on the -1..+1 normalized slider range.
# NOOP: skip corrections that are effectively imperceptible in the current gain model.
# LABEL: below this the direction word becomes "neutral" in the status message.
_AWB_NOOP_EPS = 0.005
_AWB_LABEL_EPS = 0.002
_AUTO_ADJUST_HIGHLIGHT_STEP = 0.14
_AUTO_ADJUST_BLACK_STEP = 0.07
_WINDOWS_SHARING_VIOLATION_ERRORS = frozenset({32, 33})
_RECYCLE_SHARING_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.4, 0.4)
_AUTO_VIBRANCE_EPS = 0.001
_AUTO_BRIGHTNESS_EPS = 0.001
# Midtone correction dead band and recovery. Images whose projected
# post-stretch subject luma already sits within +/- deadband of the target are
# left alone — a reasonable exposure should not be normalized toward one
# canonical brightness (that made "most images too bright"). Images outside
# the band are pulled only partway back to the nearest band *edge* (never to
# the center), which keeps the correction continuous at the band boundary and
# preserves intentional low-key / high-key character.
_AUTO_MIDTONE_DEADBAND = 0.10
_AUTO_MIDTONE_RECOVERY = 0.5
# Hard bounds on the midtone brightness factor. The endpoint stretch is the
# primary exposure tool (measured on ~90 finished photos it recovers a 2x
# under-exposure on its own); this correction is only a nudge on top, so the
# lift stays small. It used to allow 1.30x, which washed out dark-background
# macro shots — see the subject-luma note in editor.analyze_auto_levels.
_AUTO_MIDTONE_MAX_LIFT = 1.12
_AUTO_MIDTONE_MAX_DROP = 0.85
# Absolute ceiling on the blacks/whites endpoint stretch, applied whether or
# not auto_level_strength_auto is on.
_AUTO_LEVELS_MAX_STRETCH = 4.0
# Wall clock origin for the [STARTUP] breadcrumbs. Captured at import so the
# milestones AppController logs share one timeline with main()'s "Startup:
# after ..." lines (main() stamps its own t0 a few ms later, during argument
# parsing). Only meaningful for diagnosing multi-second gaps, which is all
# these lines are for.
_STARTUP_T0 = time.perf_counter()


def _startup_elapsed() -> float:
    """Seconds since process start, for the [STARTUP] trace."""
    return time.perf_counter() - _STARTUP_T0


_TRACE_TIMESTAMP_RE = re.compile(r"^\[\s*[\d:.]+\]\s*")


def _collapse_repeated_trace(lines) -> list[str]:
    """Fold runs of identical protocol messages into one line each.

    A drag hovering over its target gets the same ``wl_data_source.target()``
    event once per pointer frame - some 70 lines a second that differ only in
    their timestamp. The first line of a run is kept as is; when the run is
    longer its line says how many followed and when the last one arrived.
    """
    out: list[str] = []
    previous_body = None
    run = 0
    last_stamp = ""

    def close_run() -> None:
        if run > 1:
            out[-1] = f"{out[-1]}  (x{run}, last at {last_stamp or 'unknown'})"

    for line in lines:
        match = _TRACE_TIMESTAMP_RE.match(line)
        body = line[match.end() :] if match else line
        if body == previous_body:
            run += 1
            if match:
                last_stamp = match.group(0).strip().strip("[]").strip()
            continue
        close_run()
        out.append(line)
        previous_body = body
        run = 1
        last_stamp = ""
    close_run()
    return out


# Bounds on core.editor_preview_long_edge (0 = auto). The ceiling caps the
# float32 preview master's footprint: 4096x2304 is ~113MB, and pixels beyond
# the window are never displayed anyway. The floor keeps a hand-edited config
# from making the editing view unusably coarse.
EDITOR_PREVIEW_MIN_LONG_EDGE = 640
EDITOR_PREVIEW_MAX_LONG_EDGE = 4096
# Idle delay before a soft preview frame is re-rendered at display resolution.
# Long while a slider is held (a drag would otherwise start a display-resolution
# render between every two frames), short once input is discrete.
_HQ_PREVIEW_DEBOUNCE_DRAG_MS = 350
_HQ_PREVIEW_DEBOUNCE_SETTLED_MS = 110


def _awb_direction(value: float, pos_label: str, neg_label: str) -> str:
    """Return a human-readable direction label for an AWB shift value."""
    if abs(value) < _AWB_LABEL_EPS:
        return "neutral"
    return pos_label if value > 0 else neg_label


@dataclass
class ActiveAutoAdjustState:
    active_path_key: str
    session_id: Optional[str]
    base_blacks: float
    base_whites: float
    p_low: float
    p_high: float
    base_vibrance: float
    auto_vibrance_delta: float = 0.0
    base_brightness: float = 0.0
    auto_brightness_delta: float = 0.0
    extra_highlight_steps: int = 0
    extra_black_steps: int = 0


@dataclass
class LiveEditSessionState:
    active_path_key: str
    session_id: Optional[str]
    persisted_revision: int
    submitted_revision: int
    persisted_has_meaningful_edits: bool = False
    submitted_has_meaningful_edits: bool = False


class PreviewSessionProvenance(NamedTuple):
    """Canonical identity of pixels backing the active editor session."""

    source_path_key: str
    session_id: Optional[str]
    revision: int


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
_wayland_drag_monitor = None


class DragPayloadMimeData(QMimeData):
    """Mime data that reports when a drop target reads the payload.

    A read means a target is interested, not that a drop happened: Firefox
    reads ``text/uri-list`` while merely hovering. It is recorded as one half
    of the evidence that a Wayland drag succeeded, paired with the pointer
    release, because on Wayland ``QDrag.exec()`` can stay in its nested event
    loop forever when the target never sends ``wl_data_offer.finish``.
    """

    def __init__(self, on_payload_read):
        super().__init__()
        self._on_payload_read = on_payload_read

    def retrieveData(self, mime_type, preferred_type):
        data = super().retrieveData(mime_type, preferred_type)
        if self._on_payload_read is not None:
            self._on_payload_read(mime_type, data)
        return data


class DragReleaseWatcher(QObject):
    """Application filter spotting the pointer release that ends a drag.

    Qt does not surface the Wayland ``dnd_drop_performed`` event to Python, so
    the release is the best drop evidence the source has.
    """

    RELEASE_EVENTS = frozenset(
        {
            QEvent.MouseButtonRelease,
            QEvent.NonClientAreaMouseButtonRelease,
            QEvent.TabletRelease,
            QEvent.Drop,
            QEvent.UngrabMouse,
        }
    )

    def __init__(self, parent, on_release):
        super().__init__(parent)
        self._on_release = on_release

    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type in self.RELEASE_EVENTS:
            log.info("[drag] release-class event during drag: %s", event_type)
            self._on_release(event_type)
        return False


def _keyboard_repeat_delay_ms() -> int:
    """Return the OS keyboard-repeat delay, with a portable config fallback."""
    fallback = config.getint(
        "core",
        "navigation_repeat_delay_ms",
        fallback=500,
    )
    if sys.platform != "win32":
        return max(100, int(fallback))

    try:
        import ctypes

        # SPI_GETKEYBOARDDELAY returns 0..3, representing 250 ms increments.
        delay_setting = ctypes.c_uint()
        if ctypes.windll.user32.SystemParametersInfoW(
            0x0016,
            0,
            ctypes.byref(delay_setting),
            0,
        ):
            return (min(3, int(delay_setting.value)) + 1) * 250
    except (AttributeError, OSError, ValueError):
        log.debug("Could not read the Windows keyboard repeat delay", exc_info=True)
    return max(100, int(fallback))


# Global flags for debug modes - set by main()
_debug_mode = False
_debug_thumb_timing = False
_debug_thumb_trace = False

# Warn about repeated eviction/reinsertion cycles, not ordinary LRU turnover.
CACHE_THRASH_WINDOW_SECS = 2.0
CACHE_THRASH_THRESHOLD = 5
CACHE_WARNING_COOLDOWN_SECS = 300

# Grace period before cancelling a Wayland drag whose target read the payload
# but never sent wl_data_offer.finish; long enough for the transfer to drain.
WAYLAND_DRAG_CANCEL_DELAY_MS = 500

# Sort modes accepted from the sidecar and from set_sort_mode().
SUPPORTED_SORT_MODES = ("default", "filename", "date", "date_reverse")


from faststack.util.executors import create_daemon_threadpool_executor
from faststack.controller.image_editor_handoff import (
    ImageEditorHandoffMixin,
    PhotoshopHandoffState,
)
from faststack.controller.halo_removal import HaloRemovalMixin
from faststack.controller.loupe_display import LoupeDisplayMixin
from faststack.controller.navigation import NavigationMixin
from faststack.controller.save_pipeline import SavePipelineMixin


class AppController(
    LoupeDisplayMixin,
    NavigationMixin,
    SavePipelineMixin,
    ImageEditorHandoffMixin,
    HaloRemovalMixin,
    QObject,
):
    dataChanged = Signal()  # New signal for general data changes
    is_zoomed_changed = Signal(bool)  # Signal for zoom state changes
    histogramReady = Signal(object)  # Signal for off-thread histogram result
    previewReady = Signal(object)  # Signal for off-thread preview result
    originalCompareReady = Signal(object)  # Signal for held-space compare result
    dialogStateChanged = Signal(bool)  # Signal for dialog open/close state
    # Thread-safe signal for thumbnail ready (emitted from worker thread, received on GUI thread)
    _thumbnailReadySignal = Signal(str)

    MAX_FAILED_RESTORATIONS_TO_LOG = 10

    @Slot(float, float, result="QVariantMap")
    def available_screen_geometry(self, x: float, y: float) -> dict[str, int]:
        """Return one screen's usable geometry for a virtual-desktop point."""
        point = QPoint(round(x), round(y))
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        if screen is None:
            return {}
        geometry = screen.availableGeometry()
        return {
            "x": geometry.x(),
            "y": geometry.y(),
            "width": geometry.width(),
            "height": geometry.height(),
        }

    @staticmethod
    def _key(p: Optional[Path]) -> Optional[str]:
        """Normalize path for consistent comparison without slow resolve()."""
        if p is None:
            return None
        return normalize_path_key(p)

    editSourceModeChanged = Signal(str)  # Notify when JPEG/RAW mode changes
    rawDevelopmentStateChanged = Signal()  # Notify when RAW development starts/stops
    _rawDevelopmentFinished = Signal(object)
    _saveFinished = Signal(
        object
    )  # Signal for save completion (result or error from background)
    _deleteFinished = Signal(
        object
    )  # Signal for async delete completion (result dict from worker)
    _exifBriefReady = Signal(object, str)  # (cache_key, brief) from background thread
    _restackOrderReady = Signal(object)  # Restack EXIF ordering result
    _stackInputsReady = Signal(object)  # Stack paint: input JPG discovery result
    _stackSourceReady = Signal(object)  # Stack paint: decoded + registered input
    _haloResultReady = Signal(object)  # Halo removal: one layer's correction
    _haloInstallProgress = Signal(object)  # Halo removal: download progress
    _haloInstallDone = Signal(object)  # Halo removal: download finished/failed
    _stackHoverReady = Signal(object)  # Stack paint: hover preview render
    # (SidecarManager, outcome) from the background session-position write
    _sessionSidecarWriteFinished = Signal(object, str)
    _updateCheckFinished = Signal(object)  # Update check result from background thread
    _qualityDecodeFinished = Signal(object)  # Settled cover-quality decode result
    _pacedNavigationReady = Signal(object)  # Exact fast-tier target is cache-ready
    _loupeSourceDecodeReady = Signal(object)  # Held loupe source finished decoding
    _frameSwapObserved = Signal(float)  # render-thread timestamp -> GUI thread
    _cacheWarningRaised = Signal(str)  # cache workers -> GUI status message
    _preloadProgressReady = Signal(object)  # Worker counters -> GUI thread
    _indexScanReady = Signal(object)  # {"epoch", "result"} payload -> GUI thread
    _editorLoadReady = Signal(object)
    editorPreviewLoaded = Signal(int, bool)

    def __init__(
        self,
        image_dir: Path,
        engine: QQmlApplicationEngine,
        debug_cache: bool = False,
        debug_cache_trace: bool = False,
        debug_thumb_timing: bool = False,
        debug_thumb_trace: bool = False,
        start_in_loupe: bool = False,
        app_start_t: Optional[float] = None,
        restore_path: Optional[str] = None,
        restore_index: Optional[int] = None,
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
        self.originalCompareReady.connect(self._apply_original_compare_result)

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
        self._delete_identity_order: List[str] = []
        self._delete_batch_path_groups: List[List[Path]] = []
        self._delete_stack_path_groups: List[List[Path]] = []
        self._delete_batch_start_path: Optional[Path] = None
        self._delete_stack_start_path: Optional[Path] = None
        self._delete_stack_end_path: Optional[Path] = None
        self._rawDevelopmentFinished.connect(
            self._on_develop_finished,
            Qt.ConnectionType.QueuedConnection,
        )

        # Preview Offloading Setup
        self._preview_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="Preview"
        )
        self._preview_refine_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="PreviewRefine"
        )
        # EXIF Brief Offloading Setup (dedicated executor to avoid blocking histograms)
        self._exif_executor = create_daemon_threadpool_executor(
            max_workers=2, thread_name_prefix="EXIF"
        )
        self._restack_order_token = 0
        self._restackOrderReady.connect(self._on_restack_order_ready)
        # Stack paint: finding the in-camera stack's input JPGs and decoding /
        # registering them. Token-deduplicated per image (_stack_inputs_token).
        self._stack_paint_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="StackPaint"
        )
        self._stack_inputs_token = 0
        self._stack_inputs_key: Optional[str] = None
        self._stack_input_paths: dict[str, Path] = {}
        self._stack_source_loading: set[tuple[str, str]] = set()
        self._stackInputsReady.connect(self._on_stack_inputs_ready)
        self._stackSourceReady.connect(self._on_stack_source_ready)
        # Hover preview (the selected input, fully edited, shown inside the
        # brush circle): gate pattern + signature so it only re-renders when
        # the edits or the selected input change.
        self._stack_hover_sig: Optional[tuple] = None
        self._stack_hover_inflight = False
        self._stack_hover_pending = False
        self._stack_hover_buffer: Optional[bytes] = None
        self._stack_hover_timer = QTimer(self)
        self._stack_hover_timer.setSingleShot(True)
        self._stack_hover_timer.setInterval(120)
        self._stack_hover_timer.timeout.connect(self._start_stack_hover_render)
        self._stackHoverReady.connect(self._on_stack_hover_ready)
        self._editor_prewarm_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="EditPrewarm"
        )
        self._editor_load_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="EditorLoad"
        )
        self._editor_load_token = 0
        self._editor_load_future = None
        self._editorLoadReady.connect(self._on_editor_load_ready)
        self._update_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="UpdateCheck"
        )
        self._updateCheckFinished.connect(self._on_update_check_finished)
        self._qualityDecodeFinished.connect(self._on_quality_decode_finished)

        # Directory scan offloading for watcher-triggered rescans (single
        # worker: only one directory walk is ever useful in flight at once).
        # See _maybe_start_index_scan() for the throttle/coalescing that
        # bounds how often this actually runs under sustained file churn.
        self._index_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="IndexScan"
        )
        self._index_scan_inflight = False
        self._index_rescan_needed = False
        # Bumped whenever the scanned directory changes, so a walk that was
        # submitted for the old directory can be discarded on arrival.
        self._index_scan_epoch = 0
        self._last_index_scan_time: float = 0.0
        # Minimum wall-clock spacing between watcher-triggered full directory
        # scans. Caps CPU spent rescanning when something external is
        # touching many files in a row (e.g. a lingering save/export queue).
        self._index_scan_min_interval_s = 1.0
        self._indexScanReady.connect(self._on_index_scan_ready)
        self._pacedNavigationReady.connect(self._on_paced_navigation_ready)
        # Queued even when the decode already finished on this thread, so the
        # slot never runs inside UIState.currentImageSource's getter.
        self._loupeSourceDecodeReady.connect(
            self._on_loupe_source_decode_ready,
            Qt.ConnectionType.QueuedConnection,
        )
        self._preloadProgressReady.connect(self._on_preload_progress_ready)
        self._update_check_token = 0
        self._update_check_inflight = False
        # Payload of the most recent automatic check that found an update the
        # user has not skipped. Automatic checks only raise the footer banner;
        # this is what the banner's "What's New" action opens.
        self._pending_update_payload: Optional[dict] = None
        self._preview_inflight = False
        self._preview_pending = False
        self._preview_token = 0
        self._preview_refinement_inflight = False
        self._preview_refinement_cancel: Optional[threading.Event] = None
        self._editor_slider_drag_active = False
        self._last_quick_preview_kick_at = 0.0
        # Token of the last display-capped full-resolution kick; its accepted
        # final frame defines _live_preview_target_dims for quick renders.
        self._preview_full_res_token = -1
        self._live_preview_target_dims: Optional[Tuple[int, int]] = None
        self._live_preview_target_session_key: Optional[tuple[str, Optional[str]]] = (
            None
        )
        self._preview_lock = threading.Lock()
        self._last_rendered_preview = None  # Store latest valid render
        self._last_rendered_preview_session_key: Optional[tuple[str, Optional[str]]] = (
            None
        )
        self._original_compare_preview = None
        self._original_compare_session_key: Optional[tuple[str, Optional[str], int]] = (
            None
        )
        self._original_compare_index: int = -1
        self._original_compare_gen: int = -1
        self._original_compare_active: bool = False
        self._original_compare_inflight: bool = False
        self._original_compare_refine_inflight: bool = False
        # Tier and view identity of the buffer above: held-space compare
        # renders a quick (preview-master) frame first and sharpens it with a
        # display-resolution pass, so a cached frame is only reusable when it
        # was rendered for the same view. The identity is the display long-edge
        # cap (None while zoomed), which changes only on resize or zoom.
        self._original_compare_full_resolution: bool = False
        self._original_compare_render_target: Optional[int] = None
        self._editor_prewarm_future: Optional[concurrent.futures.Future] = None
        self._editor_prewarm_lock = threading.Lock()
        self._shutting_down = False  # Flag to gate async callbacks during shutdown
        self._critical_io_lock = threading.Lock()
        self._critical_io_labels: Dict[str, int] = {}
        self._refresh_scheduled = False  # Coalesce guard for deferred disk refresh
        self._opencv_warning_shown = False  # Only show OpenCV warning once per session
        # Only warn once per session that we fell back to Pillow decoding
        self._turbo_fallback_warning_shown = False
        self._last_auto_levels_msg: str = (
            ""  # Detail message from last auto_levels() call
        )
        self._status_message_token = 0
        self._active_auto_adjust_state: Optional[ActiveAutoAdjustState] = None
        self._auto_adjust_save_pending_action: Optional[str] = None
        self._auto_adjust_save_in_progress: bool = False
        self._live_edit_session_state: Optional[LiveEditSessionState] = None
        self._crop_mode_has_saved_geometry: bool = False
        self._crop_mode_saved_crop_box: Optional[Tuple[int, int, int, int]] = None
        self._crop_mode_saved_straighten_angle: float = 0.0
        self._crop_mode_saved_rotation: int = 0
        self._crop_mode_saved_path_key: Optional[str] = None
        self._crop_mode_saved_session_id: Optional[str] = None
        # target_path -> save request awaiting retry. Set when a background save
        # for a session the user has navigated away from fails permanently;
        # flushed synchronously on shutdown so unsaved edits are not lost.
        self._pending_save_recovery: Dict[str, dict] = {}
        self._latest_save_tokens: Dict[str, Any] = {}
        self._pending_edit_save_requests: Dict[str, dict] = {}
        self._last_save_prepare_error: Optional[str] = None
        self._shutdown_flush_prepared = False
        self._shutdown_final_sidecar_attempted = False

        # Deferred-init state: set to safe defaults, populated later by their methods
        self._saves_in_flight: Dict[str, int] = {}
        self._saving_keys: Dict[str, int] = {}
        # Per-request save-tracking ownership: id(request) -> entry. Released
        # exactly once, by _on_save_finished or by shutdown reconciliation.
        self._open_save_tracking: Dict[int, dict[str, Any]] = {}
        self._batch_indices_cache: set = set()
        self._batch_indices_cache_key: Optional[tuple] = None
        self.recycle_bin_dir: Optional[Path] = None
        self._preload_operation_token = 0
        self._preload_operation: Optional[dict] = None
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

        self.image_dir = Path(image_dir).expanduser().resolve()
        self._startup_restore_path = restore_path
        self._startup_restore_index = restore_index
        self.image_files: List[ImageFile] = []  # Filtered list for display
        self._all_images: List[ImageFile] = []  # Cached full list from disk
        self._path_to_index: Dict[str, int] = (
            {}
        )  # Normalized path key -> index for O(1) lookup
        self._filter_identity_snapshot: Optional[dict[str, Any]] = None
        self.current_index: int = 0
        self.ui_refresh_generation = 0
        self.main_window: Optional[QObject] = None
        self.engine = engine
        # Trace is a strict superset even when AppController is constructed
        # directly instead of through main()/cli().
        self.debug_cache_trace = bool(debug_cache_trace)
        self.debug_cache = bool(debug_cache or self.debug_cache_trace)

        # Navigation correlation ("seq"): one id minted per target-index change
        # and carried keypress -> prefetch/decode -> provider -> QML render ack,
        # so a single line reports target-index-changed -> frame-actually-rendered.
        self._nav_seq = 0
        self._current_nav_seq = 0
        self._nav_target_index: Optional[int] = None
        self._nav_target_path_key: Optional[str] = None
        # seq -> partial timing record, filled in layers (mint/worker/provider/present).
        self._nav_records: "OrderedDict[int, dict]" = OrderedDict()
        self._nav_trace_lock = threading.Lock()
        # Startup milestones fire once each.
        self._app_start_t = (
            app_start_t if app_start_t is not None else time.perf_counter()
        )
        self._nav_first_fast_logged = False
        self._nav_first_settled_logged = False
        # Burst summary: stats accumulated across a run of navigations and
        # emitted once the user stops (settle timer). None when no burst active.
        self._burst: Optional[dict] = None
        self._nav_settle_timer: Optional[QTimer] = None
        self._nav_summary_deadline_timer: Optional[QTimer] = None
        # Ready image requests awaiting scene-graph synchronization and then a
        # later frame swap: (seq, UI generation, ready time, sync time). Merely
        # seeing Image.Ready before a swap is insufficient: that swap may belong
        # to a frame whose scene graph was synchronized before Ready fired.
        self._nav_pending_present: list[tuple[int, int, float, Optional[float]]] = []
        # Set once the root window's frameSwapped is connected; until then the
        # Ready callback emits at ready time (labeled ready=, not presented=).
        self._frame_swapped_connected = False
        self._frame_sync_connected = False
        self._frameSwapObserved.connect(
            self._on_frame_swapped,
            Qt.ConnectionType.QueuedConnection,
        )
        self._held_navigation_direction = 0
        self._navigation_hold_source: Optional[QObject] = None
        self._auxiliary_navigation_windows: Dict[int, QObject] = {}
        self._last_navigation_direction = 1
        self._paced_navigation_epoch = 0
        self._paced_navigation_pending: Optional[dict] = None
        # Non-blocking loupe source: the provider URL QML is currently bound
        # to, the background decode it is waiting on, and a target whose
        # background decode failed (handed to the provider as before).
        self._published_loupe_source = ""
        # True while QML is being told the loupe replaced the grid; see
        # _set_grid_view_active and _resolve_loupe_image_source.
        self._entering_loupe = False
        self._published_loupe_source_dir: Optional[Path] = None
        self._loupe_source_pending: Optional[dict] = None
        self._loupe_source_failed_target: Optional[str] = None
        self._loupe_source_attempts: tuple[str, int] = ("", 0)
        self._paced_navigation_queue: deque[int] = deque()
        self._paced_present_gate: Optional[dict] = None
        self._paced_present_ready = False
        self._navigation_repeat_started = False
        self._navigation_underflow_count = 0

        # Shutdown is handled in main() via aboutToQuit connection

        self.display_width = 0
        self.display_height = 0
        self.display_generation = 0
        # Generations are allocated from this monotonic counter so a value is
        # never handed out twice, even though display_generation itself can
        # return to an earlier value when a zoom toggle is reversed (see
        # set_zoomed). (unzoomed, zoomed) generations of the current zoom pair.
        self._display_generation_counter = 0
        self._zoom_generation_pair: Optional[tuple[int, int]] = None
        # Fit-to-window sizes used recently, each with the generation its
        # frames live under, oldest first. A window that returns to a size it
        # had a moment ago (a panel toggled, a tiling window manager making
        # room for another window and taking it back) gets those frames back
        # instead of decoding the whole prefetch window again. Emptied by
        # _bump_display_generation(), which retires every frame.
        self._display_size_generations: dict[tuple[int, int], int] = {}
        self._display_lock = threading.Lock()
        self._is_decoding = False
        self._quality_decode_token = 0

        # Cache Warning State
        self._last_cache_warning_time = 0
        self._last_cache_stats_update = 0.0
        self._eviction_lock = threading.Lock()
        self._eviction_events: deque[tuple[float, str]] = deque()
        self._eviction_counts: Counter[str] = Counter()
        self._cacheWarningRaised.connect(
            self.update_status_message, Qt.ConnectionType.QueuedConnection
        )
        self.display_ready = False  # Track if display size has been reported
        self.pending_prefetch_index: Optional[int] = None  # Deferred prefetch index

        # Edit Source Mode State
        # "jpeg" (default) or "raw"
        self.current_edit_source_mode: str = "jpeg"
        self._raw_developing_keys: Set[str] = set()
        self._raw_develop_lock = threading.Lock()
        self._raw_development_operations: Dict[str, RawDevelopmentOperation] = {}

        # -- Backend Components --
        self.watcher = Watcher(self.image_dir, self._request_watcher_refresh)
        self._suppressed_paths: Dict[str, float] = {}  # key -> monotonic expiry time
        # Normalized key -> {"identity": FileIdentity, "owners": set[job_id]}.
        # Ownership is released only when a delete job settles; elapsed time
        # cannot safely bound filesystem work. Identity awareness means an
        # external replacement at the same path still reaches the watcher.
        self._pending_delete_identities: Dict[str, dict] = {}
        # Normalized key -> in-flight save count. A timed suppression window
        # cannot bound a save of unknown duration, so writes we are performing
        # ourselves are suppressed for exactly as long as they are running.
        self._saving_path_keys: Dict[str, int] = {}
        self._suppressed_paths_lock = threading.Lock()  # guards cross-thread access
        # Paths reported by the watcher since the last debounced refresh, so
        # _on_watcher_refresh can invalidate decodes per-path instead of
        # busting the entire cache via a display-generation bump.
        self._watcher_changed_paths: set = set()
        self._watcher_changed_lock = threading.Lock()
        # JPGs launched through Photoshop remain here until the watcher sees a
        # timely write that differs from the launch-time baseline. The state is
        # GUI-thread-owned; the watchdog thread only fills _watcher_changed_paths.
        self._photoshop_handoffs: Dict[str, PhotoshopHandoffState] = {}
        # Open "unsaved edits" handoff prompt, and a handoff waiting on its
        # "Save first" save (matched by save_request_id).
        self._image_editor_prompt: Optional[dict[str, Any]] = None
        self._image_editor_after_save: Optional[dict[str, Any]] = None
        # Per-path decode invalidation epochs (path key -> monotonic time).
        # _prefetch_cache_put rejects decode results that STARTED before the
        # path's epoch: their pixels were read from a file that has since
        # been replaced (e.g. by a save).
        self._decode_invalidation_epochs: Dict[str, float] = {}
        self._decode_invalidation_lock = threading.Lock()
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
        self.image_editor = ImageEditor()  # Initialize the editor
        # Nothing is loaded yet, so this only records the size; the first
        # display-size report re-applies it against the real window.
        self.image_editor.preview_master_box = self._editor_preview_master_box()
        # Dodge & burn brush state; must exist before the first navigation
        # because _reset_local_brush_on_navigation reads it. All of it is
        # per image: navigation leaves brush mode and resets the recipe.
        self._local_brush_recipe: dict[str, float] = {}
        self._local_active_layer_id: Optional[str] = None
        self._local_editing_layer_id: Optional[str] = None
        self._local_painting_layer_id: Optional[str] = None
        # Every layer the stroke in progress goes onto: one, except a stack
        # paint erase, which restores the stack on all stack layers at once.
        self._local_painting_layer_ids: tuple[str, ...] = ()
        # One entry per stroke: a layer id, or a tuple of them (multi-layer).
        self._local_stroke_history: list = []
        # GUI-thread raster cache for the mask overlay (the preview worker
        # has its own on the editor).
        self._local_overlay_cache = LayerDensityCache()
        self._local_overlay_buffer: Optional[bytes] = None
        self._dialog_open_count = 0  # Derived total of nested modal scopes.
        self._anonymous_dialog_depth = 0
        self._active_modal_tokens: set[str] = set()
        self._helicon_launches: Dict[int, FocusStackerLaunch] = {}
        self._helicon_launches_lock = threading.Lock()

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
        self._held_navigation_quality = normalize_held_navigation_quality(
            config.get(
                "core",
                "held_navigation_quality",
                fallback=DEFAULT_HELD_NAVIGATION_QUALITY,
            )
        )
        self.prefetcher = Prefetcher(
            image_files=self.image_files,
            cache_put=self._prefetch_cache_put,
            prefetch_radius=config.getint("core", "prefetch_radius", 20),
            get_display_info=self.get_display_info,
            debug=_debug_mode or self.debug_cache_trace,
            trace=self.debug_cache_trace,
            cache_contains=self.image_cache.__contains__,
            cache_get_quality=self._cache_decode_quality,
            quality_is_current=self._quality_upgrade_still_current,
            nav_trace=(self._record_nav_worker_timing if self.debug_cache else None),
            fast_decode_max_dimension=held_navigation_quality_max_dimension(
                self._held_navigation_quality
            ),
            configured_workers=config.getint(
                "core",
                "prefetch_workers",
                fallback=0,
            ),
        )
        # Foreground constraint tokens (see _enter_prefetch_constraint). Save
        # and preview refinement carry their own per-operation token on the
        # work item itself; only startup needs controller-held state.
        self._startup_prefetch_constraint_token: Optional[int] = None
        if start_in_loupe:
            # Only --loupe startup decodes a foreground frame before the user
            # does anything; grid startup would never see the Ready callback
            # that releases this, so it must not be constrained at all.
            self._startup_prefetch_constraint_token = self._enter_prefetch_constraint(
                "startup"
            )
            QTimer.singleShot(
                self._STARTUP_CONSTRAINT_FAILSAFE_MS,
                lambda: self._release_startup_prefetch_constraint("failsafe-timeout"),
            )
        self.last_displayed_image: Optional[DecodedImage] = (
            None  # Cache last image to avoid grey squares
        )
        self._last_image_lock = (
            threading.Lock()
        )  # Protect last_displayed_image from race conditions

        # -- Grid View (Thumbnail) Infrastructure --
        self._is_grid_view_active = not start_in_loupe  # Default to grid view
        self._grid_nav_history: list[Path] = (
            []
        )  # Stack of previous directories for back navigation

        # -- Optimization & Instrumentation --
        self._scan_count_variant = 0
        self._grid_refreshes = 0
        self._grid_model_dirty = True  # Start dirty to ensure initial load
        self._folder_loaded = False  # Track whether the current folder scan is complete
        self._thumbnail_cache = ThumbnailCache(
            max_bytes=DEFAULT_THUMBNAIL_CACHE_BYTES,  # cache-ready thumbnail QImages
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
        # Halo removal brush (faststack/controller/halo_removal.py).
        self._init_halo_removal()
        self.is_zoomed = False  # Track zoom state for high-res loading logic

        # Connect model selection changes to UIState for QML property notification
        # Must connect to .emit (not the signal itself) for signal-to-signal forwarding
        self._thumbnail_model.selectionChanged.connect(
            self.ui_state.gridSelectedCountChanged.emit
        )

        # -- Stacking State --
        self.stack_start_index: Optional[int] = None
        self.stack_end_index: Optional[int] = None
        self.stacks: List[List[int]] = []
        self._stack_identity_projection_blocked = False

        # -- Batch Selection State (for drag-and-drop) --
        self.batch_start_index: Optional[int] = None
        # A single non-contiguous selection of images, stored as a sorted list
        # of [start, end] index ranges. Multiple ranges exist only because some
        # input paths can produce a non-contiguous selection (e.g. pressing B
        # on images 3, 10, 22 yields [[3,3], [10,10], [22,22]]); semantically
        # this is still ONE selection. All batch-aware operations
        # (delete_batch_images, drag-and-drop, batch_auto_levels, toggle_* flag
        # helpers, get_batch_count_for_current_image) treat self.batches as the
        # union of all ranges. Do not add per-range semantics without also
        # updating every consumer — see _get_user_flag_toggle_indices for the
        # canonical pattern.
        self.batches: List[List[int]] = []

        self._filter_string: str = ""  # Default filter
        self._filter_flags: list = (
            []
        )  # Active flag filters (e.g. ["uploaded", "stacked"])
        self._filter_enabled: bool = False
        self._adopt_saved_sort_mode()

        self._metadata_cache = {}
        self._metadata_cache_index = (-1, -1)
        self._exif_brief_cache: dict = {}  # EXIF context key → formatted EXIF string
        self._native_image_size_cache: Dict[str, tuple[float, int, int]] = {}
        self._native_image_size_lock = threading.Lock()
        # Per-navigation memo for get_current_display_native_size(): keyed by
        # (current_index, ui_refresh_generation) so the two QML property reads
        # per navigation (width + height) share one stat()/decode instead of
        # each doing their own. Excludes the live-preview branch, which can
        # change without a generation bump.
        self._native_size_memo: tuple = ((-1, -1), (0, 0))
        self._exif_pending_path: Optional[tuple[str, str]] = (
            None  # current/previous source keys awaiting EXIF read
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

        # High-quality preview refinement: every preview-resolution render
        # (slider drag, adjust keys, auto-adjust) re-arms this debounce; once
        # input goes idle the same frame is re-rendered at display
        # resolution, so drags stay cheap and the settled image is sharp.
        self._hq_preview_timer = QTimer(self)
        self._hq_preview_timer.setSingleShot(True)
        self._hq_preview_timer.setInterval(_HQ_PREVIEW_DEBOUNCE_DRAG_MS)
        self._hq_preview_timer.timeout.connect(self._refine_preview_resolution)

        # Settled navigation quality upgrade: arrow-key browsing uses cheap
        # fit-within decodes, then the current image alone is re-decoded at
        # cover quality after input pauses.
        self._quality_decode_timer = QTimer(self)
        self._quality_decode_timer.setSingleShot(True)
        self._quality_decode_timer.setInterval(275)
        self._quality_decode_timer.timeout.connect(self._submit_settled_quality_decode)
        self._quality_decode_immediate_pending = False

        # Reproduce normal keyboard behavior (one immediate action, then the OS
        # repeat delay) while replacing the platform's variable repeat cadence
        # with a stable, user-configurable rate. The timer changes the visible
        # target only after the exact fast-tier buffer is ready.
        navigation_fps = config.getfloat(
            "core",
            "navigation_rate_fps",
            fallback=15.0,
        )
        navigation_fps = max(1.0, min(navigation_fps, 30.0))
        self._navigation_rate_fps = navigation_fps
        self._navigation_interval_ms = max(1, round(1000.0 / navigation_fps))
        # Adaptive cadence governor: when the decoded look-ahead buffer runs
        # low during a held key, stretch the interval multiplicatively so
        # motion slows smoothly instead of hitching on a demand-decode wait;
        # relax back toward navigation_rate_fps once the buffer refills.
        # navigation_rate_fps stays the ceiling — pacing never speeds past it.
        self._navigation_adaptive_pacing = config.getboolean(
            "core",
            "navigation_adaptive_pacing",
            fallback=True,
        )
        self._paced_interval_ms = float(self._navigation_interval_ms)
        self._paced_interval_max_ms = float(self._navigation_interval_ms) * 4.0
        self._navigation_repeat_delay_ms = _keyboard_repeat_delay_ms()
        self._navigation_hold_timer = QTimer(self)
        self._navigation_hold_timer.setSingleShot(True)
        self._navigation_hold_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._navigation_hold_timer.setInterval(self._navigation_interval_ms)
        self._navigation_hold_timer.timeout.connect(self._paced_navigation_tick)
        # A missed QML Ready/synchronize/swap acknowledgement must not leave a
        # physical arrow hold permanently stuck after its first image.
        self._navigation_gate_watchdog_timer = QTimer(self)
        self._navigation_gate_watchdog_timer.setSingleShot(True)
        self._navigation_gate_watchdog_timer.setInterval(
            max(750, self._navigation_repeat_delay_ms + 250)
        )
        self._navigation_gate_watchdog_timer.timeout.connect(
            self._on_paced_present_timeout
        )

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

        # Throttle retry timer: when a watcher refresh arrives sooner than
        # _index_scan_min_interval_s after the last scan started, the scan
        # is deferred to fire here instead of immediately.
        self._index_throttle_timer = QTimer(self)
        self._index_throttle_timer.setSingleShot(True)
        self._index_throttle_timer.timeout.connect(
            lambda: self._maybe_start_index_scan()
        )

        # Periodic summary for thumbnail debug logging
        if self.debug_thumb_timing or self.debug_thumb_trace:
            self._thumb_summary_timer = QTimer(self)
            self._thumb_summary_timer.setInterval(1000)  # Check every second
            self._thumb_summary_timer.timeout.connect(
                thumb_debug.check_periodic_summary
            )
            self._thumb_summary_timer.start()

        # Debounce timer for metadata/highlight signals during rapid navigation
        # Only emits these signals once user stops navigating (150ms, matching
        # the EXIF debounce timer). Keyboard auto-repeat fires every ~30ms, so a
        # 16ms timer fired between every repeat and never actually debounced.
        self._metadata_debounce_timer = QTimer(self)
        self._metadata_debounce_timer.setSingleShot(True)
        self._metadata_debounce_timer.setInterval(150)  # 150ms debounce
        self._metadata_debounce_timer.timeout.connect(
            self._emit_debounced_metadata_signals
        )

        # Debounce timer for auto-adjust saves triggered by rapid '-' / '=' keys.
        # Preview updates immediately; the save coalesces into one disk write
        # per burst so repeated keypresses don't stall the UI.
        self._auto_adjust_save_timer = QTimer(self)
        self._auto_adjust_save_timer.setSingleShot(True)
        self._auto_adjust_save_timer.setInterval(200)
        self._auto_adjust_save_timer.timeout.connect(
            self._fire_auto_adjust_save_debounce
        )

        # Debounce timer for crash-recovery session persistence. Flushes the
        # current image path plus an index fallback to the folder sidecar and
        # refreshes this instance's session record ~1s after the user settles,
        # so an unexpected reboot can resume close to where they left off.
        self._session_registry = SessionRegistry()
        # Tracks the directory last written to config.last_directory so we only
        # rewrite the INI when the open folder actually changes. Seeded from
        # the stored value so reopening the same folder writes nothing.
        self._last_persisted_dir = (
            config.get("core", "last_directory", fallback="") or None
        )
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.setInterval(1000)
        self._session_save_timer.timeout.connect(self._persist_session_state)
        # The writes themselves (sidecar position + session record, each an
        # fsync'd atomic replace) run here so they never stall the UI. One
        # worker keeps them FIFO; shutdown drains it before the final
        # synchronous save and the session-record removal.
        self._session_write_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="SessionWrite"
        )
        self._sessionSidecarWriteFinished.connect(
            self._on_session_sidecar_write_finished
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
        # 0 == follow auto_level_threshold. See _black_threshold_percent().
        self.auto_level_black_threshold = config.getfloat(
            "core", "auto_level_black_threshold", 0.0
        )
        self.auto_level_strength = config.getfloat("core", "auto_level_strength", 1.0)
        self.auto_level_strength_auto = config.getboolean(
            "core", "auto_level_strength_auto", False
        )
        self.auto_vibrance_enabled = config.getboolean(
            "core", "auto_vibrance_enabled", True
        )
        self.auto_level_midtone = config.getboolean("core", "auto_level_midtone", True)
        self.auto_level_midtone_target = config.getfloat(
            "core", "auto_level_midtone_target", 0.38
        )
        self.auto_level_channel_budget = config.getfloat(
            "core", "auto_level_channel_budget", 3.0
        )
        self.levels_soft_knee = config.getboolean("core", "levels_soft_knee", True)
        self.export_dither = config.getboolean("core", "export_dither", True)
        # Rendering preferences live on the editor so the imaging layer stays
        # config-free.
        self.image_editor.levels_soft_knee = self.levels_soft_knee
        self.image_editor.export_dither = self.export_dither
        self.ui_state.autoAddEditedToBatch = config.getboolean(
            "core", "auto_add_edited_to_batch", True
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
                from faststack.imaging.optional_deps import has_opencv

                if not has_opencv():
                    self._opencv_warning_shown = True
                    log.warning(
                        "OpenCV not available - detail sliders (clarity/texture/sharpness) will be slower"
                    )
                    self.update_status_message(
                        "OpenCV not installed - editor performance reduced. Install opencv-python for faster editing.",
                        timeout=8000,
                    )
        else:
            keep_preview = False
            # Cleanup large memory buffers when editor closes
            if self.image_editor:
                session_info = self._get_current_preview_session_provenance()
                # If a save is active for this session, preserve the memory
                # so the user can re-open/retry if the background task fails.
                current_key = None
                if 0 <= self.current_index < len(self.image_files):
                    try:
                        current_key = self._key(
                            self.image_files[self.current_index].path
                        )
                    except (OSError, TypeError, ValueError):
                        current_key = None
                if current_key is None and session_info is not None:
                    current_key = session_info[0]
                elif current_key is None and self.image_editor.current_filepath:
                    try:
                        current_key = self._key(self.image_editor.current_filepath)
                    except (OSError, TypeError, ValueError):
                        current_key = None

                save_in_progress = bool(
                    current_key and current_key in self._saving_keys
                )
                keep_preview = (
                    save_in_progress or self._is_current_live_edit_session_dirty()
                )
                if save_in_progress:
                    log.debug(
                        "Editor closed but save in progress for %s; keeping session memory",
                        current_key,
                    )
                elif keep_preview:
                    log.debug("Editor closed with unsaved live edits; keeping preview")
                else:
                    log.debug("Editor closed with a clean session; releasing buffers")
                    self.image_editor.clear()

            if not keep_preview:
                with self._preview_lock:
                    self._clear_last_rendered_preview_locked()

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

    def _capture_filter_identity(self) -> dict[str, Any]:
        """Capture list-relative state as stable paths before a filter transition."""
        return {
            "stack_groups": self._resolve_ranges_to_paths(self.stacks),
            "batch_groups": self._resolve_ranges_to_paths(self.batches),
            "stack_start": (
                self.image_files[self.stack_start_index].path
                if self.stack_start_index is not None
                and 0 <= self.stack_start_index < len(self.image_files)
                else None
            ),
            "stack_end": (
                self.image_files[self.stack_end_index].path
                if self.stack_end_index is not None
                and 0 <= self.stack_end_index < len(self.image_files)
                else None
            ),
            "batch_start": (
                self.image_files[self.batch_start_index].path
                if self.batch_start_index is not None
                and 0 <= self.batch_start_index < len(self.image_files)
                else None
            ),
        }

    def _restore_filter_identity(self, snapshot: dict[str, Any]) -> None:
        """Rebuild range and marker state against the current visible path map."""
        self.stacks = self._rebuild_ranges_from_paths(snapshot["stack_groups"])
        # Batch flags are authoritative, but path reconstruction prevents a
        # compressed filtered range from migrating onto unrelated rows.
        path_batches = self._rebuild_ranges_from_paths(snapshot["batch_groups"])
        self.batches = self._merge_stack_ranges(self.batches + path_batches)

        self.stack_start_index = self._visible_index_for_path(snapshot["stack_start"])
        self.stack_end_index = self._visible_index_for_path(snapshot["stack_end"])
        self.batch_start_index = self._visible_index_for_path(snapshot["batch_start"])

    def _sorted_all_images_for_identity(self) -> list[ImageFile]:
        """Return the full folder order without applying the active filter."""
        images = list(self._all_images)
        if self.sort_mode == "filename":
            images.sort(key=lambda image: image.path.name.lower())
        elif self.sort_mode == "date":
            images.sort(key=lambda image: (image.timestamp, image.path.name.lower()))
        elif self.sort_mode == "date_reverse":
            images.sort(key=lambda image: (-image.timestamp, image.path.name.lower()))
        return images

    def _update_filter_identity_snapshot(self, structure: str) -> None:
        """Merge visible structural edits into the unfiltered path snapshot."""
        snapshot = self._filter_identity_snapshot
        if not self._filter_enabled or snapshot is None:
            return

        if structure == "stack":
            groups_key = "stack_groups"
            ranges = self.stacks
            marker_names = ("stack_start", "stack_end")
            marker_indices = (self.stack_start_index, self.stack_end_index)
        elif structure == "batch":
            groups_key = "batch_groups"
            ranges = self.batches
            marker_names = ("batch_start",)
            marker_indices = (self.batch_start_index,)
        else:
            raise ValueError(f"Unknown filter identity structure: {structure}")

        visible_keys = {self._key(image.path) for image in self.image_files}
        previous_member_keys = {
            self._key(path) for group in snapshot[groups_key] for path in group
        }
        visible_member_keys = {
            self._key(path)
            for group in self._resolve_ranges_to_paths(ranges)
            for path in group
        }
        member_keys = (previous_member_keys - visible_keys) | visible_member_keys
        snapshot[groups_key] = self._path_groups_in_folder_order(member_keys)

        for marker_name, marker_index in zip(marker_names, marker_indices):
            old_marker = snapshot[marker_name]
            old_is_visible = (
                old_marker is not None and self._key(old_marker) in visible_keys
            )
            if marker_index is not None and 0 <= marker_index < len(self.image_files):
                snapshot[marker_name] = self.image_files[marker_index].path
            elif old_is_visible:
                snapshot[marker_name] = None

    def _persist_stack_state(self, *, clear_all: bool = False) -> bool:
        """Persist stack identity correctly even when the view is filtered."""
        if self._stack_identity_projection_blocked and not clear_all:
            # Runtime ranges are deliberately empty when even one persisted
            # logical group cannot be represented by the current image list.
            # Do not turn that fail-safe projection into an identity deletion.
            log.warning(
                "Stack persistence deferred: persisted identities are not fully "
                "representable in the current image list"
            )
            self.stacks = []
            return self._persist_sidecar()
        if clear_all:
            self._stack_identity_projection_blocked = False
        if self._filter_enabled and self._filter_identity_snapshot is not None:
            if clear_all:
                self._filter_identity_snapshot["stack_groups"] = []
                self._filter_identity_snapshot["stack_start"] = None
                self._filter_identity_snapshot["stack_end"] = None
            else:
                self._update_filter_identity_snapshot("stack")
            all_images = self._sorted_all_images_for_identity()
            all_path_to_index = {
                self._key(image.path): index for index, image in enumerate(all_images)
            }
            persisted_stacks = self._rebuild_ranges_from_paths_map(
                self._filter_identity_snapshot["stack_groups"],
                all_path_to_index,
            )
            persisted_groups = self._filter_identity_snapshot["stack_groups"]
        else:
            persisted_stacks = [stack[:] for stack in self.stacks]
            all_images = list(self.image_files)
            persisted_groups = self._resolve_ranges_to_paths(self.stacks)
        stack_order = [
            self.sidecar.metadata_key_for_path(image.path) for image in all_images
        ]
        stack_paths = [
            [self.sidecar.metadata_key_for_path(path) for path in group]
            for group in persisted_groups
        ]
        with self.sidecar._state_lock:
            self.sidecar.data.stacks = persisted_stacks
            self.sidecar.data.stack_paths = stack_paths
            self.sidecar.data.stack_order = stack_order
        return self._persist_sidecar()

    def _restore_stacks_from_sidecar_identity(self) -> list[list[int]]:
        """Resolve distinct persisted groups without splitting or merging them."""
        groups = self.sidecar.data.stack_paths
        if not groups:
            self._stack_identity_projection_blocked = False
            return self.sidecar.data.stacks
        key_to_index = {
            self.sidecar.metadata_key_for_path(image.path): index
            for index, image in enumerate(self.image_files)
        }
        ranges = []
        seen_members = set()
        for group in groups:
            if not group:
                log.warning(
                    "Stack identity restoration skipped: persisted group is empty"
                )
                self._stack_identity_projection_blocked = True
                return []
            group_members = set(group)
            if seen_members & group_members:
                log.warning(
                    "Stack identity restoration skipped: persisted groups overlap"
                )
                self._stack_identity_projection_blocked = True
                return []
            seen_members.update(group_members)
            missing_members = group_members - key_to_index.keys()
            if missing_members:
                log.warning(
                    "Stack identity restoration skipped: persisted group has %d "
                    "member(s) absent from the current image list",
                    len(missing_members),
                )
                self._stack_identity_projection_blocked = True
                return []
            indices = sorted(key_to_index[key] for key in group_members)
            if indices != list(range(indices[0], indices[-1] + 1)):
                log.warning(
                    "Stack identity restoration skipped: group is not contiguous "
                    "in the current image order"
                )
                self._stack_identity_projection_blocked = True
                return []
            ranges.append([indices[0], indices[-1]])

        ranges.sort()
        if any(
            current[0] <= previous[1] + 1
            for previous, current in zip(ranges, ranges[1:])
        ):
            log.warning(
                "Stack identity restoration skipped: distinct groups are adjacent "
                "in the current image order"
            )
            self._stack_identity_projection_blocked = True
            return []
        self._stack_identity_projection_blocked = False
        return ranges

    def _restore_filter_current_image(
        self, current_path: Optional[Path], previous_index: int
    ) -> None:
        if not self.image_files:
            self.current_index = 0
            self._clear_variant_override()
            return
        new_index = (
            self._path_to_index.get(self._key(current_path))
            if current_path is not None
            else None
        )
        if new_index is None:
            self.current_index = min(max(previous_index, 0), len(self.image_files) - 1)
            self._clear_variant_override()
        else:
            self.current_index = new_index

    @Slot(str, "QVariantList")
    def apply_filter(self, filter_string: str, filter_flags: list):
        filter_string = filter_string.strip()
        flags = list(filter_flags or [])

        if not filter_string and not flags:
            self.clear_filter()
            return

        self._begin_direct_image_transition("filter applied")
        previous_index = self.current_index
        current_path = self._current_image_path()
        if not self._filter_enabled or self._filter_identity_snapshot is None:
            self._filter_identity_snapshot = self._capture_filter_identity()
        self._filter_string = filter_string
        self._filter_flags = flags
        self._filter_enabled = True
        self._apply_filter_to_cached_list(notify=False)  # Fast in-memory filtering
        self._restore_filter_identity(self._filter_identity_snapshot)
        self._restore_filter_current_image(current_path, previous_index)
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
            self._refresh_thumbnail_model_from_controller()
        else:
            self._grid_model_dirty = True

        self._do_prefetch(self.current_index)
        self.sync_ui_state(image_count_changed=True)
        self._restart_quality_decode_timer()

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
        self._begin_direct_image_transition("filter cleared")
        previous_index = self.current_index
        current_path = self._current_image_path()
        identity_snapshot = (
            self._filter_identity_snapshot or self._capture_filter_identity()
        )
        self._filter_enabled = False
        self._filter_string = ""
        self._filter_flags = []
        self._apply_filter_to_cached_list(notify=False)  # Fast in-memory filtering
        self._restore_filter_identity(identity_snapshot)
        self._restore_filter_current_image(current_path, previous_index)
        self._filter_identity_snapshot = None
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
            self._refresh_thumbnail_model_from_controller()
        else:
            self._grid_model_dirty = True

        self._do_prefetch(self.current_index)
        self.sync_ui_state(image_count_changed=True)
        self._restart_quality_decode_timer()

    @Slot(result=str)
    def get_sort_mode(self):
        return self.sort_mode

    def _adopt_saved_sort_mode(self) -> None:
        """Take the sort mode from the current sidecar, deriving one if absent.

        Used both at startup and when switching directories, so the derived
        fallback stays identical in both places.
        """
        with self.sidecar._state_lock:
            saved_sort_mode = self.sidecar.data.sort_mode
            has_stacks = bool(self.sidecar.data.stacks)
        if saved_sort_mode in SUPPORTED_SORT_MODES:
            self.sort_mode = saved_sort_mode
        elif has_stacks:
            # Legacy stack ranges were recorded against the historical
            # timestamp-based default order. Preserve that order until the
            # user explicitly chooses a different sort.
            self.sort_mode = "default"
        else:
            # Stable camera filenames do not change when an external editor
            # replaces a JPG, unlike filesystem modification timestamps.
            self.sort_mode = "filename"
        with self.sidecar._state_lock:
            self.sidecar.data.sort_mode = self.sort_mode

    @Slot(str)
    def markStartup(self, label: str) -> None:
        """QML-callable startup breadcrumb.

        engine.load() is a single synchronous call that instantiates the whole
        Main.qml tree, so Python can only see its total. Component.onCompleted
        fires per subtree in declaration order, which turns that one number
        into a per-item breakdown. INFO on faststack.app, so it is visible
        under --debugcache and silent otherwise.
        """
        log.info("[STARTUP] %.3fs qml: %s", _startup_elapsed(), label)

    @Slot(str)
    def set_sort_mode(self, mode: str):
        if mode not in SUPPORTED_SORT_MODES:
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
        self._begin_direct_image_transition("sort order changed")
        self.sort_mode = mode

        preserved_path = None
        if self.image_files and 0 <= self.current_index < len(self.image_files):
            preserved_path = self.image_files[self.current_index].path

        # Snapshot paths referenced by stacks if preserving.
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
        old_stack_end_path = (
            self.image_files[self.stack_end_index].path
            if not clear_stacks
            and self.stack_end_index is not None
            and 0 <= self.stack_end_index < len(self.image_files)
            else None
        )
        old_batch_start_path = (
            self.image_files[self.batch_start_index].path
            if self.batch_start_index is not None
            and 0 <= self.batch_start_index < len(self.image_files)
            else None
        )

        self._apply_filter_to_cached_list(notify=False)
        self._bump_display_generation()

        if old_batch_start_path:
            key = self._key(old_batch_start_path)
            self.batch_start_index = self._path_to_index.get(key)

        # Handle stacks
        if clear_stacks:
            self.stacks = []
            self.stack_start_index = None
            self.stack_end_index = None
            with self.sidecar._state_lock:
                self.sidecar.data.stacks = []
        elif have_stacks:
            self.stacks = self._rebuild_ranges_from_paths(old_stack_paths)
            # Only persist to sidecar when no filter is active — filtered
            # image_files may hide stack members, producing incomplete ranges.
            if not self._filter_enabled:
                with self.sidecar._state_lock:
                    self.sidecar.data.stacks = self.stacks

        # Remap pending stack start marker (even when no completed stacks exist)
        if not clear_stacks and old_stack_start_path:
            key = self._key(old_stack_start_path)
            self.stack_start_index = self._path_to_index.get(key)
        if not clear_stacks and old_stack_end_path:
            key = self._key(old_stack_end_path)
            self.stack_end_index = self._path_to_index.get(key)

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

        with self.sidecar._state_lock:
            self.sidecar.data.sort_mode = mode
        self.sidecar.set_last_position(
            self.current_index,
            self._current_image_path(),
        )
        metadata_saved = self._persist_sidecar()

        if self._is_grid_view_active:
            self._thumbnail_prefetcher.cancel_all()

        if self._is_grid_view_active and self._thumbnail_model:
            self._refresh_thumbnail_model_from_controller()
        else:
            self._grid_model_dirty = True

        self._do_prefetch(self.current_index)
        self.sync_ui_state(image_count_changed=True)
        self._restart_quality_decode_timer()
        self.dataChanged.emit()
        if hasattr(self, "ui_state") and self.ui_state:
            self.ui_state.sortModeChanged.emit()
        return metadata_saved

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
            # migrate=False: a bulk read. Most images have no entry, and the
            # legacy-key migration scan walks every entry on each miss.
            result = [
                img
                for img in result
                if (
                    (
                        meta := self.sidecar.get_metadata(
                            img.path, create=False, migrate=False
                        )
                    )
                    and all(getattr(meta, f, False) for f in flags)
                )
            ]

        if mode == "filename":
            result.sort(key=lambda img: img.path.name.lower())
        elif mode == "date":
            # Use the timestamp captured at scan time (ImageFile.timestamp)
            # so we avoid live filesystem calls during sort.  Tiebreak on
            # lowercase filename for determinism when mtimes are equal.
            result.sort(key=lambda img: (img.timestamp, img.path.name.lower()))
        elif mode == "date_reverse":
            # Reverse chronological order (newest first)
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
        self._exec_modal_dialog(msg_box)
        return msg_box.clickedButton() == clear_btn

    def get_display_info(self):
        with self._display_lock:
            if self.is_zoomed:
                return 0, 0, self.display_generation
            return self.display_width, self.display_height, self.display_generation

    def _allocate_display_generation_locked(self) -> int:
        """Switch to a never-used display generation. Caller holds _display_lock."""
        self._display_generation_counter = (
            max(self._display_generation_counter, self.display_generation) + 1
        )
        self.display_generation = self._display_generation_counter
        return self.display_generation

    def _bump_display_generation(self):
        """Move to a fresh display_generation under _display_lock.

        Retires every cached frame, including those kept for other recent
        window sizes: the caller is saying the pixels themselves are stale.
        """
        with self._display_lock:
            self._display_size_generations.clear()
            self._allocate_display_generation_locked()

    # Sizes remembered at once, the current one included.
    _REMEMBERED_DISPLAY_SIZES = 3

    def _switch_display_generation_for_resize_locked(
        self,
        old_size: Optional[tuple[int, int]],
        new_size: tuple[int, int],
    ) -> None:
        """Pick the generation for a new fit-to-window size.

        Caller holds _display_lock. A size used a moment ago gets its
        generation back, and with it every frame decoded for that size; any
        other size gets a fresh one. Frames are stored with the size and
        generation captured together when their decode was submitted, so a
        generation never holds frames of two sizes. While zoomed the frames
        are full resolution under the zoom pair, which a resize retires as
        it always has.
        """
        remembered = self._display_size_generations
        if self.is_zoomed:
            remembered.clear()
            self._allocate_display_generation_locked()
            return
        if old_size is not None:
            remembered.pop(old_size, None)
            remembered[old_size] = self.display_generation
        generation = remembered.pop(new_size, None)
        if generation is None:
            generation = self._allocate_display_generation_locked()
        else:
            self.display_generation = generation
        remembered[new_size] = generation
        while len(remembered) > self._REMEMBERED_DISPLAY_SIZES:
            del remembered[next(iter(remembered))]

    def _reachable_display_generations(self) -> set[int]:
        """Generations whose frames can still be shown without a new decode."""
        with self._display_lock:
            reachable = set(self._display_size_generations.values())
            reachable.add(self.display_generation)
            # The zoom pair is reachable through either of its halves: a zoom
            # toggle moves between them. A pair left behind by a size that is
            # no longer remembered is not.
            pair = self._zoom_generation_pair
            if pair is not None and not reachable.isdisjoint(pair):
                reachable.update(pair)
        return reachable

    def _cache_decode_quality(self, cache_key: str) -> Optional[str]:
        """Return cached display quality for ``cache_key`` without changing key format."""
        decoded = self.image_cache.get(cache_key)
        if decoded is None:
            return None
        return getattr(decoded, "quality", "cover")

    def _navigation_fast_mode_active(self) -> bool:
        """Whether source requests should consistently use scrub-quality buffers."""
        return bool(
            getattr(self, "_held_navigation_direction", 0)
            or getattr(self, "_paced_navigation_pending", None) is not None
            or getattr(self, "_paced_navigation_queue", ())
            or getattr(self, "_paced_present_gate", None) is not None
        )

    def _quality_upgrade_still_current(
        self,
        *,
        index: int,
        display_generation: int,
        token: int,
    ) -> bool:
        """Return True if a settled quality decode still targets the visible image."""
        if token != self._quality_decode_token:
            return False
        if not self._loupe_decode_allowed():
            return False
        if index != self.current_index:
            return False
        if not self.image_files or index < 0 or index >= len(self.image_files):
            return False
        if self.view_override_path is not None:
            return False
        if self._has_current_live_preview_for_index(index):
            return False
        _, _, current_display_gen = self.get_display_info()
        return display_generation == current_display_gen

    def _navigation_in_flight(self) -> bool:
        """True while arrow navigation is held, queued or awaiting its frame."""
        return (
            self._held_navigation_direction != 0
            or self._paced_navigation_pending is not None
            or bool(self._paced_navigation_queue)
            or self._paced_present_gate is not None
        )

    def _restart_quality_decode_timer(self, *, immediate: bool = False) -> None:
        """Schedule settled quality, bypassing debounce after an explicit release."""
        immediate = immediate or self._quality_decode_immediate_pending
        self._quality_decode_token += 1
        self.prefetcher.cancel_pending_cover_tasks()
        if (
            self._held_navigation_direction != 0
            or self._paced_navigation_pending is not None
            or self._paced_navigation_queue
            or self._paced_present_gate is not None
            or not self._loupe_decode_allowed()
            or not self.display_ready
            or not self.image_files
            or self.view_override_path is not None
        ):
            self._quality_decode_immediate_pending = immediate
            self._quality_decode_timer.stop()
            return
        self._quality_decode_immediate_pending = False
        if immediate:
            self._quality_decode_timer.stop()
            self._submit_settled_quality_decode()
        else:
            self._quality_decode_timer.start()

    def _submit_settled_quality_decode(self) -> None:
        """Submit one cover-quality decode for the current image after navigation settles."""
        if (
            self._held_navigation_direction != 0
            or self._paced_navigation_pending is not None
            or self._paced_navigation_queue
            or self._paced_present_gate is not None
        ):
            return
        if not self.image_files or self.current_index >= len(self.image_files):
            return
        index = self.current_index
        if self.view_override_path is not None:
            return
        if self._has_current_live_preview_for_index(index):
            return

        display_width, display_height, display_gen = self.get_display_info()
        image_path = self.image_files[index].path
        cover_key = build_cache_key(image_path, display_gen, "cover")
        decoded = self.image_cache.get(cover_key)
        if decoded is not None:
            return

        fast_decoded = self.image_cache.get(
            build_cache_key(image_path, display_gen, "fast")
        )

        token = self._quality_decode_token
        if self.debug_cache_trace:
            log.info(
                f"[DBGCACHE] {time.perf_counter()*1000:.3f} quality_decode: SUBMIT index={index} gen={display_gen} cached={getattr(fast_decoded, 'quality', None)}"
            )

        future = self.prefetcher.submit_task(
            index,
            self.prefetcher.generation,
            priority=False,
            quality="cover",
            quality_token=token,
            quality_index=index,
            nav_seq=self._current_nav_seq if self.debug_cache else None,
        )
        if future is None:
            return

        def _on_done(fut, *, idx=index, gen=display_gen, tok=token):
            try:
                result = fut.result()
                error = None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                result = None
                error = exc
            self._qualityDecodeFinished.emit(
                {
                    "index": idx,
                    "display_generation": gen,
                    "token": tok,
                    "result": result,
                    "error": error,
                }
            )

        future.add_done_callback(_on_done)

    @Slot(object)
    def _on_quality_decode_finished(self, payload) -> None:
        """Refresh QML if the settled quality decode still matches the current view."""
        if not payload or payload.get("result") is None:
            error = payload.get("error") if payload else None
            if error is not None:
                log.debug("Settled quality decode failed: %s", error)
            return

        index = int(payload.get("index", -1))
        display_generation = int(payload.get("display_generation", -1))
        token = int(payload.get("token", -1))
        if not self._quality_upgrade_still_current(
            index=index,
            display_generation=display_generation,
            token=token,
        ):
            return

        path, decoded_display_gen = payload["result"]
        if decoded_display_gen != display_generation:
            return
        cache_key = build_cache_key(path, decoded_display_gen, "cover")
        decoded = self.image_cache.get(cache_key)
        if decoded is None or getattr(decoded, "quality", "cover") != "cover":
            return

        # Keep the small scrub frame adjacent to its settled counterpart in
        # LRU order. Returning to this photo should not require re-decoding a
        # fast tier merely because the later cover access made it look old.
        self.image_cache.get(build_cache_key(path, decoded_display_gen, "fast"))

        with self._last_image_lock:
            self.last_displayed_image = decoded
        self.ui_refresh_generation += 1
        self.ui_state.currentImageSourceChanged.emit()
        if self.debug_cache_trace:
            log.info(
                f"[DBGCACHE] {time.perf_counter()*1000:.3f} quality_decode: REFRESH index={index} gen={display_generation}"
            )

    def _prefetch_cache_put(
        self,
        cache_key,
        decoded,
        path=None,
        decode_started=None,
        *,
        quality: str = "fast",
        quality_token: Optional[int] = None,
        quality_index: int = -1,
        quality_display_generation: int = -1,
    ):
        """Cache insert for prefetch decode results, with staleness guard.

        A decode that STARTED before its path's invalidation epoch read pixels
        from a file that has since been replaced (saved); inserting it would
        resurrect stale pixels right after the targeted pop_path. Reject it —
        the next update_prefetch/blocking decode re-reads the new file.
        """
        if quality == "cover" and quality_token is not None:
            if not self._quality_upgrade_still_current(
                index=quality_index,
                display_generation=quality_display_generation,
                token=quality_token,
            ):
                self._note_cache_reject(
                    "stale cover upgrade",
                    cache_key,
                    decoded=decoded,
                )
                return False
        if path is not None and decode_started is not None:
            try:
                epoch_key = self._key(Path(path))
            except (OSError, TypeError, ValueError):
                epoch_key = str(path)
            with self._decode_invalidation_lock:
                entry = self._decode_invalidation_epochs.get(epoch_key)
            if entry is not None:
                epoch_time, fingerprint = entry
                if fingerprint is None:
                    log.debug(
                        "Discarding prefetch decode for %s because optimistic "
                        "live-preview seed is active",
                        path,
                    )
                    self._note_cache_reject(
                        "live-preview seed active",
                        cache_key,
                        decoded=decoded,
                    )
                    return False
                if decode_started < epoch_time:
                    log.debug(
                        "Discarding stale prefetch decode for %s "
                        "(file replaced while decode was in flight)",
                        path,
                    )
                    self._note_cache_reject(
                        "file replaced mid-decode",
                        cache_key,
                        decoded=decoded,
                    )
                    return False
        self.image_cache[cache_key] = decoded
        if self.image_cache.get(cache_key) is not decoded:
            self._note_cache_reject(
                "cache declined insertion",
                cache_key,
                decoded=decoded,
            )
            return False
        native_width = int(getattr(decoded, "native_width", 0))
        native_height = int(getattr(decoded, "native_height", 0))
        if path is not None and native_width > 0 and native_height > 0:
            try:
                native_path = Path(path)
                native_key = self._key(native_path)
                native_mtime = native_path.stat().st_mtime
                if native_key is not None:
                    with self._native_image_size_lock:
                        self._native_image_size_cache[native_key] = (
                            native_mtime,
                            native_width,
                            native_height,
                        )
            except (OSError, TypeError, ValueError):
                pass
        return True

    @staticmethod
    def _file_state_fingerprint(p: Path) -> Optional[tuple]:
        """Strong cheap identity of the file's current on-disk generation."""
        try:
            st = p.stat()
            return (
                st.st_dev,
                st.st_ino,
                st.st_size,
                st.st_mtime_ns,
                st.st_ctime_ns,
            )
        except OSError:
            return None

    def _invalidate_decoded_path(self, path, *, force: bool = False) -> None:
        """Targeted decode-cache invalidation for one changed file.

        Replaces the old display-generation bump (which stale-keyed every
        cached decode and forced the whole prefetch window to re-decode after
        each save). Records the invalidation epoch BEFORE popping cache
        entries so in-flight decodes of the old file contents are rejected by
        _prefetch_cache_put when they land, then cancels/unschedules any
        pending prefetch for the path so it gets re-submitted fresh.

        A save is invalidated twice (save-completion handler + the watcher
        events it triggers, in either order). The file-generation fingerprint
        dedupes watcher echoes only: if the file's on-disk state is the one
        we already invalidated, cached entries were decoded after that epoch
        and are coherent, so the duplicate watcher invalidation is skipped
        instead of forcing another blocking re-decode of the displayed image.
        """
        if path is None:
            return
        p = Path(path)
        try:
            epoch_key = self._key(p)
        except (OSError, TypeError, ValueError):
            epoch_key = str(p)
        now = time.monotonic()
        fingerprint = self._file_state_fingerprint(p)
        with self._decode_invalidation_lock:
            entry = self._decode_invalidation_epochs.get(epoch_key)
            if (
                not force
                and entry is not None
                and fingerprint is not None
                and entry[1] == fingerprint
            ):
                return
            # Opportunistic pruning: no decode stays in flight for minutes,
            # so old epochs can never reject anything and may be dropped.
            if len(self._decode_invalidation_epochs) > 64:
                cutoff = now - 120.0
                self._decode_invalidation_epochs = {
                    k: v
                    for k, v in self._decode_invalidation_epochs.items()
                    if v[0] >= cutoff
                }
            self._decode_invalidation_epochs[epoch_key] = (now, fingerprint)
        self.image_cache.pop_path(p)
        self.prefetcher.invalidate_path(p)
        self._forget_image_file_stat(p)

    def _forget_image_file_stat(self, path: Path) -> None:
        """Drop the indexer's cached stat for a file whose contents changed.

        The thumbnail grid reuses the stat captured when the folder was
        scanned instead of re-statting every image on the UI thread, so a
        file FastStack itself rewrote must be re-statted on the next refresh.
        """
        path_to_index = getattr(self, "_path_to_index", None)
        if not path_to_index:
            return
        try:
            index = path_to_index.get(self._key(path))
        except (OSError, TypeError, ValueError):
            return
        if index is None or not (0 <= index < len(self.image_files)):
            return
        image_file = self.image_files[index]
        image_file.mtime_ns = None
        image_file.source_identity = None

    def _mark_optimistic_decode_seed_epoch(self, path: Path) -> None:
        """Record that the decode cache currently contains optimistic pixels."""
        try:
            epoch_key = self._key(path)
        except (OSError, TypeError, ValueError):
            epoch_key = str(path)
        with self._decode_invalidation_lock:
            self._decode_invalidation_epochs[epoch_key] = (time.monotonic(), None)

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

    def _commit_pending_display_size(self) -> tuple[bool, bool]:
        """Commit the latest reported viewport without triggering a UI refresh.

        Returns ``(changed, first_report)``. Keeping this separate lets folder
        startup commit the size before its first decode, instead of letting a
        synchronous provider request delay the resize timer and cause a full-
        resolution generation-0 decode followed by an immediate re-decode.
        """
        if self.pending_width is None or self.pending_height is None:
            return False, False

        width = int(self.pending_width)
        height = int(self.pending_height)
        with self._display_lock:
            first_report = not self.display_ready
            changed = width != self.display_width or height != self.display_height
            if changed:
                old_size = (
                    (int(self.display_width), int(self.display_height))
                    if self.display_ready
                    else None
                )
                self._switch_display_generation_for_resize_locked(
                    old_size, (width, height)
                )
            self.display_width = width
            self.display_height = height
            self.display_ready = True
        return changed, first_report

    def _handle_resize(self, *, keep_navigation_record: bool = False) -> bool:
        """Actual resize handler, called after debounce period.

        Returns True when the size changed and the view was refreshed for it.
        ``keep_navigation_record`` is for a caller that knows no navigation is
        in flight (see ``_finish_entering_loupe``).
        """
        changed, is_first_resize = self._commit_pending_display_size()
        if not changed and not is_first_resize:
            return False
        log.info(
            "Display size changed to: %dx%d (physical pixels)",
            self.display_width,
            self.display_height,
        )

        if not keep_navigation_record:
            self._cancel_paced_navigation()
        if is_first_resize:
            log.info("Display size now stable, enabling prefetch")

        # The editing preview master is sized from the image area, so a resize
        # invalidates it the same way it invalidates decoded buffers.
        self._apply_editor_preview_master_box()

        self.prefetcher.cancel_all()  # Cancel stale tasks to avoid wasted work
        # Cache keys include the display generation. Keep the frames a recent
        # size or the zoom pair can still reach; any other generation can
        # never be shown again and would only consume the byte budget.
        self.image_cache.retain_generations(self._reachable_display_generations())

        if self._loupe_decode_allowed():
            # On first resize, execute deferred prefetch; on subsequent resizes, do normal prefetch
            if is_first_resize and self.pending_prefetch_index is not None:
                self.prefetcher.update_prefetch(self.pending_prefetch_index)
                self.pending_prefetch_index = None
            else:
                self.prefetcher.update_prefetch(self.current_index)

        self.sync_ui_state()  # To refresh the image
        self._restart_quality_decode_timer()
        return True

    @Slot(bool)
    def set_zoomed(self, zoomed: bool):
        with self._display_lock:
            if self.is_zoomed == zoomed:
                return  # No-op: avoid unnecessary cache invalidation
            if _debug_mode:
                log.info("AppController.set_zoomed: %s -> %s", self.is_zoomed, zoomed)
            self.is_zoomed = zoomed
            # Zoomed (full-resolution) and fit-to-window frames live under
            # different display generations, but neither invalidates the
            # other. Reversing a zoom toggle restores the generation that
            # state used before, so every frame decoded for it is reachable
            # again and a zoom in/out round trip no longer re-decodes the
            # whole prefetch window. Only a real change (resize, color mode)
            # allocates a fresh generation, which also retires the pair.
            current = self.display_generation
            pair = self._zoom_generation_pair
            if zoomed:
                if pair is not None and pair[0] == current:
                    self.display_generation = pair[1]
                else:
                    self._zoom_generation_pair = (
                        current,
                        self._allocate_display_generation_locked(),
                    )
            elif pair is not None and pair[1] == current:
                self.display_generation = pair[0]
            else:
                self._allocate_display_generation_locked()
                self._zoom_generation_pair = None
        self.is_zoomed_changed.emit(zoomed)
        log.info("Zoom state changed to: %s", zoomed)

        # Stop work queued for the previous zoom state and restart at the new
        # resolution. The decode cache is left alone: frames for the other
        # state stay under their own generation and the byte budget evicts
        # them if memory gets tight.
        self.prefetcher.cancel_all()
        if self._loupe_decode_allowed():
            self.prefetcher.submit_task(
                self.current_index,
                self.prefetcher.generation,
                priority=True,
                quality="fast",
            )
            self.prefetcher.update_prefetch(self.current_index, radius_limit=2)

        # Force QML to reload the image at the new resolution
        if self.image_files and self.main_window:
            self.ui_refresh_generation += 1
            self.ui_state.currentImageSourceChanged.emit()
            self.main_window.update()  # Force repaint
        self._restart_quality_decode_timer()

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

    def _is_auxiliary_navigation_window(self, watched: QObject) -> bool:
        key = id(watched)
        return self._auxiliary_navigation_windows.get(key) is watched

    @Slot(QObject)
    def register_auxiliary_navigation_window(self, window: QObject) -> None:
        """Observe physical arrow press/release events from a QML tool window."""
        if window is None:
            return
        key = id(window)
        if self._auxiliary_navigation_windows.get(key) is window:
            return
        self._auxiliary_navigation_windows[key] = window
        window.installEventFilter(self)
        window.destroyed.connect(
            lambda _object=None, window_key=key: self._on_auxiliary_navigation_window_destroyed(
                window_key
            )
        )

    @Slot(QObject)
    def unregister_auxiliary_navigation_window(self, window: QObject) -> None:
        """Stop and detach navigation input from a QML tool window."""
        if window is None:
            return
        key = id(window)
        if self._auxiliary_navigation_windows.get(key) is not window:
            return
        self._end_navigation_hold(0, source=window)
        self._auxiliary_navigation_windows.pop(key, None)
        try:
            window.removeEventFilter(self)
        except RuntimeError:
            pass

    @Slot(QObject)
    def release_auxiliary_navigation_hold(self, window: QObject) -> None:
        """Release a hold when QML state disables a registered tool window."""
        if window is not None and self._is_auxiliary_navigation_window(window):
            self._end_navigation_hold(0, source=window)

    def _on_auxiliary_navigation_window_destroyed(self, window_key: int) -> None:
        window = self._auxiliary_navigation_windows.pop(window_key, None)
        if window is not None:
            self._end_navigation_hold(0, source=window)

    @Slot(int, int, str)
    def handle_key_from_histogram(self, key: int, modifiers: int, text: str):
        """Forward key presses from the histogram window through eventFilter."""
        from PySide6.QtGui import QKeyEvent

        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier(modifiers), text
        )
        self.eventFilter(self.main_window, event)

    @Slot(int, int, str)
    def handle_key_from_compact_editor(self, key: int, modifiers: int, text: str):
        """Forward navigation keys from the compact editor through eventFilter."""
        from PySide6.QtGui import QKeyEvent

        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier(modifiers), text
        )
        self.eventFilter(self.main_window, event)

    def eventFilter(self, watched, event) -> bool:
        is_main_window = watched == self.main_window
        is_auxiliary_window = self._is_auxiliary_navigation_window(watched)
        is_navigation_window = is_main_window or is_auxiliary_window

        if is_navigation_window and event.type() in {
            QEvent.Type.WindowDeactivate,
            QEvent.Type.Close,
            QEvent.Type.Hide,
            QEvent.Type.Destroy,
        }:
            self._end_navigation_hold(0, source=watched)
            if event.type() == QEvent.Type.WindowDeactivate:
                # A key released in another app never reaches us.
                self.ui_state.brushPanHeld = False
                self._set_local_overlay_held(False)

        if is_navigation_window and event.type() == QEvent.Type.KeyRelease:
            if not event.isAutoRepeat():
                if event.key() == Qt.Key_Control:
                    self._set_local_overlay_held(False)
                if event.key() == Qt.Key_Space and self.ui_state.brushPanHeld:
                    self.ui_state.brushPanHeld = False
                    self.stop_original_compare_preview()
                    return True
            if event.key() == Qt.Key_Space and not event.isAutoRepeat():
                # Deliberately not limited to the main window. The compact
                # editor and histogram forward unhandled presses through
                # handle_key_from_* as synthetic main-window events, so Space
                # pressed while one of them has focus starts compare — but the
                # physical release lands on THAT window. Ignoring it here left
                # compare active with no release able to end it: the loupe kept
                # showing the original, and every later press was a silent
                # no-op until the user navigated away.
                was_active = self._original_compare_active
                self.stop_original_compare_preview()
                return was_active
            if (
                event.key() in (Qt.Key_Left, Qt.Key_Right)
                and not event.isAutoRepeat()
                and not self._is_grid_view_active
            ):
                direction = 1 if event.key() == Qt.Key_Right else -1
                ended = self._end_navigation_hold(direction, source=watched)
                if ended or is_main_window:
                    return True

        if (
            is_auxiliary_window
            and event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key_Left, Qt.Key_Right)
        ):
            blocked_modifiers = event.modifiers() & (
                Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier | Qt.ShiftModifier
            )
            if not blocked_modifiers and bool(
                watched.property("handlesPlainArrowKeys")
            ):
                # The window uses plain Left/Right itself: deliver the press to
                # QML instead of starting image navigation.
                self._end_navigation_hold(0, source=watched)
                return False
            if not blocked_modifiers:
                navigation_enabled = bool(
                    watched.property("plainArrowNavigationEnabled")
                ) and not (
                    self._dialog_open or getattr(self.ui_state, "isCropping", False)
                )
                if navigation_enabled:
                    direction = 1 if event.key() == Qt.Key_Right else -1
                    # Repeated OS presses are liveness signals. The controller
                    # timer still owns the cadence, but calling begin again
                    # lets a physical hold recover if a resize, refresh, or
                    # focus transition cleared its internal state.
                    self._begin_navigation_hold(direction, source=watched)
                else:
                    self._end_navigation_hold(0, source=watched)
                # Physical auto-repeat is consumed after re-arming. The precise
                # controller timer owns repeat cadence while state is healthy.
                return True

        # Don't handle key events when a dialog is open
        if self._dialog_open:
            return False

        # While cropping, claim Esc at the ShortcutOverride stage so the QML
        # Shortcut system never matches it (an app-wide "Escape" Shortcut that
        # matches but fails to run its handler — e.g. an ambiguous match —
        # consumes the key and the KeyPress below never arrives). Accepting
        # the override re-delivers Esc as a normal KeyPress, which the crop
        # branch below handles deterministically. Rotate mode is excluded:
        # there Esc must reach the loupe's QML key handler to exit rotation.
        if (
            is_main_window
            and event.type() == QEvent.Type.ShortcutOverride
            and event.key() == Qt.Key_Escape
            and getattr(self.ui_state, "isCropping", False)
            and not getattr(self.ui_state, "isCropRotating", False)
        ):
            event.accept()
            return True

        if is_main_window and event.type() == QEvent.Type.KeyPress:
            if (
                self.ui_state.isBrushMode
                and not self._is_grid_view_active
                and self._handle_brush_mode_key(event)
            ):
                return True
            if event.key() == Qt.Key_Space and self._can_show_original_compare(event):
                if not event.isAutoRepeat():
                    self.start_original_compare_preview()
                return True

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
                if getattr(self.ui_state, "isCropRotating", False):
                    return False  # Let the loupe leave rotate mode first.
                self.cancel_crop_mode()
                return True  # Consume event, crop mode cancelled

            # When editing in full (expanded) mode, let QML handle Enter/Esc
            # and related keys. In compact mode, the editor is a separate
            # window so the main window keybinder should still run.
            if getattr(self.ui_state, "isEditorOpen", False) and getattr(
                self.ui_state, "isEditorExpanded", False
            ):
                return False

            # When cropping, let QML handle Enter/Return for crop execution
            if getattr(self.ui_state, "isCropping", False):
                key = event.key()
                if key in (Qt.Key_Enter, Qt.Key_Return):
                    return False  # Let QML handle crop execution
                if key in (Qt.Key_Left, Qt.Key_Right):
                    return False  # Crop UI owns arrows; never start navigation.

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

            if event.key() in (Qt.Key_Left, Qt.Key_Right):
                blocked_modifiers = event.modifiers() & (
                    Qt.ControlModifier
                    | Qt.AltModifier
                    | Qt.MetaModifier
                    | Qt.ShiftModifier
                )
                if not blocked_modifiers:
                    direction = 1 if event.key() == Qt.Key_Right else -1
                    if not event.isAutoRepeat():
                        # The Keybinder never sees arrows, so say so here: the
                        # navigation that follows is otherwise unexplained.
                        log.debug(
                            "Key %s -> navigate %s",
                            "Right" if direction > 0 else "Left",
                            "next" if direction > 0 else "previous",
                        )
                    # Do this for both the initial and auto-repeat presses. A
                    # healthy hold is a no-op in _begin_navigation_hold(); a
                    # hold whose state was cleared while the key remained down
                    # is re-established on the next native repeat event.
                    self._begin_navigation_hold(direction, source=watched)
                    return True

            handled = self.keybinder.handle_key_press(event)
            if handled:
                return True
        return super().eventFilter(watched, event)

    def _handle_brush_mode_key(self, event) -> bool:
        """Dodge & burn keys; they shadow the loupe bindings only in brush mode."""
        key = event.key()
        mods = event.modifiers() & (
            Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier | Qt.ShiftModifier
        )
        if key == Qt.Key_Control:
            if not event.isAutoRepeat():
                self._set_local_overlay_held(True)
            return False  # still a modifier for whatever comes next
        if key == Qt.Key_Space and not mods:
            # Space shows the original, as outside brush mode; Space+drag
            # also pans instead of painting.
            self.ui_state.brushPanHeld = True
            if not event.isAutoRepeat() and self._can_show_original_compare(event):
                self.start_original_compare_preview()
            return True
        if key == Qt.Key_Escape:
            if self.ui_state.isHistogramVisible:
                return False  # Esc closes the histogram first, as elsewhere
            self.exit_local_brush_mode()
            return True
        if key in (Qt.Key_BracketLeft, Qt.Key_BracketRight) and not mods:
            self.adjust_brush_size(1 if key == Qt.Key_BracketRight else -1)
            return True
        if key == Qt.Key_Z and mods == Qt.ControlModifier:
            self.undo_local_stroke()
            return True
        if key == Qt.Key_M and not mods:
            self.toggle_local_overlay()
            return True
        return False

    def _enter_prefetch_constraint(self, reason: str) -> Optional[int]:
        """Throttle speculative decoding while foreground work runs.

        Returns a token for _leave_prefetch_constraint(), or None when the
        prefetcher is gone/shutting down. Never raises: a governor problem must
        not be able to break a save or a preview render.
        """
        prefetcher = getattr(self, "prefetcher", None)
        if prefetcher is None:
            return None
        try:
            return prefetcher.enter_constrained_mode(reason)
        except Exception:
            log.debug("Prefetch governor enter failed (%s)", reason, exc_info=True)
            return None

    def _leave_prefetch_constraint(self, token: Optional[int]) -> None:
        """Release one constraint token; duplicate/stale tokens are ignored."""
        if token is None:
            return
        prefetcher = getattr(self, "prefetcher", None)
        if prefetcher is None:
            return
        try:
            prefetcher.leave_constrained_mode(token)
        except Exception:
            log.debug("Prefetch governor release failed", exc_info=True)

    def _clear_prefetch_constraint_reason(self, reason: str) -> None:
        """Fail-safe: drop every constraint token held for one reason."""
        prefetcher = getattr(self, "prefetcher", None)
        if prefetcher is None:
            return
        try:
            prefetcher.clear_constrained_reason(reason)
        except Exception:
            log.debug("Prefetch governor clear failed (%s)", reason, exc_info=True)

    def _release_startup_prefetch_constraint(self, reason: str) -> None:
        """Restore normal speculative capacity after the first loupe frame.

        Release policy is "first decoded frame reached Image.Ready", not "first
        frame swapped": Ready means the current image is decoded and handed to
        the scene graph, which is the point where the foreground decode this
        constraint protects is finished. The swap-based presentation tracking
        is navigation- and diagnostic-scoped and would not fire reliably for a
        cold startup frame.

        Idempotent: the token is dropped before it is released, so a repeated
        Ready callback, an empty folder, a grid switch, and shutdown can all
        call this in any order.
        """
        token = getattr(self, "_startup_prefetch_constraint_token", None)
        if token is None:
            return
        self._startup_prefetch_constraint_token = None
        # INFO, not debug: reason='failsafe-timeout' means the normal
        # first-frame-ready release never fired and startup was held to
        # _STARTUP_CONSTRAINT_FAILSAFE_MS. That is the single most useful
        # startup signal there is, and it was invisible in --debugcache runs.
        log.info(
            "[STARTUP] %.3fs prefetch constraint released (%s)",
            _startup_elapsed(),
            reason,
        )
        self._leave_prefetch_constraint(token)
        # Defensive: a token that was somehow duplicated cannot outlive startup.
        self._clear_prefetch_constraint_reason("startup")

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
                quality="fast",
            )
            return  # Skip adjacent prefetch when viewing a variant override

        self.prefetcher.update_prefetch(
            index, is_navigation=is_navigation, direction=direction
        )

    def _refresh_thumbnail_model_from_controller(self) -> None:
        """Refresh the grid model from ``image_files`` with paths primed first."""
        if not self._thumbnail_model:
            return

        self._grid_refreshes += 1
        path_resolver = getattr(self, "_path_resolver", None)

        if path_resolver:
            # QML thumbnail Images use asynchronous loading. endResetModel() can publish
            # new thumbnailSource URLs and queue provider requests on Qt's image-loading
            # thread before this method returns. Prime the resolver first so
            # startup/filter/reset requests can resolve paths immediately.
            # refresh_from_controller() builds image entries directly from img.path,
            # so this primed map already covers every final image row.
            # Removing this will cause grey placeholders that only resolve on scroll.
            path_resolver.update_from_paths(img.path for img in self.image_files)

        self._thumbnail_model.refresh_from_controller(
            self.image_files,
            metadata_map_fn=self._get_bulk_metadata_map,
        )

        self._grid_model_dirty = False

    def _current_image_path(self) -> Optional[Path]:
        """Return the current visible image path, if one is selected."""
        if self.image_files and 0 <= self.current_index < len(self.image_files):
            return self.image_files[self.current_index].path
        return None

    def _notify_thumbnail_current_path_changed(
        self, old_path: Optional[Path], *, force: bool = False
    ) -> None:
        """Invalidate current-image badges without rebuilding the grid model."""
        if self._thumbnail_model:
            self._thumbnail_model.notify_current_paths_changed(
                old_path, self._current_image_path(), force=force
            )

    def _index_for_saved_path(self, saved_path: Optional[str]) -> Optional[int]:
        """Resolve a persisted absolute or folder-relative image path."""
        if not saved_path:
            return None
        path = Path(saved_path)
        if not path.is_absolute():
            path = self.image_dir / path
        try:
            key = self._key(path)
        except (OSError, TypeError, ValueError):
            return None
        return self._path_to_index.get(key)

    def load(self, skip_thumbnail_refresh: bool = False):
        """Loads images, sidecar data, and starts services."""
        # The first load is part of startup and shares its clock. Every later
        # one (opening another folder) used to print the same "[STARTUP] 7.3s"
        # lines; those get their own prefix and time from when the load began.
        first_load = not getattr(self, "_startup_load_logged", False)
        self._startup_load_logged = True
        load_started = time.perf_counter()

        def milestone(text: str, *args) -> None:
            if first_load:
                log.info("[STARTUP] %.3fs " + text, _startup_elapsed(), *args)
            else:
                log.info(
                    "[LOAD] +%.3fs " + text, time.perf_counter() - load_started, *args
                )

        milestone("load() begin: %s", self.image_dir)
        self._cancel_paced_navigation()
        # Reset instrumentation for this load operation
        self._scan_count_variant = 0
        self._grid_refreshes = 0
        self._grid_model_dirty = True

        # Establish observation before the authoritative scan. Watchdog callbacks
        # only queue the controller's debounce timer, so changes reported while
        # load() is building the initial state cannot run against a half-loaded
        # controller; they reconcile after this call returns to the event loop.
        # If watching is unavailable the scan still provides a usable snapshot.
        try:
            watching = self.watcher.start()
        except Exception:
            log.exception("Watcher failed to start for %s", self.image_dir)
            watching = False
        if watching is False:
            log.warning(
                "Continuing without live file monitoring for %s", self.image_dir
            )
        self.ui_state.automaticMonitoringAvailable = bool(watching)

        # Publish the new list only after its restored current index and decode
        # state are ready. An early imageCountChanged used to make QML request
        # index 0 while the real saved index was still being restored.
        # Because watching is already active, this is also the authoritative
        # post-watch snapshot that closes the startup/switch observation gap.
        self.refresh_image_list(notify=False)  # Initial scan from disk
        provisional_current_path = self._current_image_path()
        if not self.image_files:
            self.current_index = 0
            # Nothing will ever be decoded or presented here, so the startup
            # constraint has no release event of its own.
            self._release_startup_prefetch_constraint("empty-folder")
        else:
            saved_path = self._startup_restore_path or self.sidecar.data.last_path
            saved_index = (
                self._startup_restore_index
                if self._startup_restore_index is not None
                else self.sidecar.data.last_index
            )
            path_index = self._index_for_saved_path(saved_path)
            self.current_index = (
                path_index
                if path_index is not None
                else max(0, min(saved_index, len(self.image_files) - 1))
            )
        # The model may have reset before the restored index was known (startup)
        # or before the new directory's loupe map existed (directory switch).
        self._notify_thumbnail_current_path_changed(
            provisional_current_path, force=True
        )
        milestone(
            "index restored: index=%d of %d", self.current_index, len(self.image_files)
        )
        self._startup_restore_path = None
        self._startup_restore_index = None
        with self.sidecar._state_lock:
            restored_stacks = self._restore_stacks_from_sidecar_identity()
            self.sidecar.data.stacks = restored_stacks
            # SidecarManager intentionally mutates its list in place after a
            # concurrent merge.  Keep an unsafe empty runtime projection
            # detached so such a save cannot re-expose historical indexes.
            self.stacks = (
                []
                if self._stack_identity_projection_blocked
                else self.sidecar.data.stacks
            )
        self.dataChanged.emit()  # Emit after stacks are loaded
        # Register this instance for crash recovery as soon as a folder is open.
        self._session_registry.update(
            self.image_dir,
            self.current_index,
            self._is_grid_view_active,
            self._current_image_path(),
        )
        self._remember_last_directory()

        # A pending resize timer cannot fire while requestImage() is blocking.
        # Commit its latest dimensions now so the first decode is display-sized.
        if self.resize_timer.isActive():
            self.resize_timer.stop()
        display_changed, first_display_report = self._commit_pending_display_size()
        if first_display_report:
            log.info("Display size now stable, enabling prefetch")
        if display_changed:
            self.prefetcher.cancel_all()
        if display_changed or first_display_report:
            self._apply_editor_preview_master_box()

        # Make decoding internally legal before prefetch, but defer the folder
        # readiness notification until the complete list/index state is emitted.
        folder_loaded_changed = False
        if not self._is_grid_view_active:
            folder_loaded_changed = self._set_folder_loaded(True, notify=False)
        self._do_prefetch(self.current_index)
        if not self.image_files:
            milestone("nothing to decode (no images in this folder)")
        elif not self._loupe_decode_allowed():
            milestone("nothing decoded yet (grid view)")
        else:
            milestone("first decode submitted")

        # Publish count, index, and source from one generation. imageCountChanged
        # may synchronously evaluate the source binding, so generation/index must
        # already be final before this call.
        self.sync_ui_state(image_count_changed=True)
        # sync_ui_state can evaluate the QML source binding inline, so this line
        # lands AFTER a blocking requestImage(). A long gap between it and the
        # previous line is the provider waiting on the first decode.
        milestone("UI state published")
        if folder_loaded_changed:
            self.ui_state.isFolderLoadedChanged.emit()

        if self._is_grid_view_active and not skip_thumbnail_refresh:

            # Ensure grid model is populated if starting in grid mode
            if (
                self._thumbnail_model
                and self._grid_model_dirty
                and self._thumbnail_model.rowCount() == 0
            ):
                self._refresh_thumbnail_model_from_controller()

        if self._is_grid_view_active:
            self._set_folder_loaded(True)
            # A loupe startup that ends up in the grid has no first frame.
            self._release_startup_prefetch_constraint("grid-startup")

        if not self._is_grid_view_active:
            self._maybe_decode_current_image("startup-loupe")
            self._restart_quality_decode_timer()
            self._maybe_show_turbo_fallback_warning()

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
                if key in self._saving_path_keys:
                    # Our own atomic save is replacing this file right now. The
                    # save's own completion handler refreshes what is needed.
                    if _debug_mode:
                        log.debug(
                            "Suppressing watcher refresh for in-flight save: %s",
                            path,
                        )
                    return
                if self._is_pending_delete_path(key, p, now):
                    self._index_rescan_needed = True
                    if _debug_mode:
                        log.debug(
                            "Suppressing watcher refresh for in-flight delete: %s",
                            path,
                        )
                    return
                expiry = self._suppressed_paths.get(key)
                if expiry:
                    if now < expiry:
                        # An external tool (Photoshop, GIMP, ...) can overwrite
                        # this path during our own suppression window, right
                        # after FastStack's save already completed. Compare
                        # against the fingerprint recorded the last time we
                        # invalidated the decode cache for this path: if the
                        # file no longer matches, this is a genuine external
                        # write and must not be silently dropped, or the
                        # external content would stay absent from the
                        # display/cache until some unrelated later event.
                        current_fingerprint = self._file_state_fingerprint(p)
                        with self._decode_invalidation_lock:
                            epoch_entry = self._decode_invalidation_epochs.get(key)
                        expected_fingerprint = epoch_entry[1] if epoch_entry else None
                        if (
                            current_fingerprint is None
                            or expected_fingerprint is None
                            or current_fingerprint == expected_fingerprint
                        ):
                            if _debug_mode:
                                log.debug(
                                    "Suppressing watcher refresh within own-write window "
                                    "(recent save/delete/restore): %s",
                                    path,
                                )
                            return
                        if _debug_mode:
                            log.debug(
                                "Not suppressing watcher refresh for %s: "
                                "on-disk state changed during suppression "
                                "window (external write)",
                                path,
                            )
                    else:
                        # Cleanup expired entry
                        del self._suppressed_paths[key]
            with self._watcher_changed_lock:
                self._watcher_changed_paths.add(p)
        else:
            # Caller didn't say what changed; the debounced refresh must fall
            # back to a global cache invalidation. None marks "unknown".
            with self._watcher_changed_lock:
                self._watcher_changed_paths.add(None)

        try:
            QMetaObject.invokeMethod(
                self, "_start_watcher_debounce_timer", Qt.QueuedConnection
            )
        except RuntimeError:
            pass  # QObject already deleted during shutdown

    def _suppress_watcher_paths(self, *paths: Optional[Path], ttl: float = 3.0) -> None:
        """Temporarily ignore watcher refreshes for specific paths."""
        now = time.monotonic()
        with self._suppressed_paths_lock:
            for path in paths:
                if path is None:
                    continue
                try:
                    key = self._key(path)
                except (OSError, TypeError, ValueError):
                    continue
                if key:
                    self._suppressed_paths[key] = now + ttl

    def _register_pending_delete_paths(self, job_id: int, work_items) -> None:
        """Suppress watcher refreshes for files an in-flight delete job owns.

        Must run before the worker starts I/O: watchdog events for the first
        move can arrive while later files in the same batch are still on disk,
        and a rescan would reinsert rows the worker is about to remove.
        """
        with self._suppressed_paths_lock:
            for item in work_items:
                for path, identity in (
                    (item.jpg_path, item.jpg_identity),
                    (item.raw_path, item.raw_identity),
                ):
                    if path is None:
                        continue
                    try:
                        key = self._key(path)
                    except (OSError, TypeError, ValueError):
                        continue
                    if not key:
                        continue
                    entry = self._pending_delete_identities.get(key)
                    if entry is None:
                        self._pending_delete_identities[key] = {
                            "identity": identity,
                            "owners": {job_id},
                        }
                    else:
                        entry["owners"].add(job_id)

    def _release_pending_delete_paths(self, job_id: int, work_items) -> None:
        """Drop the pending-delete suppression registered for *work_items*."""
        with self._suppressed_paths_lock:
            for item in work_items:
                for path in (item.jpg_path, item.raw_path):
                    if path is None:
                        continue
                    try:
                        key = self._key(path)
                    except (OSError, TypeError, ValueError):
                        continue
                    entry = self._pending_delete_identities.get(key)
                    if entry is None:
                        continue
                    entry["owners"].discard(job_id)
                    if not entry["owners"]:
                        del self._pending_delete_identities[key]

    def _is_pending_delete_path(self, key: str, path: Path, _now: float) -> bool:
        """Return True when *path* is a file our own delete worker is moving.

        Caller must hold ``_suppressed_paths_lock``. A path still registered but
        whose on-disk file no longer matches the identity deletion was
        authorized against is treated as an external replacement, so the
        watcher event is delivered normally.
        """
        entry = self._pending_delete_identities.get(key)
        if entry is None:
            return False
        try:
            current = capture_file_identity(path)
        except OSError:
            return True
        # Gone means our move already happened; unchanged means it is still
        # waiting its turn in the same job.
        return not current.exists or current == entry["identity"]

    def _invalidate_inflight_index_scans(self) -> None:
        """Discard any background scan snapshot taken before an in-memory
        image-list mutation (delete/recycle/restore).

        A walk captured before the mutation would otherwise publish afterwards
        and resurrect (or re-hide) rows. Bumping the epoch makes the stale
        completion take the discard path, and requesting a rescan there
        replaces it with a snapshot of the post-mutation directory.
        """
        self._index_scan_epoch += 1
        if self._index_scan_inflight:
            self._index_rescan_needed = True

    def _drop_pending_delete_images(self, images):
        """Remove scan rows for files an in-flight delete job still owns."""
        if not self._pending_delete_identities:
            return images
        now = time.monotonic()
        kept = []
        with self._suppressed_paths_lock:
            for image in images:
                try:
                    key = self._key(image.path)
                except (OSError, TypeError, ValueError):
                    kept.append(image)
                    continue
                if key and self._is_pending_delete_path(key, image.path, now):
                    continue
                kept.append(image)
        return kept

    @Slot()
    def _start_watcher_debounce_timer(self) -> None:
        """Non-overloaded slot to restart the watcher debounce timer.

        QTimer.start is overloaded (start() / start(int)), which can cause
        ambiguity with QMetaObject.invokeMethod in some PySide versions.
        """
        self._watcher_debounce_timer.start()

    def refresh_image_list(self, *, notify: bool = True):
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
        pending_snapshot = getattr(self, "_pending_directory_snapshot", None)
        if pending_snapshot is not None and pending_snapshot[0] == self._key(
            self.image_dir
        ):
            images, variant_map = pending_snapshot[1]
            self._pending_directory_snapshot = None
        else:
            try:
                images, variant_map = find_images_with_variants(self.image_dir)
            except OSError as exc:
                log.error(
                    "Directory scan failed; preserving the last known image list for %s: %s",
                    self.image_dir,
                    exc,
                )
                if notify:
                    self.update_status_message(
                        f"Could not refresh folder: {exc}",
                        timeout=10000,
                        color="#FFD54F",
                    )
                return False
        # This synchronous snapshot is newer than any watcher scan already in
        # flight. Invalidate those results before publishing it so an older
        # worker cannot overwrite the refreshed list when it finishes later.
        self._index_scan_epoch += 1
        self._all_images = images
        self._variant_map = variant_map
        self._apply_filter_to_cached_list(notify=notify)

        # Mark model as dirty since the underlying directory was rescanned
        self._grid_model_dirty = True

        # Refresh thumbnail model if it exists (for external file changes or startup)
        if self._thumbnail_model and self._is_grid_view_active:
            self._refresh_thumbnail_model_from_controller()
        return True

    @Slot()
    def _on_watcher_refresh(self):
        """Watcher debounce handler: kick off a (throttled, background) rescan.

        The actual directory walk runs off the GUI thread via
        _maybe_start_index_scan()/_start_index_scan() so that a burst of
        filesystem churn (e.g. an external process rewriting many files)
        cannot repeatedly block the UI thread. Completion is handled by
        _on_index_scan_ready().
        """
        self._maybe_start_index_scan()

    def _maybe_start_index_scan(self) -> None:
        """Starts a background directory scan, subject to throttling.

        At most one scan runs at a time (single-worker executor), and scans
        are capped to _index_scan_min_interval_s apart no matter how often
        the watcher fires — otherwise sustained external file churn (a
        lingering save/export queue, a sync client, etc.) could keep the
        directory walk running back-to-back indefinitely.
        """
        if self._index_scan_inflight:
            # A scan is already running; make sure we scan again once it
            # finishes so this change isn't lost.
            self._index_rescan_needed = True
            return

        now = time.monotonic()
        elapsed = now - self._last_index_scan_time
        if elapsed < self._index_scan_min_interval_s:
            self._index_rescan_needed = True
            remaining_ms = int((self._index_scan_min_interval_s - elapsed) * 1000) + 1
            if not self._index_throttle_timer.isActive():
                self._index_throttle_timer.start(remaining_ms)
            return

        self._start_index_scan()

    def _start_index_scan(self) -> None:
        """Submits the directory walk to the background index executor."""
        if getattr(self, "_shutting_down", False):
            return

        clear_raw_count_cache()
        self._index_scan_inflight = True
        self._index_rescan_needed = False
        self._last_index_scan_time = time.monotonic()
        self._scan_count_variant += 1

        epoch = self._index_scan_epoch
        try:
            fut = self._index_executor.submit(find_images_with_variants, self.image_dir)
            fut.add_done_callback(functools.partial(self._on_index_scan_done, epoch))
        except RuntimeError:
            # Executor already shut down; leave no inflight flag behind or the
            # next watcher tick would think a scan is still running.
            log.warning("Index executor failed (shutting down?)")
            self._index_scan_inflight = False
            return

    def _on_index_scan_done(self, epoch: int, fut: concurrent.futures.Future) -> None:
        """Runs on the index executor's worker thread — no QObject access here."""
        error = None
        try:
            result = fut.result()
        except Exception as e:
            log.error("Background directory scan failed: %s", e)
            result = None
            error = str(e)
        self._indexScanReady.emit({"epoch": epoch, "result": result, "error": error})

    @Slot(object)
    def _on_index_scan_ready(self, payload) -> None:
        """GUI-thread continuation of a watcher-triggered background scan.

        The list rebuild repairs current_index; this also primes decoding and
        publishes the final count, index, and source together.
        """
        background_result = payload.get("background", True)
        if getattr(self, "_shutting_down", False):
            if background_result:
                self._index_scan_inflight = False
            return

        if background_result:
            self._index_scan_inflight = False

        result = payload.get("result")
        # A scan submitted for a directory we have since left describes files
        # that are not in self.image_dir any more — applying it would repopulate
        # the list with the old directory's images.
        if result is None or payload.get("epoch") != self._index_scan_epoch:
            error = payload.get("error")
            if error and payload.get("epoch") == self._index_scan_epoch:
                self.update_status_message(
                    f"Could not refresh folder: {error}",
                    timeout=10000,
                    color="#FFD54F",
                )
            # A newer synchronous refresh made this result stale, but watcher
            # paths collected for cache invalidation and Photoshop handoff
            # handling still need a current-generation scan to consume them.
            with self._watcher_changed_lock:
                if self._watcher_changed_paths:
                    self._index_rescan_needed = True
            if self._index_rescan_needed:
                self._maybe_start_index_scan()
            return

        images, variant_map = result
        # A walk that overlapped a delete job can still list files the worker
        # has not moved yet; they were already removed from the UI.
        images = self._drop_pending_delete_images(images)

        with self._watcher_changed_lock:
            changed_paths = self._watcher_changed_paths
            self._watcher_changed_paths = set()

        # A rescan that found nothing must not disturb navigation. The debounce
        # fires for events we filtered out, and coalesced duplicates arrive with
        # an already-drained change set — but _begin_direct_image_transition()
        # below zeroes the held arrow direction, so a no-op rescan mid-burst
        # costs a ~200 ms cold demand decode and drains the prefetch buffer.
        if (
            not changed_paths
            and images == self._all_images
            and variant_map == self._variant_map
        ):
            log.debug("Index rescan found no changes; leaving navigation untouched")
            if self._index_rescan_needed:
                self._maybe_start_index_scan()
            return

        self._begin_direct_image_transition("watcher rebuilt the image list")

        # Remember which image we were viewing so we can stay on it. Read
        # now (not at scan-submission time) since the user may have kept
        # navigating while the scan ran in the background.
        preserved_path = None
        if self.image_files and 0 <= self.current_index < len(self.image_files):
            preserved_path = self.image_files[self.current_index].path

        photoshop_overwrites = self._consume_photoshop_overwrites(changed_paths)
        photoshop_batch_additions = self._persist_photoshop_overwrite_batches(
            photoshop_overwrites
        )
        for path in photoshop_overwrites:
            log.info("Detected completed image editor overwrite: %s", path)

        # Stack ranges are indices into image_files; capture their identities
        # before the swap so an inserted/removed earlier row cannot shift them
        # onto different images (launching the wrong files).
        pre_scan_stack_paths = self._resolve_ranges_to_paths(self.stacks)
        pre_scan_stack_start = (
            self.image_files[self.stack_start_index].path
            if self.stack_start_index is not None
            and 0 <= self.stack_start_index < len(self.image_files)
            else None
        )
        pre_scan_stack_end = (
            self.image_files[self.stack_end_index].path
            if self.stack_end_index is not None
            and 0 <= self.stack_end_index < len(self.image_files)
            else None
        )

        self._all_images = images
        self._variant_map = variant_map
        self._apply_filter_to_cached_list(notify=False)
        stack_ranges_changed = False
        if self._filter_enabled and self._filter_identity_snapshot is not None:
            # Filtered view: the snapshot holds the full semantic groups
            # (including hidden members) and is kept current by every visible
            # stack edit. Re-project it, exactly as apply_filter() does.
            previous_stacks = self.stacks
            snapshot = self._filter_identity_snapshot
            self.stacks = self._rebuild_ranges_from_paths(snapshot["stack_groups"])
            self.stack_start_index = (
                self._path_to_index.get(self._key(snapshot["stack_start"]))
                if snapshot["stack_start"]
                else None
            )
            self.stack_end_index = (
                self._path_to_index.get(self._key(snapshot["stack_end"]))
                if snapshot["stack_end"]
                else None
            )
            stack_ranges_changed = self.stacks != previous_stacks
        elif not self.sidecar.data.stack_paths:
            # No persisted identities (legacy sidecar): remap by path.
            previous_stacks = self.stacks
            # Distinct stacks may be adjacent, so do not merge them.
            self.stacks = self._rebuild_ranges_from_paths(pre_scan_stack_paths)
            stack_ranges_changed = self.stacks != previous_stacks
            if stack_ranges_changed and not self._filter_enabled:
                # Persist the remapped numeric ranges, or the next load would
                # restore the stale indices. Deliberately no stack_paths here:
                # identity restoration rejects adjacent distinct groups.
                with self.sidecar._state_lock:
                    self.sidecar.data.stacks = [s[:] for s in self.stacks]
                self._persist_sidecar()
        if not self._filter_enabled:
            self.stack_start_index = (
                self._path_to_index.get(self._key(pre_scan_stack_start))
                if pre_scan_stack_start
                else None
            )
            self.stack_end_index = (
                self._path_to_index.get(self._key(pre_scan_stack_end))
                if pre_scan_stack_end
                else None
            )
        if not self._filter_enabled and self.sidecar.data.stack_paths:
            previous_stacks = self.stacks
            with self.sidecar._state_lock:
                restored_stacks = self._restore_stacks_from_sidecar_identity()
                self.sidecar.data.stacks = restored_stacks
                self.stacks = (
                    []
                    if self._stack_identity_projection_blocked
                    else self.sidecar.data.stacks
                )
            stack_ranges_changed = self.stacks != previous_stacks
        self._grid_model_dirty = True
        if self._thumbnail_model and self._is_grid_view_active:
            self._refresh_thumbnail_model_from_controller()
        provisional_current_path = self._current_image_path()

        # Bust decode caches so modified-on-disk files are re-decoded.
        # Invalidate only the files the watcher reported (a save produces a
        # handful of events; the other ~hundreds of cached decodes are still
        # valid). Fall back to the global display-generation bump when the
        # change set is unattributable (None sentinel) or implausibly large.
        # An EMPTY drain means a coalesced/duplicate debounce fire whose
        # changes were already drained by an earlier fire — invalidate
        # nothing, or every duplicate fire would re-decode the whole window.
        if changed_paths:
            if None not in changed_paths and len(changed_paths) <= 100:
                for p in changed_paths:
                    self._invalidate_decoded_path(p)
            else:
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

        if self.view_override_path and self.image_files:
            current_group_key = get_group_key_for_path(
                self.image_files[self.current_index].path,
                self._variant_map,
            )
            current_group = (
                self._variant_map.get(current_group_key)
                if current_group_key is not None
                else None
            )
            override_norm = norm_path(Path(self.view_override_path))
            if current_group is None or not any(
                norm_path(info.path) == override_norm
                for info in current_group.all_files
            ):
                self._clear_variant_override()
        self._notify_thumbnail_current_path_changed(
            provisional_current_path, force=True
        )

        self._do_prefetch(self.current_index)
        self.sync_ui_state(image_count_changed=True)
        self._restart_quality_decode_timer()
        if photoshop_batch_additions or stack_ranges_changed:
            self.dataChanged.emit()
        if stack_ranges_changed:
            self.ui_state.stackSummaryChanged.emit()
        if photoshop_batch_additions:
            self.update_status_message(
                "Image editor edit reloaded and added to batch",
                timeout=5000,
            )

        # More changes arrived (or were throttled) during this scan — go again.
        if self._index_rescan_needed:
            self._maybe_start_index_scan()

    def _apply_filter_to_cached_list(self, *, notify: bool = True):
        """Applies current filter to cached image list without disk I/O."""
        current_path = self._current_image_path()
        previous_index = self.current_index
        old_batch_start_path = (
            self.image_files[self.batch_start_index].path
            if self.batch_start_index is not None
            and 0 <= self.batch_start_index < len(self.image_files)
            else None
        )
        self.image_files = self._filtered_sorted_copy(self.sort_mode)
        self._rebuild_path_to_index()
        # A rescan may shrink/reorder the list while delete jobs are still
        # moving files. Repair the selection before any signal can expose the
        # new list to QML, even when the caller's preferred target is absent.
        self._restore_filter_current_image(current_path, previous_index)
        self._restore_batches_from_sidecar_flags()
        if old_batch_start_path:
            self.batch_start_index = self._path_to_index.get(
                self._key(old_batch_start_path)
            )
        else:
            self.batch_start_index = None
        self.prefetcher.set_image_files(self.image_files)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        if notify:
            self.ui_state.imageCountChanged.emit()

    def _reapply_flag_filter_after_metadata_change(
        self, flag_attr: str, preserved_path: Optional[Path]
    ) -> bool:
        """Rebuild the visible list if an active flag filter depends on ``flag_attr``."""
        if not (
            self._filter_enabled
            and self._filter_flags
            and flag_attr in self._filter_flags
        ):
            return False

        self._begin_direct_image_transition("active flag filter rebuilt the list")
        old_index = self.current_index
        old_stack_paths = self._resolve_ranges_to_paths(self.stacks)
        old_stack_start_path = (
            self.image_files[self.stack_start_index].path
            if self.stack_start_index is not None
            and 0 <= self.stack_start_index < len(self.image_files)
            else None
        )
        old_stack_end_path = (
            self.image_files[self.stack_end_index].path
            if self.stack_end_index is not None
            and 0 <= self.stack_end_index < len(self.image_files)
            else None
        )

        self._apply_filter_to_cached_list(notify=False)
        self._bump_display_generation()

        self.stacks = self._merge_stack_ranges(
            self._rebuild_ranges_from_paths(old_stack_paths)
        )
        self.stack_start_index = (
            self._path_to_index.get(self._key(old_stack_start_path))
            if old_stack_start_path
            else None
        )
        self.stack_end_index = (
            self._path_to_index.get(self._key(old_stack_end_path))
            if old_stack_end_path
            else None
        )

        if self.image_files and preserved_path:
            target_key = self._key(preserved_path)
            new_idx = self._path_to_index.get(target_key)
            if new_idx is not None:
                self.current_index = new_idx
            else:
                self._clear_variant_override()
                self.current_index = min(old_index, len(self.image_files) - 1)
        else:
            self._clear_variant_override()
            self.current_index = 0

        if self._is_grid_view_active:
            self._thumbnail_prefetcher.cancel_all()

        if self._is_grid_view_active and self._thumbnail_model:
            self._refresh_thumbnail_model_from_controller()
        else:
            self._grid_model_dirty = True

        self._do_prefetch(self.current_index)
        self._restart_quality_decode_timer()
        return True

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
        return self._rebuild_ranges_from_paths_map(groups, self._path_to_index)

    def _rebuild_ranges_from_paths_map(
        self,
        groups: List[List[Path]],
        path_to_index: dict[str, int],
    ) -> List[List[int]]:
        """Rebuild path groups against an explicit ordered path mapping."""
        ranges = []
        for paths in groups:
            indices = []
            for p in paths:
                idx = path_to_index.get(self._key(p))
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

    @staticmethod
    def _ranges_from_indices(indices: List[int]) -> List[List[int]]:
        """Convert sorted or unsorted indices into normalized contiguous ranges."""
        if not indices:
            return []

        unique_indices = sorted(set(indices))
        ranges: List[List[int]] = []
        run_start = unique_indices[0]
        run_end = unique_indices[0]
        for idx in unique_indices[1:]:
            if idx == run_end + 1:
                run_end = idx
            else:
                ranges.append([run_start, run_end])
                run_start = idx
                run_end = idx
        ranges.append([run_start, run_end])
        return ranges

    def _batch_metadata_key(self, path: Path) -> str:
        """Return the sidecar key used for persistent batch membership."""
        try:
            return self.sidecar.metadata_key_for_path(path)
        except (OSError, TypeError, ValueError):
            log.debug("Could not build batch metadata key for %s", path)
            return ""

    def _current_batch_metadata_keys(self) -> set[str]:
        """Return sidecar keys for the current runtime batch selection."""
        keys: set[str] = set()
        max_index = len(self.image_files)
        for idx in self._get_batch_indices():
            if not 0 <= idx < max_index:
                continue
            key = self._batch_metadata_key(self.image_files[idx].path)
            if key:
                keys.add(key)
        return keys

    def _persist_batch_flags(
        self, *, force: bool = False, background: bool = False
    ) -> bool:
        """Write the current runtime batch selection to per-entry sidecar flags.

        The sidecar is written only when a batch flag changed. ``force`` writes
        it regardless, for a caller with other metadata changes that should go
        out in the same write. ``background`` hands the write to the
        session-write worker and returns True at once; a write that fails
        there is retried synchronously and reported the usual way.
        """
        active_keys = self._current_batch_metadata_keys()
        # Only touch entries in the current (possibly filtered) view. active_keys
        # is derived from self.image_files, so iterating _all_images here would
        # write batch=False to hidden entries and drop their batch membership.
        images_to_update = list(self.image_files)

        seen_keys: set[str] = set()
        changed = False
        for img in images_to_update:
            key = self._batch_metadata_key(img.path)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)

            should_be_batched = key in active_keys
            meta = self.sidecar.get_metadata(
                img.path,
                create=should_be_batched,
                migrate=should_be_batched,
            )
            if meta is None:
                continue
            if bool(getattr(meta, "batch", False)) != should_be_batched:
                meta.batch = should_be_batched
                changed = True

        if not (changed or force):
            return True
        if background:
            self._queue_sidecar_write()
            return True
        return self._persist_sidecar()

    def _queue_sidecar_write(self, manager: Optional[SidecarManager] = None) -> None:
        """Write the folder sidecar on the session-write worker.

        For metadata changes made in the middle of an interaction (a slider
        drag, an auto-adjust keystroke, a save finishing while the user is
        already on the next image), where a synchronous write would hold the
        GUI thread for a serialise, a write and two fsyncs.
        """
        if manager is None:
            manager = self.sidecar
        try:
            prepared = manager.prepare_background_write()
        except Exception as e:
            log.warning("Could not snapshot sidecar for a background write: %s", e)
            _ = self._persist_sidecar(manager)
            return
        self._queue_session_write(manager, prepared, None)

    def _restore_batches_from_sidecar_flags(self) -> None:
        """Rebuild runtime batch ranges from sidecar flags for image_files order."""
        batched_indices = []
        for idx, img in enumerate(self.image_files):
            meta = self.sidecar.get_metadata(
                img.path,
                create=False,
                migrate=False,
            )
            if meta is not None and bool(getattr(meta, "batch", False)):
                batched_indices.append(idx)

        self.batches = self._ranges_from_indices(batched_indices)
        _ = self._finalize_batch_state(
            persist=False,
            emit=False,
            sync=False,
            notify_model=False,
        )

    def _finalize_batch_state(
        self,
        *,
        persist: bool = True,
        emit: bool = True,
        sync: bool = True,
        notify_model: bool = True,
        reapply_filter: bool = False,
        preserved_path: Optional[Path] = None,
        force_persist: bool = False,
        background_persist: bool = False,
    ) -> bool:
        """Normalize batch state and fan out persistence/UI invalidations.

        ``force_persist`` writes the sidecar even when no batch flag changed
        and ``background_persist`` writes it off the GUI thread (see
        ``_persist_batch_flags``).
        """
        self._normalize_batches()
        self._invalidate_batch_cache()
        self._update_filter_identity_snapshot("batch")
        persisted = not persist or self._persist_batch_flags(
            force=force_persist, background=background_persist
        )
        filter_reapplied = False
        if reapply_filter and persist:
            if (
                preserved_path is None
                and self.image_files
                and 0 <= self.current_index < len(self.image_files)
            ):
                preserved_path = self.image_files[self.current_index].path
            filter_reapplied = self._reapply_flag_filter_after_metadata_change(
                "batch", preserved_path
            )
        self._metadata_cache_index = (-1, -1)
        if emit:
            self.dataChanged.emit()
        if (
            notify_model
            and not filter_reapplied
            and getattr(self, "_thumbnail_model", None)
        ):
            self._thumbnail_model.notify_batch_state_changed()
        if sync:
            self.sync_ui_state(image_count_changed=filter_reapplied)
        return persisted

    @staticmethod
    def _shift_ranges_after_insert(
        ranges: List[List[int]], insert_index: int
    ) -> List[List[int]]:
        """Shift index ranges after inserting one unselected image."""
        shifted: List[List[int]] = []
        for start, end in ranges:
            if end < insert_index:
                shifted.append([start, end])
            elif start >= insert_index:
                shifted.append([start + 1, end + 1])
            else:
                shifted.append([start, insert_index - 1])
                shifted.append([insert_index + 1, end + 1])
        return [r for r in shifted if r[0] <= r[1]]

    @staticmethod
    def _duplicate_path_for(source: Path) -> Path:
        """Return an unused duplicate filename next to source."""
        first = source.with_name(f"{source.stem}_duplicate{source.suffix}")
        if not first.exists():
            return first

        counter = 2
        while True:
            candidate = source.with_name(
                f"{source.stem}_duplicate{counter}{source.suffix}"
            )
            if not candidate.exists():
                return candidate
            counter += 1

    def _reindex_after_save(self, saved_path: str) -> bool:
        """Re-derive current_index to point at *saved_path* after a save.

        Backup files are excluded from the visible image list (the indexer
        skips ``-backup`` stems), so the list itself is unchanged.  We just
        need to make sure current_index still points at the right entry.

        Returns True if saved_path was found.
        """
        cp = Path(saved_path)
        old_current_path = self._current_image_path()

        # Fast path: normalized key lookup (must match _rebuild_path_to_index format)
        path_key = self._key(cp)
        new_idx = self._path_to_index.get(path_key)
        if new_idx is not None:
            self.current_index = new_idx
            self._notify_thumbnail_current_path_changed(old_current_path)
            return True

        # Name-based fallback (drive letter / symlink mismatches)
        target_name = cp.name
        for i, img_file in enumerate(self.image_files):
            if img_file.path.name == target_name:
                self.current_index = i
                self._notify_thumbnail_current_path_changed(old_current_path)
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

    def sync_ui_state(self, *, image_count_changed: bool = False):
        """Forces the UI to update by emitting all state change signals.

        Essential signals (currentIndexChanged, currentImageSourceChanged) are emitted
        immediately. Non-essential signals (highlightStateChanged, metadataChanged) are
        debounced to reduce overhead during rapid navigation.
        """
        if self.debug_cache_trace:
            _t_start = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} sync_ui_state: START gen={self.ui_refresh_generation + 1}"
            )

        self.ui_refresh_generation += 1
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        # Stack paint availability follows the current image on every path
        # that changes it (startup restore, rescans, deletes); a no-op when
        # the image did not change.
        self._refresh_stack_inputs()

        # Keep the editor preview reachable at the new generation so callers
        # that bump ui_refresh_generation for non-image reasons (batch toggle,
        # metadata refresh, etc.) don't cause the provider to drop a valid
        # edited preview in favour of the stale decode cache.
        if (
            self._last_rendered_preview is not None
            and self._last_rendered_preview_index == self.current_index
            and self._last_rendered_preview_session_key
            == self._get_current_live_preview_session_key()
        ):
            self._last_rendered_preview_gen = self.ui_refresh_generation
        if (
            self._original_compare_preview is not None
            and self._original_compare_index == self.current_index
            and self._original_compare_session_key
            == self._get_current_original_compare_session_key()
        ):
            self._original_compare_gen = self.ui_refresh_generation

        # Essential signals - emit immediately for responsive image display
        if image_count_changed:
            self.ui_state.imageCountChanged.emit()
        self.ui_state.currentIndexChanged.emit()
        self.ui_state.currentImageSourceChanged.emit()

        # Debounce non-essential signals during rapid navigation
        # These will emit once after user stops navigating (150ms)
        self._metadata_debounce_timer.start()

        # Coalesced crash-recovery persistence (~1s after navigation settles)
        self._session_save_timer.start()

        if self.debug_cache_trace:
            _t_end = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_end*1000:.3f} sync_ui_state: DONE signals emitted, total={(_t_end - _t_start)*1000:.2f}ms"
            )

        log.debug(
            "UI State Synced: Index=%d, Count=%d",
            self.ui_state.currentIndex,
            self.ui_state.imageCount,
        )

    def _persist_session_state(self):
        """Flush the current position for crash/reboot recovery.

        Writes the current path and index fallback to the folder sidecar (so
        they survive an unexpected reboot, not just a clean shutdown) and
        refreshes this instance's session record. Best-effort: a transient I/O
        error must never disrupt navigation.
        """
        current_path = self._current_image_path()
        manager = self.sidecar
        prepared = None
        try:
            with manager._state_lock:
                manager.data.sort_mode = self.sort_mode
            manager.set_last_position(self.current_index, current_path)
            # Snapshot only; the write happens on the session-write worker.
            prepared = manager.prepare_background_write()
        except Exception as e:
            log.warning("Error persisting sidecar position: %s", e)
        self._queue_session_write(
            manager,
            prepared,
            (
                self.image_dir,
                self.current_index,
                self._is_grid_view_active,
                current_path,
            ),
        )
        self._remember_last_directory()

    def _queue_session_write(self, manager, prepared, record_args) -> None:
        """Run a sidecar snapshot write and/or session-record update off-thread.

        ``prepared`` is a ``SidecarManager.prepare_background_write()`` result
        (or None to skip the sidecar). ``record_args`` are the
        ``SessionRegistry.update`` arguments (or None). If the worker is
        unavailable, the work runs inline, except during shutdown, which does
        its own final save and removes the session record.
        """
        registry = self._session_registry
        signal = self._sessionSidecarWriteFinished

        def _worker():
            outcome = None
            if manager is not None and prepared is not None:
                outcome = manager.write_prepared(*prepared)
            if record_args is not None:
                try:
                    registry.update(*record_args)
                except Exception as e:
                    log.warning("Error updating session record: %s", e)
            return outcome

        def _done(fut):
            if fut.cancelled():
                return  # superseded by the shutdown save
            try:
                outcome = fut.result()
            except Exception:
                log.exception("Background session write failed")
                outcome = "failed"
            if outcome is None:
                return
            try:
                signal.emit(manager, outcome)
            except RuntimeError:
                pass  # controller already destroyed during shutdown

        executor = getattr(self, "_session_write_executor", None)
        try:
            if executor is None:
                raise RuntimeError("session write executor unavailable")
            future = executor.submit(_worker)
        except RuntimeError:
            if self._shutting_down:
                # The shutdown path saves the sidecar itself, and a session
                # record written now could outlive SessionRegistry.close().
                return
            # Executor unexpectedly unavailable: write inline, as before.
            outcome = _worker()
            if outcome is not None and outcome not in (
                "written",
                "unchanged",
                "superseded",
            ):
                self._on_session_sidecar_write_finished(manager, outcome)
            return
        future.add_done_callback(_done)

    @Slot(object, str)
    def _on_session_sidecar_write_finished(self, manager, outcome: str) -> None:
        """Main thread: fall back to a full merge-save when the fast write could not."""
        if outcome in ("written", "unchanged", "superseded"):
            return
        if self._shutting_down:
            return  # the shutdown path does its own final synchronous save
        log.info(
            "Background sidecar write returned %s; saving synchronously",
            outcome,
        )
        try:
            _ = self._persist_sidecar(manager)
        except Exception as e:
            log.warning("Error persisting sidecar position: %s", e)

    def _remember_last_directory(self):
        """Persist the open folder so a no-argument launch resumes here.

        Unlike the per-instance session record (which is removed on clean
        exit), this lives in the INI config and survives a clean shutdown, so
        it is the fallback when there is no crash to recover from. Only writes
        when the folder actually changes to avoid needless INI churn.
        """
        try:
            current = str(Path(self.image_dir).expanduser().resolve())
            if current and current != self._last_persisted_dir:
                config.set("core", "last_directory", current)
                config.save()
                self._last_persisted_dir = current
        except Exception as e:
            log.warning("Error persisting last directory: %s", e)

    def _emit_debounced_metadata_signals(self):
        """Emit deferred metadata/highlight signals after navigation stops."""
        if not self.ui_state:
            return

        if self.debug_cache_trace:
            _t_start = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} _emit_debounced_metadata_signals: emitting deferred signals"
            )

        self.ui_state.highlightStateChanged.emit()
        self.ui_state.metadataChanged.emit()
        self.ui_state.variantBadgesChanged.emit()
        self.ui_state.variantSaveHintChanged.emit()

        if self.debug_cache_trace:
            _t_end = time.perf_counter()
            log.info(
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

    def get_current_display_native_size(self) -> Tuple[int, int]:
        """Return native pixel dimensions for the image currently represented in loupe."""
        if (
            self._last_rendered_preview is not None
            and self._last_rendered_preview_index == self.current_index
            and self._current_live_session_has_geometry_edits()
            and self._last_rendered_preview_session_key
            == self._get_current_live_preview_session_key()
        ):
            return (
                int(self._last_rendered_preview.width),
                int(self._last_rendered_preview.height),
            )

        # QML's currentNativeImageWidth/Height both notify on
        # currentImageSourceChanged and each call this method, so every
        # navigation would otherwise do this twice (two path.stat() calls,
        # plus a synchronous Image.open()/EXIF parse on the main thread for
        # the first visit). Memoize per navigation so the second call is free.
        memo_key = (self.current_index, self.ui_refresh_generation)
        if self._native_size_memo[0] == memo_key:
            return self._native_size_memo[1]

        result = self._compute_current_display_native_size()
        self._native_size_memo = (memo_key, result)
        return result

    def _compute_current_display_native_size(self) -> Tuple[int, int]:
        """Stat/decode path backing get_current_display_native_size()'s memo."""
        if not self.image_files or not (
            0 <= self.current_index < len(self.image_files)
        ):
            return (0, 0)

        try:
            path = (
                Path(self.view_override_path)
                if self.view_override_path
                else self.image_files[self.current_index].path
            )
            mtime = path.stat().st_mtime
            key = self._key(path)
            if key is None:
                return (0, 0)

            with self._native_image_size_lock:
                cached = self._native_image_size_cache.get(key)
            if cached is not None and cached[0] == mtime:
                return (cached[1], cached[2])

            with Image.open(path) as img:
                width, height = img.size
                orientation = img.getexif().get(274, 1)
            if orientation in (5, 6, 7, 8):
                width, height = height, width

            with self._native_image_size_lock:
                self._native_image_size_cache[key] = (
                    mtime,
                    int(width),
                    int(height),
                )
            return (int(width), int(height))
        except (OSError, TypeError, ValueError):
            return (0, 0)

    def _get_current_auto_adjust_path(self) -> Optional[Path]:
        """Return the currently viewed file path used as the auto-adjust source."""
        if not self.image_files or not (
            0 <= self.current_index < len(self.image_files)
        ):
            return None
        try:
            if self.view_override_path:
                return Path(self.view_override_path)
            return self.get_active_edit_path(self.current_index)
        except (IndexError, OSError, TypeError, ValueError):
            return None

    def _block_auto_adjust_during_crop(self) -> bool:
        """Return True after blocking auto-adjust while crop selection is transient."""
        if self.ui_state and getattr(self.ui_state, "isCropping", False) is True:
            self.update_status_message(
                "Apply or cancel the crop before auto-adjusting."
            )
            return True
        return False

    def _schedule_auto_adjust_save(self, action_type: str) -> None:
        """Legacy no-op: quick auto-adjust saves are now session-based."""
        self._auto_adjust_save_pending_action = None

    def _cancel_pending_auto_adjust_save(self) -> None:
        """Legacy no-op: quick auto-adjust saves are now session-based."""
        self._auto_adjust_save_pending_action = None

    def _flush_pending_auto_adjust_save(self) -> None:
        """Legacy no-op: quick auto-adjust saves are now session-based."""
        self._auto_adjust_save_pending_action = None

    @Slot()
    def _fire_auto_adjust_save_debounce(self) -> None:
        self._auto_adjust_save_pending_action = None

    def _get_current_preview_session_provenance(
        self,
    ) -> Optional[PreviewSessionProvenance]:
        """Return normalized source/session/revision identity for active previews."""
        editor = self.image_editor
        if editor is None:
            return None
        active_path = self._get_current_auto_adjust_path()
        if active_path is None:
            return None

        with editor._lock:
            if editor.current_filepath is None or editor.original_image is None:
                return None
            editor_path = Path(editor.current_filepath)
            session_id = getattr(editor, "session_id", None)
            revision = int(getattr(editor, "_edits_rev", 0))

        try:
            active_key = self._key(active_path)
            editor_key = self._key(editor_path)
        except (OSError, TypeError, ValueError):
            return None

        if active_key != editor_key:
            return None

        return PreviewSessionProvenance(
            active_key,
            session_id,
            revision,
        )

    def _get_current_live_preview_session_key(
        self,
    ) -> Optional[tuple[str, Optional[str]]]:
        """Return the path/session identity for the active live preview."""
        info = self._get_current_preview_session_provenance()
        if info is None:
            return None
        active_path_key, session_id, _revision = info
        return (active_path_key, session_id)

    def _get_current_original_compare_session_key(
        self,
    ) -> Optional[tuple[str, Optional[str], int]]:
        """Return the path/session/revision identity for held-space compare."""
        info = self._get_current_preview_session_provenance()
        if info is None:
            return None
        active_path_key, session_id, revision = info
        return (active_path_key, session_id, revision)

    def _publish_last_rendered_preview_locked(
        self,
        decoded: DecodedImage,
        session_key: Optional[tuple[str, Optional[str]]] = None,
    ) -> None:
        """Publish a rendered preview while holding _preview_lock."""
        resolved_session_key = (
            session_key
            if session_key is not None
            else self._get_current_live_preview_session_key()
        )
        # Editor renders do not pass through Prefetcher, so stamp their exact
        # session path here. The provider can then enforce the same wrong-image
        # invariant for previews as it does for cached JPEG decodes.
        if resolved_session_key is not None:
            decoded.source_path = resolved_session_key[0]
        self._last_rendered_preview = decoded
        self._last_rendered_preview_session_key = resolved_session_key

    def _clear_last_rendered_preview_locked(self) -> None:
        """Clear rendered preview state while holding _preview_lock."""
        self._last_rendered_preview = None
        self._last_rendered_preview_session_key = None

    def _has_current_live_preview_for_index(self, index: int) -> bool:
        """Return True when the rendered preview belongs to the current live edit session."""
        if self._last_rendered_preview is None:
            return False
        if index != self.current_index:
            return False
        if self._last_rendered_preview_index != index:
            return False
        session_key = self._get_current_live_preview_session_key()
        if session_key is None:
            return False
        if self._last_rendered_preview_session_key != session_key:
            return False
        return self._current_live_session_has_meaningful_edits()

    def _ensure_live_edit_session_state(
        self,
        *,
        force_reset: bool = False,
        restored_has_meaningful_edits: bool = False,
    ) -> None:
        """Bind dirty-session tracking to the currently loaded editor session."""
        info = self._get_current_preview_session_provenance()
        if info is None:
            if force_reset:
                self._live_edit_session_state = None
            return

        active_path_key, session_id, revision = info
        state = self._live_edit_session_state
        if (
            force_reset
            or state is None
            or state.active_path_key != active_path_key
            or state.session_id != session_id
        ):
            self._live_edit_session_state = LiveEditSessionState(
                active_path_key=active_path_key,
                session_id=session_id,
                persisted_revision=revision,
                submitted_revision=revision,
                persisted_has_meaningful_edits=restored_has_meaningful_edits,
                submitted_has_meaningful_edits=restored_has_meaningful_edits,
            )

    def _clear_live_edit_session_state(self) -> None:
        """Drop dirty-session tracking for the current image."""
        self._live_edit_session_state = None

    def _current_live_session_has_meaningful_edits(self) -> bool:
        """Return True when the current editor session would change persisted pixels."""
        if self.image_editor is None:
            return False
        if (
            self.image_editor.current_filepath is None
            or self.image_editor.original_image is None
        ):
            return False

        edits = getattr(self.image_editor, "current_edits", None) or {}
        numeric_keys = (
            "brightness",
            "contrast",
            "saturation",
            "white_balance_by",
            "white_balance_mg",
            "sharpness",
            "exposure",
            "highlights",
            "shadows",
            "vibrance",
            "vignette",
            "blacks",
            "whites",
            "clarity",
            "texture",
            "straighten_angle",
            *ImageEditor._COLOR_MIX_KEYS,
        )
        for key in numeric_keys:
            try:
                if abs(float(edits.get(key, 0.0))) > 0.001:
                    return True
            except (TypeError, ValueError):
                return True

        try:
            if int(edits.get("rotation", 0)) % 360 != 0:
                return True
        except (TypeError, ValueError):
            return True

        crop_box = edits.get("crop_box")
        if crop_box is not None:
            try:
                normalized_crop_box = tuple(int(v) for v in crop_box)
            except (TypeError, ValueError):
                return True
            if normalized_crop_box != (0, 0, 1000, 1000):
                return True

        for layer in (
            *effective_layers(edits),
            *stack_layers(edits),
            *halo_layers(edits),
        ):
            mask_data = self.image_editor._mask_assets.get(layer["id"])
            if mask_data is not None and mask_data.has_strokes():
                return True

        return False

    def _current_live_session_has_geometry_edits(self) -> bool:
        """Return True when live edits change image dimensions/orientation."""
        if self.image_editor is None:
            return False
        if (
            self.image_editor.current_filepath is None
            or self.image_editor.original_image is None
        ):
            return False

        edits = getattr(self.image_editor, "current_edits", None) or {}

        try:
            if int(edits.get("rotation", 0)) % 360 != 0:
                return True
        except (TypeError, ValueError):
            return True

        try:
            if abs(float(edits.get("straighten_angle", 0.0))) > 0.001:
                return True
        except (TypeError, ValueError):
            return True

        crop_box = edits.get("crop_box")
        if crop_box is not None:
            try:
                normalized_crop_box = tuple(int(v) for v in crop_box)
            except (TypeError, ValueError):
                return True
            if normalized_crop_box != (0, 0, 1000, 1000):
                return True

        return False

    @classmethod
    def _json_safe_edit_value(cls, value: Any) -> Any:
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, tuple):
            return [cls._json_safe_edit_value(v) for v in value]
        if isinstance(value, list):
            return [cls._json_safe_edit_value(v) for v in value]
        if isinstance(value, dict):
            return {str(k): cls._json_safe_edit_value(v) for k, v in value.items()}
        return value

    @classmethod
    def _serialize_editor_edits(cls, edits: dict[str, Any]) -> dict[str, Any]:
        """Convert editor parameters into JSON-safe pending edit metadata."""
        serialized: dict[str, Any] = {}
        for key, value in edits.items():
            serialized[key] = cls._json_safe_edit_value(value)
        return serialized

    def _deserialize_editor_edits(self, serialized: Any) -> dict[str, Any]:
        edits = self.image_editor._initial_edits()
        if not isinstance(serialized, dict):
            return edits

        # Recipes saved before tone curves were versioned used linear
        # brightness/contrast. Reopening them must not change their appearance.
        # New sessions and Reset use the latest version from _initial_edits().
        try:
            edits["tone_curve_version"] = ImageEditor.tone_curve_version(serialized)
        except RuntimeError as exc:
            # Keep the entire unknown recipe (including future fields), not a
            # lossy subset stamped with an older renderer. Rendering and export
            # reject this version; simply viewing its saved JPEG is still safe.
            log.warning("Cannot resume image edits: %s", exc)
            self.update_status_message(str(exc), timeout=10000)
            fallback = copy.deepcopy(serialized)
            # Typed fields still have to be typed: the brush tool and renderer
            # read local_layers as a list of clean layer dicts.
            fallback["local_layers"] = normalize_local_layers(
                fallback.get("local_layers")
            )
            return fallback
        numeric_keys = {
            "brightness",
            "contrast",
            "saturation",
            "white_balance_by",
            "white_balance_mg",
            "sharpness",
            "exposure",
            "highlights",
            "shadows",
            "vibrance",
            "vignette",
            "blacks",
            "whites",
            "clarity",
            "texture",
            "straighten_angle",
            *ImageEditor._COLOR_MIX_KEYS,
        }
        for key in edits:
            if key == "tone_curve_version":
                continue  # Already resolved centrally above.
            if key not in serialized:
                continue
            value = serialized[key]
            try:
                if key == "crop_box":
                    edits[key] = (
                        self._normalize_crop_box_tuple(value)
                        if value is not None
                        else None
                    )
                elif key == "local_layers":
                    edits[key] = normalize_local_layers(value)
                elif key == "rotation":
                    edits[key] = int(value) % 360
                elif key in numeric_keys:
                    edits[key] = float(value)
                else:
                    edits[key] = value
            except (TypeError, ValueError):
                log.warning(
                    "Ignoring invalid pending edit value for %s: %r", key, value
                )
        return edits

    @classmethod
    def _serialize_mask_assets(cls, masks: Any) -> dict[str, Any]:
        if not isinstance(masks, dict):
            return {}
        serialized: dict[str, Any] = {}
        for mask_id, mask_data in masks.items():
            if isinstance(mask_data, MaskData):
                serialized[str(mask_id)] = mask_data.to_dict()
            elif isinstance(mask_data, dict):
                serialized[str(mask_id)] = cls._json_safe_edit_value(mask_data)
        return serialized

    @staticmethod
    def _deserialize_mask_assets(serialized: Any) -> dict[str, MaskData]:
        if not isinstance(serialized, dict):
            return {}
        masks: dict[str, MaskData] = {}
        for mask_id, mask_data in serialized.items():
            if not isinstance(mask_data, dict):
                continue
            try:
                masks[str(mask_id)] = MaskData.from_dict(mask_data)
            except (TypeError, ValueError, KeyError):
                log.warning("Ignoring invalid pending mask data for %s", mask_id)
        return masks

    @staticmethod
    def _serialize_auto_adjust_state(
        state: Optional[ActiveAutoAdjustState],
    ) -> Optional[dict[str, Any]]:
        if state is None:
            return None
        return {
            "base_blacks": float(state.base_blacks),
            "base_whites": float(state.base_whites),
            "p_low": float(state.p_low),
            "p_high": float(state.p_high),
            "base_vibrance": float(state.base_vibrance),
            "auto_vibrance_delta": float(state.auto_vibrance_delta),
            "base_brightness": float(state.base_brightness),
            "auto_brightness_delta": float(state.auto_brightness_delta),
            "extra_highlight_steps": int(state.extra_highlight_steps),
            "extra_black_steps": int(state.extra_black_steps),
        }

    def _serialize_current_auto_adjust_state(self) -> Optional[dict[str, Any]]:
        if not self._has_valid_active_auto_adjust_state():
            return None
        return self._serialize_auto_adjust_state(self._active_auto_adjust_state)

    def _deserialize_auto_adjust_state(
        self,
        serialized: Any,
        *,
        active_path: Path,
    ) -> Optional[ActiveAutoAdjustState]:
        if not isinstance(serialized, dict):
            return None
        try:
            return ActiveAutoAdjustState(
                active_path_key=self._key(active_path),
                session_id=getattr(self.image_editor, "session_id", None),
                base_blacks=float(serialized["base_blacks"]),
                base_whites=float(serialized["base_whites"]),
                p_low=float(serialized.get("p_low", 0.0)),
                p_high=float(serialized.get("p_high", 255.0)),
                base_vibrance=float(serialized.get("base_vibrance", 0.0)),
                auto_vibrance_delta=float(serialized.get("auto_vibrance_delta", 0.0)),
                base_brightness=float(serialized.get("base_brightness", 0.0)),
                auto_brightness_delta=float(
                    serialized.get("auto_brightness_delta", 0.0)
                ),
                extra_highlight_steps=int(serialized.get("extra_highlight_steps", 0)),
                extra_black_steps=int(serialized.get("extra_black_steps", 0)),
            )
        except (KeyError, TypeError, ValueError):
            log.warning("Ignoring invalid saved auto-adjust state: %r", serialized)
            return None

    def _clear_active_auto_adjust_state(
        self,
        reason: str = "",
        *,
        clear_editor: bool = False,
    ) -> None:
        """Drop transient auto-adjust state and optionally discard the hidden session."""
        self._cancel_pending_auto_adjust_save()
        had_state = self._active_auto_adjust_state is not None
        self._active_auto_adjust_state = None

        if had_state and reason:
            log.debug("Cleared active auto-adjust state: %s", reason)

        if clear_editor and self.image_editor:
            self._clear_crop_mode_snapshot()
            self.image_editor.clear()

    def _has_valid_active_auto_adjust_state(self) -> bool:
        """Return True when the transient state still matches the current session."""
        state = self._active_auto_adjust_state
        if state is None or self.image_editor is None:
            return False

        active_path = self._get_current_auto_adjust_path()
        editor_path = getattr(self.image_editor, "current_filepath", None)
        if active_path is None or editor_path is None:
            return False

        try:
            active_path_key = self._key(active_path)
            editor_path_key = self._key(editor_path)
        except (OSError, TypeError, ValueError):
            return False

        return (
            state.active_path_key == active_path_key
            and editor_path_key == active_path_key
            and state.session_id == getattr(self.image_editor, "session_id", None)
        )

    def _capture_source_exif_for_active_image(self) -> Optional[bytes]:
        """Capture source EXIF when editing from RAW mode."""
        if self.current_edit_source_mode != "raw":
            return None
        if not (0 <= self.current_index < len(self.image_files)):
            return None

        image_file = self.image_files[self.current_index]
        jpeg_path = image_file.path
        if jpeg_path.suffix.lower() in (".tif", ".tiff") or not jpeg_path.exists():
            return None

        try:
            with Image.open(jpeg_path) as src_im:
                return src_im.info.get("exif")
        except Exception as e:
            log.warning("Failed to capture source EXIF from %s: %s", jpeg_path, e)
            return None

    def _ensure_active_image_loaded_for_auto_adjust(
        self,
        *,
        force_reload: bool = False,
    ) -> Optional[Path]:
        """Ensure the currently viewed image is loaded for auto-adjust operations."""
        active_path = self._get_current_auto_adjust_path()
        if active_path is None:
            self.update_status_message("No image to adjust")
            return None

        if force_reload:
            self.image_editor.clear()

        editor_path = getattr(self.image_editor, "current_filepath", None)
        paths_match = False
        if editor_path:
            try:
                paths_match = Path(editor_path).resolve() == active_path.resolve()
            except (OSError, ValueError):
                paths_match = str(editor_path) == str(active_path)

        # Auto-adjust sessions load preview_only, so accept a session that has
        # only the float preview; the full float master is materialized lazily
        # by _ensure_float_image() (pre-warmed in the background below).
        has_buffers = self.image_editor.original_image is not None and (
            self.image_editor.float_image is not None
            or self.image_editor.float_preview is not None
        )
        if paths_match and has_buffers:
            if self._has_valid_active_auto_adjust_state():
                self._ensure_live_edit_session_state()
                return active_path

            try:
                current_mtime = active_path.stat().st_mtime
            except OSError:
                current_mtime = 0.0

            if math.isclose(
                current_mtime,
                getattr(self.image_editor, "current_mtime", 0.0),
                rel_tol=0.0,
                abs_tol=1e-6,
            ):
                self._ensure_live_edit_session_state()
                return active_path

        load_path, _edit_state = self._active_edit_load_path(active_path)
        # preview_only skips the ~400ms full-resolution float conversion that
        # would otherwise block the UI thread on the first auto-adjust
        # keypress; analysis only needs the preview-sized buffer.
        if self.image_editor.load_image(
            str(load_path),
            cached_preview=None,
            source_exif=self._capture_source_exif_for_active_image(),
            preview_only=True,
        ):
            self._bind_loaded_editor_output_path(active_path)
            restored = self._restore_pending_edit_state_for_loaded_path(active_path)
            self._ensure_live_edit_session_state(
                force_reset=True,
                restored_has_meaningful_edits=(
                    restored and self._current_live_session_has_meaningful_edits()
                ),
            )
            self._prewarm_editor_float_image()
            return active_path

        self.update_status_message("Failed to load image")
        return None

    def _prewarm_editor_float_image(self) -> None:
        """Materialize the editor's float master off the UI thread.

        After a preview_only load, the first consumer of float_image would
        otherwise pay the full-resolution conversion inline — and
        snapshot_for_export runs on the main thread. _ensure_float_image is
        thread-safe and no-ops if the master already exists or the image
        changed underneath it.
        """
        editor = self.image_editor
        if editor is None:
            return

        with editor._lock:
            session_id = editor.session_id
            current_filepath = editor.current_filepath

        with self._editor_prewarm_lock:
            future = self._editor_prewarm_future
            if future is not None and not future.done():
                return

            def _job() -> None:
                try:
                    with editor._lock:
                        if (
                            editor.session_id != session_id
                            or editor.current_filepath != current_filepath
                        ):
                            return
                    editor._ensure_float_image()
                except Exception:
                    pass  # editor cleared/reloaded before the worker ran

            def _clear_future(_future: concurrent.futures.Future) -> None:
                with self._editor_prewarm_lock:
                    if self._editor_prewarm_future is _future:
                        self._editor_prewarm_future = None

            try:
                future = self._editor_prewarm_executor.submit(_job)
            except RuntimeError:
                return  # shutting down

            self._editor_prewarm_future = future

        # Registered outside the lock on purpose: a future that already finished
        # runs its callback inline on this thread, and _clear_future takes the
        # same non-reentrant lock. _job no-ops whenever the float master is
        # already warm, so it really can complete before we get here.
        future.add_done_callback(_clear_future)

    def _black_threshold_percent(self) -> Optional[float]:
        """Shadow-end clip percentile, or None to follow the shared threshold."""
        value = self.auto_level_black_threshold
        return value if value > 0.0 else None

    def _compute_auto_levels_recommendation(self) -> dict[str, Any]:
        """Compute the current baseline auto-level recommendation."""
        blacks, whites, p_low, p_high = self.image_editor.analyze_auto_levels(
            self.auto_level_threshold,
            reset_levels=True,
            channel_budget=self.auto_level_channel_budget,
            black_threshold_percent=self._black_threshold_percent(),
        )

        dynamic_range = p_high - p_low
        if dynamic_range < 1.0:
            strength = (
                0.0 if self.auto_level_strength_auto else self.auto_level_strength
            )
        else:
            strength = (
                1.0 if self.auto_level_strength_auto else self.auto_level_strength
            )
            # Ceiling on the endpoint stretch. This used to apply only when
            # auto_level_strength_auto was on — which is off by default, so the
            # default path had no bound at all and a very flat image could be
            # stretched arbitrarily far. Measured on ~90 finished photos the
            # honest stretch never exceeds ~2.7x even for a 2x under-exposure,
            # so the ceiling is a guard rail, not a tuning knob.
            stretch_full = 255.0 / dynamic_range
            if stretch_full > _AUTO_LEVELS_MAX_STRETCH:
                strength = min(
                    strength,
                    max(
                        0.0,
                        (_AUTO_LEVELS_MAX_STRETCH - 1.0) / (stretch_full - 1.0),
                    ),
                )

            log.debug(
                "Auto levels: p_low=%0.1f, p_high=%0.1f, range=%0.1f, stretch_full=%0.2f, strength=%0.3f",
                p_low,
                p_high,
                dynamic_range,
                stretch_full,
                strength,
            )

        base_blacks = blacks * strength
        base_whites = whites * strength
        noop_reason = None
        if dynamic_range < 1.0:
            noop_reason = "flat image"
        elif p_low <= 0.0 and p_high >= 255.0:
            noop_reason = "already full range"

        # Midtone correction: an endpoint stretch leaves a full-range but
        # underexposed (or overexposed) image untouched. When the projected
        # post-stretch subject luma falls outside the dead band around the
        # target, nudge brightness partway back toward the nearest band edge.
        # A well-exposed image inside the band is left alone (normalizing every
        # photo to one brightness made "most images too bright"); images that
        # are corrected only recover partway, preserving intentional low-key /
        # high-key character. The factor is capped so the auto result stays
        # conservative; the levels soft knee protects highlights from the lift.
        #
        # The metric is subject luma, not whole-frame median: on a black-
        # background macro shot the median is the backdrop, so driving the
        # correction from it lifted an already well-exposed subject by up to
        # 1.3x and hazed the background. See editor.analyze_auto_levels.
        base_brightness = 0.0
        if self.auto_level_midtone and dynamic_range >= 1.0:
            stats = getattr(self.image_editor, "last_auto_levels_stats", {})
            subject_luma = float(
                stats.get("subject_luma", stats.get("median_luma", 0.0))
            )
            if subject_luma > 0.02:
                bp = -base_blacks * 0.15
                wp = 1.0 - base_whites * 0.15
                span = max(wp - bp, 1e-4)
                target = max(0.2, min(0.6, self.auto_level_midtone_target))
                # Subject luma projected through the levels stretch.
                projected = (subject_luma - bp) / span
                band_low = target - _AUTO_MIDTONE_DEADBAND
                band_high = target + _AUTO_MIDTONE_DEADBAND
                desired = None
                if 0.02 < projected < band_low:
                    desired = projected + _AUTO_MIDTONE_RECOVERY * (
                        band_low - projected
                    )
                elif projected > band_high:
                    desired = projected + _AUTO_MIDTONE_RECOVERY * (
                        band_high - projected
                    )
                if desired is not None:
                    # Brightness multiplies before the levels ramp, so solve
                    # (subject * factor - bp) / span = desired for factor.
                    factor = (desired * span + bp) / subject_luma
                    factor = max(
                        _AUTO_MIDTONE_MAX_DROP, min(_AUTO_MIDTONE_MAX_LIFT, factor)
                    )
                    if abs(factor - 1.0) >= 0.02:
                        base_brightness = factor - 1.0

        return {
            "base_blacks": base_blacks,
            "base_whites": base_whites,
            "base_brightness": base_brightness,
            "p_low": p_low,
            "p_high": p_high,
            "dynamic_range": dynamic_range,
            "strength": strength,
            "noop_reason": noop_reason,
        }

    @staticmethod
    def _derive_auto_adjust_levels(
        state: ActiveAutoAdjustState,
    ) -> tuple[float, float]:
        """Translate transient auto-adjust state into final blacks/whites values."""
        blacks = state.base_blacks - (_AUTO_ADJUST_BLACK_STEP * state.extra_black_steps)
        whites = state.base_whites - (
            _AUTO_ADJUST_HIGHLIGHT_STEP * state.extra_highlight_steps
        )
        return blacks, whites

    def _format_auto_levels_detail(
        self,
        *,
        p_low: float,
        p_high: float,
        blacks: float,
        whites: float,
        extra_highlight_steps: int = 0,
        extra_black_steps: int = 0,
        auto_vibrance_delta: float = 0.0,
        auto_brightness_delta: float = 0.0,
    ) -> str:
        """Build a human-readable status line for the current levels state."""
        if abs(blacks) <= 0.001 and abs(whites) <= 0.001:
            msg = "Auto levels: no change"
        elif p_high >= 255.0 and abs(whites) <= 0.001:
            msg = (
                f"Auto levels: highlights clipped; shadows only (blacks {blacks:+.1f})"
            )
        elif p_low <= 0.0 and abs(blacks) <= 0.001:
            msg = (
                f"Auto levels: shadows clipped; highlights only (whites {whites:+.1f})"
            )
        else:
            dynamic_range = max(1.0, p_high - p_low)
            gain = 255.0 / dynamic_range
            msg = (
                f"Auto levels: blacks {blacks:+.1f}, whites {whites:+.1f} "
                f"(range {p_low:.0f}-{p_high:.0f}, gain {gain:.2f})"
            )

        suffixes = []
        highlight_step_points = round(_AUTO_ADJUST_HIGHLIGHT_STEP * 100)
        if extra_highlight_steps > 0:
            suffixes.append(
                f"highlights -{extra_highlight_steps * highlight_step_points}pt"
            )
        elif extra_highlight_steps < 0:
            suffixes.append(
                f"whites +{-extra_highlight_steps * highlight_step_points}pt"
            )
        black_step_points = round(_AUTO_ADJUST_BLACK_STEP * 100)
        if extra_black_steps > 0:
            suffixes.append(f"blacks -{extra_black_steps * black_step_points}pt")
        elif extra_black_steps < 0:
            suffixes.append(f"blacks +{-extra_black_steps * black_step_points}pt")
        if auto_vibrance_delta > _AUTO_VIBRANCE_EPS:
            suffixes.append(f"vibrance +{auto_vibrance_delta:.2f}")
        if abs(auto_brightness_delta) > _AUTO_BRIGHTNESS_EPS:
            suffixes.append(f"midtone {auto_brightness_delta:+.2f}")
        if suffixes:
            msg = f"{msg}; {', '.join(suffixes)}"
        return msg

    def _apply_levels_to_editor(
        self,
        *,
        blacks: float,
        whites: float,
        kick_preview: bool,
    ) -> bool:
        """Apply derived levels to the editor and synchronize the UI state."""
        changed_blacks = self.image_editor.set_edit_param("blacks", blacks)
        changed_whites = self.image_editor.set_edit_param("whites", whites)

        self.ui_state.blacks = blacks
        self.ui_state.whites = whites

        changed = changed_blacks or changed_whites
        if changed and kick_preview:
            self._kick_preview_worker()
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
        return changed

    def _apply_vibrance_to_editor(
        self,
        *,
        vibrance: float,
    ) -> bool:
        """Apply derived vibrance to the editor and synchronize the UI state."""
        changed = self.image_editor.set_edit_param("vibrance", vibrance)
        self.ui_state.vibrance = vibrance
        return changed

    def _apply_brightness_to_editor(
        self,
        *,
        brightness: float,
    ) -> bool:
        """Apply derived brightness to the editor and synchronize the UI state."""
        changed = self.image_editor.set_edit_param("brightness", brightness)
        self.ui_state.brightness = brightness
        return changed

    def _build_active_auto_adjust_state(
        self,
        recommendation: dict[str, Any],
        *,
        include_auto_vibrance: bool = False,
    ) -> ActiveAutoAdjustState:
        """Create transient auto-adjust state for the current loaded session."""
        active_path = self._get_current_auto_adjust_path()
        active_path_key = (
            self._key(active_path)
            if active_path is not None
            else self._key(self.image_editor.current_filepath)
        )
        try:
            current_vibrance = float(self.image_editor.get_edit_value("vibrance", 0.0))
        except (TypeError, ValueError):
            current_vibrance = 0.0
        auto_vibrance_delta = 0.0
        if include_auto_vibrance and self.auto_vibrance_enabled:
            auto_vibrance_delta = self.image_editor.analyze_auto_vibrance(
                blacks=float(recommendation["base_blacks"]),
                whites=float(recommendation["base_whites"]),
            )

        base_vibrance = max(
            -1.0,
            min(1.0, current_vibrance + auto_vibrance_delta),
        )
        auto_vibrance_delta = base_vibrance - current_vibrance

        # Midtone brightness: only auto-set when the user has not touched the
        # brightness slider themselves, mirroring the vibrance behavior.
        try:
            current_brightness = float(
                self.image_editor.get_edit_value("brightness", 0.0)
            )
        except (TypeError, ValueError):
            current_brightness = 0.0
        base_brightness = current_brightness
        if abs(current_brightness) <= 0.001:
            base_brightness = float(recommendation.get("base_brightness", 0.0))
        auto_brightness_delta = base_brightness - current_brightness

        return ActiveAutoAdjustState(
            active_path_key=active_path_key,
            session_id=getattr(self.image_editor, "session_id", None),
            base_blacks=float(recommendation["base_blacks"]),
            base_whites=float(recommendation["base_whites"]),
            p_low=float(recommendation["p_low"]),
            p_high=float(recommendation["p_high"]),
            base_vibrance=base_vibrance,
            auto_vibrance_delta=auto_vibrance_delta,
            base_brightness=base_brightness,
            auto_brightness_delta=auto_brightness_delta,
        )

    # --- Image Editor Integration ---

    # --- Actions ---

    # ---- Navigation trace (seq correlation) --------------------------------
    #
    # One seq is minted per target change. Every provider request also has a UI
    # generation, because a single navigation can display a fast frame and then
    # a cover-quality replacement. Correlation is therefore (seq, generation),
    # never "whatever index happens to be current when a worker finishes".

    _NAV_RECORDS_MAX = 96
    _NAV_SETTLE_MS = 400
    _NAV_SUMMARY_DEADLINE_MS = 1800
    # Upper bound on how long --loupe startup may throttle speculative decodes.
    # The real release is the first Ready frame; this only guarantees that a
    # missed Ready callback (QML error, decode failure, unusual view state)
    # cannot leave FastStack permanently constrained.
    _STARTUP_CONSTRAINT_FAILSAFE_MS = 10000

    def _set_current_index(
        self,
        index: int,
        direction: int = 0,
        is_navigation: bool = True,
        prefetch_already_updated: bool = False,
        paced_presentation: bool = False,
    ):
        """Centralized method to change current image index and reset state."""
        _trace_started = time.perf_counter() if self.debug_cache_trace else 0.0
        if self.debug_cache_trace:
            log.info(
                f"[DBGCACHE] {_trace_started*1000:.3f} _set_current_index: START index={index} dir={direction}"
            )

        if index < 0 or index >= len(self.image_files):
            return

        if not paced_presentation and index != self.current_index:
            self._abandon_paced_navigation_state("direct index change")

        if not self._flush_current_live_edit_session_for_navigation():
            return

        self._clear_active_auto_adjust_state(
            "navigated to a different image",
            clear_editor=True,
        )
        self._clear_live_edit_session_state()
        self._clear_variant_override()
        self._reset_crop_settings()
        self._reset_local_brush_on_navigation()

        old_current_path = self._current_image_path()
        self.current_index = index  # Set index first so signals pick up correct image
        self._notify_thumbnail_current_path_changed(old_current_path)
        self._refresh_stack_inputs()
        if direction in (-1, 1):
            self._last_navigation_direction = direction

        # Mint the navigation correlation id for this target-index change. This
        # is the anchor for "target index changed -> frame actually rendered".
        if self.debug_cache and not self._is_grid_view_active:
            path_key = self._key(self.image_files[index].path)
            with self._nav_trace_lock:
                target_already_seeded = (
                    self._nav_target_index == index
                    and self._nav_target_path_key == path_key
                )
            if not target_already_seeded:
                self._seed_nav_record(index, direction=direction)

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
        self.rawDevelopmentStateChanged.emit()

        if self.debug_cache_trace:
            _t_prefetch = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_prefetch*1000:.3f} _set_current_index: calling _do_prefetch"
            )

        if not prefetch_already_updated:
            self._do_prefetch(
                self.current_index,
                is_navigation=is_navigation,
                direction=direction,
            )

        if self.debug_cache_trace:
            _t_sync = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_sync*1000:.3f} _set_current_index: calling sync_ui_state (prefetch took {(_t_sync - _t_prefetch)*1000:.2f}ms)"
            )

        self.sync_ui_state()

        # Update histogram if visible
        if self.ui_state.isHistogramVisible:
            self.update_histogram()

        self._restart_quality_decode_timer()

        if self.debug_cache_trace:
            _t_end = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_end*1000:.3f} _set_current_index: DONE total={(_t_end - _trace_started)*1000:.2f}ms"
            )

    def next_image(self):
        if self.debug_cache_trace:
            _t_start = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} next_image: START from index={self.current_index}"
            )

        if self.current_index < len(self.image_files) - 1:
            self._request_paced_navigation_step(1)

        if self.debug_cache_trace:
            _t_end = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_end*1000:.3f} next_image: DONE total={(_t_end - _t_start)*1000:.2f}ms"
            )

    def next_image_by_10(self):
        target_index = min(self.current_index + 10, len(self.image_files) - 1)
        if target_index != self.current_index:
            self._set_current_index(target_index, direction=1)

    def prev_image(self):
        if self.debug_cache_trace:
            _t_start = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_start*1000:.3f} prev_image: START from index={self.current_index}"
            )

        if self.current_index > 0:
            self._request_paced_navigation_step(-1)

        if self.debug_cache_trace:
            _t_end = time.perf_counter()
            log.info(
                f"[DBGCACHE] {_t_end*1000:.3f} prev_image: DONE total={(_t_end - _t_start)*1000:.2f}ms"
            )

    def prev_image_by_10(self):
        target_index = max(self.current_index - 10, 0)
        if target_index != self.current_index:
            self._set_current_index(target_index, direction=-1)

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
            # This is a read-only sweep across the folder. Skipping migration
            # avoids an O(images x sidecar entries) filesystem scan on misses.
            meta = self.sidecar.get_metadata(
                img.path,
                create=False,
                migrate=False,
            )

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

    def _update_dialog_open_state(self) -> None:
        """Publish modal state after a balanced token/depth change."""
        count = self._anonymous_dialog_depth + len(self._active_modal_tokens)
        was_open = self._dialog_open_count > 0
        is_open = count > 0
        self._dialog_open_count = count
        if not was_open and is_open:
            if self._navigation_in_flight():
                # A modal takes the keyboard, so the release of a held arrow
                # key never reaches the navigation code. Stop here, and
                # schedule the settled decode that the release would have.
                self._begin_direct_image_transition("dialog opened")
                self._restart_quality_decode_timer()
            # Otherwise the image on screen is not changing, so its settled
            # decode and any preview refinement carry on behind the dialog.
            # Cancelling them here left the image soft for as long as the
            # dialog stayed open, then decoded it again after it closed. An
            # action in the dialog that replaces the image begins its own
            # transition.
            self._dialog_open = True
            self.dialogStateChanged.emit(True)
            log.debug("Dialog opened, disabling global keybindings")
        elif was_open and not is_open:
            self._dialog_open = False
            self.dialogStateChanged.emit(False)
            log.debug("All dialogs closed, re-enabling global keybindings")

    @Slot()
    @Slot(str)
    def dialog_opened(self, token: str = ""):
        """Register a modal scope, idempotently when a token is supplied."""
        if token:
            if token in self._active_modal_tokens:
                log.warning("Ignoring duplicate dialog-open token %s", token)
                return
            self._active_modal_tokens.add(token)
        else:
            self._anonymous_dialog_depth += 1
        self._update_dialog_open_state()

    @Slot()
    @Slot(str)
    def dialog_closed(self, token: str = ""):
        """Release a modal scope without allowing underflow or cross-close."""
        if token:
            if token not in self._active_modal_tokens:
                if not self._shutting_down:
                    # At exit every dialog's Component.onDestruction lands
                    # here; that is teardown, not a mismatch worth a line each.
                    log.debug("Ignoring unmatched dialog-close token %s", token)
                return
            self._active_modal_tokens.remove(token)
        elif self._anonymous_dialog_depth > 0:
            self._anonymous_dialog_depth -= 1
        else:
            log.warning("Ignoring unmatched anonymous dialog-close")
            return
        self._update_dialog_open_state()

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
        clear_raw_count_cache()
        self._scan_count_variant += 1
        try:
            result = find_images_with_variants(self.image_dir)
        except OSError as exc:
            log.error(
                "Manual directory scan failed; preserving the last known list: %s",
                exc,
            )
            self.update_status_message(
                f"Could not refresh folder: {exc}",
                timeout=10000,
                color="#FFD54F",
            )
            return

        # Manual refresh is deliberately unattributed: it exists to recover
        # changes the watcher missed, including a same-path replacement with
        # unchanged stat metadata. Force both thumbnail and loupe pixels stale.
        if self._thumbnail_cache:
            self._thumbnail_cache.clear()
        with self._watcher_changed_lock:
            self._watcher_changed_paths.add(None)
        # The manual result was obtained after any already-running watcher
        # scan. Give it a new generation so the older result is discarded.
        # It does not own the background inflight flag; that worker will clear
        # its own flag when its stale result arrives.
        self._index_scan_epoch += 1
        self._on_index_scan_ready(
            {
                "epoch": self._index_scan_epoch,
                "result": result,
                "error": None,
                "background": False,
            }
        )

    def switch_to_grid_view(self):
        """Switch to grid view (from loupe view). Called by Esc key."""
        if not self._is_grid_view_active:
            self._set_grid_view_active(True)

    def _set_grid_view_active(self, active: bool):
        """Set grid view active state and handle side effects."""
        if self._is_grid_view_active == active:
            return

        if active and self.ui_state.isBrushMode:
            self.exit_local_brush_mode()
        self._is_grid_view_active = active

        if active:
            # Entering grid view
            self._clear_published_loupe_source()
            # No loupe frame will be presented, so a still-held startup
            # constraint has to be released here instead.
            self._release_startup_prefetch_constraint("left-loupe")
            self._cancel_paced_navigation()
            self.pending_prefetch_index = None
            self._quality_decode_token += 1
            self._quality_decode_timer.stop()
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
                # Always use controller's list, even if empty.
                self._refresh_thumbnail_model_from_controller()

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
            self._maybe_show_turbo_fallback_warning()

        if active:
            self.ui_state.isGridViewActiveChanged.emit(active)
            return

        # QML builds the loupe while this signal is delivered: it reads
        # currentImageSource and reports the image area's size. The decode is
        # requested afterwards, once that size is known. Requesting it first
        # decoded for the size left over from before the grid was shown and
        # again 150 ms later when the resize debounce fired.
        self._entering_loupe = True
        try:
            self.ui_state.isGridViewActiveChanged.emit(active)
        finally:
            self._entering_loupe = False
        self._finish_entering_loupe()

    def _finish_entering_loupe(self) -> None:
        """Request the decode for the image the loupe has just been built on."""
        refreshed = False
        if self.resize_timer.isActive():
            # The loupe reported a size during the switch: commit it now so
            # the one decode is at the right resolution. Entering the grid
            # cancelled any paced navigation, and the trace record minted
            # while QML read the source is for this very image; keeping it
            # shows the switch as one navigation instead of an abandoned one
            # followed by its replacement.
            self.resize_timer.stop()
            refreshed = self._handle_resize(keep_navigation_record=True)
        if not refreshed:
            # Trigger exactly one decode for the current index
            self._maybe_decode_current_image("enter-loupe")
            # The source read during the switch was held back when the image
            # was not cached yet (_resolve_loupe_image_source); read it again
            # now that its decode is queued.
            self.ui_state.currentImageSourceChanged.emit()
        self._restart_quality_decode_timer()

    def _grid_child_folder_for_delete(self, path: str) -> Optional[Path]:
        """Resolve *path* when it is a direct child of the grid directory."""
        model = self._thumbnail_model
        if model is None:
            return None
        try:
            folder_path = Path(path).resolve()
            current_directory = Path(model.current_directory).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        if folder_path.parent != current_directory:
            return None
        return folder_path

    def grid_inspect_folder_for_delete(self, path: str) -> dict:
        """Report whether a grid folder is empty / almost empty (no images)."""
        folder_path = self._grid_child_folder_for_delete(path)
        if folder_path is None:
            # Only child folders of the grid's directory (never "..").
            return {"deletable": False, "reason": "not a child folder"}
        return inspect_folder_for_delete(folder_path).to_qml()

    def grid_delete_folder(self, path: str, expected_items: list) -> None:
        """Delete a folder previously reported deletable by the inspector."""
        folder_path = self._grid_child_folder_for_delete(path)
        if folder_path is None:
            log.warning("Refusing to delete non-child folder %s", path)
            return
        error = delete_inspected_folder(
            folder_path, [str(item) for item in expected_items]
        )
        if error:
            self.update_status_message(error, timeout=6000, color="#FFD54F")
        else:
            self.update_status_message(f"Deleted folder {folder_path.name}")
        if self._thumbnail_model and self._is_grid_view_active:
            self._thumbnail_model.refresh()

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

        # Select the image while the grid is still showing: nothing is decoded
        # or published for the loupe in grid view, so the loupe is then built
        # directly on this image. Switching first bound the previously viewed
        # image, decoded its neighbourhood, and showed it for about 100 ms
        # before the image that was clicked.
        self._set_current_index(loupe_index, is_navigation=True)
        self._set_grid_view_active(False)

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

    @Slot()
    def duplicate_current_image(self):
        """Copy the current visible image and insert the copy after it."""
        if not self.image_files:
            self.update_status_message("No image to duplicate.")
            return

        if not (0 <= self.current_index < len(self.image_files)):
            self.update_status_message("No image to duplicate.")
            return

        if self._filter_enabled:
            self.update_status_message("Clear filters before duplicating.")
            return

        if self.ui_state and getattr(self.ui_state, "isCropping", False) is True:
            self.update_status_message(
                "Apply/save or discard edits before duplicating."
            )
            return

        if self.has_unsaved_edits():
            self.update_status_message(
                "Apply/save or discard edits before duplicating."
            )
            return

        source_image = self.image_files[self.current_index]
        source_path = (
            Path(self.view_override_path)
            if self.view_override_path
            else source_image.path
        )
        if self._block_if_saving(source_path):
            return

        if not source_path.exists():
            self.update_status_message("Current image file is missing.")
            return

        duplicate_path = self._duplicate_path_for(source_path)

        try:
            shutil.copy2(source_path, duplicate_path)
            stat = duplicate_path.stat()
        except OSError as exc:
            log.exception("Failed to duplicate image %s", source_path)
            self.update_status_message(f"Duplicate failed: {exc}")
            return

        duplicate_image = ImageFile(
            path=duplicate_path,
            raw_pair=None,
            timestamp=stat.st_mtime,
        )
        self._begin_direct_image_transition("image duplicated")
        insert_index = self.current_index + 1
        self.image_files.insert(insert_index, duplicate_image)

        all_insert_index = len(self._all_images)
        for idx, img in enumerate(self._all_images):
            if self._key(img.path) == self._key(source_image.path):
                all_insert_index = idx + 1
                break
        self._all_images.insert(all_insert_index, duplicate_image)

        batch_changed = False
        metadata_saved = True
        if self.batches:
            self.batches = self._shift_ranges_after_insert(self.batches, insert_index)
            batch_changed = True
        if (
            self.batch_start_index is not None
            and self.batch_start_index >= insert_index
        ):
            self.batch_start_index += 1
            batch_changed = True
        if batch_changed:
            metadata_saved = self._finalize_batch_state(
                emit=False, sync=False, notify_model=False
            )

        stacks_changed = False
        if self.stacks:
            old_stacks = [s[:] for s in self.stacks]
            self.stacks = self._shift_ranges_after_insert(self.stacks, insert_index)
            stacks_changed = self.stacks != old_stacks
        if (
            self.stack_start_index is not None
            and self.stack_start_index >= insert_index
        ):
            self.stack_start_index += 1
        if self.stack_end_index is not None and self.stack_end_index >= insert_index:
            self.stack_end_index += 1
        if stacks_changed:
            metadata_saved = self._persist_stack_state() and metadata_saved
            self._metadata_cache_index = (-1, -1)

        self.prefetcher.set_image_files(self.image_files)
        self._rebuild_path_to_index()
        clear_raw_count_cache()
        self._grid_model_dirty = True

        if self._thumbnail_model and self._is_grid_view_active:
            self._refresh_thumbnail_model_from_controller()

        self.dataChanged.emit()
        if stacks_changed:
            self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()
        self._restart_quality_decode_timer()
        if metadata_saved:
            self.update_status_message(f"Duplicated image as {duplicate_path.name}")
        log.info("Duplicated image %s -> %s", source_path, duplicate_path)

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
            # migrate=False: this runs per image on grid refresh; the legacy
            # migration scan costs O(entries) filesystem stats per miss.
            meta = self.sidecar.get_metadata(image_path, create=False, migrate=False)
            if meta is None:
                return {
                    "stacked": False,
                    "uploaded": False,
                    "edited": False,
                    "restacked": False,
                    "favorite": False,
                    "todo": False,
                    "batch": False,
                }
            return {
                "stacked": meta.stacked,
                "uploaded": meta.uploaded,
                "edited": meta.edited,
                "restacked": meta.restacked,
                "favorite": meta.favorite,
                "todo": meta.todo,
                "batch": meta.batch,
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
                "batch": False,
            }

    def _get_bulk_metadata_map(self, images=None) -> Dict[str, dict]:
        """Get flattened metadata map for the given images, keyed by str(img.path).

        Used to avoid per-image sidecar lookups on the UI thread during grid
        refresh. Keying by the raw path string lets downstream consumers
        look up entries directly without re-resolving the stable sidecar key.

        The map is treated as authoritative by the grid (see
        ``ThumbnailModel.refresh_from_controller``), so per-image errors must
        be isolated — a single bad path must not collapse flags for the
        whole folder.
        """
        bulk_map = {}
        for img in (images if images is not None else self.image_files):
            try:
                # migrate=False: see _get_metadata_dict — most images have no
                # entry, and the migration scan is O(entries) stats per miss.
                meta = self.sidecar.get_metadata(img.path, create=False, migrate=False)
                if meta is None:
                    continue
                bulk_map[str(img.path)] = {
                    "stacked": getattr(meta, "stacked", False),
                    "uploaded": getattr(meta, "uploaded", False),
                    "edited": getattr(meta, "edited", False),
                    "restacked": getattr(meta, "restacked", False),
                    "favorite": getattr(meta, "favorite", False),
                    "todo": getattr(meta, "todo", False),
                    "batch": getattr(meta, "batch", False),
                }
            except Exception as e:
                log.warning(
                    "Failed to read metadata for %s during bulk build: %s",
                    img.path,
                    e,
                )
        return bulk_map

    def _invalidate_batch_cache(self):
        """Clear the batch indices cache. Call after mutating self.batches."""
        self._batch_indices_cache = set()
        self._batch_indices_cache_key = None

    def _normalize_batches(self) -> None:
        """Sort ``self.batches`` by start index and merge overlapping or
        adjacent ranges in place.

        Call after any append to ``self.batches`` to maintain the invariant
        that ranges are sorted, non-overlapping, and non-adjacent. See the
        attribute comment on ``self.batches`` for the selection semantics.
        """
        if not self.batches:
            return
        self.batches = [[int(start), int(end)] for start, end in self.batches]
        self.batches.sort()
        merged: List[List[int]] = [self.batches[0]]
        for current_start, current_end in self.batches[1:]:
            last_start, last_end = merged[-1]
            if current_start <= last_end + 1:
                merged[-1] = [last_start, max(last_end, current_end)]
            else:
                merged.append([current_start, current_end])
        self.batches = merged

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

    def _get_user_flag_toggle_indices(self) -> List[int]:
        """Return target indices for user-facing flag toggles.

        ``self.batches`` is a single non-contiguous selection (see the
        attribute comment on ``self.batches``). When the current image is part
        of that selection, apply the toggle to the entire selection —
        matching the behavior of ``delete_batch_images``, drag-and-drop, and
        ``batch_auto_levels``. Otherwise, only toggle the current image.
        """
        if not self.image_files or not (
            0 <= self.current_index < len(self.image_files)
        ):
            return []

        in_any_batch = any(
            start <= self.current_index <= end for start, end in self.batches
        )
        if in_any_batch:
            max_index = len(self.image_files)
            return sorted(
                idx for idx in self._get_batch_indices() if 0 <= idx < max_index
            )

        return [self.current_index]

    def _toggle_user_flag(
        self,
        *,
        flag_attr: str,
        date_attr: Optional[str] = None,
        single_on_status: str,
        single_off_status: str,
        batch_on_status: str,
        batch_off_status: str,
    ) -> bool:
        """Toggle a user-facing flag for the current image or active batch.

        The current image determines the new state. When batch mode is active,
        that state is applied uniformly across all batched images.
        """
        indices = self._get_user_flag_toggle_indices()
        if not indices:
            return False

        current_path = self.image_files[self.current_index].path
        current_meta = self.sidecar.get_metadata(current_path)
        new_value = not bool(getattr(current_meta, flag_attr, False))
        today = datetime.now().strftime("%Y-%m-%d") if date_attr else None

        for idx in indices:
            image_path = self.image_files[idx].path
            meta = self.sidecar.get_metadata(image_path)
            setattr(meta, flag_attr, new_value)
            if date_attr is not None:
                setattr(meta, date_attr, today if new_value else None)

        metadata_saved = self._persist_sidecar()
        self._metadata_cache_index = (-1, -1)
        filter_reapplied = self._reapply_flag_filter_after_metadata_change(
            flag_attr,
            current_path,
        )
        self.dataChanged.emit()
        self.sync_ui_state(image_count_changed=filter_reapplied)

        if not metadata_saved:
            return False
        if len(indices) == 1:
            self.update_status_message(
                single_on_status if new_value else single_off_status
            )
        else:
            self.update_status_message(
                (batch_on_status if new_value else batch_off_status).format(
                    count=len(indices)
                )
            )

        log.info(
            "Set %s=%s for %d %s (current=%s)",
            flag_attr,
            new_value,
            len(indices),
            "image" if len(indices) == 1 else "images",
            current_path,
        )
        return True

    def toggle_uploaded(self):
        """Toggle uploaded flag for current image or active batch."""
        return self._toggle_user_flag(
            flag_attr="uploaded",
            date_attr="uploaded_date",
            single_on_status="Marked as uploaded",
            single_off_status="Marked as not uploaded",
            batch_on_status="Marked {count} images as uploaded",
            batch_off_status="Marked {count} images as not uploaded",
        )

    def toggle_todo(self):
        """Toggle todo flag for current image or active batch."""
        return self._toggle_user_flag(
            flag_attr="todo",
            date_attr="todo_date",
            single_on_status="Marked as todo",
            single_off_status="Marked as not todo",
            batch_on_status="Marked {count} images as todo",
            batch_off_status="Marked {count} images as not todo",
        )

    def toggle_edited(self):
        """Toggle edited flag for current image or active batch."""
        return self._toggle_user_flag(
            flag_attr="edited",
            date_attr="edited_date",
            single_on_status="Marked as edited",
            single_off_status="Marked as not edited",
            batch_on_status="Marked {count} images as edited",
            batch_off_status="Marked {count} images as not edited",
        )

    # Restacked is pipeline-managed (set after Helicon success), so keep it
    # single-image and non-batch-aware.
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

        if not self._persist_sidecar():
            return False
        self._metadata_cache_index = (-1, -1)
        filter_reapplied = self._reapply_flag_filter_after_metadata_change(
            "restacked",
            image_path,
        )
        self.dataChanged.emit()
        self.sync_ui_state(image_count_changed=filter_reapplied)
        status = "restacked" if meta.restacked else "not restacked"
        self.update_status_message(f"Marked as {status}")
        log.info("Toggled restacked flag to %s for %s", meta.restacked, image_path)

    def toggle_favorite(self):
        """Toggle favorite flag for current image or active batch."""
        return self._toggle_user_flag(
            flag_attr="favorite",
            single_on_status="Favorited",
            single_off_status="Unfavorited",
            batch_on_status="Favorited {count} images",
            batch_off_status="Unfavorited {count} images",
        )

    def toggle_stacked(self):
        """Toggle stacked flag for current image or active batch."""
        return self._toggle_user_flag(
            flag_attr="stacked",
            date_attr="stacked_date",
            single_on_status="Marked as stacked",
            single_off_status="Marked as not stacked",
            batch_on_status="Marked {count} images as stacked",
            batch_off_status="Marked {count} images as not stacked",
        )

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
        # migrate=False: this is a read-only display lookup that runs once per
        # navigation step; the legacy-key migration scan would otherwise do
        # O(sidecar entries) filesystem checks for every image that has no
        # entry — hundreds of stats per keypress while holding an arrow key.
        meta = self.sidecar.get_metadata(image_path, create=False, migrate=False)
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
        exif_key = self._exif_brief_context_key(self.current_index)
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
            "batch": meta.batch if meta else False,
            "stack_info_text": stack_info,
            "batch_info_text": batch_info,
        }
        self._metadata_cache_index = cache_key
        return self._metadata_cache

    _EXIF_SUFFIXES = frozenset(
        {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".heif", ".heic"}
    )

    def _exif_source_path(self, index: int) -> Path:
        """Return the file path used as the EXIF source of image at *index*."""
        img = self.image_files[index]
        source_path = img.path
        key_cf = get_group_key_for_path(img.path, self._variant_map)
        if key_cf is not None:
            group = self._variant_map[key_cf]
            if group.main_path is not None:
                source_path = group.main_path
        return source_path

    def _exif_source_key(self, index: int) -> str:
        """Return a normalized cache key for the EXIF source of image at *index*.

        Resolves to the variant group's main image path when available,
        so backups/developed files use the same EXIF as the original.
        Uses ``normalize_path_key`` for Windows case-insensitivity.
        """
        source_path = self._exif_source_path(index)
        return normalize_path_key(source_path)

    def _exif_brief_context_key(self, index: int) -> tuple[str, str]:
        """Return the EXIF brief cache key for current image and its neighbor."""
        previous_key = self._exif_source_key(index - 1) if index > 0 else ""
        return self._exif_source_key(index), previous_key

    def _read_exif_deferred(self):
        """Called after 150ms of no navigation. Submits EXIF read to background."""
        if getattr(self, "_shutting_down", False):
            return
        if not self.image_files or self.current_index >= len(self.image_files):
            return
        exif_key = self._exif_brief_context_key(self.current_index)
        if self._exif_pending_path is not None and self._exif_pending_path != exif_key:
            self._exif_pending_path = None
            return  # Different image path pending, or we aren't tracking this key.

        if exif_key in self._exif_brief_cache:
            self._exif_pending_path = None
            return  # already cached
        source_path = self._exif_source_path(self.current_index)
        previous_path = (
            self._exif_source_path(self.current_index - 1)
            if self.current_index > 0
            else None
        )
        # Early return for formats without EXIF support
        if source_path.suffix.lower() not in self._EXIF_SUFFIXES:
            self._exif_brief_cache[exif_key] = ""
            self._exif_pending_path = None
            return
        # Submit to dedicated EXIF executor to avoid blocking histograms
        signal = self._exifBriefReady

        def _worker(
            key=exif_key,
            p=str(source_path),
            previous=str(previous_path) if previous_path is not None else None,
        ):
            from faststack.imaging.metadata import get_exif_brief

            try:
                brief = get_exif_brief(p, previous)
            except Exception as e:
                log.error("Failed to get EXIF brief for %s: %s", p, e, exc_info=True)
                brief = ""
            signal.emit(key, brief)

        try:
            self._exif_executor.submit(_worker)
        except RuntimeError:
            pass  # executor shut down, ignore

    @Slot(object, str)
    def _on_exif_brief_ready(self, exif_key: tuple[str, str], brief: str):
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
            and self._exif_brief_context_key(self.current_index) == exif_key
        ):
            self._metadata_cache_index = (-1, -1)
            if self.ui_state:
                self.ui_state.metadataChanged.emit()

    @staticmethod
    def _merge_stack_ranges(ranges: List[List[int]]) -> List[List[int]]:
        """Sort ranges and merge overlapping or adjacent ones."""
        if not ranges:
            return []
        ranges = sorted([int(start), int(end)] for start, end in ranges)
        merged = [ranges[0]]
        for current_start, current_end in ranges[1:]:
            last_start, last_end = merged[-1]
            if current_start <= last_end + 1:
                merged[-1] = [last_start, max(last_end, current_end)]
            else:
                merged.append([current_start, current_end])
        return merged

    def _define_pending_stack(self) -> Optional[bool]:
        if self.stack_start_index is None or self.stack_end_index is None:
            return None

        start = min(self.stack_start_index, self.stack_end_index)
        end = max(self.stack_start_index, self.stack_end_index)
        self.stacks.append([start, end])
        self.stacks = self._merge_stack_ranges(self.stacks)
        if not self._persist_stack_state():
            return False
        log.info("Defined new stack: [%d, %d]", start, end)
        self.stack_start_index = None
        self.stack_end_index = None
        self._update_filter_identity_snapshot("stack")
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()  # Notify QML of data change
        self.ui_state.stackSummaryChanged.emit()  # Update stack summary in dialog
        self.sync_ui_state()
        count = end - start + 1
        self.update_status_message(
            f"Stack defined: {count} image{'' if count == 1 else 's'}"
        )
        return True

    def begin_new_stack(self):
        # Pressing [ again on the image that already holds the pending start
        # marker toggles it off, so an accidental press can be undone.
        if self.stack_start_index == self.current_index:
            self.stack_start_index = None
            self._update_filter_identity_snapshot("stack")
            log.info("Stack start cleared at index %d", self.current_index)
            self._metadata_cache_index = (-1, -1)  # Invalidate cache
            self.dataChanged.emit()  # Update UI to remove start marker
            self.sync_ui_state()
            self.update_status_message("Stack start cleared")
            return
        self.stack_start_index = self.current_index
        log.info("Stack start marked at index %d", self.stack_start_index)
        defined = self._define_pending_stack()
        if defined is not None:
            return defined
        self._update_filter_identity_snapshot("stack")
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()  # Update UI to show start marker
        self.sync_ui_state()
        self.update_status_message("Stack start marked")

    def end_current_stack(self):
        # Pressing ] again on the image that already holds the pending end
        # marker toggles it off, so an accidental press can be undone.
        if self.stack_end_index == self.current_index:
            self.stack_end_index = None
            self._update_filter_identity_snapshot("stack")
            log.info("Stack end cleared at index %d", self.current_index)
            self._metadata_cache_index = (-1, -1)  # Invalidate cache
            self.dataChanged.emit()  # Update UI to remove end marker
            self.sync_ui_state()
            self.update_status_message("Stack end cleared")
            return
        self.stack_end_index = self.current_index
        log.info("Stack end marked at index %d", self.stack_end_index)
        defined = self._define_pending_stack()
        if defined is not None:
            return defined
        self._update_filter_identity_snapshot("stack")
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()  # Update UI to show end marker
        self.sync_ui_state()
        self.update_status_message("Stack end marked")

    def begin_new_batch(self):
        """Mark the start of a new batch for drag-and-drop."""
        self.batch_start_index = self.current_index
        self._update_filter_identity_snapshot("batch")
        log.info("Batch start marked at index %d", self.batch_start_index)
        self._metadata_cache_index = (-1, -1)  # Invalidate cache
        self.dataChanged.emit()
        self.sync_ui_state()
        self.update_status_message("Batch start marked")

    def end_current_batch(self) -> bool:
        """End the current batch and save the range."""
        log.info(
            "end_current_batch called. batch_start_index: %s", self.batch_start_index
        )
        if self.batch_start_index is not None:
            start = min(self.batch_start_index, self.current_index)
            end = max(self.batch_start_index, self.current_index)
            self.batches.append([start, end])
            log.info("Defined new batch: [%d, %d]", start, end)
            self.batch_start_index = None
            if not self._finalize_batch_state(reapply_filter=True):
                return False
            count = self.get_batch_count_for_current_image()
            self.update_status_message(
                f"Batch defined: {count} image{'' if count == 1 else 's'}"
            )
            return True
        else:
            log.warning("No batch start marked. Press '{' first.")
            self.update_status_message("No batch start marked")
            return False

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
            if not self._finalize_batch_state(reapply_filter=True):
                return False
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
            if not self._finalize_batch_state(reapply_filter=True):
                return False
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
            if not self._finalize_batch_state(reapply_filter=True):
                return False
            self.update_status_message(
                f"Added {added_count} uploaded image(s) to batch ({len(indices_to_add)} total uploaded)"
            )
            log.info("Added %d uploaded image(s) to batch", added_count)
        else:
            self.update_status_message(
                f"All {len(indices_to_add)} uploaded image(s) already in batch."
            )

    def add_edited_to_batch(self):
        """Add all edited-flagged images in the current directory to the batch."""
        if not self.image_files:
            self.update_status_message("No images loaded.")
            return

        indices_to_add = []
        for i, img in enumerate(self.image_files):
            meta = self.sidecar.get_metadata(img.path, create=False)
            if meta and meta.edited:
                indices_to_add.append(i)

        if not indices_to_add:
            self.update_status_message("No edited images found.")
            return

        added_count = 0
        for idx in indices_to_add:
            in_batch = any(start <= idx <= end for start, end in self.batches)
            if not in_batch:
                self.batches.append([idx, idx])
                added_count += 1

        if added_count > 0:
            if not self._finalize_batch_state(reapply_filter=True):
                return False
            self.update_status_message(
                f"Added {added_count} edited image(s) to batch ({len(indices_to_add)} total edited)"
            )
            log.info("Added %d edited image(s) to batch", added_count)
        else:
            self.update_status_message(
                f"All {len(indices_to_add)} edited image(s) already in batch."
            )

    def _auto_add_edited_to_batch_if_enabled(self, image_path: Path) -> bool:
        """If auto-add is enabled, add the edited image at this path to the batch."""
        if not self.ui_state.autoAddEditedToBatch:
            return True

        if not self.image_files:
            return True

        try:
            image_key = self.sidecar.metadata_key_for_path(image_path)
        except (OSError, TypeError, ValueError):
            image_key = None

        # The edited image is almost always the current one; checking it first
        # avoids paying a per-image sidecar-key lookup across the whole folder
        # on every quick auto-adjust keystroke.
        indices = list(range(len(self.image_files)))
        if 0 <= self.current_index < len(indices):
            indices.insert(0, indices.pop(self.current_index))
        for i in indices:
            img = self.image_files[i]
            try:
                matches = (
                    image_key is not None
                    and self.sidecar.metadata_key_for_path(img.path) == image_key
                )
            except (OSError, TypeError, ValueError):
                matches = self._key(img.path) == self._key(image_path)

            if matches:
                in_batch = any(start <= i <= end for start, end in self.batches)
                if not in_batch:
                    self.batches.append([i, i])
                    # This runs inside the first slider move or auto-adjust
                    # keystroke on an image, so the sidecar write goes to the
                    # background worker instead of stalling that interaction.
                    if not self._finalize_batch_state(
                        reapply_filter=True, background_persist=True
                    ):
                        return False
                    log.info("Auto-added edited image to batch: %s", image_path.name)
                return True
        return True

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
                removed = True
                batch_modified = True
            else:
                new_batches.append([start, end])

        if batch_modified:
            self.batches = new_batches
            persisted = self._finalize_batch_state(
                emit=False, sync=False, reapply_filter=True
            )

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
                    removed = True
                    stack_modified = True
                else:
                    new_stacks.append([start, end])

            if stack_modified:
                self.stacks = new_stacks
                persisted = self._persist_stack_state()
        if removed:
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.ui_state.stackSummaryChanged.emit()
            self.sync_ui_state()
            if not persisted:
                return False
            self.update_status_message(
                "Removed from batch" if batch_modified else "Removed from stack"
            )
            return True
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
            success_message = "Removed image from batch"
            log.info("Removed index %d from a batch.", index_to_toggle)
        else:
            had_batches = bool(self.batches)
            self.batches.append([index_to_toggle, index_to_toggle])
            if not had_batches:
                success_message = "Created new batch with current image."
                log.info(
                    "No existing batches. Created new batch for index %d.",
                    index_to_toggle,
                )
            else:
                success_message = "Added image to batch"
                log.info("Added index %d to batch.", index_to_toggle)

        if not self._finalize_batch_state(reapply_filter=True):
            return False
        self.update_status_message(success_message)
        return True

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

                    # Merge overlapping or adjacent stacks
                    self.stacks = self._merge_stack_ranges(self.stacks)

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

        if not self._persist_stack_state():
            return False
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()
        return True

    @staticmethod
    def _normalize_crop_box_tuple(
        crop_box: Any,
    ) -> Optional[Tuple[int, int, int, int]]:
        if crop_box is None:
            return None
        try:
            if hasattr(crop_box, "toVariant"):
                crop_box = crop_box.toVariant()
            if isinstance(crop_box, list):
                crop_box = tuple(crop_box)
            if not isinstance(crop_box, tuple) or len(crop_box) != 4:
                return None
            left, top, right, bottom = [
                max(0, min(1000, int(value))) for value in crop_box
            ]
        except (TypeError, ValueError):
            return None
        return (
            min(left, right),
            min(top, bottom),
            max(left, right),
            max(top, bottom),
        )

    def _set_crop_overlay_box_only(self, crop_box: Tuple[int, int, int, int]) -> None:
        if hasattr(self.ui_state, "set_current_crop_box_visual_only"):
            self.ui_state.set_current_crop_box_visual_only(crop_box)
            return
        self.ui_state.currentCropBox = crop_box

    def _clear_crop_mode_snapshot(self) -> None:
        self._crop_mode_has_saved_geometry = False
        self._crop_mode_saved_crop_box = None
        self._crop_mode_saved_straighten_angle = 0.0
        self._crop_mode_saved_rotation = 0
        self._crop_mode_saved_path_key = None
        self._crop_mode_saved_session_id = None

    def _snapshot_crop_mode_geometry(self) -> None:
        edits = getattr(self.image_editor, "current_edits", None) or {}
        self._crop_mode_saved_crop_box = self._normalize_crop_box_tuple(
            edits.get("crop_box")
        )
        try:
            self._crop_mode_saved_straighten_angle = float(
                edits.get("straighten_angle", 0.0)
            )
        except (TypeError, ValueError):
            self._crop_mode_saved_straighten_angle = 0.0
        try:
            self._crop_mode_saved_rotation = int(edits.get("rotation", 0)) % 360
        except (TypeError, ValueError):
            self._crop_mode_saved_rotation = 0

        self._crop_mode_saved_path_key = None
        if self.image_editor.current_filepath is not None:
            try:
                self._crop_mode_saved_path_key = self._key(
                    self.image_editor.current_filepath
                )
            except (OSError, TypeError, ValueError):
                self._crop_mode_saved_path_key = None
        self._crop_mode_saved_session_id = getattr(
            self.image_editor, "session_id", None
        )
        self._crop_mode_has_saved_geometry = True
        log.debug(
            "Crop snapshot: crop=%s angle=%.4f rotation=%s path=%s session=%s",
            self._crop_mode_saved_crop_box,
            self._crop_mode_saved_straighten_angle,
            self._crop_mode_saved_rotation,
            self._crop_mode_saved_path_key,
            self._crop_mode_saved_session_id,
        )

    def _restore_crop_mode_geometry(self) -> bool:
        if not self._crop_mode_has_saved_geometry:
            return False
        if not self.image_editor:
            return False

        if self._crop_mode_saved_path_key and self.image_editor.current_filepath:
            try:
                current_path_key = self._key(self.image_editor.current_filepath)
            except (OSError, TypeError, ValueError):
                current_path_key = None
            if current_path_key != self._crop_mode_saved_path_key:
                return False

        saved_session_id = self._crop_mode_saved_session_id
        if (
            saved_session_id is not None
            and getattr(self.image_editor, "session_id", None) != saved_session_id
        ):
            return False

        saved_crop_box = self._crop_mode_saved_crop_box
        saved_angle = self._crop_mode_saved_straighten_angle
        saved_rotation = self._crop_mode_saved_rotation
        changed = False
        with self.image_editor._lock:
            edits = self.image_editor.current_edits
            if edits.get("crop_box") != saved_crop_box:
                edits["crop_box"] = saved_crop_box
                changed = True

            try:
                current_angle = float(edits.get("straighten_angle", 0.0))
            except (TypeError, ValueError):
                current_angle = 0.0
            if not math.isclose(
                current_angle,
                saved_angle,
                rel_tol=1e-5,
                abs_tol=1e-7,
            ):
                edits["straighten_angle"] = saved_angle
                changed = True

            try:
                current_rotation = int(edits.get("rotation", 0)) % 360
            except (TypeError, ValueError):
                current_rotation = 0
            if current_rotation != saved_rotation:
                edits["rotation"] = saved_rotation
                changed = True

            if changed:
                self.image_editor._edits_rev += 1
                self.image_editor._cached_preview = None
                self.image_editor._cached_rev = -1

        overlay_box = saved_crop_box or (0, 0, 1000, 1000)
        self._set_crop_overlay_box_only(overlay_box)
        if hasattr(self.ui_state, "cropRotation"):
            self.ui_state.cropRotation = 0.0
        log.debug(
            "Crop restore: crop=%s angle=%.4f rotation=%s changed=%s",
            saved_crop_box,
            saved_angle,
            saved_rotation,
            changed,
        )
        return True

    def _reset_crop_only(self, *, clear_crop_transaction: bool = True):
        """Resets crop settings (crop box and straighten) to default and exits crop mode, PRESERVING 90-deg rotation."""
        if clear_crop_transaction:
            self._clear_crop_mode_snapshot()
        if self.ui_state.isCropping:
            self.ui_state.isCropRotating = False
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
        """Launch the focus stacker with defined stacks, preferring RAW if asked."""
        semantic_stack_groups = (
            self._filter_identity_snapshot["stack_groups"]
            if self._filter_enabled and self._filter_identity_snapshot is not None
            else []
        )
        if self.stacks or semantic_stack_groups:
            log.info(
                "Launching focus stacker for %d defined stacks (use_raw=%s).",
                len(semantic_stack_groups) or len(self.stacks),
                use_raw,
            )
            launched = 0
            failed_stacks = []
            launch_failed = 0
            visible_keys = {self._key(image.path) for image in self.image_files}
            hidden_semantic_groups = [
                group
                for group in semantic_stack_groups
                if any(self._key(path) not in visible_keys for path in group)
            ]
            hidden_group_keys = {
                self._key(path) for group in hidden_semantic_groups for path in group
            }
            requested_stacks = [stack[:] for stack in self.stacks]
            for start, end in requested_stacks:
                files_to_process = []
                expected_count = end - start + 1
                range_is_valid = (
                    start >= 0 and end >= start and end < len(self.image_files)
                )
                if range_is_valid:
                    for idx in range(start, end + 1):
                        img_file = self.image_files[idx]
                        # Use RAW if available and requested, otherwise use JPG
                        if use_raw and img_file.raw_pair:
                            file_to_use = img_file.raw_pair
                        else:
                            file_to_use = img_file.path
                        files_to_process.append(file_to_use)

                visible_stack_keys = {
                    self._key(self.image_files[idx].path)
                    for idx in range(start, end + 1)
                    if 0 <= idx < len(self.image_files)
                }
                if visible_stack_keys & hidden_group_keys:
                    log.warning(
                        "Stack [%d, %d] contains members hidden by the active "
                        "filter; keeping the complete semantic stack for retry.",
                        start,
                        end,
                    )
                    failed_stacks.append([start, end])
                    continue

                unique_count = len(dict.fromkeys(files_to_process))
                if (
                    range_is_valid
                    and len(files_to_process) == expected_count
                    and unique_count == expected_count
                ):
                    success = self._launch_helicon_with_files(files_to_process)
                    if success:
                        launched += 1
                    else:
                        launch_failed += 1
                        failed_stacks.append([start, end])
                else:
                    log.warning(
                        "Stack [%d, %d] resolved %d unique inputs; expected %d. "
                        "Keeping it for retry.",
                        start,
                        end,
                        unique_count,
                        expected_count,
                    )
                    launch_failed += 1
                    failed_stacks.append([start, end])

            if launched:
                stack_state_saved = True
                if failed_stacks:
                    self.stacks = failed_stacks
                    self.stack_start_index = None
                    self.stack_end_index = None
                    stack_state_saved = self._persist_stack_state()
                    self._metadata_cache_index = (-1, -1)
                    self.dataChanged.emit()
                    self.ui_state.stackSummaryChanged.emit()
                elif self._filter_enabled:
                    # Every launched stack was complete and fully visible.
                    # Remove those visible identities while retaining semantic
                    # stacks that were wholly hidden and therefore unlaunched.
                    self.stacks = []
                    self.stack_start_index = None
                    self.stack_end_index = None
                    stack_state_saved = self._persist_stack_state()
                    self._metadata_cache_index = (-1, -1)
                    self.dataChanged.emit()
                    self.ui_state.stackSummaryChanged.emit()
                else:
                    stack_state_saved = self.clear_all_stacks()
                if not stack_state_saved:
                    return False

            hidden_count = len(hidden_semantic_groups)
            if hidden_count:
                self.update_status_message(
                    f"{hidden_count} stack{'s' if hidden_count != 1 else ''} "
                    "contains images hidden by the current filter; clear the "
                    f"filter before launching. {launched} other "
                    f"stack{'s' if launched != 1 else ''} launched"
                    + (
                        f"; {launch_failed} other "
                        f"stack{'s' if launch_failed != 1 else ''} kept after failure."
                        if launch_failed
                        else "."
                    ),
                    timeout=10000,
                    color="#FFD54F",
                )
            elif failed_stacks:
                self.update_status_message(
                    f"{launched} stack{'s' if launched != 1 else ''} launched; "
                    f"{len(failed_stacks)} kept for retry.",
                    timeout=8000,
                    color="#FFD54F",
                )
            elif launched:
                self.update_status_message(
                    f"{launched} stack{'s' if launched != 1 else ''} launched."
                )

        else:
            log.warning("No selection or stacks defined to launch the focus stacker.")
            return

        self.sync_ui_state()

    def _launch_helicon_with_files(self, files: List[Path]) -> bool:
        """Launch the configured focus stacker with RAW or JPG files.

        Returns:
            True if the focus stacker was successfully launched, otherwise False.
            This reports the launch only: once the external process is running
            the stack must not be offered for retry (that would duplicate the
            work), so a stacked-metadata save failure is reported and left
            dirty for a later save instead of turning into a False here.
        """
        log.info("Launching focus stacker with %d files.", len(files))
        unique_files = list(dict.fromkeys(files))
        success, launch = launch_focus_stacker(unique_files)
        if success and launch:
            self._track_helicon_launch(launch)

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
            if not self._persist_sidecar():
                # _persist_sidecar() already surfaced the error and kept the
                # in-memory flags dirty for the next save.
                log.warning("Focus stacker launched but stacked metadata was not saved")
            self._metadata_cache_index = (-1, -1)  # Invalidate cache

        return success

    def _track_helicon_launch(self, launch: FocusStackerLaunch) -> None:
        """Track a stacker child and retain any Helicon manifest until exit."""
        launch_key = id(launch.process)
        with self._helicon_launches_lock:
            self._helicon_launches[launch_key] = launch

        def cleanup_after_exit() -> None:
            try:
                try:
                    launch.process.wait()
                except (OSError, subprocess.SubprocessError):
                    log.warning(
                        "Error waiting for focus stacker process", exc_info=True
                    )
                    return
                if launch.manifest_path is not None:
                    try:
                        launch.manifest_path.unlink(missing_ok=True)
                    except OSError:
                        log.warning(
                            "Helicon manifest cleanup failed; artifact remains at %s",
                            launch.manifest_path,
                            exc_info=True,
                        )
            finally:
                with self._helicon_launches_lock:
                    self._helicon_launches.pop(launch_key, None)

        threading.Thread(
            target=cleanup_after_exit,
            daemon=True,
            name="FocusStackerCleanup",
        ).start()

    def _visible_index_for_path(self, path: Optional[Path]) -> Optional[int]:
        return self._path_to_index.get(self._key(path)) if path else None

    def _path_groups_in_folder_order(self, member_keys: set[str]) -> list[list[Path]]:
        """Group member paths into contiguous runs of the unfiltered folder order."""
        groups: list[list[Path]] = []
        run: list[Path] = []
        for image in self._sorted_all_images_for_identity():
            if self._key(image.path) in member_keys:
                run.append(image.path)
            elif run:
                groups.append(run)
                run = []
        if run:
            groups.append(run)
        return groups

    def _undo_entry_is_for_this_folder(self, data: dict[str, Any], what: str) -> bool:
        """Clear-undo entries apply to one folder's sidecar only."""
        if data.get("directory_key") == self._key(self.image_dir):
            return True
        folder = Path(data.get("directory") or "").name or "another folder"
        self.update_status_message(
            f"Can't undo here: the {what} were cleared in {folder}.", timeout=5000
        )
        return False

    def _snapshot_stacks_for_undo(self) -> Optional[dict[str, Any]]:
        """Capture everything clear_all_stacks() drops, keyed by path.

        ``groups`` covers the whole folder, hidden images included, so the undo
        is right whatever the filter state is when it runs.
        """
        with self.sidecar._state_lock:
            sidecar_state = copy.deepcopy(
                (
                    self.sidecar.data.stacks,
                    self.sidecar.data.stack_paths,
                    self.sidecar.data.stack_order,
                )
            )
        identity = self._capture_filter_identity()
        snapshot = self._filter_identity_snapshot
        if self._filter_enabled and snapshot is not None:
            self._update_filter_identity_snapshot("stack")
            groups = copy.deepcopy(snapshot["stack_groups"])
            start, end = snapshot["stack_start"], snapshot["stack_end"]
        else:
            groups = identity["stack_groups"]
            start, end = identity["stack_start"], identity["stack_end"]
        if not (groups or start or end or sidecar_state[0] or sidecar_state[1]):
            return None
        return {
            "directory_key": self._key(self.image_dir),
            "directory": str(self.image_dir),
            "groups": groups,
            "start": start,
            "end": end,
            "blocked": self._stack_identity_projection_blocked,
            "sidecar": sidecar_state,
        }

    def _undo_clear_stacks(self, data: dict[str, Any]) -> bool:
        """Restore the stacks removed by the last clear_all_stacks()."""
        if not self._undo_entry_is_for_this_folder(data, "stacks"):
            return False
        snapshot = self._filter_identity_snapshot
        if data["blocked"]:
            # The stacks could not be shown in this image list, so they were
            # only on disk: put the stored identities back as they were.
            stacks, stack_paths, stack_order = copy.deepcopy(data["sidecar"])
            with self.sidecar._state_lock:
                self.sidecar.data.stacks = stacks
                self.sidecar.data.stack_paths = stack_paths
                self.sidecar.data.stack_order = stack_order
            self._stack_identity_projection_blocked = True
            self.stacks = []
            persisted = self._persist_sidecar()
        else:
            if self._filter_enabled and snapshot is not None:
                # Hidden members live only here until the filter lifts.
                snapshot["stack_groups"] = copy.deepcopy(data["groups"])
                snapshot["stack_start"] = data["start"]
                snapshot["stack_end"] = data["end"]
            self.stacks = self._rebuild_ranges_from_paths(data["groups"])
            persisted = self._persist_stack_state()
        self.stack_start_index = self._visible_index_for_path(data["start"])
        self.stack_end_index = self._visible_index_for_path(data["end"])
        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()
        if not persisted:
            return False
        self.update_status_message("Restored cleared stacks")
        return True

    def _undo_clear_batches(self, data: dict[str, Any]) -> bool:
        """Restore the batches removed by the last clear_all_batches()."""
        if not self._undo_entry_is_for_this_folder(data, "batches"):
            return False
        preserved_path = self._current_image_path()
        present = {self._key(image.path) for image in self._all_images}
        restored_keys: set[str] = set()
        for path in data["paths"]:
            key = self._key(path)
            if key not in present:
                continue  # Deleted or moved since; never recreate its entry.
            meta = self.sidecar.get_metadata(path)
            if meta is not None:
                meta.batch = True
                restored_keys.add(key)
        snapshot = self._filter_identity_snapshot
        if self._filter_enabled and snapshot is not None:
            # Hidden members live only here until the filter lifts.
            snapshot["batch_groups"] = self._path_groups_in_folder_order(restored_keys)
            snapshot["batch_start"] = data["start"]
        self.batches = self._ranges_from_indices(
            [
                index
                for index, image in enumerate(self.image_files)
                if self._key(image.path) in restored_keys
            ]
        )
        self.batch_start_index = self._visible_index_for_path(data["start"])
        _ = self._finalize_batch_state(persist=False)
        if not self._persist_sidecar():
            return False
        filter_reapplied = self._reapply_flag_filter_after_metadata_change(
            "batch", preserved_path
        )
        if filter_reapplied:
            self.sync_ui_state(image_count_changed=True)
        self.update_status_message("Restored cleared batches")
        return True

    def clear_all_stacks(self):
        log.info("Clearing all defined stacks.")
        undo_data = self._snapshot_stacks_for_undo()
        if undo_data is not None:
            undo_data["live_edit_mark"] = (
                self._get_current_preview_session_provenance()
            )
            # Recorded before the write so a failed persist is still undoable.
            self.undo_history.append(("clear_stacks", undo_data, time.time()))
        self.stacks = []
        self.stack_start_index = None
        self.stack_end_index = None
        # Do NOT clear batches here

        persisted = self._persist_stack_state(clear_all=True)

        self._metadata_cache_index = (-1, -1)
        self.dataChanged.emit()
        self.ui_state.stackSummaryChanged.emit()
        self.sync_ui_state()
        if not persisted:
            return False
        if undo_data is not None:
            self.update_status_message("All stacks cleared (Ctrl+Z to undo)")
        else:
            self.update_status_message("All stacks cleared")
        return True

    def clear_all_batches(self):
        """Clear all defined batches."""
        log.info("Clearing all defined batches.")
        preserved_path = self._current_image_path()
        snapshot = self._filter_identity_snapshot
        batch_start = (
            snapshot["batch_start"]
            if self._filter_enabled and snapshot is not None
            else self._capture_filter_identity()["batch_start"]
        )
        batched_paths: list[Path] = []
        for image in self._all_images:
            meta = self.sidecar.get_metadata(image.path, create=False, migrate=False)
            if meta is not None:
                if meta.batch:
                    batched_paths.append(image.path)
                meta.batch = False
        undoable = bool(batched_paths or self.batches or batch_start is not None)
        if undoable:
            # Recorded before the write so a failed persist is still undoable.
            self.undo_history.append(
                (
                    "clear_batches",
                    {
                        "directory_key": self._key(self.image_dir),
                        "directory": str(self.image_dir),
                        "paths": batched_paths,
                        "start": batch_start,
                        "live_edit_mark": (
                            self._get_current_preview_session_provenance()
                        ),
                    },
                    time.time(),
                )
            )
        self.batches = []
        self.batch_start_index = None
        if snapshot is not None:
            snapshot["batch_groups"] = []
            snapshot["batch_start"] = None
        _ = self._finalize_batch_state(persist=False)
        if not self._persist_sidecar():
            return False
        filter_reapplied = self._reapply_flag_filter_after_metadata_change(
            "batch", preserved_path
        )
        if filter_reapplied:
            self.sync_ui_state(image_count_changed=True)
        if undoable:
            self.update_status_message("All batches cleared (Ctrl+Z to undo)")
        else:
            self.update_status_message("All batches cleared")
        return True

    def get_focus_stacker_path(self):
        return config.get("helicon", "exe")

    def set_focus_stacker_path(self, path):
        config.set("helicon", "exe", path)
        config.save()

    # Compatibility for older QML/plugins; the persisted config key remains
    # [helicon] exe so existing Windows and macOS installations keep working.
    def get_helicon_path(self):
        return self.get_focus_stacker_path()

    def set_helicon_path(self, path):
        self.set_focus_stacker_path(path)

    def get_image_editor_path(self):
        return config.get("photoshop", "exe")

    def get_image_editor_history(self) -> List[str]:
        import json

        try:
            saved = json.loads(config.get("photoshop", "history", fallback="[]"))
        except (TypeError, ValueError):
            saved = []
        if not isinstance(saved, list):
            saved = []
        paths = [self.get_image_editor_path()]
        paths.extend(path for path in saved if isinstance(path, str))
        return list(
            dict.fromkeys(path.strip() for path in paths if path and path.strip())
        )

    def set_image_editor_path(self, path):
        import json

        history = self.get_image_editor_history()
        path = path.strip()
        if path:
            history = [path] + [previous for previous in history if previous != path]
        config.set("photoshop", "history", json.dumps(history))
        config.set("photoshop", "exe", path)
        config.save()

    def get_rawtherapee_path(self):
        return config.get("rawtherapee", "exe")

    def set_rawtherapee_path(self, path):
        config.set("rawtherapee", "exe", path)
        config.save()

    def get_raw_source_dir(self):
        return config.get("raw", "source_dir", fallback="")

    def set_raw_source_dir(self, path):
        config.set("raw", "source_dir", path)
        config.save()

    def get_main_photo_dir(self):
        return config.get("raw", "mirror_base", fallback="")

    def set_main_photo_dir(self, path):
        config.set("raw", "mirror_base", path)
        config.save()

    def get_secondary_raw_source_dir(self):
        return config.get("raw", "secondary_source_dir", fallback="")

    def set_secondary_raw_source_dir(self, path):
        config.set("raw", "secondary_source_dir", path)
        config.save()

    def get_additional_raw_source_dirs(self):
        return config.get("raw", "additional_source_dirs", fallback="")

    def set_additional_raw_source_dirs(self, paths):
        config.set("raw", "additional_source_dirs", paths)
        config.save()

    @staticmethod
    def _resolve_configured_directory(path_value: str | Path | None) -> Path | None:
        """Expand and resolve a configured directory path without requiring it to exist."""
        cleaned = str(path_value or "").strip().strip('"')
        if not cleaned:
            return None

        try:
            return resolve_configured_path(cleaned).resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    def _main_photo_directory(self) -> Path | None:
        return self._resolve_configured_directory(
            config.get("raw", "mirror_base", fallback="")
        )

    def _current_stack_input_context(self) -> tuple[Path, Path] | None:
        """Return the matching stack-input root and current relative directory."""
        current_directory = self.image_dir.resolve()
        for _label, configured_root in self._configured_restack_raw_locations():
            root = self._resolve_configured_directory(configured_root)
            if root is None:
                continue
            try:
                relative_directory = current_directory.relative_to(root)
            except ValueError:
                continue
            return root, relative_directory
        return None

    def _matching_stack_input_directory(self) -> Path | None:
        """Find the first available stack-input directory matching the current one."""
        main_photo_directory = self._main_photo_directory()
        if main_photo_directory is None:
            return None

        try:
            relative_directory = self.image_dir.resolve().relative_to(
                main_photo_directory
            )
        except ValueError:
            return None

        for _label, configured_root in self._configured_restack_raw_locations():
            stack_input_root = self._resolve_configured_directory(configured_root)
            if stack_input_root is None:
                continue
            candidate = stack_input_root / relative_directory
            if candidate.is_dir():
                return candidate
        return None

    def is_in_stack_input_directory(self) -> bool:
        return self._current_stack_input_context() is not None

    def stack_directory_switch_visible(self) -> bool:
        """Whether the Actions menu should include a photo-directory switch."""
        if self.is_in_stack_input_directory():
            return True
        return self._matching_stack_input_directory() is not None

    def switch_stack_input_directory(self):
        """Switch between matching main-photo and stack-input directories."""
        stack_input_context = self._current_stack_input_context()
        switching_to_main = stack_input_context is not None

        if switching_to_main:
            _stack_input_root, relative_directory = stack_input_context
            main_photo_directory = self._main_photo_directory()
            if main_photo_directory is None:
                self.update_status_message(
                    "The main photo directory is not configured or available"
                )
                return
            target_directory = main_photo_directory / relative_directory
            if not target_directory.is_dir():
                self.update_status_message(
                    f"Matching main photo directory is not available: {target_directory}"
                )
                return
        else:
            target_directory = self._matching_stack_input_directory()
            if target_directory is None:
                self.update_status_message(
                    "No matching stack input directory is available"
                )
                return

        log.info(
            "Switching photo directory from %s to %s",
            self.image_dir,
            target_directory,
        )
        self._switch_to_directory(target_directory, update_base_directory=True)
        destination_label = "main photo" if switching_to_main else "stack input"
        self.update_status_message(f"Switched to {destination_label} directory")

    def _dialog_start_directory(self, current_path: str) -> str:
        cleaned = current_path.strip().strip('"') if current_path else ""
        if not cleaned:
            return ""

        try:
            candidate = Path(os.path.expanduser(os.path.expandvars(cleaned)))
        except (OSError, ValueError):
            return ""

        if candidate.is_file():
            return str(candidate.parent)
        if candidate.is_dir():
            return str(candidate)

        for parent in candidate.parents:
            if parent.is_dir():
                return str(parent)

        return ""

    @staticmethod
    def _make_file_dialog(file_mode: QFileDialog.FileMode) -> QFileDialog:
        """Create a file dialog that stays usable over FastStack's QML windows."""
        dialog = QFileDialog()
        dialog.setFileMode(file_mode)
        if sys.platform.startswith("linux"):
            # The GTK3 native dialog keeps Open/Cancel in its header bar. When it
            # opens over another window, tiling compositors (e.g. Hyprland) can
            # fullscreen it, GTK hides the header bar, and nothing can be
            # selected. Qt's own dialog keeps its buttons in the window body.
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.resize(900, 600)
            parent_window = QGuiApplication.focusWindow()
            if parent_window is not None:
                # Make it a transient child so compositors float it as a dialog.
                dialog.winId()
                handle = dialog.windowHandle()
                if handle is not None:
                    handle.setTransientParent(parent_window)
        return dialog

    def open_file_dialog(self, current_path: str = ""):
        dialog = self._make_file_dialog(QFileDialog.FileMode.ExistingFile)
        # On Windows tool executables end in .exe, so lead with that filter.
        # On macOS/Linux the binaries are extensionless (e.g. inside a .app
        # bundle), so default to All Files or the dialog would appear empty.
        if os.name == "nt":
            dialog.setNameFilter("Executables (*.exe);;All Files (*)")
        else:
            dialog.setNameFilter("All Files (*);;Executables (*.exe)")
        start_dir = self._dialog_start_directory(current_path)
        if start_dir:
            dialog.setDirectory(start_dir)
        if self._exec_modal_dialog(dialog):
            return dialog.selectedFiles()[0]
        return ""

    def check_executable_path(self, path):
        if not path:
            return False
        # Reuse the same validation the launcher uses so the settings checkmark
        # never disagrees with whether the tool can actually be launched.
        expanded = os.path.expanduser(os.path.expandvars(path.strip().strip('"')))
        is_valid, _ = validate_executable_path(expanded, allow_custom_paths=True)
        return is_valid

    def check_focus_stacker_path(self, path):
        is_valid, _ = validate_focus_stacker_path(path)
        return is_valid

    def get_cache_size(self):
        return config.getfloat("core", "cache_size_gb")

    def get_cache_usage_gb(self):
        """Returns current cache usage in GB."""
        return self.image_cache.currsize / (1024**3)

    def set_cache_size(self, size):
        """Update cache size at runtime and persist to config."""
        size = max(0.5, min(size, 16.0))  # enforce sane bounds
        old_max_bytes = self.image_cache.max_bytes
        new_max_bytes = int(size * 1024**3)
        if old_max_bytes != new_max_bytes:
            self.image_cache.max_bytes = new_max_bytes
            log.info(
                "Resized decoded image cache from %.2f GB to %.2f GB",
                old_max_bytes / (1024**3),
                size,
            )
            with self._eviction_lock:
                self._eviction_events.clear()
                self._eviction_counts.clear()

        # Apply successfully before persisting, so a failed resize cannot leave
        # Settings reporting a saved size that the running cache did not adopt.
        config.set("core", "cache_size_gb", size)
        config.save()

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

    def get_navigation_rate_fps(self) -> int:
        """Return the configured steady held-key navigation rate."""
        fps = config.getfloat("core", "navigation_rate_fps", fallback=15.0)
        return round(max(1.0, min(fps, 30.0)))

    def set_navigation_rate_fps(self, fps: int) -> None:
        """Persist and immediately apply the held-key navigation rate."""
        bounded_fps = max(1, min(int(fps), 30))
        config.set("core", "navigation_rate_fps", bounded_fps)
        config.save()
        self._navigation_rate_fps = float(bounded_fps)
        self._navigation_interval_ms = max(1, round(1000.0 / bounded_fps))
        # Re-base the adaptive governor so a stale stretched (or relaxed)
        # interval from the previous rate cannot out-pace the new one.
        self._paced_interval_ms = float(self._navigation_interval_ms)
        self._paced_interval_max_ms = float(self._navigation_interval_ms) * 4.0
        self._navigation_hold_timer.setInterval(self._navigation_interval_ms)

    def get_editor_preview_long_edge(self) -> int:
        """Return the configured editing-preview long edge (0 = match window)."""
        configured = config.getint("core", "editor_preview_long_edge", fallback=0)
        if configured <= 0:
            return 0
        return max(
            EDITOR_PREVIEW_MIN_LONG_EDGE, min(configured, EDITOR_PREVIEW_MAX_LONG_EDGE)
        )

    def set_editor_preview_long_edge(self, long_edge: int) -> None:
        """Persist and immediately apply the editing-preview resolution."""
        try:
            requested = int(long_edge)
        except (TypeError, ValueError):
            log.warning("Ignoring invalid editor preview long edge: %r", long_edge)
            return
        if requested > 0:
            requested = max(
                EDITOR_PREVIEW_MIN_LONG_EDGE,
                min(requested, EDITOR_PREVIEW_MAX_LONG_EDGE),
            )
        else:
            requested = 0

        if requested == self.get_editor_preview_long_edge():
            return
        config.set("core", "editor_preview_long_edge", requested)
        config.save()
        self._apply_editor_preview_master_box()
        box_w, box_h = self.image_editor.preview_master_box
        log.info(
            "Editing preview resolution set to %s (master box %dx%d)",
            "auto" if requested == 0 else f"{requested}px",
            box_w,
            box_h,
        )
        self.update_status_message(
            "Editing preview: match window"
            if requested == 0
            else f"Editing preview: {requested}px"
        )

    def get_held_navigation_quality(self) -> str:
        """Return the configured held-key browsing quality tier."""
        return self._held_navigation_quality

    def set_held_navigation_quality(self, quality: str) -> None:
        """Persist and immediately apply the held-key browsing quality tier."""
        requested = str(quality or "").strip().lower()
        if requested not in HELD_NAVIGATION_QUALITY_DIMENSIONS:
            log.warning("Ignoring invalid held navigation quality: %r", quality)
            return

        config.set("core", "held_navigation_quality", requested)
        config.save()
        if requested == self._held_navigation_quality:
            return

        old_quality = self._held_navigation_quality
        self._held_navigation_quality = requested
        max_dimension = held_navigation_quality_max_dimension(requested)
        self.prefetcher.fast_decode_max_dimension = max_dimension

        # Cached ``fast`` keys do not encode the browsing tier. Advance both
        # generations and clear the display cache so no buffer decoded at the
        # previous size can satisfy a request for the newly selected tier.
        self._cancel_paced_navigation()
        self._quality_decode_immediate_pending = False
        self._quality_decode_token += 1
        self._quality_decode_timer.stop()
        self.prefetcher.cancel_all()
        self._bump_display_generation()
        self.image_cache.clear()

        if self._loupe_decode_allowed() and self.image_files:
            self.prefetcher.update_prefetch(
                self.current_index,
                direction=self._last_navigation_direction,
            )
            self.sync_ui_state()
            self._restart_quality_decode_timer()

        log.info(
            "Held navigation quality changed from %s to %s (max dimension %dpx)",
            old_quality,
            requested,
            max_dimension,
        )
        self.update_status_message(
            f"Held-key image quality: {requested.title()}",
        )

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

    def save_config(self):
        """Save current UI state config values."""
        config.set(
            "core", "auto_add_edited_to_batch", self.ui_state.autoAddEditedToBatch
        )
        config.save()

    @Slot(result=str)
    def get_current_version(self):
        # Resolved once per run. This backs a QML binding, and every read
        # opened pyproject.toml and the package metadata again (and logged
        # their mismatch each time on an editable install).
        version = getattr(self, "_current_version", None)
        if version is None:
            version = self._current_version = get_current_version()
        return version

    @Slot(result=str)
    def get_readme_text(self):
        """Return the bundled README.md contents for the Help > View Readme view."""
        readme_path = faststack_readme_path()
        if readme_path is not None:
            try:
                return readme_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("Failed to read README at %s: %s", readme_path, exc)
                return f"# Unable to read README\n\n{exc}"
        # Wheel installs don't ship README.md as a file; fall back to the copy
        # setuptools embeds in the package metadata.
        text = readme_from_metadata()
        if text:
            return text
        return "# README not found\n\nThe README.md file could not be located."

    @Slot(result=bool)
    def get_update_check_enabled(self):
        return config.getboolean("updates", "check_for_updates", True)

    @Slot(bool)
    def set_update_check_enabled(self, enabled):
        config.set("updates", "check_for_updates", "true" if enabled else "false")
        config.save()

    def _update_timestamp(self, key: str) -> Optional[datetime]:
        """Read an ISO timestamp from the [updates] config section."""
        raw_value = config.get("updates", key, fallback="").strip()
        if not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            log.debug("Ignoring unparseable updates.%s value %r", key, raw_value)
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _record_update_check_outcome(self, *, succeeded: bool) -> None:
        """Persist when the last check reached GitHub, or failed to.

        The timestamps are written *after* the attempt finishes so a request
        that never got an answer cannot masquerade as a completed check.
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if succeeded:
            config.set("updates", "last_successful_update_check", now)
            # A success clears the backoff so the next failure starts fresh.
            config.set("updates", "last_failed_update_check", "")
        else:
            # The latest outcome controls automatic retry pacing. Keeping an
            # older success here would incorrectly retain the 24-hour cooldown.
            config.set("updates", "last_successful_update_check", "")
            config.set("updates", "last_failed_update_check", now)
        config.save()

    @Slot()
    def maybe_check_for_updates(self):
        """Run the automatic update check if enabled and outside the cooldown."""
        if not self.get_update_check_enabled():
            return

        if not should_check_for_updates(
            now=datetime.now(timezone.utc),
            last_success=self._update_timestamp("last_successful_update_check"),
            last_failure=self._update_timestamp("last_failed_update_check"),
        ):
            return

        self.check_for_updates(False)

    @Slot(bool)
    def check_for_updates(self, manual=False):
        """Check GitHub Releases for a newer FastStack version.

        The HTTP request always runs on the single-worker update executor; the
        GUI thread never blocks on the network. A manual check ignores the
        cooldown entirely (the caller decides), an automatic one has already
        been gated by maybe_check_for_updates().
        """
        manual = bool(manual)
        if self._shutting_down:
            return
        if self._update_check_inflight:
            if manual:
                self.update_status_message("Update check already running")
            return
        if not manual and not self.get_update_check_enabled():
            return

        self._update_check_inflight = True
        self._update_check_token += 1
        token = self._update_check_token

        if manual:
            self.update_status_message("Checking for updates...")

        current_version = get_current_version()
        try:
            future = self._update_executor.submit(
                check_for_update,
                current_version=current_version,
            )
        except RuntimeError as e:
            self._update_check_inflight = False
            log.warning("Could not start update check: %s", e)
            return

        def _done(fut):
            if self._shutting_down:
                return
            try:
                payload = fut.result().to_qml_dict()
                payload["error"] = ""
            except Exception as e:
                log.warning("Update check failed: %s", e)
                payload = {
                    "currentVersion": current_version,
                    "latestVersion": "",
                    "releaseUrl": "",
                    "summary": "",
                    "isNewer": False,
                    "error": str(e),
                }
            payload["manual"] = manual
            payload["token"] = token
            if self._shutting_down:
                return
            self._updateCheckFinished.emit(payload)

        future.add_done_callback(_done)

    @Slot(object)
    def _on_update_check_finished(self, payload):
        if self._shutting_down:
            return
        if int(payload.get("token", -1)) != self._update_check_token:
            return

        self._update_check_inflight = False
        manual = bool(payload.get("manual", False))
        error = str(payload.get("error") or "")
        if error:
            self._record_update_check_outcome(succeeded=False)
            if manual:
                self.update_status_message(
                    f"Could not check for updates: {error}", 6000
                )
            else:
                # An automatic check must never interrupt the user; the retry
                # window in updater.should_check_for_updates handles it.
                log.info("Automatic update check failed: %s", error)
            return

        self._record_update_check_outcome(succeeded=True)

        current_version = str(payload.get("currentVersion") or get_current_version())
        latest_version = str(payload.get("latestVersion") or "")
        if payload.get("isNewer"):
            if not manual and self._is_update_version_skipped(latest_version):
                log.info("Update %s is available but skipped by user.", latest_version)
                return

            if manual:
                # The user asked, so answer immediately with the full dialog.
                self._show_update_dialog(payload)
            else:
                # Automatic checks stay out of the way: raise the footer
                # banner and let the user open the dialog when they want to.
                self._set_pending_update(payload)
            return

        self._clear_pending_update()
        if manual:
            self.update_status_message(f"FastStack {current_version} is up to date")

    def _is_update_version_skipped(self, version: str) -> bool:
        """True when the user skipped this version (any build of it)."""
        ignored_version = config.get(
            "updates", "last_ignored_version", fallback=""
        ).strip()
        if not ignored_version:
            return False
        return same_base_version(ignored_version, version)

    def _show_update_dialog(self, payload) -> bool:
        """Open the modal update dialog, falling back to a status message."""
        if self.main_window and hasattr(self.main_window, "openUpdateDialog"):
            self.main_window.openUpdateDialog(payload)
            return True

        latest_version = str(payload.get("latestVersion") or "")
        self.update_status_message(f"FastStack {latest_version} is available", 8000)
        return False

    def _set_pending_update(self, payload) -> None:
        """Remember an available update and raise the unobtrusive banner."""
        self._pending_update_payload = dict(payload)
        self._notify_update_notice_changed()

    def _clear_pending_update(self) -> None:
        if self._pending_update_payload is None:
            return
        self._pending_update_payload = None
        self._notify_update_notice_changed()

    def _notify_update_notice_changed(self) -> None:
        ui_state = getattr(self, "ui_state", None)
        signal = getattr(ui_state, "updateNoticeChanged", None)
        if signal is not None:
            signal.emit()

    def get_pending_update_version(self) -> str:
        """Version string for the banner, or "" when there is nothing to show."""
        if not self._pending_update_payload:
            return ""
        return str(self._pending_update_payload.get("latestVersion") or "")

    @Slot()
    def show_pending_update(self):
        """Banner action: open the existing update dialog for the pending update."""
        payload = self._pending_update_payload
        if not payload:
            return
        self._show_update_dialog(payload)

    @Slot()
    def dismiss_pending_update(self):
        """Banner action: hide the notice for now without skipping the version."""
        self._clear_pending_update()

    @Slot(str)
    def skip_update_version(self, version):
        """Never notify about this application version again.

        The base version is stored, so skipping 1.6.8 also covers every
        v1.6.8-buildN rebuild of it. A later 1.6.9 still notifies normally.
        """
        normalized = normalize_version(str(version or ""))
        if not normalized:
            return
        config.set("updates", "last_ignored_version", normalized)
        config.save()
        self._clear_pending_update()
        self.update_status_message(f"Skipped FastStack {normalized}")

    @Slot(str)
    def open_update_release(self, url):
        """Open a release page, but only if it really is one of ours."""
        url = str(url or "").strip()
        if not url:
            self.update_status_message("No update release URL available")
            return
        if not is_release_url_allowed(url):
            log.warning("Refusing to open non-release update URL: %r", url)
            self.update_status_message(
                f"Update link was not a {GITHUB_REPOSITORY} release page; not opening",
                6000,
            )
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            self.update_status_message("Could not open update release page", 5000)

    @Slot()
    def open_changelog(self):
        changelog_path = faststack_changelog_path()
        if changelog_path is not None:
            if QDesktopServices.openUrl(QUrl.fromLocalFile(str(changelog_path))):
                return
            log.warning("Could not open local changelog: %s", changelog_path)

        changelog_url = f"https://github.com/{GITHUB_REPOSITORY}/blob/main/ChangeLog.md"
        if QDesktopServices.openUrl(QUrl(changelog_url)):
            message = (
                "Could not open local changelog; opened online changelog"
                if changelog_path is not None
                else "Local changelog not found; opened online changelog"
            )
            self.update_status_message(message, 5000)
            return

        self.update_status_message(
            "Could not open changelog locally or in browser",
            5000,
        )

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

    @Slot()
    def show_color_information_dialog(self):
        """Show a read-only dialog with color-management diagnostics."""
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image selected")
            return
        if self._is_grid_view_active:
            self.update_status_message(
                "Color Information is only available in loupe mode"
            )
            return

        if self.view_override_path and self.view_override_kind:
            displayed_path = self.view_override_path
        else:
            displayed_path = str(self.image_files[self.current_index].path)
        if self._block_if_saving(displayed_path):
            return

        try:
            text = self._build_color_info_text()
        except Exception as e:
            log.error("Failed to build color info: %s", e)
            text = f"Error gathering color information:\n{e}"

        if self.main_window and hasattr(self.main_window, "openColorInfoDialog"):
            self.main_window.openColorInfoDialog(text)

    def _build_color_info_text(self) -> str:
        from PIL import ExifTags, ImageCms

        lines: list[str] = []

        # --- FastStack Pipeline ---
        lines.append("=== FastStack Pipeline ===")
        lines.append("")

        color_mode = self.get_color_mode()
        mode_labels = {
            "none": "Off",
            "saturation": "Saturation Compensation",
            "icc": "Full ICC Profile",
        }
        lines.append(f"Color mode:  {mode_labels.get(color_mode, color_mode)}")

        if color_mode == "saturation":
            lines.append(f"Saturation factor:  {self.get_saturation_factor():.2f}")

        monitor_icc_path = config.get("color", "monitor_icc_path", fallback="").strip()
        lines.append(f"Monitor ICC path:  {monitor_icc_path or '(not configured)'}")

        if monitor_icc_path:
            exists = os.path.isfile(monitor_icc_path)
            lines.append(f"Monitor ICC exists:  {'Yes' if exists else 'No'}")
            if exists:
                try:
                    prof = get_monitor_profile()
                    if prof is not None:
                        lines.append("Monitor ICC loads:  Yes")
                        desc = get_icc_profile_description(prof)
                        lines.append(f"Monitor ICC description:  {desc}")
                        details = get_icc_profile_details(prof)
                        for key, val in details.items():
                            if key != "Description":
                                lines.append(f"  {key}:  {val}")
                    else:
                        lines.append("Monitor ICC loads:  No (returned None)")
                except Exception as e:
                    lines.append(f"Monitor ICC loads:  No ({e})")

        lines.append("")
        if color_mode == "none":
            lines.append("Output behavior:  No explicit color transform")
        elif color_mode == "saturation":
            lines.append(
                "Output behavior:  sRGB-style display with saturation adjustment"
            )
        elif color_mode == "icc":
            lines.append(
                "Output behavior:  Source profile converted to monitor ICC"
                " using ImageCms"
            )

        # --- Current Photo ---
        lines.append("")
        lines.append("=== Current Photo ===")
        lines.append("")

        img_file = self.image_files[self.current_index]
        if self.view_override_path and self.view_override_kind:
            displayed_path = self.view_override_path
            variant_label = self.view_override_kind
        else:
            displayed_path = str(img_file.path)
            variant_label = "main"

        lines.append(f"File path:  {displayed_path}")
        lines.append(f"Viewing:  {variant_label}")

        icc_bytes = None
        exif_color_space = None
        try:
            with Image.open(displayed_path) as pil_img:
                icc_bytes = pil_img.info.get("icc_profile")
                lines.append(f"Image mode:  {pil_img.mode}")
                fmt = pil_img.format
                if fmt:
                    lines.append(f"Image format:  {fmt}")

                try:
                    exif = pil_img.getexif()
                    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                    raw_cs = exif_ifd.get(0xA001)
                    if raw_cs is not None:
                        cs_labels = {1: "sRGB", 2: "AdobeRGB", 65535: "Uncalibrated"}
                        exif_color_space = cs_labels.get(raw_cs, f"Unknown ({raw_cs})")
                except Exception as e:
                    log.debug("Failed to parse EXIF color space: %s", e)
                    exif_color_space = f"Unreadable ({e})"
        except Exception as e:
            lines.append(f"Image read error:  {e}")

        # --- Color Profile Info ---
        lines.append("")
        lines.append("--- Color Profile ---")

        icc_desc = None
        if icc_bytes:
            lines.append("Embedded ICC profile:  Yes")
            try:
                src_prof = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
                icc_desc = get_icc_profile_description(src_prof)
                lines.append(f"Embedded ICC description:  {icc_desc}")
                details = get_icc_profile_details(src_prof)
                for key, val in details.items():
                    if key != "Description":
                        lines.append(f"  {key}:  {val}")
            except Exception as e:
                lines.append(f"  (unreadable: {e})")
        else:
            lines.append("Embedded ICC profile:  None")

        if exif_color_space:
            lines.append(f"EXIF color space:  {exif_color_space}")
        else:
            lines.append("EXIF color space:  Not set")

        # --- Source Interpretation ---
        # Reflects the real loupe decode path: embedded ICC is used when
        # present and readable, otherwise FastStack falls back to sRGB.
        # EXIF ColorSpace is reported as metadata only; it is NOT used as the
        # source profile during decode.
        lines.append("")
        if icc_desc:
            lines.append(
                f"Source interpretation:  {icc_desc} (from embedded ICC profile)"
            )
        elif icc_bytes:
            lines.append(
                "Source interpretation:  sRGB (FastStack fallback;"
                " embedded ICC unreadable)"
            )
        else:
            lines.append(
                "Source interpretation:  sRGB (FastStack fallback;"
                " no embedded ICC profile)"
            )

        return "\n".join(lines)

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

    @Slot(result=float)
    def get_awb_tint_damp(self):
        return config.getfloat("awb", "tint_damp", 0.6)

    @Slot(float)
    def set_awb_tint_damp(self, value):
        value = max(0.0, min(1.0, value))
        config.set("awb", "tint_damp", f"{value:.6g}")
        config.save()

    @Slot(float)
    def set_awb_strength(self, value):
        # The strength is read when auto white balance is next applied; it
        # does not change how already-decoded frames are displayed, so there
        # is nothing to invalidate here.
        config.set("awb", "strength", value)
        config.save()

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

        # During a crop transaction the QML rotate knob reports an angle
        # relative to the displayed image, which already has any committed
        # straighten baked in. Compose them so re-straightening an
        # already-straightened image doesn't silently discard the old angle.
        if self.ui_state.isCropping and self._crop_mode_has_saved_geometry:
            angle = self._crop_mode_saved_straighten_angle + angle

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

                    crop_box = (left, top, right, bottom)
                    if self.ui_state.isCropping:
                        self._set_crop_overlay_box_only(crop_box)
                    else:
                        self.ui_state.currentCropBox = crop_box
                        self.image_editor.set_crop_box(crop_box)

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
        if self.ui_state.isCropping:
            self._kick_preview_worker()

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

        # The setting changes how every frame is decoded, so invalidate the
        # way set_color_mode does: a fresh display generation stops in-flight
        # decodes made with the old setting from landing under live keys, and
        # the prefetch window is rebuilt instead of just redrawing one image.
        if old_value != optimize_for:
            log.info(
                f"Optimize for changed from {old_value} to {optimize_for}, clearing cache and redrawing"
            )
            self.image_cache.clear()
            self.prefetcher.cancel_all()
            self._bump_display_generation()
            if 0 <= self.current_index < len(self.image_files):
                self.prefetcher.update_prefetch(self.current_index)
                self.sync_ui_state()

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
    def get_auto_level_black_threshold(self):
        return self.auto_level_black_threshold

    @Slot(float)
    def set_auto_level_black_threshold(self, value):
        # 0 means "follow the shared clip threshold". The upper bound is far
        # looser than the shared threshold's because deepening blacks past a
        # few percent is a legitimate look for dark-background macro work.
        value = max(0.0, min(50.0, value))
        self.auto_level_black_threshold = value
        config.set("core", "auto_level_black_threshold", f"{value:.6g}")
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

    @Slot(result=bool)
    def get_auto_vibrance_enabled(self):
        return self.auto_vibrance_enabled

    @Slot(bool)
    def set_auto_vibrance_enabled(self, value):
        self.auto_vibrance_enabled = bool(value)
        config.set(
            "core",
            "auto_vibrance_enabled",
            "true" if self.auto_vibrance_enabled else "false",
        )
        config.save()

    @Slot(result=bool)
    def get_auto_level_midtone(self):
        return self.auto_level_midtone

    @Slot(bool)
    def set_auto_level_midtone(self, value):
        self.auto_level_midtone = bool(value)
        config.set(
            "core",
            "auto_level_midtone",
            "true" if self.auto_level_midtone else "false",
        )
        config.save()

    @Slot(result=float)
    def get_auto_level_midtone_target(self):
        return self.auto_level_midtone_target

    @Slot(float)
    def set_auto_level_midtone_target(self, value):
        value = max(0.2, min(0.6, value))
        self.auto_level_midtone_target = value
        config.set("core", "auto_level_midtone_target", f"{value:.6g}")
        config.save()

    @Slot(result=float)
    def get_auto_level_channel_budget(self):
        return self.auto_level_channel_budget

    @Slot(float)
    def set_auto_level_channel_budget(self, value):
        value = max(1.0, min(10.0, value))
        self.auto_level_channel_budget = value
        config.set("core", "auto_level_channel_budget", f"{value:.6g}")
        config.save()

    @Slot(result=bool)
    def get_levels_soft_knee(self):
        return self.levels_soft_knee

    @Slot(bool)
    def set_levels_soft_knee(self, value):
        self.levels_soft_knee = bool(value)
        self.image_editor.levels_soft_knee = self.levels_soft_knee
        config.set(
            "core",
            "levels_soft_knee",
            "true" if self.levels_soft_knee else "false",
        )
        config.save()
        # Levels rendering changed; refresh the live preview if edits exist.
        self._kick_preview_worker()

    @Slot(result=bool)
    def get_export_dither(self):
        return self.export_dither

    @Slot(bool)
    def set_export_dither(self, value):
        self.export_dither = bool(value)
        self.image_editor.export_dither = self.export_dither
        config.set(
            "core",
            "export_dither",
            "true" if self.export_dither else "false",
        )
        config.save()

    def open_directory_dialog(self, current_path: str = ""):
        dialog = self._make_file_dialog(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        start_dir = self._dialog_start_directory(current_path)
        if start_dir:
            dialog.setDirectory(start_dir)
        if self._exec_modal_dialog(dialog):
            return dialog.selectedFiles()[0]
        return ""

    def _exec_modal_dialog(self, dialog) -> int:
        """Run a native modal dialog while global shortcuts are disabled."""
        token = f"native:{id(dialog)}"
        self.dialog_opened(token)
        try:
            return dialog.exec()
        finally:
            self.dialog_closed(token)

    def _flush_directory_state_before_switch(self) -> None:
        """Persist the outgoing folder's sidecar and session position."""
        old_dir = getattr(self, "image_dir", None)
        old_sidecar = getattr(self, "sidecar", None)
        if old_sidecar is not None and getattr(old_sidecar, "dirty", False):
            try:
                old_sidecar.save()
            except Exception:
                log.exception("Could not flush the sidecar before leaving %s", old_dir)
        timer = getattr(self, "_session_save_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            try:
                self._persist_session_state()
            except Exception:
                log.exception(
                    "Could not persist the position before leaving %s", old_dir
                )

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
        target_dir = Path(folder_path).expanduser().resolve()
        target_watcher = Watcher(target_dir, self._request_watcher_refresh)
        try:
            target_watcher.start()
        except Exception:
            log.exception(
                "Watcher failed to start during directory switch for %s",
                target_dir,
            )
        try:
            target_snapshot = find_images_with_variants(target_dir)
        except OSError as exc:
            # Do not commit a half-switched controller whose list still belongs
            # to the old directory.
            try:
                target_watcher.stop()
            except Exception:
                pass
            log.error("Could not switch to directory %s: %s", target_dir, exc)
            self.update_status_message(
                f"Could not open folder: {exc}",
                timeout=10000,
                color="#FFD54F",
            )
            return False

        self._begin_direct_image_transition("directory switched")

        # Flush the folder being left before its sidecar manager is replaced:
        # retry a durable write that failed earlier, and persist the position
        # the 1s session debounce has not written yet (the timer would
        # otherwise fire after the switch and record the new folder instead).
        self._flush_directory_state_before_switch()

        # Stop the old watcher
        if self.watcher:
            self.watcher.stop()

        # Update the directory path
        self.image_dir = target_dir

        # Reinitialize directory-bound components
        self.watcher = target_watcher
        self._pending_directory_snapshot = (
            self._key(self.image_dir),
            target_snapshot,
        )
        self.sidecar = SidecarManager(self.image_dir, self.watcher, debug=_debug_mode)
        self._adopt_saved_sort_mode()
        self.ui_state.sortModeChanged.emit()
        # Clearing stacks/batches is undone against one folder's sidecar, so
        # entries made elsewhere stop being undoable once this folder is open.
        directory_key = self._key(self.image_dir)
        self.undo_history = [
            entry
            for entry in self.undo_history
            if entry[0] not in ("clear_stacks", "clear_batches")
            or entry[1].get("directory_key") == directory_key
        ]

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

        # Clear directory-specific state. Bumping the index-scan epoch makes any
        # walk still running against the old directory discard its result; the
        # in-flight flag stays owned by that scan so a second walk cannot queue
        # behind it and then have its flag cleared by the stale arrival.
        self._index_scan_epoch += 1
        self._index_rescan_needed = False
        self._photoshop_handoffs.clear()
        self.stacks = []
        self._stack_identity_projection_blocked = False
        self.batches = []
        self.batch_start_index = None
        self.stack_start_index = None
        self.stack_end_index = None

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
        self._clear_active_auto_adjust_state(
            "folder-level state reset",
            clear_editor=False,
        )
        self.image_editor.clear()

        # Clear thumbnail cache BEFORE refresh to avoid stale thumbs
        if self._thumbnail_cache:
            self._thumbnail_cache.clear()

        self._set_folder_loaded(False)

        # Update thumbnail view infrastructure
        if self._thumbnail_model:
            if update_base_directory:
                self._thumbnail_model.set_directories(self.image_dir, self.image_dir)
            else:
                self._thumbnail_model.set_directories(
                    self._thumbnail_model.base_directory,
                    self.image_dir,
                )
            self.ui_state.gridDirectoryChanged.emit(str(self.image_dir))

        # Notify that the current directory changed (for window title)
        self.ui_state.currentDirectoryChanged.emit()

        # Load the observed authoritative snapshot into both loupe and grid.
        self.load(skip_thumbnail_refresh=True)
        return True

    def _set_folder_loaded(self, loaded: bool, *, notify: bool = True) -> bool:
        """Update the current-folder load state and notify QML when it changes."""
        if self._folder_loaded == loaded:
            return False

        self._folder_loaded = loaded
        if not loaded:
            self._clear_published_loupe_source()
        # Defensive for initialization and tests that may not have bound UIState yet.
        if notify and hasattr(self, "ui_state") and self.ui_state:
            self.ui_state.isFolderLoadedChanged.emit()
        return True

    @Slot()
    def open_folder(self):
        """Opens a directory dialog and reloads the application with the selected folder."""
        path = self.open_directory_dialog()
        if path:
            self._switch_to_directory(Path(path), update_base_directory=True)

    def _preload_dataset_identity(
        self,
    ) -> tuple[Optional[str], tuple[Optional[str], ...]]:
        """Return the current folder and ordered visible-path identity."""
        return (
            self._key(self.image_dir),
            tuple(self._key(image_file.path) for image_file in self.image_files),
        )

    def preload_all_images(self):
        if self.ui_state.isPreloading:
            log.info("Preloading is already in progress.")
            return

        log.info("Starting to preload all images, skipping cached.")
        self.ui_state.isPreloading = True
        self.ui_state.preloadProgress = 0

        image_files = list(self.image_files)
        total_images = len(image_files)
        if total_images == 0:
            log.info("No images to preload.")
            self.ui_state.isPreloading = False
            self.ui_state.preloadProgress = 0
            return

        # Snapshot the requested dataset, but deliberately do not snapshot the
        # starting neighborhood. The final MRU pass recomputes that from the
        # then-current list and index after verifying this identity still matches.
        dataset_identity = self._preload_dataset_identity()
        display_width, display_height, display_gen = self.get_display_info()
        cache_quality = "fast" if display_width > 0 and display_height > 0 else "cover"

        images_to_preload: list[tuple[int, Path]] = []
        already_cached_count = 0

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

        # Nearby cached entries still receive a logical task so they are touched
        # after farther work. The deterministic final pass below establishes the
        # exact ordering without relying on worker completion order.
        nearby_radius = self.prefetcher.prefetch_radius * 2
        for i, dist in all_images_with_dist:
            image_path = image_files[i].path
            cache_key = build_cache_key(image_path, display_gen, cache_quality)
            is_cached = cache_key in self.image_cache
            is_nearby = dist <= nearby_radius

            if is_cached and not is_nearby:
                already_cached_count += 1
            else:
                images_to_preload.append((i, image_path))

        log.info(
            f"Found {already_cached_count} cached images (skipping). Preloading {len(images_to_preload)} images (including nearby refreshes)."
        )

        self._preload_operation_token += 1
        operation = {
            "token": self._preload_operation_token,
            "lock": threading.Lock(),
            "total": total_images,
            "terminal": already_cached_count,
            "succeeded": already_cached_count,
            "canceled": 0,
            "failed": 0,
            "skipped": 0,
            "applied_terminal": -1,
            "applied_progress": -1,
            "finalized": False,
            "dataset_identity": dataset_identity,
            "display_generation": display_gen,
            "cache_quality": cache_quality,
        }
        self._preload_operation = operation

        def _snapshot_locked() -> dict:
            return {
                "token": operation["token"],
                "total": operation["total"],
                "terminal": operation["terminal"],
                "succeeded": operation["succeeded"],
                "canceled": operation["canceled"],
                "failed": operation["failed"],
                "skipped": operation["skipped"],
            }

        def _on_done(future, *, expected_path: Path):
            result = None
            terminal_kind = "skipped"
            if future is not None:
                if future.cancelled():
                    terminal_kind = "canceled"
                else:
                    try:
                        result = future.result()
                    except concurrent.futures.CancelledError:
                        terminal_kind = "canceled"
                    except Exception:
                        terminal_kind = "failed"
                        log.warning(
                            "Preload decode failed for %s",
                            expected_path,
                            exc_info=True,
                        )

            result_matches = False
            if isinstance(result, tuple) and len(result) == 2:
                result_path, result_generation = result
                result_matches = (
                    self._key(Path(result_path)) == self._key(expected_path)
                    and result_generation == display_gen
                )

            # A cancellation or stale result may race another logical decode
            # that satisfies the same requested entry. Conversely, a matching
            # non-None result is proof that insertion was accepted (or that the
            # entry was already present) even if later cache pressure evicts it.
            cache_key = build_cache_key(expected_path, display_gen, cache_quality)
            cached = self.image_cache.get(cache_key)
            cache_satisfied = cached is not None and (
                cache_quality == "fast"
                or getattr(cached, "quality", "cover") == "cover"
            )
            succeeded = result_matches or cache_satisfied

            with operation["lock"]:
                operation["terminal"] += 1
                if succeeded:
                    operation["succeeded"] += 1
                else:
                    operation[terminal_kind] += 1
                payload = _snapshot_locked()
            try:
                self._preloadProgressReady.emit(payload)
            except RuntimeError:
                pass

        if not images_to_preload:
            log.info("All images are already cached.")
            self._preloadProgressReady.emit(_snapshot_locked())
            return

        # Publish the cached/skipped baseline through the same serialized path
        # used by worker completions. Cross-thread callback emission order is not
        # meaningful; the GUI-thread slot below applies only newer snapshots.
        self._preloadProgressReady.emit(_snapshot_locked())

        # --- Submit tasks ---
        # images_to_preload is already sorted furthest -> nearest
        for preload_order, (i, image_path) in enumerate(images_to_preload):
            future = self.prefetcher.submit_task(
                i,
                self.prefetcher.generation,
                # Pin the path captured in the snapshot. Without this the worker
                # decodes whatever occupies index i at execution time, which no
                # longer matches the expected_path _on_done accounts against if
                # the image list is rebuilt mid-preload — every remaining image
                # then reports as "skipped".
                override_path=image_path,
                quality="fast",
                preload_order=preload_order,
            )
            if future:
                future.add_done_callback(
                    lambda fut, path=image_path: _on_done(
                        fut,
                        expected_path=path,
                    )
                )
            else:
                _on_done(None, expected_path=image_path)

    @Slot(object)
    def _on_preload_progress_ready(self, payload) -> None:
        """Apply one preload counter snapshot on the controller's Qt thread."""
        if not isinstance(payload, dict):
            return
        operation = self._preload_operation
        if operation is None or payload.get("token") != operation.get("token"):
            return
        if operation["finalized"]:
            return

        terminal = int(payload.get("terminal", 0))
        total = max(1, int(payload.get("total", operation["total"])))
        if terminal < operation["applied_terminal"]:
            return

        operation["applied_terminal"] = terminal
        progress = min(100, int((terminal / total) * 100))
        if progress > operation["applied_progress"]:
            operation["applied_progress"] = progress
            log.debug("Updating preload progress in UI: %d%%", progress)
            self.ui_state.preloadProgress = progress

        if terminal < total:
            return
        operation["finalized"] = True
        self._finish_preloading(operation, payload)

    def _finish_preloading(self, operation: dict, counters: dict) -> None:
        """Finalize one current preload operation exactly once on the GUI thread."""

        # This slot runs on the controller's Qt thread. Recompute the protected
        # neighborhood only if the exact folder, ordered visible list, and
        # display generation still belong to this operation.
        _, _, current_display_gen = self.get_display_info()
        same_dataset = self._preload_dataset_identity() == operation["dataset_identity"]
        operation_is_current = (
            same_dataset and current_display_gen == operation["display_generation"]
        )
        if operation_is_current and self.image_files:
            current_index = max(0, min(self.current_index, len(self.image_files) - 1))
            nearby_radius = self.prefetcher.prefetch_radius * 2
            nearby = [
                (image_file.path, abs(index - current_index))
                for index, image_file in enumerate(self.image_files)
                if abs(index - current_index) <= nearby_radius
            ]
            nearby.sort(key=lambda item: item[1], reverse=True)
            for path, _distance in nearby:
                for quality in ("cover", "fast"):
                    self.image_cache.get(
                        build_cache_key(
                            path,
                            current_display_gen,
                            quality,
                        )
                    )

        succeeded = int(counters.get("succeeded", 0))
        total = int(counters.get("total", operation["total"]))
        canceled = int(counters.get("canceled", 0))
        failed = int(counters.get("failed", 0))
        skipped = int(counters.get("skipped", 0))

        if self._preload_operation is operation:
            self._preload_operation = None
        self.ui_state.isPreloading = False
        self.ui_state.preloadProgress = 0
        if not operation_is_current:
            message = (
                f"Preload finished for an earlier image view: "
                f"{succeeded}/{total} images loaded; current view changed."
            )
            log.warning(message)
        elif succeeded == total:
            message = f"Preload complete: {succeeded} images loaded."
            log.info(message)
        else:
            message = (
                f"Preload finished: {succeeded}/{total} images loaded; "
                f"{canceled} canceled, {failed} failed, {skipped} skipped."
            )
            log.warning(message)
        if not self._shutting_down:
            self.update_status_message(message, timeout=7000)

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

    @Slot(result=int)
    def get_defined_batch_count(self) -> int:
        """Return the total number of valid image indices in defined batches."""
        if not self.batches:
            return 0

        max_index = len(self.image_files) - 1
        total_count = 0
        for start, end in self.batches:
            clamped_start = max(start, 0)
            clamped_end = min(end, max_index)
            if clamped_start <= clamped_end:
                total_count += clamped_end - clamped_start + 1
        return total_count

    @staticmethod
    def _move_to_recycle(
        src: Path,
        _created_bins: set | None = None,
        unique_tag: str | None = None,
        expected_identity=None,
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
        if expected_identity is not None:
            validate_file_identity(src, expected_identity)
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

        # Fast path: rename within the same filesystem. On Windows, a decode
        # that won the cancellation race can retain a file mapping briefly.
        # This worker is already off the UI thread, so allow that handle time
        # to close before treating the recycle operation as failed.
        for attempt in range(len(_RECYCLE_SHARING_RETRY_DELAYS) + 1):
            try:
                if expected_identity is not None:
                    validate_file_identity(src, expected_identity)
                os.replace(str(src), str(dest))
                log.info("Moved %s to recycle bin: %s (rename)", src.name, dest.name)
                return dest
            except OSError as e:
                sharing_violation = (
                    os.name == "nt"
                    and getattr(e, "winerror", None)
                    in _WINDOWS_SHARING_VIOLATION_ERRORS
                )
                if not sharing_violation or attempt >= len(
                    _RECYCLE_SHARING_RETRY_DELAYS
                ):
                    break
                delay = _RECYCLE_SHARING_RETRY_DELAYS[attempt]
                log.debug(
                    "Recycle waiting %.2fs for an open handle on %s",
                    delay,
                    src.name,
                )
                time.sleep(delay)

        try:
            if expected_identity is not None:
                validate_file_identity(src, expected_identity)
            shutil.move(str(src), str(dest))
            log.info("Moved %s to recycle bin: %s (copy)", src.name, dest.name)
            return dest
        except SourceChangedError:
            raise
        except OSError as e:
            log.error("Failed to recycle %s: %s", src.name, e)
            return None

    @staticmethod
    def _perm_delete_worker(
        job_id: int,
        items: list,
    ) -> dict:
        """Background worker: performs permanent deletion. No Qt access."""
        perm_success = []
        perm_fail = []

        perm_changed = []

        for item in items:
            idx, img = item[:2]
            work_item = item[2] if len(item) > 2 else None
            try:
                identities = None
                if work_item is not None:
                    identities = (
                        work_item.jpg_identity,
                        work_item.raw_identity,
                    )
                if permanently_delete_image_files(img, identities):
                    perm_success.append((idx, img))
                else:
                    perm_fail.append((idx, img))
            except SourceChangedError as e:
                log.warning("Permanent deletion cancelled for %s: %s", img.path, e)
                perm_fail.append((idx, img))
                perm_changed.append((idx, img, str(e)))
            except Exception as e:
                log.error("Perm delete failed for %s: %s", img.path, e)
                perm_fail.append((idx, img))

        return {
            "job_id": job_id,
            "_perm_result": True,
            "perm_success": perm_success,
            "perm_fail": perm_fail,
            "perm_changed": perm_changed,
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
            images_to_delete: List of DeleteWorkItem objects. Legacy two-tuples
                are accepted for direct callers and snapshotted immediately.
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

            if isinstance(item, DeleteWorkItem):
                work_item = item
                jpg_path = item.jpg_path
                raw_path = item.raw_path
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                jpg_path, raw_path = item
                if isinstance(raw_path, (tuple, list)):
                    work_item = None
                else:
                    work_item = DeleteWorkItem(
                        jpg_path=jpg_path,
                        raw_path=raw_path,
                        jpg_identity=capture_file_identity(jpg_path),
                        raw_identity=(
                            capture_file_identity(raw_path) if raw_path else None
                        ),
                    )
            else:
                work_item = None

            if work_item is None:
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

            processed_count += 1

            # An orphan RAW is indexed with path == raw_pair (see
            # find_images()); both members name one physical file. Collapse the
            # pair so the file is validated and moved exactly once, instead of
            # the second move seeing it already gone and reporting
            # SOURCE_CHANGED.
            if raw_path is not None and AppController._key(
                raw_path
            ) == AppController._key(jpg_path):
                raw_path = None

            try:
                # Pair preflight: neither member may have changed before the
                # first destructive operation begins.
                validate_file_identity(jpg_path, work_item.jpg_identity)
                if raw_path and work_item.raw_identity is not None:
                    validate_file_identity(raw_path, work_item.raw_identity)

                # Share the same UUID tag for JPG + RAW so their stems
                # still match in the recycle bin (enables re-pairing).
                shared_tag = uuid.uuid4().hex[:8]
                recycled_jpg = AppController._move_to_recycle(
                    jpg_path,
                    created_bins,
                    unique_tag=shared_tag,
                    expected_identity=work_item.jpg_identity,
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
                if (
                    raw_path
                    and work_item.raw_identity
                    and work_item.raw_identity.exists
                ):
                    try:
                        recycled_raw = AppController._move_to_recycle(
                            raw_path,
                            created_bins,
                            unique_tag=shared_tag,
                            expected_identity=work_item.raw_identity,
                        )
                        if not recycled_raw:
                            raise OSError("RAW move failed")
                    except SourceChangedError:
                        # The JPG was already moved. Restore it only if its
                        # original pathname is still empty; never overwrite a
                        # replacement that appeared during the operation.
                        if recycled_jpg:
                            try:
                                # No-replace restore: exists()+os.replace could
                                # overwrite a file that appeared in between.
                                restore_without_replace(
                                    Path(recycled_jpg), Path(jpg_path)
                                )
                            except FileExistsError:
                                log.warning(
                                    "Not restoring %s: a replacement appeared; "
                                    "the original remains at %s",
                                    jpg_path,
                                    recycled_jpg,
                                )
                            except (OSError, NotImplementedError):
                                log.exception(
                                    "Could not restore JPG after RAW identity changed"
                                )
                        raise
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

            except SourceChangedError as e:
                log.warning("Deletion cancelled for %s: %s", jpg_path.name, e)
                failures.append(
                    {
                        "jpg": jpg_path,
                        "raw": raw_path,
                        "code": DeletionErrorCodes.SOURCE_CHANGED.value,
                        "message": str(e),
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
                if isinstance(item, DeleteWorkItem):
                    jpg_path, raw_path = item.jpg_path, item.raw_path
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    jpg_path, raw_path = item
                else:
                    continue
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

    @Slot(object)
    def _on_delete_finished(
        self, result_dict: dict, *, allow_during_shutdown: bool = False
    ) -> None:
        """Main-thread completion handler for async delete worker.

        Refactored to 3-phase flow with typed data structures.
        """
        if self._shutting_down and not allow_during_shutdown:
            return

        # --- Phase 1: Resolve Job & Result ---
        # Convert raw dict to typed result immediately
        result = DeleteResult.from_worker_dict(result_dict)

        # Retrieve job context
        job = self._pending_delete_jobs.pop(result.job_id, None)

        if job:
            self._release_pending_delete_paths(job.job_id, job.work_items)
            # A scan that ran during the worker's I/O may list files it has
            # since moved; once suppression is released nothing filters them.
            self._invalidate_inflight_index_scans()
            pending_history_index = next(
                (
                    index
                    for index, entry in enumerate(self.undo_history)
                    if entry[0] == "pending_delete" and entry[1] == job.job_id
                ),
                None,
            )
        else:
            # Job might have been popped by undo_delete logic already?
            # Or this is a stray signal.
            log.warning(
                "Delete job %d completed but not found in pending jobs", result.job_id
            )
            return

        # --- Phase 1.5: Handle Perm Delete Result ---
        if result.is_perm_result:
            if pending_history_index is not None:
                self.undo_history[pending_history_index : pending_history_index + 1] = (
                    job.completed_undo_actions
                )
            if result.perm_success:
                metadata_saved = self._remove_permanently_deleted_metadata(
                    [img for _index, img in result.perm_success]
                )
                self._record_permanently_deleted_identities(
                    [img.path for _index, img in result.perm_success]
                )
                if metadata_saved:
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
                if result.perm_changed:
                    self.update_status_message(
                        "Deletion cancelled because the file changed."
                    )

            self._rebuild_path_to_index()
            self.sync_ui_state()
            self._schedule_delete_refresh()
            self._clear_delete_identity_state_if_settled()
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

            # The optimistic undo may have happened after the worker moved the
            # selected file. Now that all worker I/O and auto-restores are
            # complete, remove the deletion cache tombstone, jump to the
            # restored image, and issue a fresh provider request.
            if job.removed_items:
                restored_paths = [
                    path
                    for _, image in job.removed_items
                    for path in (image.path, image.raw_pair)
                    if path is not None and path.exists()
                ]
                self.image_cache.release_tombstones(restored_paths)
                _, target_image = min(
                    job.removed_items,
                    key=lambda item: abs(item[0] - job.previous_index),
                )
                if target_image.path.exists():
                    self._post_undo_refresh_and_select(target_image.path)

            # Update status
            self.update_status_message("Deletion cancelled (files restored)")
            self._schedule_delete_refresh()
            self._clear_delete_identity_state_if_settled()
            return

        # 2c. Normal Completion: Record History & Handle Failures

        # Track recycle bins
        for s in result.successes:
            if s.recycled_jpg:
                self.active_recycle_bins.add(s.recycled_jpg.parent)

        # Log warnings
        for w in result.warnings:
            log.warning("Partial delete warning for %s: %s", w.jpg, w.message)

        # Replace this job's placeholder in place so history remains ordered by
        # user action even when jobs complete out of order.
        completed_actions = []
        for s in result.successes:
            # Store tuple of tuples: ((jpg, recycled_jpg), (raw, recycled_raw))
            record = ((s.jpg, s.recycled_jpg), (s.raw, s.recycled_raw))
            self.delete_history.append(record)
            completed_actions.append(("delete", record, job.timestamp))
        self._record_permanently_deleted_identities(
            [record.jpg for record in result.successes if record.jpg is not None]
        )

        # Handle Failures / Rollback UI
        # Only failed items need to be restored to UI.
        # Check for permanent delete candidates (recycle bin failures).
        self._handle_delete_failures(result, job)
        if job.job_id in self._pending_delete_jobs:
            job.completed_undo_actions = completed_actions
        elif pending_history_index is not None:
            self.undo_history[pending_history_index : pending_history_index + 1] = (
                completed_actions
            )

        # --- Phase 3: Post Actions ---

        # Status Message
        count = len(result.successes)
        if count > 0:
            msg = f"Deleted {count} images"
            if result.warnings:
                msg += " (some RAW moves failed)"
            elif count == 1:
                msg = "Image moved to recycle bin"
            if getattr(self.sidecar, "dirty", False) is not True:
                self.update_status_message(msg)
        elif result.failures:
            source_changed = any(
                failure.code == DeletionErrorCodes.SOURCE_CHANGED.value
                for failure in result.failures
            )
            if source_changed:
                self.update_status_message(
                    "Deletion cancelled because the file changed."
                )
            else:
                self.update_status_message(
                    "Deletion cancelled" if result.cancelled else "Delete failed"
                )

        self._schedule_delete_refresh()
        self._clear_delete_identity_state_if_settled()

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

        if self._shutting_down:
            self._rollback_ui_items(failed_indices_and_imgs, job)
            self._rebuild_path_to_index()
            self.sync_ui_state(image_count_changed=True)
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
                confirmed = confirm_permanent_delete(
                    candidate_imgs[0],
                    reason=reason,
                    dialog_exec=self._exec_modal_dialog,
                )
            else:
                confirmed = confirm_batch_permanent_delete(
                    candidate_imgs,
                    reason=reason,
                    dialog_exec=self._exec_modal_dialog,
                )

            if confirmed:
                # ASYNC permanent delete
                # Put job back in pending map so _on_delete_finished can find it again
                self._pending_delete_jobs[job.job_id] = job
                self._register_pending_delete_paths(job.job_id, job.work_items)

                work_by_key = {
                    self._key(item.jpg_path): item for item in job.work_items
                }
                permanent_items = [
                    (idx, img, work_by_key.get(self._key(img.path)))
                    for idx, img in perm_candidates
                ]
                perm_job_id = job.job_id

                # Define callback to bridge back to main thread
                def _on_perm_done(future):
                    try:
                        res = future.result()
                    except BaseException as e:
                        log.error("Perm delete worker exception: %s", e)
                        # Report every candidate as failed so the completion
                        # handler rolls back and releases the pending job and
                        # its watcher suppression instead of leaving it stuck.
                        res = {
                            "job_id": perm_job_id,
                            "_perm_result": True,
                            "perm_success": [],
                            "perm_fail": [
                                (idx, img) for idx, img, _work in permanent_items
                            ],
                            "perm_changed": [],
                        }
                    # Emit on main thread via signal
                    self._deleteFinished.emit(res)

                fut = self._delete_executor.submit(
                    self._run_critical_user_data_operation,
                    "permanent delete",
                    self._perm_delete_worker,
                    job.job_id,
                    permanent_items,
                )
                job.future = fut
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
        self.sync_ui_state(image_count_changed=True)

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

    def _record_permanently_deleted_identities(self, paths: List[Path]) -> None:
        """Remove completed deletions from path-based rollback group state."""
        deleted_keys = {self._key(path) for path in paths if path is not None}
        if not deleted_keys:
            return
        self._delete_batch_path_groups = [
            [path for path in group if self._key(path) not in deleted_keys]
            for group in self._delete_batch_path_groups
        ]
        self._delete_stack_path_groups = [
            [path for path in group if self._key(path) not in deleted_keys]
            for group in self._delete_stack_path_groups
        ]
        self._delete_batch_path_groups = [
            group for group in self._delete_batch_path_groups if group
        ]
        self._delete_stack_path_groups = [
            group for group in self._delete_stack_path_groups if group
        ]
        for attr in (
            "_delete_batch_start_path",
            "_delete_stack_start_path",
            "_delete_stack_end_path",
        ):
            path = getattr(self, attr)
            if path is not None and self._key(path) in deleted_keys:
                setattr(self, attr, None)

    def _remove_permanently_deleted_metadata(self, images: List[ImageFile]) -> bool:
        """Forget metadata only after permanent deletion has actually succeeded."""
        keys = {
            self.sidecar.metadata_key_for_path(path)
            for image in images
            for path in (image.path, image.raw_pair)
            if path is not None
        }
        removed = False
        with self.sidecar._state_lock:
            for key in keys:
                if self.sidecar.data.entries.pop(key, None) is not None:
                    removed = True
        return not removed or self._persist_sidecar()

    def _clear_delete_identity_state_if_settled(self) -> None:
        if self._pending_delete_jobs:
            return
        self._delete_identity_order = []
        self._delete_batch_path_groups = []
        self._delete_stack_path_groups = []
        self._delete_batch_start_path = None
        self._delete_stack_start_path = None
        self._delete_stack_end_path = None
        if self._index_rescan_needed and not self._index_scan_inflight:
            self._maybe_start_index_scan()

    def _rollback_ui_items(self, items: List[Tuple[int, Any]], job: DeleteJob) -> bool:
        """Restore items by stable path identity in the delete transaction order."""
        if not items:
            return True
        metadata_saved = True
        self._begin_direct_image_transition("delete rollback restored the image list")

        current_path = self._current_image_path()
        images_by_key = {self._key(img.path): img for img in self.image_files}
        filtered_order = [
            self._key(image.path)
            for image in self._filtered_sorted_copy(self.sort_mode)
        ]
        allowed_keys = set(filtered_order)
        for _old_index, image in items:
            image_key = self._key(image.path)
            if not filtered_order or image_key in allowed_keys:
                images_by_key.setdefault(image_key, image)

        order = (
            filtered_order
            or self._delete_identity_order
            or [self._key(path) for path in job.visible_order_paths]
        )
        rank = {key: index for index, key in enumerate(order)}
        current_rank = {
            self._key(image.path): index for index, image in enumerate(self.image_files)
        }
        self.image_files = sorted(
            images_by_key.values(),
            key=lambda image: (
                rank.get(self._key(image.path), len(rank)),
                current_rank.get(self._key(image.path), len(current_rank)),
            ),
        )
        self._rebuild_path_to_index()

        visible_paths = {image.path for image in self.image_files}
        target_path = (
            job.previous_path if job.previous_path in visible_paths else current_path
        )
        target_key = self._key(target_path) if target_path is not None else None
        self.current_index = self._path_to_index.get(
            target_key, min(job.previous_index, len(self.image_files) - 1)
        )
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

        self.prefetcher.set_image_files(self.image_files)
        if self.image_files:
            self.prefetcher.update_prefetch(self.current_index)
        self._restart_quality_decode_timer()

        # Group membership and active markers are reconstructed from path
        # identity. Numeric snapshots are retained only for compatibility.
        ui = job.ui_state
        if ui is not None:
            batch_groups = self._delete_batch_path_groups or ui.saved_batch_paths
            self.batches = self._rebuild_ranges_from_paths(batch_groups)
            batch_start_path = (
                self._delete_batch_start_path or ui.saved_batch_start_path
            )
            self.batch_start_index = (
                self._path_to_index.get(self._key(batch_start_path))
                if batch_start_path is not None
                else None
            )
            metadata_saved = self._finalize_batch_state(emit=False, sync=False)

            stack_groups = self._delete_stack_path_groups or ui.saved_stack_paths
            self.stacks = self._rebuild_ranges_from_paths(stack_groups)
            stack_start_path = (
                self._delete_stack_start_path or ui.saved_stack_start_path
            )
            stack_end_path = self._delete_stack_end_path or ui.saved_stack_end_path
            self.stack_start_index = (
                self._path_to_index.get(self._key(stack_start_path))
                if stack_start_path is not None
                else None
            )
            self.stack_end_index = (
                self._path_to_index.get(self._key(stack_end_path))
                if stack_end_path is not None
                else None
            )
            metadata_saved = self._persist_stack_state()
            self._metadata_cache_index = (-1, -1)
        return metadata_saved

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
            "metadata_persisted": True,
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

        # Capture authorization before any optimistic UI mutation. Absence is
        # also part of the snapshot, so a paired path that appears later is
        # never treated as part of this request.
        worker_items = []
        try:
            for img in images_to_delete:
                jpg_identity = capture_file_identity(img.path)
                raw_identity = (
                    capture_file_identity(img.raw_pair) if img.raw_pair else None
                )
                worker_items.append(
                    DeleteWorkItem(
                        jpg_path=img.path,
                        raw_path=img.raw_pair,
                        jpg_identity=jpg_identity,
                        raw_identity=raw_identity,
                    )
                )
        except OSError as e:
            log.warning("Could not capture deletion identity: %s", e)
            self.update_status_message(
                "Deletion cancelled because a file could not be verified."
            )
            return summary

        self._begin_direct_image_transition("image deletion changed the list")
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
        previous_path = (
            self.image_files[previous_index].path
            if 0 <= previous_index < original_count
            else None
        )
        visible_order_paths = [img.path for img in self.image_files]
        pre_batch_path_groups = self._resolve_ranges_to_paths(self.batches)
        pre_stack_path_groups = self._resolve_ranges_to_paths(self.stacks)
        pre_batch_start_path = (
            self.image_files[self.batch_start_index].path
            if self.batch_start_index is not None
            and 0 <= self.batch_start_index < original_count
            else None
        )
        pre_stack_start_path = (
            self.image_files[self.stack_start_index].path
            if self.stack_start_index is not None
            and 0 <= self.stack_start_index < original_count
            else None
        )
        pre_stack_end_path = (
            self.image_files[self.stack_end_index].path
            if self.stack_end_index is not None
            and 0 <= self.stack_end_index < original_count
            else None
        )
        if not self._pending_delete_jobs:
            self._delete_identity_order = [
                self._key(path) for path in visible_order_paths
            ]
            self._delete_batch_path_groups = pre_batch_path_groups
            self._delete_stack_path_groups = pre_stack_path_groups
            self._delete_batch_start_path = pre_batch_start_path
            self._delete_stack_start_path = pre_stack_start_path
            self._delete_stack_end_path = pre_stack_end_path

        # Remove from in-memory list immediately for instant visual feedback
        for idx in sorted_indices:
            if 0 <= idx < len(self.image_files):
                del self.image_files[idx]
        self._invalidate_inflight_index_scans()

        # Reposition current_index immediately (fast, in-memory only)
        validated_sorted = sorted(i for i in sorted_indices if 0 <= i < original_count)
        deleted_set = set(validated_sorted)
        if not self.image_files:
            self.current_index = 0
        else:
            # Preserve the current image's position in the compressed list. If
            # the current image was deleted, this selects the first survivor
            # after it. This must include other deleted images before the
            # cursor; otherwise deleting a batch ending at the cursor jumps
            # forward by the size of the batch.
            shift = sum(1 for d in validated_sorted if d < previous_index)
            self.current_index = max(
                0, min(previous_index - shift, len(self.image_files) - 1)
            )

        # Save batch/stack state before mutation so rollback can restore it.
        pre_batch_snapshot = [b[:] for b in self.batches]
        pre_batch_start_snapshot = self.batch_start_index
        pre_stack_snapshot = [s[:] for s in self.stacks]
        pre_stack_start_snapshot = self.stack_start_index
        pre_stack_end_snapshot = self.stack_end_index

        # Shared helper: compute post-deletion index for a surviving index.
        deleted_ascending = sorted(validated_sorted)

        def _shift(orig_idx: int) -> int:
            return orig_idx - sum(1 for d in deleted_ascending if d < orig_idx)

        # Adjust batch index ranges to account for removed entries.
        # Deleting index d shifts every index > d down by one.  Without this,
        # batches that sit above any deleted image reference the wrong files.
        batch_changed = False
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
                batch_changed = True

        # Adjust batch_start_index for removed entries.
        if pre_batch_start_snapshot is not None:
            if pre_batch_start_snapshot in deleted_set:
                self.batch_start_index = None
                batch_changed = True
            else:
                shifted = _shift(pre_batch_start_snapshot)
                if shifted != self.batch_start_index:
                    self.batch_start_index = shifted
                    batch_changed = True

        if batch_changed:
            summary["metadata_persisted"] = self._finalize_batch_state(
                emit=False, sync=False, notify_model=False
            )

        # Adjust stack index ranges (same algorithm as batches).
        stacks_changed = False
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
                stacks_changed = True
                self._metadata_cache_index = (-1, -1)

        if stacks_changed:
            # A later successful sidecar save also durably includes any batch
            # mutation whose earlier save attempt failed.
            summary["metadata_persisted"] = self._persist_stack_state()

        # Adjust stack_start_index for removed entries
        if pre_stack_start_snapshot is not None:
            if pre_stack_start_snapshot in deleted_set:
                self.stack_start_index = None
            else:
                self.stack_start_index = _shift(pre_stack_start_snapshot)
        if pre_stack_end_snapshot is not None:
            if pre_stack_end_snapshot in deleted_set:
                self.stack_end_index = None
            else:
                self.stack_end_index = _shift(pre_stack_end_snapshot)

        # Update UI immediately - this is fast since it just reads from memory
        # Check for existence, not truthiness (empty cache is falsy)
        paths_to_evict = [
            path
            for img in images_to_delete
            for path in (img.path, img.raw_pair)
            if path is not None
        ]
        if self.image_cache is not None:
            # Targeted eviction: remove only deleted images and their raw pairs
            # This preserves the cache for remaining images (huge perf win)
            # Use new targeted eviction with tombstones
            self.image_cache.evict_paths(paths_to_evict)

        # Prevent queued grid work from opening a file after the recycle worker
        # starts. Running jobs receive a cancellation marker; the sharing-
        # violation retry in _move_to_recycle covers the brief unwind window.
        if self._thumbnail_prefetcher:
            self._thumbnail_prefetcher.cancel_paths(paths_to_evict)

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
            self._notify_thumbnail_current_path_changed(previous_path)

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

        self.sync_ui_state(image_count_changed=True)
        self._restart_quality_decode_timer()

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
            work_items=worker_items,
            ui_state=UIStateRestoration(
                saved_batches=pre_batch_snapshot,
                saved_batch_start_index=pre_batch_start_snapshot,
                saved_stacks=pre_stack_snapshot,
                saved_stack_start_index=pre_stack_start_snapshot,
                saved_stack_end_index=pre_stack_end_snapshot,
                saved_batch_paths=pre_batch_path_groups,
                saved_stack_paths=pre_stack_path_groups,
                saved_batch_start_path=pre_batch_start_path,
                saved_stack_start_path=pre_stack_start_path,
                saved_stack_end_path=pre_stack_end_path,
            ),
            previous_path=previous_path,
            visible_order_paths=visible_order_paths,
        )

        # Add single placeholder undo entry per job
        self.undo_history.append(("pending_delete", job_id, timestamp))

        # Suppress watcher refreshes for these paths for as long as the job
        # runs, so a scan triggered by the first move cannot reinsert files
        # later in the same batch that are still on disk.
        self._register_pending_delete_paths(job_id, worker_items)

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
                            for p, r in (
                                (item.jpg_path, item.raw_path) for item in worker_items
                            )
                        ],
                        "cancelled": False,
                    }
                )

        fut = self._delete_executor.submit(
            self._run_critical_user_data_operation,
            "recycle delete",
            self._delete_worker,
            job_id,
            worker_items,
            cancel_event,
        )
        self._pending_delete_jobs[job_id].future = fut
        fut.add_done_callback(_on_worker_done)

        summary["queued"] = True
        summary["job_id"] = job_id
        summary["requested_count"] = len(images_to_delete)
        return summary

    def _reposition_after_delete(
        self, preserved_path: Optional[Path], previous_index: int
    ):
        """Reposition current_index after the image list refreshed post-deletion."""
        old_current_path = self._current_image_path()
        if not self.image_files:
            self.current_index = 0
            self._notify_thumbnail_current_path_changed(old_current_path)
            return

        if preserved_path:
            for i, img_file in enumerate(self.image_files):
                if img_file.path == preserved_path:
                    self.current_index = i
                    self._notify_thumbnail_current_path_changed(old_current_path)
                    return

        self.current_index = min(previous_index, len(self.image_files) - 1)
        self._notify_thumbnail_current_path_changed(old_current_path)

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
        metadata_persisted = self._finalize_batch_state(
            emit=False, sync=False, notify_model=False
        )
        log.info("Batch state cleared optimistically for delete job %d.", job_id)
        return metadata_persisted and summary.get("metadata_persisted", True)

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
        self._begin_direct_image_transition("undo rebuilt the image list")

        # Clear stale editor/preview state so the provider doesn't serve a
        # pre-undo preview frame.  image_editor.clear() leaves current_edits
        # populated; reset_edits() zeroes them out.
        if self.image_editor:
            self.image_editor.reset_edits()
        self._clear_live_edit_session_state()
        with self._preview_lock:
            self._clear_last_rendered_preview_locked()

        # Publish only after the final selection and decode state are ready.
        # The list rebuild preserves/clamps the old selection, providing a
        # valid fallback if the restored target is hidden by a filter/variant.
        self.refresh_image_list(notify=False)
        provisional_current_path = self._current_image_path()

        # Use _key-based lookup (consistent with _reindex_after_save) for
        # robust cross-platform path matching on WSL/Windows.
        target_key = self._key(target)
        new_idx = self._path_to_index.get(target_key)
        if new_idx is not None:
            self.current_index = new_idx
        else:
            # Fallback: name-based match
            target_name = target.name
            for i, img_file in enumerate(self.image_files):
                if img_file.path.name == target_name:
                    self.current_index = i
                    break
        self._notify_thumbnail_current_path_changed(
            provisional_current_path, force=True
        )

        self._bump_display_generation()
        self.image_cache.clear()
        self.image_cache.release_tombstones([target])
        self.prefetcher.cancel_all()
        self.prefetcher.update_prefetch(self.current_index)
        self.sync_ui_state(image_count_changed=True)
        self._restart_quality_decode_timer()

        if update_hist and self.ui_state.isHistogramVisible:
            self.update_histogram()

    def _newest_undo_is_clear_after_live_edits(self) -> bool:
        """True when Ctrl+Z should undo a clear before reverting unsaved edits.

        Clear Stacks and Clear Batches record the editor's path, session and
        edit revision. If those still match, nothing has been edited since the
        clear, so the clear is the most recent action and is what the user
        means to take back. Any edit made afterwards changes the revision and
        the unsaved edits are reverted first, as before.
        """
        if not self.undo_history:
            return False
        action_type, action_data, _timestamp = self.undo_history[-1]
        if action_type not in ("clear_stacks", "clear_batches"):
            return False
        if not isinstance(action_data, dict):
            return False
        mark = action_data.get("live_edit_mark")
        return (
            mark is not None and mark == self._get_current_preview_session_provenance()
        )

    @Slot()
    def undo_delete(self):
        """Unified undo that handles delete, pending_delete, and edit operations."""
        if self._auto_adjust_save_pending_action is not None:
            try:
                self._flush_pending_auto_adjust_save()
            except Exception:
                log.exception("Failed to flush pending auto-adjust save before undo")

        # If the live session has unsaved edits (e.g. crop or auto-levels
        # applied as preview-only), revert them before touching undo_history,
        # unless the newest history entry is a clear made after those edits.
        if (
            self._is_current_live_edit_session_dirty()
            and not self._newest_undo_is_clear_after_live_edits()
        ):
            self._clear_active_auto_adjust_state(
                "undo reverted unsaved live edits",
                clear_editor=False,
            )
            if self.image_editor:
                self.image_editor.reset_edits()
            if hasattr(self.ui_state, "reset_editor_state"):
                self.ui_state.reset_editor_state()
            # The editor session stays loaded, so re-baseline instead of
            # dropping tracking: with no state, later slider edits would never
            # read as dirty and navigation would silently discard them.
            self._mark_current_live_edit_session_clean()
            with self._preview_lock:
                self._clear_last_rendered_preview_locked()
            # Nothing changed on disk; just force a fresh decode of the
            # current image so the display drops the reverted edits.
            if self.image_cache and 0 <= self.current_index < len(self.image_files):
                self._invalidate_decoded_path(
                    self.image_files[self.current_index].path,
                    force=True,
                )
            self.prefetcher.update_prefetch(self.current_index)
            self.sync_ui_state()
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
            self.update_status_message("Undid unsaved edits")
            return

        if not self.undo_history:
            self.update_status_message("Nothing to undo.")
            return

        # Get the most recent action
        action_type, action_data, timestamp = self.undo_history.pop()

        if action_type == "clear_stacks":
            self._undo_clear_stacks(action_data)
            return
        if action_type == "clear_batches":
            self._undo_clear_batches(action_data)
            return

        # --- PENDING DELETE: cancel in-flight and restore UI immediately ---
        if action_type == "pending_delete":
            job_id = action_data
            job = self._pending_delete_jobs.get(job_id)

            if job is not None:
                metadata_saved = True
                # Cancel the background worker (best-effort)
                job.cancel_event.set()
                # Mark as undo_requested so completion handler automatically restores files (Policy 1)
                job.undo_requested = True
                job.user_undone = True  # Keep for logic that checks if user intervened

                # Restore removed items to in-memory list immediately
                removed_items = job.removed_items
                previous_index = job.previous_index
                preserved_path = self._current_image_path()
                self._begin_direct_image_transition("undo restored a pending deletion")

                # Re-insert in ascending index order so each insertion shifts
                # subsequent indices correctly, restoring original positions.
                for idx, img in sorted(removed_items, key=lambda x: x[0]):
                    insert_idx = min(idx, len(self.image_files))
                    self.image_files.insert(insert_idx, img)
                self._invalidate_inflight_index_scans()

                self._rebuild_path_to_index()
                # Keep showing the user's current image until the worker has
                # definitely stopped moving files. Its completion path jumps
                # to the restored image and starts the fresh decode.
                selected_index = (
                    self._path_to_index.get(self._key(preserved_path))
                    if preserved_path is not None
                    else None
                )
                if selected_index is not None:
                    self.current_index = selected_index
                else:
                    self.current_index = min(previous_index, len(self.image_files) - 1)
                self._notify_thumbnail_current_path_changed(preserved_path)
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
                # Restore batch state that was shifted during _delete_indices
                ui = job.ui_state
                if ui is not None and ui.saved_batches is not None and removed_items:
                    self.batches = ui.saved_batches
                    self.batch_start_index = ui.saved_batch_start_index
                    metadata_saved = self._finalize_batch_state(emit=False, sync=False)
                # Restore stack state that was shifted during _delete_indices
                if ui is not None and ui.saved_stacks is not None and removed_items:
                    self.stacks = ui.saved_stacks
                    self.stack_start_index = ui.saved_stack_start_index
                    self.stack_end_index = ui.saved_stack_end_index
                    stack_saved = self._persist_stack_state()
                    metadata_saved = stack_saved
                    self._metadata_cache_index = (-1, -1)
                self.sync_ui_state(image_count_changed=True)
                self._restart_quality_decode_timer()

                count = len(removed_items)
                if metadata_saved:
                    self.update_status_message(
                        f"Cancel requested... restoring view ({count} item{'s' if count > 1 else ''})"
                    )
                log.info(
                    "Undo cancelled pending delete job %d (%d items)", job_id, count
                )
            else:
                # Job already completed: its files are in the recycle bin and
                # the real most-recent action is the next history entry. Run
                # the full dispatch on it rather than falling through, which
                # only handled "delete" and edit entries and silently dropped
                # a clear_stacks/clear_batches entry popped this way.
                self.update_status_message("Delete already completed, undoing...")
                if not self.undo_history:
                    self.update_status_message("Nothing to undo.")
                    return
                return self.undo_delete()

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
            elif reason == "missing_in_bin":
                # The recycled file is gone (e.g. recycle bin was emptied) —
                # retrying this undo can never succeed, so don't re-append it.
                self.update_status_message(
                    f"Cannot undo: {jpg_src.name} is no longer in the recycle bin"
                )
                return
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
                elif reason == "missing_in_bin":
                    # The RAW is gone from the bin and can never be restored.
                    # Rolling the JPG back into the (possibly emptied) bin
                    # would strand it, so leave the JPG restored in place
                    # and don't re-append this dead entry.
                    self.update_status_message(
                        f"Restored {jpg_src.name}; RAW was no longer in the "
                        "recycle bin"
                    )
                    self._post_undo_refresh_and_select(jpg_src, update_hist=False)
                    if self._thumbnail_model and self._is_grid_view_active:
                        self._thumbnail_model.refresh()
                    return
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
            "auto_adjust",
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

            if self._block_if_saving(saved_path):
                # A save for this file is still in flight; swapping the backup
                # in now would race the writer. Keep the entry for a retry.
                self.undo_history.append((action_type, action_data, timestamp))
                return

            try:
                if self._restore_backup_safe(saved_path, backup_path):
                    restore_sidecar = metadata_sidecar or self.sidecar
                    restore_metadata_path = (
                        Path(metadata_path) if metadata_path else Path(saved_path)
                    )
                    self._clear_active_auto_adjust_state(
                        "undo restored a prior image state",
                        clear_editor=True,
                    )
                    metadata_restored = self._restore_metadata_snapshot(
                        restore_sidecar, restore_metadata_path, metadata_before
                    )
                    self._post_undo_refresh_and_select(
                        Path(saved_path),
                        update_hist=action_type != "crop",
                    )
                    if not metadata_restored:
                        return False
                    if action_type == "save_edit":
                        self.update_status_message("Undid saved edit")
                    elif action_type == "auto_white_balance":
                        self.update_status_message("Undid auto white balance")
                    elif action_type == "auto_levels":
                        self.update_status_message("Undid auto levels")
                    elif action_type == "auto_adjust":
                        self.update_status_message("Undid auto adjust")
                    else:
                        self.update_status_message("Undid crop")
            except (FileNotFoundError, OSError, shutil.Error) as e:
                self.update_status_message(f"Undo failed: {e}")
                if Path(backup_path).exists():
                    self.undo_history.append((action_type, action_data, timestamp))

    def shutdown_qt(self):
        """Shutdown Qt objects only - MUST run on main/Qt thread."""
        # Persist the current live edit session before tearing down Qt so
        # preview-only quick actions and editor changes are not lost on exit.
        if not self._shutdown_flush_prepared:
            try:
                flushed = self._flush_current_live_edit_session_for_drag(
                    saving_status=None
                )
                if not flushed and self._last_save_prepare_error is not None:
                    log.error(
                        "Shutdown proceeding without a live-session flush: %s",
                        self._last_save_prepare_error,
                    )
            except Exception:
                log.exception("Failed to flush live edit session on shutdown")
        self._shutdown_flush_prepared = False

        self._shutting_down = True  # set EARLY to make all slots no-op
        self._exif_pending_path = None
        log.info("Application shutting down (Qt cleanup).")

        # Drop the startup constraint: its release callbacks are now no-ops.
        # Save/refinement reasons stay until their own completion paths run or
        # Prefetcher.shutdown() clears them (executor shutdown ignores the
        # governor either way, so no worker can be parked on it).
        self._release_startup_prefetch_constraint("shutdown")

        # Stop Qt timers
        try:
            self._metadata_debounce_timer.stop()
            self._exif_debounce_timer.stop()
            self._watcher_debounce_timer.stop()
            self._index_throttle_timer.stop()
            self._auto_adjust_save_timer.stop()
            self._quality_decode_timer.stop()
            self._session_save_timer.stop()
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

    def _reconcile_delete_jobs_for_shutdown(self) -> None:
        """Synchronously apply settled delete results when Qt delivery is stopped."""
        for job_id, job in list(self._pending_delete_jobs.items()):
            future = job.future
            if future is None or not future.done():
                log.error(
                    "Delete job %d has no settled future after executor shutdown; "
                    "retaining rollback context",
                    job_id,
                )
                continue
            try:
                result = future.result()
            except Exception as exc:
                log.exception("Delete job %d failed during shutdown", job_id)
                result = {
                    "job_id": job_id,
                    "successes": [],
                    "failures": [
                        {
                            "jpg": str(item.jpg_path),
                            "raw": str(item.raw_path) if item.raw_path else None,
                            "code": DeletionErrorCodes.UNKNOWN.value,
                            "message": str(exc),
                        }
                        for item in job.work_items
                    ],
                    "cancelled": False,
                }
            with self._critical_user_data_operation("delete reconciliation"):
                self._on_delete_finished(result, allow_during_shutdown=True)

    def shutdown_nonqt(self):
        """Shutdown non-Qt resources - safe to run in background thread."""
        log.info("Shutting down background resources.")

        self._shutting_down = True  # gate async callbacks during shutdown_nonqt too
        inat_upload = getattr(self, "_inat_upload", None)
        if inat_upload is not None:
            try:
                inat_upload.shutdown()
            except Exception:
                log.debug("iNat upload shutdown failed", exc_info=True)
        self._exif_pending_path = None  # optional but consistent with shutdown_qt
        self._cancel_raw_development_operations()

        if self._pending_delete_jobs:
            log.info(
                "Retaining %d pending delete jobs until shutdown reconciliation",
                len(self._pending_delete_jobs),
            )

        # Shutdown thread pool executors — each isolated so one failure can't
        # prevent the others (especially save/delete) from shutting down.
        log.info("Shutting down background executors...")
        self._safe_shutdown_executor(self._hist_executor, "histogram", wait=False)
        self._safe_shutdown_executor(self._preview_executor, "preview", wait=False)
        self._safe_shutdown_executor(
            self._editor_load_executor, "editor load", wait=False
        )
        self._safe_shutdown_executor(
            getattr(self, "_preview_refine_executor", None),
            "preview refinement",
            wait=False,
        )
        self._safe_shutdown_executor(
            getattr(self, "_exif_executor", None),
            "exif",
            wait=False,
        )
        self._safe_shutdown_executor(
            getattr(self, "_editor_prewarm_executor", None),
            "editor prewarm",
            wait=False,
        )
        self._safe_shutdown_executor(
            getattr(self, "_update_executor", None),
            "update",
            wait=False,
        )
        self._safe_shutdown_executor(
            getattr(self, "_index_executor", None),
            "index scan",
            wait=False,
        )
        self._safe_shutdown_executor(
            getattr(self, "_stack_paint_executor", None),
            "stack paint",
            wait=False,
        )
        self._shutdown_halo_removal()
        # wait=True ensures pending saves/deletes complete to avoid data loss/corruption
        self._safe_shutdown_executor(
            self._save_executor, "save", wait=True, cancel_futures=False
        )
        self._reconcile_save_tracking_for_shutdown()
        self._safe_shutdown_executor(
            self._delete_executor, "delete", wait=True, cancel_futures=False
        )
        self._reconcile_delete_jobs_for_shutdown()

        # Final attempt to persist snapshots whose background save previously
        # failed, or whose latest revision was deferred at shutdown because an
        # older save for the same target was still in flight. Run this only
        # after the save executor drains so we do not race the same file twice.
        for tgt, req in list(self._pending_save_recovery.items()):
            try:
                with self._critical_user_data_operation("shutdown save recovery"):
                    recovery_result = self.image_editor.save_from_snapshot(
                        req["snapshot"]
                    )
            except Exception:
                log.exception("Final shutdown retry failed for %s", tgt)
                continue
            try:
                ctx = req.get("context", {})
                meta_path_str = ctx.get("save_metadata_path") or tgt
                meta_sidecar = ctx.get("save_sidecar") or self.sidecar
                if meta_path_str and meta_sidecar is not None:
                    saved_edit_state = None
                    if isinstance(recovery_result, tuple) and len(recovery_result) == 2:
                        saved_edit_state = self._build_saved_edit_state_for_result(
                            {"request": req},
                            saved_path=recovery_result[0],
                            backup_path=recovery_result[1],
                        )
                    self._mark_image_edited_in_sidecar(
                        meta_sidecar,
                        Path(meta_path_str),
                        completed_edit_state_request_id=ctx.get("save_request_id"),
                        saved_edit_state=saved_edit_state,
                        clear_saved_edit_state=not bool(
                            ctx.get("save_has_meaningful_edits", True)
                        ),
                    )
                    self._forget_pending_edit_save_request(
                        ctx.get("save_image_key"), ctx.get("save_request_id")
                    )
                log.info("Recovered pending save for %s on shutdown", tgt)
            except Exception:
                log.exception(
                    "Recovered save for %s but sidecar bookkeeping failed", tgt
                )
        self._pending_save_recovery.clear()
        # Any remaining request has now received its one final shutdown path;
        # retaining the bookkeeping must not make the watchdog wait forever.
        self._pending_edit_save_requests.clear()

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

        # Drain the session-write worker first: a queued position write is
        # superseded by the save below, and a late session-record update must
        # not recreate the record after close() removes it.
        self._safe_shutdown_executor(
            getattr(self, "_session_write_executor", None),
            "session write",
            wait=True,
            cancel_futures=True,
        )

        # Save sidecar state
        # NOTE: This runs on the main thread during shutdown (via main() -> shutdown_nonqt()).
        # It needs to be robust against file I/O errors to avoid hanging the exit.
        try:
            with self.sidecar._state_lock:
                self.sidecar.data.sort_mode = self.sort_mode
            self.sidecar.set_last_position(
                self.current_index,
                self._current_image_path(),
            )
            if not self._persist_sidecar(notify=False) and self.sidecar.dirty:
                log.warning("Retrying dirty sidecar once during shutdown")
                _ = self._persist_sidecar(notify=False)
        except Exception as e:
            log.warning("Error saving sidecar during shutdown: %s", e)
        finally:
            self._shutdown_final_sidecar_attempted = True

        # Clean shutdown — remove this instance's crash-recovery record so it is
        # not offered for reopening on the next launch.
        try:
            self._session_registry.close()
        except Exception as e:
            log.warning("Error removing session record during shutdown: %s", e)

        log.info("Background shutdown complete.")

    def shutdown(self):
        """Legacy shutdown method - calls both Qt and non-Qt shutdown."""
        self.shutdown_qt()
        self.shutdown_nonqt()

    def _prune_delete_history_for_bins(self, bin_dirs: Set[Path]) -> None:
        """Drop delete/undo records whose recycled files lived in *bin_dirs*.

        Called after recycle bins are removed so Ctrl+Z cannot get stuck
        retrying restores that can never succeed.
        """
        if not bin_dirs:
            return
        resolved_bins = set()
        for d in bin_dirs:
            try:
                resolved_bins.add(d.resolve())
            except OSError:
                resolved_bins.add(d)

        def _record_gone(record) -> bool:
            try:
                (_, jpg_bin), (_, raw_bin) = record
            except (TypeError, ValueError):
                return False
            for bin_path in (jpg_bin, raw_bin):
                if bin_path is None:
                    continue
                try:
                    parent = Path(bin_path).parent.resolve()
                except OSError:
                    parent = Path(bin_path).parent
                if parent in resolved_bins:
                    return True
            return False

        self.delete_history = [r for r in self.delete_history if not _record_gone(r)]
        self.undo_history = [
            (atype, adata, ts)
            for atype, adata, ts in self.undo_history
            if atype != "delete" or not _record_gone(adata)
        ]

    def _purge_recycle_bins(self, bin_dirs: Set[Path]) -> bool:
        """Delete bins, purge original metadata, then retire their history."""
        removed_bins: Set[Path] = set()
        failed = False
        for bin_path in bin_dirs:
            try:
                if bin_path.exists():
                    shutil.rmtree(bin_path)
                removed_bins.add(bin_path)
            except OSError:
                failed = True
                log.exception("Failed to delete recycle bin %s", bin_path)

        resolved_bins = set()
        for bin_path in removed_bins:
            try:
                resolved_bins.add(bin_path.resolve())
            except OSError:
                resolved_bins.add(bin_path)

        original_paths: set[Path] = set()
        delete_records = list(self.delete_history)
        delete_records.extend(
            action_data
            for action_type, action_data, _timestamp in self.undo_history
            if action_type == "delete" and action_data not in delete_records
        )

        def recycled_parent(recycled) -> Optional[Path]:
            if recycled is None:
                return None
            parent = Path(recycled).parent
            try:
                return parent.resolve()
            except OSError:
                return parent

        for record in delete_records:
            try:
                (jpg_src, jpg_bin), (raw_src, raw_bin) = record
            except (TypeError, ValueError):
                continue
            if any(
                recycled_parent(recycled) in resolved_bins
                for recycled in (jpg_bin, raw_bin)
            ):
                original_paths.update(
                    Path(path) for path in (jpg_src, raw_src) if path is not None
                )

        # The recycled file was the only thing that tied an original-path
        # entry to the deleted image. If a file now occupies that path (a
        # replacement copied in, a re-export, a restore by other means), the
        # entry describes the occupant -- its edits and flags -- and must
        # survive emptying the bin.
        def path_reoccupied(path: Path) -> bool:
            try:
                return os.path.lexists(path)
            except (OSError, ValueError):
                return True  # Unknown: keep metadata rather than risk loss.

        metadata_changed = False
        with self.sidecar._state_lock:
            for path in original_paths:
                if path_reoccupied(path):
                    log.debug(
                        "Keeping metadata for %s: path is occupied by another file",
                        path,
                    )
                    continue
                key = self.sidecar.metadata_key_for_path(path)
                if self.sidecar.data.entries.pop(key, None) is not None:
                    metadata_changed = True

        if metadata_changed and not self._persist_sidecar():
            return False

        self._prune_delete_history_for_bins(removed_bins)
        self.active_recycle_bins.difference_update(removed_bins)
        clear_raw_count_cache()
        return not failed

    def empty_recycle_bin(self) -> bool:
        """Permanently deletes all files in all tracked recycle bins."""
        # Clean up tracked bins
        bins_to_clean = set(self.active_recycle_bins)
        # Check base bin too
        try:
            bins_to_clean.add(self.image_dir / "image recycle bin")
        except Exception:
            pass

        persisted = self._purge_recycle_bins(bins_to_clean)
        if persisted:
            self.update_status_message("Recycle bin emptied")
            log.info("Emptied recycle bins and purged their metadata")
        return persisted

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

        # Let the prefetcher know this path's cached bytes are gone, so its
        # `_scheduled` bookkeeping doesn't permanently treat the index as
        # "already decoded" (which would silently stop prefetching it for
        # the rest of the generation). "replace"/"manual" evictions are
        # already handled by invalidate_path()/pop_path() at the call site
        # that caused them, so only do this for capacity-pressure evictions.
        # NOTE: this callback can run outside the cache's own lock, possibly
        # on a prefetch worker thread; unschedule_path() only takes
        # Prefetcher._futures_lock (never the cache lock), so there is no
        # lock-ordering hazard here.
        prefetcher = getattr(self, "prefetcher", None)
        if prefetcher is not None:
            try:
                path_str = str(key).rsplit("::", 2)[0]
                prefetcher.unschedule_path(path_str)
            except Exception:
                log.debug(
                    "unschedule_path failed for evicted key=%s", key, exc_info=True
                )

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

        key_name = str(key)
        msg = None
        with self._eviction_lock:
            # Timestamp under the lock so worker callbacks cannot append events
            # out of order and prevent expired entries from being pruned.
            now = time.monotonic()
            cutoff = now - CACHE_THRASH_WINDOW_SECS
            while self._eviction_events and self._eviction_events[0][0] <= cutoff:
                _, expired_key = self._eviction_events.popleft()
                self._eviction_counts[expired_key] -= 1
                if self._eviction_counts[expired_key] == 0:
                    del self._eviction_counts[expired_key]
            self._eviction_events.append((now, key_name))
            self._eviction_counts[key_name] += 1

            # One eviction per key is normal turnover. Evicting the SAME key
            # again proves it was reinserted and lost to capacity pressure
            # repeatedly. Full keys distinguish generations and quality tiers,
            # so resize/zoom changes do not look like repeated decode churn.
            repeat_evictions = len(self._eviction_events) - len(self._eviction_counts)
            if (
                repeat_evictions > CACHE_THRASH_THRESHOLD
                and now - self._last_cache_warning_time > CACHE_WARNING_COOLDOWN_SECS
            ):
                self._last_cache_warning_time = now
                self._has_warned_cache_full = True
                repeated_keys = sum(
                    count > 1 for count in self._eviction_counts.values()
                )
                used_gb = eviction_usage / (1024**3)
                max_gb = eviction_max / (1024**3)
                msg = (
                    f"Cache thrashing: {repeat_evictions} repeat capacity evictions "
                    f"across {repeated_keys} entries in {CACHE_THRASH_WINDOW_SECS}s. "
                    f"Usage: {used_gb:.1f}GB / {max_gb:.1f}GB. "
                    "Consider increasing Cache Size in Settings."
                )

        if msg is not None:
            log.warning(msg)
            self._cacheWarningRaised.emit(msg)

    @Slot()
    def open_raw_in_rawtherapee(self):
        """Open the current RAW in RawTherapee's interactive editor."""
        if not self.image_files or not 0 <= self.current_index < len(self.image_files):
            self.update_status_message("No image selected.")
            return

        image = self.image_files[self.current_index]
        raw_path = image.raw_pair
        if raw_path is None and image.path.suffix.lower() in RAW_EXTENSIONS:
            raw_path = image.path
        if raw_path is None or not raw_path.is_file():
            self.update_status_message("No RAW file exists for the current image.")
            return

        executable = self._configured_rawtherapee_executable()
        # Development uses the CLI binary; interactive editing needs its GUI sibling.
        if executable:
            path = Path(executable)
            if path.stem.lower() == "rawtherapee-cli":
                executable = str(path.with_name(f"rawtherapee{path.suffix}"))
        if not executable or not Path(executable).is_file():
            executable = shutil.which("rawtherapee") or shutil.which("rawtherapee.exe")
        if not executable:
            self.update_status_message("RawTherapee editor not found. Check Settings.")
            return
        valid, error = validate_executable_path(
            executable, app_type="rawtherapee", allow_custom_paths=True
        )
        if not valid:
            self.update_status_message(f"RawTherapee validation failed: {error}")
            return
        try:
            # Never a shell. RawTherapee's output is relayed as "[RawTherapee]"
            # lines and the editor keeps running if FastStack is closed first.
            launch_with_relayed_output(
                [executable, str(raw_path.resolve())],
                name="RawTherapee",
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            self.update_status_message(f"Opened {raw_path.name} in RawTherapee.")
        except OSError as exc:
            log.exception("Failed to launch RawTherapee")
            self.update_status_message(f"Could not open RawTherapee: {exc}")

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
        self.ui_state.resetZoomPan(True)  # the user asked: ease back to fit
        self.update_status_message("Reset zoom and pan")

    def _persist_sidecar(
        self,
        sidecar: Optional[SidecarManager] = None,
        *,
        notify: bool = True,
    ) -> bool:
        """Persist metadata without losing retryable in-memory state on failure."""
        manager = sidecar or self.sidecar
        saved = manager.save()
        recovery_backup = (
            manager.take_recovery_backup_notice() if saved and notify else None
        )
        if recovery_backup is not None and not self._shutting_down:
            recovery_message = (
                f"Unreadable metadata was preserved as {recovery_backup.name}; "
                "new metadata was saved."
            )
            QTimer.singleShot(
                0,
                lambda: self.update_status_message(
                    recovery_message,
                    timeout=12000,
                    color="#FFD54F",
                ),
            )
        if not saved:
            log.error(
                "Metadata remains dirty after sidecar persistence failure: %s",
                manager.last_save_error,
            )
            if notify and not self._shutting_down:
                self.update_status_message(
                    "Metadata could not be saved; changes are retained for retry.",
                    timeout=8000,
                    color="#FFD54F",
                )
        return saved

    def update_status_message(self, message: str, timeout: int = 3000, color: str = ""):
        """
        Updates the UI status message and clears it after a timeout.

        ``color`` optionally overrides the status text color (e.g. a yellow
        warning). Pass "" (the default) to use the standard text color.
        """
        self._status_message_token += 1
        token = self._status_message_token

        def clear_message():
            if (
                self._status_message_token == token
                and self.ui_state.statusMessage == message
            ):
                self.ui_state.statusMessage = ""
                self.ui_state.statusMessageColor = ""

        # statusMessage's setter resets statusMessageColor to "" as soon as
        # the text changes, so the color must be applied after the text.
        self.ui_state.statusMessage = message
        self.ui_state.statusMessageColor = color
        QTimer.singleShot(timeout, clear_message)

    def _maybe_show_turbo_fallback_warning(self):
        """Warn once in loupe view if TurboJPEG is unavailable (slower Pillow path)."""
        if self._turbo_fallback_warning_shown:
            return
        from faststack.imaging.jpeg import TURBO_AVAILABLE

        if TURBO_AVAILABLE:
            return
        self._turbo_fallback_warning_shown = True
        log.warning(
            "TurboJPEG not available - falling back to Pillow (slower decoding)"
        )
        self.update_status_message(
            "TurboJPEG not installed - using slower Pillow decoding. "
            "Install PyTurboJPEG for faster image loading.",
            timeout=10000,
            color="#FFD54F",
        )

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

    @staticmethod
    def _pending_edit_state_is_superseded(
        pending_state: dict[str, Any], saved_state: dict[str, Any]
    ) -> bool:
        """Is a stored pending_save marker provably OLDER than a saved state?

        A differing ``request_id`` alone does not prove the stored marker is
        newer. Every published request carries the live session's monotonic
        ``save_revision`` plus a ``session_token`` of
        ``[image_key, view_override_kind, session_id, revision]``. When both
        markers come from the same session lineage (the first three token
        components match) their revisions are directly comparable, so a stored
        revision that is not greater than the one just saved is stale and must
        be overwritten rather than "preserved as newer" (FS-P1-002).

        When the lineage cannot be compared the answer is False: keeping an
        unorderable pending marker risks a redundant replay, dropping it risks
        losing edits, and only the latter is data loss.
        """
        pending_lineage = pending_state.get("session_token")
        saved_lineage = saved_state.get("session_token")
        if not isinstance(pending_lineage, (list, tuple)) or not isinstance(
            saved_lineage, (list, tuple)
        ):
            return False
        if len(pending_lineage) < 3 or len(saved_lineage) < 3:
            return False
        if list(pending_lineage[:3]) != list(saved_lineage[:3]):
            return False

        pending_revision = pending_state.get("revision")
        saved_revision = saved_state.get("revision")
        if isinstance(pending_revision, bool) or isinstance(saved_revision, bool):
            return False
        if not isinstance(pending_revision, int) or not isinstance(saved_revision, int):
            return False
        return pending_revision <= saved_revision

    def _mark_image_edited_in_sidecar(
        self,
        sidecar: SidecarManager,
        image_path: Path,
        *,
        completed_edit_state_request_id: Optional[str] = None,
        saved_edit_state: Optional[dict[str, Any]] = None,
        clear_saved_edit_state: bool = False,
        background: bool = False,
    ) -> Optional[dict]:
        """Mark an image as edited and return the pre-save metadata snapshot.

        ``background`` writes the sidecar on the session-write worker instead
        of on the calling (GUI) thread. A write that fails there is retried
        synchronously and reported. A crash before it lands leaves the saved
        image without its edited flag, and its edit state as the start of the
        save left it: still marked pending_save, or absent when no pending
        state was written.
        """
        old_meta = self._capture_metadata_snapshot(sidecar, image_path)
        new_meta = dict(old_meta or {})
        new_meta["edited"] = True
        new_meta["edited_date"] = datetime.now().strftime("%Y-%m-%d")
        if saved_edit_state is not None:
            current_edit_state = new_meta.get("edit_state")
            if (
                isinstance(current_edit_state, dict)
                and current_edit_state.get("status") == "pending_save"
                and completed_edit_state_request_id is not None
                and current_edit_state.get("request_id")
                != completed_edit_state_request_id
                and not self._pending_edit_state_is_superseded(
                    current_edit_state, saved_edit_state
                )
            ):
                log.debug(
                    "Preserving newer pending edit state for %s after save %s finished",
                    image_path,
                    completed_edit_state_request_id,
                )
            else:
                new_meta["edit_state"] = (
                    None if clear_saved_edit_state else saved_edit_state
                )
        elif clear_saved_edit_state:
            current_edit_state = new_meta.get("edit_state")
            if not (
                isinstance(current_edit_state, dict)
                and current_edit_state.get("status") == "pending_save"
                and completed_edit_state_request_id is not None
                and current_edit_state.get("request_id")
                != completed_edit_state_request_id
            ):
                new_meta["edit_state"] = None
        # Only the current folder's sidecar goes to the worker: it is the one
        # the shutdown path saves, so a write still queued at exit is covered.
        if background and sidecar is self.sidecar and not self._shutting_down:
            sidecar.update_metadata(image_path, new_meta, save=False)
            self._queue_sidecar_write(sidecar)
        elif not sidecar.update_metadata(image_path, new_meta):
            log.error(
                "Image pixels were saved but edited metadata remains pending for %s",
                image_path,
            )
            if not self._shutting_down:
                self.update_status_message(
                    "Image saved; metadata is retained for retry.",
                    timeout=8000,
                    color="#FFD54F",
                )
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
    ) -> bool:
        """Restore only the metadata fields owned by edit-save actions."""
        stable_key = sidecar.metadata_key_for_path(image_path)
        restored_edited = bool(snapshot.get("edited", False)) if snapshot else False
        restored_edited_date = snapshot.get("edited_date") if snapshot else None
        restored_edit_state = snapshot.get("edit_state") if snapshot else None
        changed = False

        with sidecar._state_lock:
            current_meta = sidecar.data.entries.get(stable_key)
            if current_meta is None:
                if not restored_edited and restored_edited_date is None:
                    return True
                current_meta = sidecar.get_metadata(image_path, create=True)
                changed = True

            if current_meta.edited != restored_edited:
                current_meta.edited = restored_edited
                changed = True
            if current_meta.edited_date != restored_edited_date:
                current_meta.edited_date = restored_edited_date
                changed = True
            if current_meta.edit_state != restored_edit_state:
                current_meta.edit_state = restored_edit_state
                changed = True

            if snapshot is None:
                default_meta = EntryMetadata()
                if current_meta.__dict__ == default_meta.__dict__:
                    del sidecar.data.entries[stable_key]
                    changed = True

        return not changed or self._persist_sidecar(sidecar)

    def _is_image_saving(self, file_path_str: str) -> bool:
        if not file_path_str or not hasattr(self, "_saving_keys"):
            return False
        return self._saving_keys.get(self._key(Path(file_path_str)), 0) > 0

    def _block_if_saving(self, *paths) -> bool:
        """Helper to block actions if any of the given paths are currently saving."""
        for path in paths:
            if path and self._is_image_saving(str(path)):
                self.update_status_message(
                    "This image is still saving. Please wait a moment.", timeout=3000
                )
                return True
        return False

    _DRAG_PREVIEW_EDGE = 128

    def _drag_preview_pixmap(self, path: Path) -> Optional[QPixmap]:
        """Build the small drag icon without decoding the whole file.

        QImageReader with a scaled size lets the JPEG decoder work at a
        reduced DCT scale, so this stays cheap on the UI thread even for
        very large files.
        """
        edge = self._DRAG_PREVIEW_EDGE
        try:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and not size.isEmpty():
                size.scale(edge, edge, Qt.KeepAspectRatio)
                reader.setScaledSize(size)
            image = reader.read()
        except (RuntimeError, OSError):
            return None
        if image.isNull():
            return None
        if image.width() > edge or image.height() > edge:
            image = image.scaled(
                edge, edge, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        return QPixmap.fromImage(image)

    @Slot()
    def start_drag_current_image(self):
        if not self.image_files or self.current_index >= len(self.image_files):
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

        current_path = self.image_files[self.current_index].path
        current_in_drag = self.current_index in existing_indices
        if current_in_drag and self._is_current_live_edit_session_dirty():
            if self._block_if_saving(current_path):
                return
            if not self._flush_current_live_edit_session_for_drag():
                return

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

        # Paths, not indices: the list can shift while the drag's nested event
        # loop runs (a watcher refresh, a delete), and the bookkeeping below may
        # fire from inside that loop.
        dragged_paths = [self.image_files[idx].path for idx in existing_indices]
        drag_state = {
            "payload_read": False,
            "completed": False,
            "action": None,
            "exec_returned": False,
            "trigger": None,
        }
        # --debug only: a timeline of what the drop target and Qt did.
        drag_t0 = time.perf_counter()
        drag_diag = {"reads": [], "actions": [], "releases": []}

        def drag_elapsed_ms() -> float:
            return (time.perf_counter() - drag_t0) * 1000.0

        def complete_drag(trigger: str) -> None:
            if drag_state["completed"]:
                return
            drag_state["completed"] = True
            drag_state["trigger"] = trigger
            self._mark_drag_uploaded(dragged_paths, trigger)

        def release_stuck_drag() -> None:
            # QDrag.cancel() is global: it cancels whatever drag is current,
            # not the one that armed this timer. If exec() already returned
            # (the target finished the drop properly), a drag the user started
            # in the meantime would be the one cancelled.
            if drag_state["exec_returned"]:
                return
            try:
                QDrag.cancel()
            except RuntimeError:
                log.debug("Could not cancel the drag", exc_info=True)

        def note_payload_read(mime_type: str, data) -> None:
            if not drag_state["payload_read"]:
                log.info("[drag] drop target read %s", mime_type)
            drag_state["payload_read"] = True
            if _debug_mode:
                if isinstance(data, (bytes, bytearray)):
                    size = f"{len(data)} bytes"
                elif isinstance(data, (list, tuple)):
                    size = f"{len(data)} item(s)"
                else:
                    size = "size unknown"
                drag_diag["reads"].append(mime_type)
                log.debug(
                    "[drag +%.0fms] payload read: %s (%s, %s)",
                    drag_elapsed_ms(),
                    mime_type,
                    type(data).__name__,
                    size,
                )

        def note_action(action) -> None:
            if action != drag_state["action"]:
                log.info("[drag] target action now %s", action)
            drag_state["action"] = action
            if _debug_mode:
                drag_diag["actions"].append(str(action))
                log.debug("[drag +%.0fms] actionChanged: %s", drag_elapsed_ms(), action)

        def note_target(target) -> None:
            # Only fires for targets inside this process; external targets
            # (Firefox) show up as None.
            log.debug("[drag +%.0fms] targetChanged: %s", drag_elapsed_ms(), target)

        def note_release(event_type) -> None:
            if _debug_mode:
                drag_diag["releases"].append(str(event_type))
                log.debug(
                    "[drag +%.0fms] release-class event: %s",
                    drag_elapsed_ms(),
                    event_type,
                )
            # A read alone is not a drop -- Firefox reads on hover -- and a
            # release alone may be an aborted drag over empty desktop.
            if not drag_state["payload_read"]:
                log.info("[drag] release before any payload read; not a drop")
                return
            # Released over something that refused the files (the user hovered
            # a target, thought better of it, and let go elsewhere).
            if drag_state["action"] == Qt.IgnoreAction:
                log.info("[drag] release over a target refusing the drop")
                return
            complete_drag("payload read + pointer release")
            # exec() is still parked in its nested loop; release it so the next
            # drag does not nest inside this one.
            QTimer.singleShot(WAYLAND_DRAG_CANCEL_DELAY_MS, release_stuck_drag)

        # On Wayland a target that never sends wl_data_offer.finish leaves
        # exec() blocked forever, so the drop is inferred from those two signals
        # instead of from the return value.
        on_wayland = QGuiApplication.platformName().startswith("wayland")
        watcher = None
        if on_wayland:
            drag.actionChanged.connect(note_action)
            mime_data = DragPayloadMimeData(note_payload_read)
            app = QGuiApplication.instance()
            if app is not None:
                watcher = DragReleaseWatcher(self, note_release)
                app.installEventFilter(watcher)
        elif _debug_mode:
            # Instrumented for the timeline only; outcome still comes from exec().
            drag.actionChanged.connect(note_action)
            mime_data = DragPayloadMimeData(note_payload_read)
        else:
            mime_data = QMimeData()
        if _debug_mode:
            drag.targetChanged.connect(note_target)

        # Use Qt's standard setUrls - it handles both browser and native app compatibility
        urls = [QUrl.fromLocalFile(str(p)) for p in file_paths]
        mime_data.setUrls(urls)

        drag.setMimeData(mime_data)

        # --- thumbnail / drag preview ---
        pix = self._drag_preview_pixmap(file_paths[0])
        if pix is not None and not pix.isNull():
            drag.setPixmap(pix)
            # hotspot = center of image
            drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))

        log.info(
            "Starting drag for %d file(s) on platform %r with formats %s: %s",
            len(file_paths),
            QGuiApplication.platformName(),
            mime_data.formats(),
            [str(p) for p in file_paths],
        )
        protocol_monitor = _wayland_drag_monitor if on_wayland else None
        if _debug_mode:
            self._log_drag_start_diagnostics(
                file_paths, urls, pix, on_wayland, protocol_monitor
            )
        # Support both Copy and Move actions for browser compatibility
        if protocol_monitor is not None:
            protocol_monitor.begin()
        try:
            result = drag.exec(Qt.CopyAction | Qt.MoveAction)
        finally:
            drag_state["exec_returned"] = True
            protocol_finished = (
                protocol_monitor.end() if protocol_monitor is not None else False
            )
            if watcher is not None:
                app = QGuiApplication.instance()
                if app is not None:
                    app.removeEventFilter(watcher)
                watcher.deleteLater()
        log.info("Drag completed with result: %s", result)

        # Reset zoom/pan after drag completes (drag can cause unwanted panning)
        self.ui_state.resetZoomPan()

        if result in (Qt.CopyAction, Qt.MoveAction):
            complete_drag("exec result")
        elif on_wayland and protocol_finished:
            log.info("[drag] Wayland data source finished after accepted drop")
            complete_drag("Wayland dnd_finished")

        if _debug_mode:
            elapsed = drag_elapsed_ms()
            if protocol_monitor is not None:
                for line in _collapse_repeated_trace(protocol_monitor.take_trace()):
                    log.debug("[drag wayland] %s", line)
                protocol_outcome = protocol_monitor.outcome()
            elif on_wayland:
                protocol_outcome = "monitor not installed"
            else:
                protocol_outcome = "n/a (not Wayland)"
            log.debug(
                "[drag] summary: exec=%.0fms result=%s reads=%s actions=%s "
                "releases=%s protocol=%s marked_uploaded=%s",
                elapsed,
                result,
                drag_diag["reads"] or "none",
                drag_diag["actions"] or "none",
                drag_diag["releases"] or "none",
                protocol_outcome,
                drag_state["trigger"] or "no",
            )
            if not drag_diag["reads"] and not drag_diag["actions"]:
                log.debug(
                    "[drag] no target ever saw the drag: it was cancelled by the "
                    "compositor/Qt or released without crossing a drop target"
                )

    def _log_drag_start_diagnostics(
        self, file_paths, urls, pix, on_wayland, protocol_monitor
    ) -> None:
        """--debug: record the environment a drag starts in."""
        env = {
            key: os.environ.get(key)
            for key in (
                "QT_QPA_PLATFORM",
                "XDG_SESSION_TYPE",
                "XDG_CURRENT_DESKTOP",
                "WAYLAND_DISPLAY",
                "DISPLAY",
            )
        }
        log.debug(
            "[drag] platform=%s on_wayland=%s protocol_monitor=%s qt=%s env=%s",
            QGuiApplication.platformName(),
            on_wayland,
            protocol_monitor is not None,
            qVersion(),
            env,
        )
        window = self.main_window
        log.debug(
            "[drag] cursor=%s window_visible=%s window_active=%s focus_window=%s "
            "mouse_buttons=%s pixmap=%s",
            QCursor.pos(),
            window.isVisible() if window is not None else None,
            window.isActive() if window is not None else None,
            QGuiApplication.focusWindow(),
            QGuiApplication.mouseButtons(),
            "none" if pix.isNull() else f"{pix.width()}x{pix.height()}",
        )
        for path, url in zip(file_paths, urls):
            try:
                size = path.stat().st_size
            except OSError as exc:
                size = f"stat failed: {exc}"
            log.debug(
                "[drag] file %s size=%s readable=%s url=%s",
                path,
                size,
                os.access(path, os.R_OK),
                url.toString(),
            )

    # --- iNaturalist upload (Actions menu) ---------------------------------

    def _batch_export_paths(self) -> tuple[list[Path], list[Path]]:
        """(files to send, their catalogue paths), batch files or the current image.

        Same selection as drag-and-drop: every photo in every defined batch,
        else the current image, preferring the developed JPG in RAW mode.
        """
        if not self.image_files:
            return [], []
        indices: set[int] = set()
        for start, end in self.batches:
            for idx in range(start, end + 1):
                if 0 <= idx < len(self.image_files):
                    indices.add(idx)
        if not indices and 0 <= self.current_index < len(self.image_files):
            indices.add(self.current_index)
        send: list[Path] = []
        catalogue: list[Path] = []
        in_raw_mode = getattr(self, "current_edit_source_mode", "jpeg") == "raw"
        for idx in sorted(indices):
            img = self.image_files[idx]
            if not img.path.exists():
                continue
            is_developed_artifact = img.path.stem.lower().endswith("-developed")
            if (in_raw_mode or is_developed_artifact) and img.developed_jpg_path.exists():
                send.append(img.developed_jpg_path)
            else:
                send.append(img.path)
            catalogue.append(img.path)
        return send, catalogue

    @Slot(result=bool)
    def open_inat_upload(self) -> bool:
        """Prepare the iNaturalist upload dialog for the current batch."""
        inat_upload = getattr(self, "_inat_upload", None)
        if inat_upload is None:
            self.update_status_message("iNaturalist upload is not available.")
            return False
        send, catalogue = self._batch_export_paths()
        if not send:
            self.update_status_message("No photos in the batch to upload.")
            return False
        current_path = (
            self.image_files[self.current_index].path
            if 0 <= self.current_index < len(self.image_files)
            else None
        )
        if current_path in catalogue and self._is_current_live_edit_session_dirty():
            if self._block_if_saving(current_path):
                return False
            if not self._flush_current_live_edit_session_for_drag(
                saving_status="Saving edits before upload..."
            ):
                return False
        for path in send:
            if self._block_if_saving(path):
                return False
        self._inat_upload_catalogue = {str(s): c for s, c in zip(send, catalogue)}
        if not inat_upload.open([str(p) for p in send]):
            self.update_status_message("Could not read any of the batch photos.")
            return False
        return True

    def _on_inat_upload_finished(self, uploaded_paths, all_done: bool) -> None:
        """Mark the photos uploaded in the sidecar, like an accepted drag.

        Batches are cleared only once every card went up, so a partial run
        leaves the failed photos batched for another try.
        """
        lookup = getattr(self, "_inat_upload_catalogue", {}) or {}
        catalogue_paths = []
        for raw in uploaded_paths:
            path = lookup.get(str(raw), Path(str(raw)))
            catalogue_paths.append(Path(path))
        if not catalogue_paths:
            return
        if all_done:
            self._mark_drag_uploaded(catalogue_paths, "inaturalist upload")
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            for path in catalogue_paths:
                meta = self.sidecar.get_metadata(path)
                meta.uploaded = True
                meta.uploaded_date = today
            self._persist_sidecar()
            # Same fan-out as the flag toggles: drop the cached metadata line,
            # re-run an active "uploaded" filter, and refresh grid and loupe.
            self._metadata_cache_index = (-1, -1)
            current_path = (
                self.image_files[self.current_index].path
                if 0 <= self.current_index < len(self.image_files)
                else None
            )
            filter_reapplied = self._reapply_flag_filter_after_metadata_change(
                "uploaded", current_path
            )
            self.dataChanged.emit()
            if not filter_reapplied and getattr(self, "_thumbnail_model", None):
                self._thumbnail_model.notify_batch_state_changed()
            self.sync_ui_state(image_count_changed=filter_reapplied)
        self.update_status_message(
            f"Uploaded {len(catalogue_paths)} photo(s) to iNaturalist", 5000
        )

    def _mark_drag_uploaded(self, dragged_paths, trigger: str) -> None:
        """Mark dragged files uploaded and clear batches after an accepted drop."""
        today = datetime.now().strftime("%Y-%m-%d")

        for path in dragged_paths:
            meta = self.sidecar.get_metadata(path)
            meta.uploaded = True
            meta.uploaded_date = today

        # Clear all batches after successful drag (like pressing \). The
        # uploaded flags and the cleared batch flags go out in one sidecar
        # write; on failure everything stays in memory, marked dirty for retry.
        self.batches = []
        self.batch_start_index = None
        if not self._finalize_batch_state(reapply_filter=True, force_persist=True):
            return
        log.info(
            "Marked %d file(s) as uploaded on %s (%s). Cleared all batches.",
            len(dragged_paths),
            today,
            trigger,
        )

    @Slot()
    def enable_raw_editing(self):
        """Switches the current image to RAW mode (using developed TIFF)."""
        if not self.image_files:
            return

        current_image_path = self.image_files[self.current_index].path
        if self._block_if_saving(current_image_path):
            return

        image_file = self.image_files[self.current_index]
        if not image_file.has_raw:
            self.update_status_message("No RAW file available.")
            return

        # 1. Update State
        if self.current_edit_source_mode != "raw":
            self.current_edit_source_mode = "raw"
            self.editSourceModeChanged.emit("raw")
        self.sync_ui_state()

        # 2. Check if we have a valid TIFF ready
        path = self.get_active_edit_path(self.current_index)

        # If the path returned IS the working TIFF (and it exists), we can just load it.
        # Check specific condition:
        if path == image_file.working_tif_path and self.is_valid_working_tif(path):
            log.info("Valid working TIFF exists, switching to RAW mode immediately.")
            self.load_image_for_editing()  # This will now pick up the TIFF via get_active_edit_path
            return

        # 3. If not ready, trigger development
        # (Pass through to existing backend logic)
        self._develop_raw_backend()

    def _raw_development_key_for_image(self, image_file: ImageFile) -> str:
        return self._key(image_file.working_tif_path) or str(
            image_file.working_tif_path
        )

    def _is_raw_development_in_flight(self, image_file: ImageFile) -> bool:
        key = self._raw_development_key_for_image(image_file)
        with self._raw_develop_lock:
            return key in self._raw_developing_keys

    @Slot(result=bool)
    def is_raw_developing_current(self) -> bool:
        if not self.image_files or not (
            0 <= self.current_index < len(self.image_files)
        ):
            return False
        return self._is_raw_development_in_flight(self.image_files[self.current_index])

    def _mark_raw_development_started(self, image_file: ImageFile) -> Optional[str]:
        key = self._raw_development_key_for_image(image_file)
        with self._raw_develop_lock:
            if key in self._raw_developing_keys:
                return None
            self._raw_developing_keys.add(key)
        self.rawDevelopmentStateChanged.emit()
        return key

    def _mark_raw_development_finished(
        self, key: Optional[str], operation_id: Optional[str] = None
    ) -> None:
        if not key:
            return
        with self._raw_develop_lock:
            operations = getattr(self, "_raw_development_operations", {})
            current = operations.get(key)
            if (
                operation_id is not None
                and current is not None
                and current.operation_id != operation_id
            ):
                return
            if current is not None:
                operations.pop(key, None)
            if key not in self._raw_developing_keys:
                return
            self._raw_developing_keys.remove(key)
        self.rawDevelopmentStateChanged.emit()

    @staticmethod
    def _stop_raw_process(process: subprocess.Popen, *, grace_seconds: float = 1.0):
        """Terminate exactly one owned RawTherapee child, escalating if needed."""
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=grace_seconds)
            return
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            log.warning("RawTherapee did not terminate gracefully; killing it")
        try:
            process.kill()
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            log.error("RawTherapee could not be confirmed stopped", exc_info=True)

    def _cancel_raw_development_operations(self) -> None:
        """Cancel active RAW operations and briefly reconcile owned workers."""
        with self._raw_develop_lock:
            operations = list(getattr(self, "_raw_development_operations", {}).values())
            for operation in operations:
                operation.cancel_requested = True
        for operation in operations:
            process = operation.process
            if process is not None:
                self._stop_raw_process(process)
        current_thread = threading.current_thread()
        for operation in operations:
            worker = operation.worker
            if worker is None or worker is current_thread:
                continue
            worker.join(timeout=0.5)
            if worker.is_alive():
                log.warning(
                    "RAW development worker did not stop within shutdown timeout: %s",
                    operation.source_path,
                )
        for operation in operations:
            try:
                operation.tmp_tif_path.unlink(missing_ok=True)
            except OSError:
                log.warning(
                    "Could not remove cancelled RAW temporary output %s",
                    operation.tmp_tif_path,
                    exc_info=True,
                )

    def _configured_rawtherapee_executable(self) -> str:
        """Return the configured RawTherapee path, unquoted and expanded."""
        configured = str(config.get("rawtherapee", "exe") or "")
        return os.path.expanduser(os.path.expandvars(configured.strip('" ')))

    def _develop_raw_backend(self):
        """Internal: Triggers the actual RawTherapee process."""
        if not self.image_files:
            return False

        image_file = self.image_files[self.current_index]
        if not image_file.has_raw:
            self.update_status_message("No RAW file available.")
            return False

        if self._is_raw_development_in_flight(image_file):
            self.update_status_message("RAW development already in progress.")
            return False

        raw_path = image_file.raw_path
        tif_path = image_file.working_tif_path
        if raw_path is None or not raw_path.exists():
            self.update_status_message("RAW file is missing.")
            return False

        develop_key = self._mark_raw_development_started(image_file)
        if develop_key is None:
            self.update_status_message("RAW development already in progress.")
            return False
        tmp_tif_path = tif_path.with_name(
            f".{tif_path.stem}_{uuid.uuid4().hex[:8]}{tif_path.suffix}"
        )

        # Resolve and validate the RawTherapee executable the same way the
        # interactive launcher does (expansion, unquoting, validation).
        rt_exe = self._configured_rawtherapee_executable()
        if not rt_exe or not os.path.exists(rt_exe):
            self._mark_raw_development_finished(develop_key)
            self.update_status_message("RawTherapee not found. Check settings.")
            log.error("RawTherapee executable not configured or missing: %s", rt_exe)
            return False
        valid, error = validate_executable_path(
            rt_exe, app_type="rawtherapee", allow_custom_paths=True
        )
        if not valid:
            self._mark_raw_development_finished(develop_key)
            self.update_status_message(f"RawTherapee validation failed: {error}")
            log.error("RawTherapee executable rejected: %s", error)
            return False
        self.update_status_message("Developing RAW... please wait.")
        log.info("Starting RAW development: %s -> %s", raw_path, tif_path)
        operation_id = uuid.uuid4().hex
        source_path = image_file.path
        operation = RawDevelopmentOperation(
            operation_id=operation_id,
            develop_key=develop_key,
            source_path=source_path,
            raw_path=raw_path,
            tif_path=tif_path,
            tmp_tif_path=tmp_tif_path,
        )
        with self._raw_develop_lock:
            operations = getattr(self, "_raw_development_operations", None)
            if operations is None:
                operations = {}
                self._raw_development_operations = operations
            operations[develop_key] = operation

        def worker():
            completion = RawDevelopmentResult(
                operation_id=operation_id,
                source_path=source_path,
                tif_path=tif_path,
                develop_key=develop_key,
                success=False,
                error="RAW development did not complete.",
            )
            try:
                rt_args = config.get("rawtherapee", "args")
                cmd = [rt_exe, "-t", "-b16", "-Y", "-o", str(tmp_tif_path)]
                if rt_args:
                    try:
                        cmd.extend(parse_external_arguments(rt_args))
                    except ValueError as e:
                        log.error("Invalid rawtherapee args format: %s", e)
                cmd.extend(["-c", str(raw_path)])
                cmd_str = " ".join(cmd)
                popen_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "env": child_environment(),
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

                process = subprocess.Popen(cmd, shell=False, **popen_kwargs)
                with self._raw_develop_lock:
                    current = self._raw_development_operations.get(develop_key)
                    if current is operation:
                        operation.process = process
                    cancelled = operation.cancel_requested or current is not operation
                if cancelled:
                    self._stop_raw_process(process)
                    raise RuntimeError("RAW development cancelled during shutdown")

                try:
                    stdout, stderr = process.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    operation.cancel_requested = True
                    self._stop_raw_process(process)
                    raise
                # Show what the CLI printed, with its name in front, like the
                # tools FastStack does not wait for.
                tool_name = Path(rt_exe).stem or "rawtherapee-cli"
                log_tool_output(tool_name, stdout)
                log_tool_output(tool_name, stderr)

                if process.returncode == 0:
                    with self._raw_develop_lock:
                        current = self._raw_development_operations.get(develop_key)
                        cancelled = (
                            operation.cancel_requested or current is not operation
                        )
                        output_is_valid = (
                            not cancelled
                            and tmp_tif_path.exists()
                            and tmp_tif_path.stat().st_size > 0
                        )
                        if output_is_valid:
                            try:
                                _safe_replace(tmp_tif_path, tif_path)
                            except OSError as e:
                                replace_error = e
                            else:
                                replace_error = None
                        else:
                            replace_error = None
                    if output_is_valid:
                        if replace_error is not None:
                            e = replace_error
                            msg = f"RawTherapee output was valid but could not replace working TIFF: {e}"
                            log.error(msg)
                            completion = RawDevelopmentResult(
                                operation_id,
                                source_path,
                                tif_path,
                                develop_key,
                                False,
                                msg,
                            )
                        else:
                            log.info("RAW development successful.")
                            completion = RawDevelopmentResult(
                                operation_id,
                                source_path,
                                tif_path,
                                develop_key,
                                True,
                            )
                    elif not cancelled:
                        msg = f"RawTherapee exited successfully but output file is missing or empty.\nCommand: {cmd_str}"
                        log.error(msg)
                        completion = RawDevelopmentResult(
                            operation_id,
                            source_path,
                            tif_path,
                            develop_key,
                            False,
                            msg,
                        )
                    else:
                        err_msg = "RAW development was cancelled."
                        log.error(err_msg)
                        completion = RawDevelopmentResult(
                            operation_id,
                            source_path,
                            tif_path,
                            develop_key,
                            False,
                            err_msg,
                        )
                else:
                    with self._raw_develop_lock:
                        current = self._raw_development_operations.get(develop_key)
                        cancelled = (
                            operation.cancel_requested or current is not operation
                        )
                    stderr = stderr.strip() if stderr else "(no stderr)"
                    stdout = stdout.strip() if stdout else "(no stdout)"
                    if cancelled:
                        err_msg = "RAW development was cancelled."
                    else:
                        err_msg = f"RawTherapee failed (exit code {process.returncode}):\nCommand: {cmd_str}\nstderr: {stderr}\nstdout: {stdout}"
                    log.error(err_msg)
                    completion = RawDevelopmentResult(
                        operation_id,
                        source_path,
                        tif_path,
                        develop_key,
                        False,
                        err_msg,
                    )
            except subprocess.TimeoutExpired:
                err_msg = f"RawTherapee timed out after 60 seconds.\nCommand: {cmd_str}"
                log.error(err_msg)
                completion = RawDevelopmentResult(
                    operation_id,
                    source_path,
                    tif_path,
                    develop_key,
                    False,
                    err_msg,
                )
            except Exception as e:
                err_msg = f"Unexpected error running RawTherapee: {str(e)}"
                log.exception(err_msg)
                completion = RawDevelopmentResult(
                    operation_id,
                    source_path,
                    tif_path,
                    develop_key,
                    False,
                    err_msg,
                )
            finally:
                try:
                    tmp_tif_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._rawDevelopmentFinished.emit(completion)

        thread = threading.Thread(target=worker, daemon=True, name="RawDevelopment")
        operation.worker = thread
        thread.start()
        return True

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

    @Slot(int, bool, int)
    @Slot(int, bool, int, str)
    def note_compact_editor_reload_scheduled(
        self, index: int, coalesced: bool, delay_ms: int, reason: str = "navigation"
    ):
        """Debug hook for compact-editor navigation reload debounce."""
        log.debug(
            "[COMPACT_EDITOR_RELOAD] scheduled preview-only index=%d "
            "reason=%s delay=%dms coalesced=%s",
            index,
            reason,
            delay_ms,
            coalesced,
        )

    @Slot(int, str)
    def note_compact_editor_full_load_required(self, index: int, reason: str):
        """Debug hook for compact-editor actions that need a full editor load."""
        log.debug(
            "[COMPACT_EDITOR_RELOAD] full load required index=%d reason=%s",
            index,
            reason,
        )

    @Slot(int, str)
    def note_compact_editor_reload_skipped(self, index: int, reason: str):
        """Debug hook for compact-editor navigation reload debounce."""
        log.debug(
            "[COMPACT_EDITOR_RELOAD] skipped index=%d reason=%s",
            index,
            reason,
        )

    @Slot(result=bool)
    def load_image_for_editing_preview(self):
        """Prepare a detached editor on a worker; install it on the Qt thread."""
        self._editor_load_token += 1
        token = self._editor_load_token
        index = self.current_index
        if (
            self._shutting_down
            or self.ui_state.isCropping
            or not 0 <= index < len(self.image_files)
        ):
            return False
        active_path = (
            Path(self.view_override_path)
            if self.view_override_path
            else self.get_active_edit_path(index)
        )
        if (
            self.current_edit_source_mode == "raw"
            and active_path.suffix.lower() in RAW_EXTENSIONS
        ):
            self._develop_raw_backend()
            return False
        load_path, _ = self._active_edit_load_path(active_path)
        original_editor = self.image_editor
        revision = original_editor._edits_rev
        editor = ImageEditor()
        editor.preview_master_box = original_editor.preview_master_box
        editor.levels_soft_knee = original_editor.levels_soft_knee
        editor.export_dither = original_editor.export_dither
        source_exif_path = (
            self.image_files[index].path
            if self.current_edit_source_mode == "raw"
            else None
        )
        if self._editor_load_future is not None:
            self._editor_load_future.cancel()

        def prepare():
            started = time.perf_counter()
            source_exif = None
            if source_exif_path is not None and source_exif_path.suffix.lower() not in (
                ".tif",
                ".tiff",
            ):
                try:
                    with Image.open(source_exif_path) as source_image:
                        source_exif = source_image.info.get("exif")
                except OSError:
                    log.warning("Could not read source EXIF for %s", source_exif_path)
            loaded = editor.load_image(
                str(load_path), source_exif=source_exif, preview_only=True
            )
            if loaded:
                # Prepare real pixels here too, so opening crop/edit controls
                # after navigation does not promote buffers on the UI thread.
                editor._ensure_float_image()
            log.debug(
                "[COMPACT_EDITOR_RELOAD] background load index=%d total=%dms",
                index,
                int((time.perf_counter() - started) * 1000),
            )
            return loaded

        def finished(future):
            if future.cancelled():
                return
            try:
                loaded = future.result()
            except Exception:
                log.exception("Background editor load failed for %s", load_path)
                loaded = False
            try:
                self._editorLoadReady.emit(
                    (
                        token,
                        index,
                        active_path,
                        load_path,
                        original_editor,
                        revision,
                        editor,
                        loaded,
                    )
                )
            except RuntimeError:
                pass  # Qt objects already destroyed during shutdown.

        self._editor_load_future = self._editor_load_executor.submit(prepare)
        self._editor_load_future.add_done_callback(finished)
        return True

    @Slot(object)
    def _on_editor_load_ready(self, result):
        (
            token,
            index,
            active_path,
            load_path,
            original_editor,
            revision,
            editor,
            loaded,
        ) = result
        if (
            self._shutting_down
            or token != self._editor_load_token
            or index != self.current_index
        ):
            return
        if not 0 <= index < len(self.image_files):
            return
        current_path = (
            Path(self.view_override_path)
            if self.view_override_path
            else self.get_active_edit_path(index)
        )
        if (
            current_path != active_path
            or self._active_edit_load_path(current_path)[0] != load_path
        ):
            return
        if (
            self.ui_state.isCropping
            or self.image_editor is not original_editor
            or original_editor._edits_rev != revision
        ):
            return
        if editor.preview_master_box != original_editor.preview_master_box:
            self.load_image_for_editing_preview()
            return
        if loaded:
            try:
                if load_path.stat().st_mtime != editor.current_mtime:
                    self.load_image_for_editing_preview()
                    return
            except OSError:
                loaded = False
        installed = loaded and bool(
            self._load_image_for_editing(preview_only=True, prepared_editor=editor)
        )
        self.editorPreviewLoaded.emit(index, installed)

    @Slot(result=bool)
    def load_image_for_editing(self):
        self._editor_load_token += 1
        return self._load_image_for_editing(preview_only=False)

    def _load_image_for_editing(
        self, *, preview_only: bool, prepared_editor: Optional[ImageEditor] = None
    ):
        """Load the currently viewed image into the editor.

        Returns True on real reload, _REUSED when the existing session
        was kept, or False on failure.  The @Slot annotation coerces
        _REUSED to true for QML callers (none of which inspect the value).
        """
        if self.ui_state.isCropping:
            self.update_status_message("Apply or cancel the crop before editing")
            return False
        if not 0 <= self.current_index < len(self.image_files):
            log.debug(
                "Skipping editor load without a valid image: index=%d, count=%d",
                self.current_index,
                len(self.image_files),
            )
            if not preview_only:
                self.update_status_message("No image to edit")
            if self.ui_state:
                self.ui_state.isEditorOpen = False
            return False
        try:
            if self.view_override_path:
                active_path = Path(self.view_override_path)
            else:
                active_path = self.get_active_edit_path(self.current_index)
            if (
                self.current_edit_source_mode == "raw"
                and active_path.suffix.lower() in RAW_EXTENSIONS
            ):
                self._develop_raw_backend()
                return False
            load_path, _edit_state = self._active_edit_load_path(active_path)
            filepath = str(active_path)
            load_filepath = str(load_path)

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

            if match and not preview_only:
                # Also require an intact float buffer. A preview_only load leaves
                # current_filepath/mtime set but float_image=None; promote that
                # session before crop, brush, save, or full-editor flows use it.
                if getattr(self.image_editor, "float_image", None) is None:
                    try:
                        t_promote = time.perf_counter()
                        self.image_editor._ensure_float_image()
                        log.debug(
                            "[COMPACT_EDITOR_RELOAD] full promotion index=%d "
                            "total=%dms  %s",
                            self.current_index,
                            int((time.perf_counter() - t_promote) * 1000),
                            active_path.name,
                        )
                    except RuntimeError:
                        match = False
                    else:
                        if getattr(self.image_editor, "float_image", None) is None:
                            match = False

            if match:
                log.debug(
                    "load_image_for_editing: Reusing existing session for %s (preview_only=%s)",
                    filepath,
                    preview_only,
                )
                self._ensure_live_edit_session_state()
                # Ensure the background renderer is current and notify UI to refresh
                # Also synchronize sliders/crop state to the backend session.
                self._sync_editor_state_from_session()
                return self._REUSED

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
            if prepared_editor is not None or self.image_editor.load_image(
                load_filepath,
                cached_preview=None,
                source_exif=source_exif,
                preview_only=preview_only,
            ):
                if prepared_editor is not None:
                    self.image_editor = prepared_editor
                log.debug(
                    "load_image_for_editing: loaded %s for %s (preview_only=%s)",
                    load_filepath,
                    filepath,
                    preview_only,
                )
                self._clear_active_auto_adjust_state(
                    "editor session reloaded",
                    clear_editor=False,
                )
                self._bind_loaded_editor_output_path(active_path)
                restored = self._restore_pending_edit_state_for_loaded_path(active_path)
                self._ensure_live_edit_session_state(
                    force_reset=True,
                    restored_has_meaningful_edits=(
                        restored and self._current_live_session_has_meaningful_edits()
                    ),
                )
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
        # Covers directory switches and first loads that skip navigation;
        # a no-op when the inputs for this image are already known.
        self._refresh_stack_inputs()
        self._ensure_stack_sources_for_edits()
        self._schedule_halo_jobs()
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

        # Kick off a background render only when the session holds unsaved
        # edits that the decoded file can't show. After a navigation reload
        # the session merely replays the already-saved (or save-submitted)
        # state, and publishing a preview-resolution render here would
        # replace the sharp full-resolution decode in the loupe ~250ms after
        # the image appears: a slight shift (crop rounded at preview vs full
        # resolution), visible blur, and a sourceSize change that breaks the
        # zoom percentage and Ctrl+1 (1:1 of preview pixels instead of
        # native pixels).
        if self._is_current_live_edit_session_dirty():
            self._kick_preview_worker()
        # Notify UI
        self.ui_state.editorImageChanged.emit()

    @Slot(object)
    def _on_develop_finished(self, result: RawDevelopmentResult) -> None:
        """Apply a RAW completion on the controller's Qt thread."""
        if self._shutting_down:
            self._mark_raw_development_finished(result.develop_key, result.operation_id)
            return

        with self._raw_develop_lock:
            operation = getattr(self, "_raw_development_operations", {}).get(
                result.develop_key
            )
            result_is_current = (
                operation is not None and operation.operation_id == result.operation_id
            )
        if not result_is_current:
            log.info("Ignoring stale RAW development result %s", result.operation_id)
            return

        self._mark_raw_development_finished(result.develop_key, result.operation_id)
        if result.success:
            self.update_status_message("RAW Development complete.")
            if (
                self.image_files
                and 0 <= self.current_index < len(self.image_files)
                and self._key(result.source_path)
                == self._key(self.image_files[self.current_index].path)
                and result.develop_key
                == self._raw_development_key_for_image(
                    self.image_files[self.current_index]
                )
            ):
                self.load_image_for_editing()
        else:
            self.update_status_message(f"Development failed: {result.error}")

    @Slot(result=DecodedImage)
    def get_preview_data(self) -> Optional[DecodedImage]:
        """Gets the preview data of the currently edited image as a DecodedImage."""
        return self.image_editor.get_preview_data()

    @Slot(bool)
    def set_editor_slider_drag_active(self, active: bool):
        """Tell the preview scheduler when a compact-editor slider is pressed."""
        self._editor_slider_drag_active = bool(active)
        if not active:
            # The release leaves a deliberately degraded drag frame on screen
            # and no further kick is coming, so pull the armed refinement in to
            # the discrete-edit delay instead of the drag one.
            if self._hq_preview_timer.isActive():
                self._hq_preview_timer.start(_HQ_PREVIEW_DEBOUNCE_SETTLED_MS)
            return
        # A press can precede the first valueChanged signal. Cancel obsolete
        # refinement immediately so it is not competing with the first drag
        # frame when that signal arrives.
        with self._preview_lock:
            cancel_event = self._preview_refinement_cancel
        if cancel_event is not None:
            cancel_event.set()

    @Slot(str, "QVariant")
    def set_edit_parameter(self, key: str, value: Any):
        """Sets an edit parameter and updates the UIState for the slider visual."""
        if self.ui_state.isCropping:
            self.update_status_message("Apply or cancel the crop before editing")
            return

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

            if changed and key == "rotation":
                crop_box = self._normalize_crop_box_tuple(
                    self.image_editor.get_edit_value("crop_box")
                )
                if crop_box is not None:
                    self._set_crop_overlay_box_only(crop_box)

            # Trigger a refresh of the image to show the edit, ONLY if something changed
            # Uses gate pattern: runs immediately if not inflight, else queues for next
            if changed:
                self._clear_active_auto_adjust_state(
                    f"manual edit changed '{key}'",
                    clear_editor=False,
                )
                self._kick_preview_worker()

                # Auto-add to batch if enabled and this is the first edit
                if self.image_editor.current_filepath:
                    self._auto_add_edited_to_batch_if_enabled(
                        Path(self.image_editor.current_filepath)
                    )
        except RuntimeError as e:
            self.update_status_message(str(e), timeout=10000)
            log.warning("Cannot change edit parameter %s: %s", key, e)
        except Exception as e:
            log.error("Error setting edit parameter %s=%s: %s", key, value, e)

    @Slot(int, int, int, int)
    def set_crop_box(self, left: int, top: int, right: int, bottom: int):
        """Sets the normalized crop box (0-1000) in the editor."""
        crop_box: Tuple[int, int, int, int] = (left, top, right, bottom)
        self.image_editor.set_crop_box(crop_box)
        self.ui_state.currentCropBox = crop_box  # Update QML visual (if implemented)

    def _reset_edit_parameters(self, *, allow_during_crop: bool = False) -> bool:
        """Reset all editing parameters, optionally as a confirmed discard."""
        if self.ui_state.isCropping and not allow_during_crop:
            self.update_status_message(
                "Apply or cancel the crop before resetting edits"
            )
            return False

        if self.ui_state.isCropping:
            # Discard resets the whole edit session, so do not restore the crop
            # snapshot as cancel_crop_mode() would.  Clearing isCropping emits the
            # QML cleanup path that releases frozen crop interaction state.
            self.ui_state.isCropRotating = False
            self.ui_state.isCropping = False
            self._clear_crop_mode_snapshot()

        self.image_editor.reset_edits()
        self._clear_active_auto_adjust_state(
            "editor parameters reset",
            clear_editor=False,
        )
        if hasattr(self.ui_state, "reset_editor_state"):
            self.ui_state.reset_editor_state()
        # Reset clears the dodge & burn layers too (reset_edits dropped them),
        # and the brush recipe so the local sliders read zero as well.
        self._local_brush_recipe = {}
        self._reset_local_brush_session_state()
        self._refresh_local_brush_ui()
        self._update_local_overlay()

        self.update_status_message("Edits reset")

        # Trigger a refresh to show the reset image
        self.ui_refresh_generation += 1
        self._kick_preview_worker()

        if self.ui_state.isHistogramVisible:
            self.update_histogram()

        return True

    @Slot()
    def reset_edit_parameters(self):
        """Resets all editing parameters in the editor."""
        self._reset_edit_parameters()

    @Slot()
    def discard_edit_parameters(self):
        """Discard edits even if a transient crop transaction is active."""
        if self._reset_edit_parameters(allow_during_crop=True):
            self._mark_current_live_edit_session_clean()

    # ---- Dodge & Burn Brush (local adjustment layers) ----
    #
    # Each layer is a recipe of compact-editor slider deltas (kept in
    # current_edits["local_layers"], replaced wholesale on every change) plus
    # a stroke mask (MaskData in image_editor._mask_assets[layer_id]).
    #
    # While no layer is selected for editing, the sliders edit the *brush
    # recipe*. A paint stroke goes onto the last-painted layer if its recipe
    # still matches the brush recipe, otherwise onto a new layer — so changing
    # the recipe never restyles strokes already painted. Selecting a layer
    # makes the sliders edit that layer's recipe live, and strokes go onto it.

    _LOCAL_OVERLAY_LONG_EDGE = 1024
    _LOCAL_OVERLAY_RGB = (255, 96, 48)
    _BRUSH_RADIUS_MIN = 0.002
    _BRUSH_RADIUS_MAX = 0.25

    def _local_layers(self) -> list[dict]:
        return list(self.image_editor.current_edits.get("local_layers") or [])

    def _local_layer(self, layer_id: Optional[str]) -> Optional[dict]:
        if not layer_id:
            return None
        for layer in self._local_layers():
            if layer["id"] == layer_id:
                return layer
        return None

    def _reset_local_brush_session_state(self) -> None:
        """Forget per-image brush state (selection, undo history, live stroke)."""
        self._local_active_layer_id = None
        self._local_editing_layer_id = None
        self._local_painting_layer_id = None
        self._local_painting_layer_ids = ()
        self._local_stroke_history = []
        self._local_overlay_cache.clear()

    def _reset_local_brush_on_navigation(self):
        """Clear the previous image's layers before the next image loads.

        Called from _set_current_index() after the live session was flushed.
        Navigation while the editor is open does not reload the editor right
        away, so the old image's layers must not linger on the new one. Brush
        mode is turned off and the brush recipe reset: a new photo that needs
        dodge & burn will need a different recipe.
        """
        if self.ui_state.isBrushMode:
            self.exit_local_brush_mode()
        self._local_brush_recipe = {}
        editor = self.image_editor
        if (
            self._local_active_layer_id is None
            and self._local_editing_layer_id is None
            and self._local_painting_layer_id is None
            and not self._local_stroke_history
            and not editor._mask_assets
            and not editor.current_edits.get("local_layers")
        ):
            return
        editor._mask_assets.clear()
        editor._local_density_cache.clear()
        editor._stack_sources = {}
        editor._stack_sources_session_id = None
        editor._halo_results = {}
        self._halo_reset_for_navigation()
        if editor.current_edits.get("local_layers"):
            editor.set_local_layers([])
        self._reset_local_brush_session_state()
        self._refresh_local_brush_ui()

    def _prepare_local_brush_image_state(self) -> bool:
        """Ensure the current image is fully loaded for editing."""
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
                    needs_load = Path(current_p).resolve() != Path(active).resolve()
                else:
                    needs_load = True
            except (IndexError, TypeError, OSError, ValueError):
                needs_load = True

        if needs_load:
            load_result = self.load_image_for_editing()
            if load_result is False:
                return False
            if load_result is True:
                # A real reload restored this image's saved layers; only the
                # selection/undo state from the previous session is stale.
                self._reset_local_brush_session_state()
        return True

    def _enter_brush_mode(self, tool: str) -> bool:
        """Enter brush mode with *tool* ("local" = dodge & burn, "stack")."""
        if self.ui_state.isCropping:
            self.update_status_message("Apply or cancel the crop before brushing")
            return False
        if self._is_grid_view_active or not self.image_files:
            return False
        if not self.ui_state.isEditorOpen or self.ui_state.isEditorExpanded:
            self.ui_state.isEditorExpanded = False
            self.ui_state.isEditorOpen = True
        if not self._prepare_local_brush_image_state():
            return False
        if self._local_painting_layer_id is not None:
            self.finish_local_stroke()
        self.ui_state.brushTool = tool
        self.ui_state.isBrushMode = True
        self._refresh_local_brush_ui()
        self._update_local_overlay()
        return True

    @Slot()
    def toggle_local_brush_mode(self):
        """Shift+D: enter or leave dodge & burn brush mode."""
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "local":
            self.exit_local_brush_mode()
            return
        if self._enter_brush_mode("local"):
            self.update_status_message(
                "Dodge & burn: set sliders, then paint. Right/Alt-drag erases."
            )

    @Slot()
    def toggle_stack_brush_mode(self):
        """Enter or leave stack paint (paint a stack input over the stack)."""
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "stack":
            self.exit_local_brush_mode()
            return
        if not self._stack_input_paths:
            self.update_status_message(
                "Stack paint needs an in-camera stacked JPG with its input JPGs"
            )
            return
        if not self._enter_brush_mode("stack"):
            return
        self._schedule_stack_hover()
        name = self.ui_state.stackInputName
        if name == STACK_OUTPUT_SOURCE or name in self.image_editor._stack_sources:
            self.update_status_message(
                f"Stack paint: paint to reveal {stack_input_label(name)}. "
                "Right/Alt-drag restores the stack."
            )
        else:
            self._request_stack_source(name)

    @Slot()
    def exit_local_brush_mode(self):
        if self._local_painting_layer_id is not None:
            self.finish_local_stroke()
        self.ui_state.brushPanHeld = False
        self.ui_state.localOverlayHeld = False
        self._clear_stack_hover()
        if self.ui_state.isBrushMode:
            self.ui_state.isBrushMode = False
            self._update_local_overlay()

    def _commit_local_layers(self, layers: list[dict], reason: str) -> None:
        self.image_editor.set_local_layers(layers)
        self._after_local_edit(reason)

    def _after_local_edit(self, reason: str) -> None:
        self._clear_active_auto_adjust_state(reason, clear_editor=False)
        self._kick_preview_worker()
        # Halo layers whose strokes (or the stack paint under them) changed
        # get a fresh correction; before the refresh, so the layer list
        # shows the new job.
        self._schedule_halo_jobs(refresh_ui=False)
        self._refresh_local_brush_ui()

    @Slot(str, float)
    def set_local_recipe_param(self, key: str, value: float):
        """Slider change in brush mode: edits the selected layer or the brush."""
        if key not in LOCAL_ADJUST_KEYS:
            return
        editing = self._local_layer(self._local_editing_layer_id)
        if editing is None:
            recipe = dict(self._local_brush_recipe)
            recipe[key] = float(value)
            self._local_brush_recipe = normalize_recipe(recipe)
            self._refresh_local_brush_ui()
            return
        recipe = dict(editing["recipe"])
        recipe[key] = float(value)
        recipe = normalize_recipe(recipe)
        if recipe == editing["recipe"]:
            return
        layers = [
            {**layer, "recipe": recipe} if layer["id"] == editing["id"] else layer
            for layer in self._local_layers()
        ]
        self._commit_local_layers(layers, f"local layer recipe changed '{key}'")

    @Slot(str)
    def select_local_layer(self, layer_id: str):
        """Select a layer for editing; selecting it again goes back to the brush.

        An empty id also returns to the brush recipe.
        """
        layer = self._local_layer(layer_id)
        if is_stack_layer(layer) or is_halo_layer(layer):
            return  # stack and halo layers have no recipe to edit
        if not layer_id or layer_id == self._local_editing_layer_id:
            previous = self._local_layer(self._local_editing_layer_id)
            self._local_editing_layer_id = None
            if previous is not None:
                # Keep painting the layer just edited unless the recipe moves.
                self._local_brush_recipe = dict(previous["recipe"])
                self._local_active_layer_id = previous["id"]
        elif self._local_layer(layer_id) is not None:
            self._local_editing_layer_id = layer_id
            self._local_active_layer_id = layer_id
        self._refresh_local_brush_ui()
        self._update_local_overlay()

    @Slot(str, bool)
    def set_local_layer_visible(self, layer_id: str, visible: bool):
        layers = self._local_layers()
        if not any(layer["id"] == layer_id for layer in layers):
            return
        layers = [
            {**layer, "visible": bool(visible)} if layer["id"] == layer_id else layer
            for layer in layers
        ]
        self._commit_local_layers(layers, "local layer visibility changed")

    @Slot(str)
    def delete_local_layer(self, layer_id: str):
        layers = self._local_layers()
        if not any(layer["id"] == layer_id for layer in layers):
            return
        if layer_id in self._local_painting_layer_ids:
            self._local_painting_layer_ids = tuple(
                lid for lid in self._local_painting_layer_ids if lid != layer_id
            )
            self._local_painting_layer_id = (
                self._local_painting_layer_ids[0]
                if self._local_painting_layer_ids
                else None
            )
        self.image_editor._mask_assets.pop(layer_id, None)
        history = []
        for entry in self._local_stroke_history:
            if isinstance(entry, tuple):
                entry = tuple(lid for lid in entry if lid != layer_id)
                if entry:
                    history.append(entry)
            elif entry != layer_id:
                history.append(entry)
        self._local_stroke_history = history
        if self._local_editing_layer_id == layer_id:
            self._local_editing_layer_id = None
        if self._local_active_layer_id == layer_id:
            self._local_active_layer_id = None
        self._commit_local_layers(
            [layer for layer in layers if layer["id"] != layer_id],
            "local layer deleted",
        )
        self._update_local_overlay()

    def _new_local_layer_id(self) -> str:
        used = {layer["id"] for layer in self._local_layers()}
        used.update(self.image_editor._mask_assets)
        n = 1
        while f"L{n}" in used:
            n += 1
        return f"L{n}"

    def _local_stroke_target(self, erase: bool) -> Optional[str]:
        """Layer the next stroke goes onto, creating one for a new recipe."""
        if self._local_layer(self._local_editing_layer_id) is not None:
            return self._local_editing_layer_id
        active = self._local_layer(self._local_active_layer_id)
        if erase:
            if active is None:
                self.update_status_message("Nothing to erase on this layer")
            return active["id"] if active is not None else None
        if active is not None and active["recipe"] == self._local_brush_recipe:
            return active["id"]
        if not self._local_brush_recipe:
            self.update_status_message(
                "Move a slider first to choose what the brush does"
            )
            return None
        layer_id = self._new_local_layer_id()
        self.image_editor._mask_assets[layer_id] = MaskData()
        self.image_editor.set_local_layers(
            [
                *self._local_layers(),
                {
                    "id": layer_id,
                    "visible": True,
                    "recipe": dict(self._local_brush_recipe),
                },
            ]
        )
        self._local_active_layer_id = layer_id
        return layer_id

    def _stack_stroke_targets(self, erase: bool) -> list[str]:
        """Stack layers the next stack paint stroke goes onto.

        Painting goes onto the top stack layer when it shows the selected
        input, otherwise onto a new layer on top (so it covers what is below).
        Erasing restores the stack: it goes onto every visible stack layer.
        """
        assets = self.image_editor._mask_assets
        layers = [layer for layer in self._local_layers() if is_stack_layer(layer)]
        if erase:
            ids = [
                layer["id"]
                for layer in layers
                if layer.get("visible", True)
                and assets.get(layer["id"]) is not None
                and assets[layer["id"]].has_strokes()
            ]
            if not ids:
                self.update_status_message("No stack paint to erase")
            return ids
        name = self.ui_state.stackInputName
        if not name:
            self.update_status_message("Choose a stack input to paint from")
            return []
        if name not in self.image_editor._stack_sources:
            # Strokes are kept; the pixels appear once the input is aligned.
            self._request_stack_source(name)
        top = layers[-1] if layers else None
        if top is not None and top["source"] == name and top.get("visible", True):
            return [top["id"]]
        layer_id = self._new_local_layer_id()
        assets[layer_id] = MaskData()
        self.image_editor.set_local_layers(
            [
                *self._local_layers(),
                {"id": layer_id, "visible": True, "recipe": {}, "source": name},
            ]
        )
        return [layer_id]

    @Slot(float, float, bool)
    def start_local_stroke(self, x_norm: float, y_norm: float, erase: bool):
        """Begin a stroke. Coords are normalised to the displayed image."""
        if not self.ui_state.isBrushMode or self.ui_state.isCropping:
            return
        if not self._prepare_local_brush_image_state():
            return
        if self.ui_state.brushTool == "stack":
            layer_ids = self._stack_stroke_targets(bool(erase))
        elif self.ui_state.brushTool == "halo":
            layer_ids = self._halo_stroke_targets(bool(erase))
        else:
            layer_id = self._local_stroke_target(bool(erase))
            layer_ids = [layer_id] if layer_id is not None else []
        if not layer_ids:
            return
        point = self._brush_point(x_norm, y_norm)
        stroke = MaskStroke(
            points=[point],
            radius=float(self.ui_state.brushRadius),
            hardness=float(self.ui_state.brushHardness),
            mode=STROKE_ERASE if erase else STROKE_PAINT,
        )
        for layer_id in layer_ids:
            mask = self.image_editor._mask_assets.setdefault(layer_id, MaskData())
            mask.live_stroke = stroke
        self._local_painting_layer_id = layer_ids[0]
        self._local_painting_layer_ids = tuple(layer_ids)
        self.image_editor.touch_local_masks()
        self._kick_preview_worker()
        self._update_local_overlay()

    def _stack_geometry(self):
        """(base_shape, A, out_shape) for exact brush geometry, or None."""
        base_shape = self.image_editor.geometry_base_shape()
        if (
            not isinstance(base_shape, (tuple, list))
            or len(base_shape) != 2
            or any(not isinstance(v, (int, np.integer)) or v <= 0 for v in base_shape)
        ):
            return None
        base_shape = (int(base_shape[0]), int(base_shape[1]))
        a, out_shape = geometry_affine(self.image_editor.current_edits, base_shape)
        return base_shape, a, out_shape

    def _brush_point(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        """Displayed-image [0,1] coords -> oriented base image [0,1] coords.

        Both brush renderers use the renderer's exact geometry affine, so
        capture strokes through that affine's exact inverse too.
        """
        edits = self.image_editor.current_edits
        geometry = self._stack_geometry()
        if geometry is not None:
            base_shape, _affine, _out_shape = geometry
            inv = np.linalg.inv(geometry_normalized_affine(edits, base_shape))
            u, v, _ = inv @ np.array([x_norm, y_norm, 1.0])
            return float(u), float(v)
        return inverse_transform(x_norm, y_norm, edits, (1, 1))

    def _painting_masks(self) -> list[MaskData]:
        assets = self.image_editor._mask_assets
        return [
            assets[lid]
            for lid in self._local_painting_layer_ids
            if assets.get(lid) is not None and assets[lid].live_stroke is not None
        ]

    @Slot(float, float)
    def continue_local_stroke(self, x_norm: float, y_norm: float):
        masks = self._painting_masks()
        if not masks:
            return
        live = masks[0].live_stroke
        point = self._brush_point(x_norm, y_norm)
        last = live.points[-1]
        if abs(point[0] - last[0]) + abs(point[1] - last[1]) < live.radius * 0.05:
            return
        # A new object each move: the render worker may hold the old one.
        stroke = dataclass_replace(live, points=[*live.points, point])
        for mask in masks:
            mask.live_stroke = stroke
        self.image_editor.touch_local_masks()
        self._kick_preview_worker()
        self._update_local_overlay()

    @Slot()
    def finish_local_stroke(self):
        assets = self.image_editor._mask_assets
        layer_ids = [
            lid
            for lid in self._local_painting_layer_ids
            if assets.get(lid) is not None and assets[lid].live_stroke is not None
        ]
        self._local_painting_layer_id = None
        self._local_painting_layer_ids = ()
        if not layer_ids:
            return
        for layer_id in layer_ids:
            mask = assets[layer_id]
            mask.add_stroke(mask.live_stroke)
            mask.live_stroke = None
        self._local_stroke_history.append(
            layer_ids[0] if len(layer_ids) == 1 else tuple(layer_ids)
        )
        self.image_editor.touch_local_masks()
        self._after_local_edit("local brush stroke committed")
        self._update_local_overlay()
        if self.image_editor.current_filepath:
            self._auto_add_edited_to_batch_if_enabled(
                Path(self.image_editor.current_filepath)
            )

    @Slot()
    def undo_local_stroke(self):
        """Ctrl+Z in brush mode: remove the most recent stroke."""
        assets = self.image_editor._mask_assets
        while self._local_stroke_history:
            entry = self._local_stroke_history.pop()
            ids = entry if isinstance(entry, tuple) else (entry,)
            targets = [
                lid
                for lid in ids
                if assets.get(lid) is not None and assets[lid].has_strokes()
            ]
            if targets:
                break
        else:
            # Strokes restored from a saved edit have no history: fall back to
            # the newest layer that has any.
            layer_id = next(
                (
                    layer["id"]
                    for layer in reversed(self._local_layers())
                    if assets.get(layer["id"]) and assets[layer["id"]].has_strokes()
                ),
                None,
            )
            if layer_id is None:
                self.update_status_message("No brush strokes to undo")
                return
            targets = [layer_id]
        emptied = []
        for layer_id in targets:
            assets[layer_id].undo_last_stroke()
            if not assets[layer_id].has_strokes():
                emptied.append(layer_id)
        # An emptied layer is just clutter in the list.
        for layer_id in emptied:
            self.delete_local_layer(layer_id)
        if len(emptied) < len(targets):
            self.image_editor.touch_local_masks()
            self._after_local_edit("local brush stroke undone")
            self._update_local_overlay()

    @Slot(int)
    def adjust_brush_size(self, direction: int):
        factor = 1.2 if direction > 0 else 1 / 1.2
        self.set_brush_radius(self.ui_state.brushRadius * factor)

    @Slot(float)
    def set_brush_radius(self, radius: float):
        self.ui_state.brushRadius = min(
            self._BRUSH_RADIUS_MAX, max(self._BRUSH_RADIUS_MIN, float(radius))
        )

    @Slot(float)
    def set_brush_hardness(self, hardness: float):
        self.ui_state.brushHardness = min(1.0, max(0.0, float(hardness)))

    @Slot()
    def toggle_local_overlay(self):
        self.ui_state.localOverlayVisible = not self.ui_state.localOverlayVisible
        self._update_local_overlay()

    def _set_local_overlay_held(self, held: bool) -> None:
        if self.ui_state.localOverlayHeld != held:
            self.ui_state.localOverlayHeld = held
            self._update_local_overlay()

    @staticmethod
    def _local_recipe_summary(recipe: dict) -> str:
        names = {
            "exposure": ("Exp", 2.0),
            "brightness": ("Bri", 1.0),
            "contrast": ("Con", 0.5),
            "whites": ("Wht", 2.0),
            "highlights": ("Hi", 1.0),
            "shadows": ("Shd", 1.0),
            "blacks": ("Blk", 1.0),
            "white_balance_by": ("Temp", 1.0),
            "white_balance_mg": ("Tint", 1.0),
            "vibrance": ("Vib", 1.0),
        }
        parts = []
        for key in LOCAL_ADJUST_KEYS:
            if key not in recipe:
                continue
            label, scale = names.get(key, (key.removeprefix("color_sat_").title(), 1.0))
            parts.append(f"{label} {recipe[key] / scale * 100:+.0f}")
        return " · ".join(parts) if parts else "no effect"

    def _refresh_local_brush_ui(self) -> None:
        """Push layer list, slider recipe and crop scale to QML."""
        ui = self.ui_state
        editing = self._local_layer(self._local_editing_layer_id)
        if editing is None:
            self._local_editing_layer_id = None
        rows = []
        for layer in self._local_layers():
            mask = self.image_editor._mask_assets.get(layer["id"])
            stack = is_stack_layer(layer)
            halo = is_halo_layer(layer)
            rows.append(
                {
                    "id": layer["id"],
                    "kind": "stack" if stack else ("halo" if halo else "recipe"),
                    "source": layer.get("source", ""),
                    "name": f"Layer {layer['id'].removeprefix('L')}",
                    "visible": bool(layer.get("visible", True)),
                    "summary": (
                        stack_input_label(layer["source"])
                        if stack
                        else (
                            self._halo_layer_summary(layer)
                            if halo
                            else self._local_recipe_summary(layer["recipe"])
                        )
                    ),
                    "strokes": len(mask.strokes) if mask is not None else 0,
                    "active": not stack
                    and not halo
                    and layer["id"] == self._local_active_layer_id,
                    "editing": editing is not None and layer["id"] == editing["id"],
                }
            )
        ui.localLayers = rows
        ui.localEditingLayerId = editing["id"] if editing is not None else ""
        recipe = editing["recipe"] if editing is not None else self._local_brush_recipe
        ui.localRecipe = dict(recipe)
        ui.localBrushSummary = self._local_recipe_summary(self._local_brush_recipe)

        crop_box = self.image_editor.current_edits.get("crop_box")
        cw = ch = 1.0
        if crop_box is not None and len(crop_box) == 4:
            cw = max(1e-3, (crop_box[2] - crop_box[0]) / 1000)
            ch = max(1e-3, (crop_box[3] - crop_box[1]) / 1000)
        geometry = self._stack_geometry()
        if geometry is not None:
            # Exact displayed fraction of the (rotated) base image, including
            # the straighten autocrop, so the ring matches the painted size.
            (bh, bw), _a, (oh, ow) = geometry
            if (int(self.image_editor.current_edits.get("rotation", 0)) // 90) % 2:
                bh, bw = bw, bh
            cw, ch = max(1e-3, ow / bw), max(1e-3, oh / bh)
        ui.brushCropScale = [cw, ch]

    def _local_overlay_shape(self) -> Optional[Tuple[int, int]]:
        geometry = self._stack_geometry()
        if geometry is None:
            return None
        _base_shape, _affine, (h, w) = geometry
        scale = self._LOCAL_OVERLAY_LONG_EDGE / max(h, w, 1)
        return max(1, round(h * scale)), max(1, round(w * scale))

    def _update_stack_overlay(self, layers: Optional[list] = None) -> None:
        """Mask overlay for stack paint: every visible stack layer, flat.

        Rasterised in base image space and warped through the renderer's
        exact geometry, so it lines up with the painted pixels. Stack paint
        is opaque after one coat, so the overlay does not build up. The halo
        brush shows its layers the same way (*layers*).
        """
        ui = self.ui_state
        assets = self.image_editor._mask_assets
        if layers is None:
            layers = stack_layers(self.image_editor.current_edits)
        masks = [
            (layer["id"], assets[layer["id"]])
            for layer in layers
            if assets.get(layer["id"]) is not None
        ]
        geometry = self._stack_geometry()
        cv2 = get_cv2()
        if not masks or geometry is None or cv2 is None:
            self._clear_local_overlay()
            return
        try:
            from PySide6.QtGui import QImage

            (bh, bw), a, (out_h, out_w) = geometry
            scale = self._LOCAL_OVERLAY_LONG_EDGE / max(out_h, out_w, 1)
            oh, ow = max(1, round(out_h * scale)), max(1, round(out_w * scale))
            # Base raster at about the overlay's pixel density.
            bs = min(1.0, scale)
            rh, rw = max(1, round(bh * bs)), max(1, round(bw * bs))
            density = np.zeros((rh, rw), np.float32)
            for layer_id, mask in masks:
                d = rasterize_layer_density(
                    layer_id,
                    mask,
                    (rh, rw),
                    BASE_SPACE_EDITS,
                    cache=self._local_overlay_cache,
                )
                if d is not None:
                    region = density[d.y0 : d.y1, d.x0 : d.x1]
                    np.maximum(region, np.minimum(d.arr, 1.0), out=region)
            # raster px -> base px -> displayed px -> overlay px, index coords.
            m = (
                np.array([[1.0, 0, -0.5], [0, 1.0, -0.5], [0, 0, 1.0]])
                @ np.diag([ow / out_w, oh / out_h, 1.0])
                @ a
                @ np.diag([bw / rw, bh / rh, 1.0])
                @ np.array([[1.0, 0, 0.5], [0, 1.0, 0.5], [0, 0, 1.0]])
            )
            warped = cv2.warpAffine(
                density, m[:2], (ow, oh), flags=cv2.INTER_LINEAR, borderValue=0
            )
            overlay = np.zeros((oh, ow, 4), dtype=np.uint8)
            r, g, b = self._LOCAL_OVERLAY_RGB
            overlay[..., 0] = b  # ARGB32 is BGRA in memory on little-endian
            overlay[..., 1] = g
            overlay[..., 2] = r
            alpha = np.clip(warped, 0.0, 1.0) * (0.45 * 255)
            overlay[..., 3] = alpha.astype(np.uint8)
            buf = overlay.tobytes()
            # QImage wraps buf without copying; keep it alive with the image.
            self._local_overlay_buffer = buf
            ui._local_overlay_image = QImage(
                buf, ow, oh, ow * 4, QImage.Format.Format_ARGB32
            )
            ui.bump_local_overlay_generation()
        except Exception:
            log.exception("Failed to update stack paint overlay")

    def _clear_local_overlay(self) -> None:
        if self.ui_state._local_overlay_image is None:
            return
        self.ui_state._local_overlay_image = None
        self._local_overlay_buffer = None
        self.ui_state.bump_local_overlay_generation()

    def _update_local_overlay(self) -> None:
        """Rebuild the selected/last-painted layer's density overlay.

        Rasterises on the GUI thread at a small fixed size (committed strokes
        are cached, the live stroke is cheap), so the overlay tracks the brush
        without waiting for a preview render.
        """
        ui = self.ui_state
        if not ui.isBrushMode or not (ui.localOverlayVisible or ui.localOverlayHeld):
            self._clear_local_overlay()
            return
        assets = self.image_editor._mask_assets
        if ui.brushTool == "stack":
            self._update_stack_overlay()
            return
        if ui.brushTool == "halo":
            self._update_stack_overlay(halo_layers(self.image_editor.current_edits))
            return
        layer_ids = [self._local_editing_layer_id or self._local_active_layer_id]
        masks = [
            (lid, assets[lid])
            for lid in layer_ids
            if lid and assets.get(lid) is not None
        ]
        shape = self._local_overlay_shape()
        if not masks or shape is None:
            self._clear_local_overlay()
            return
        try:
            from PySide6.QtGui import QImage

            h, w = shape
            overlay = np.zeros((h, w, 4), dtype=np.uint8)
            r, g, b = self._LOCAL_OVERLAY_RGB
            base_shape = self.image_editor.geometry_base_shape()
            point_transform = None
            if base_shape is not None:
                point_transform = np.diag([w, h, 1.0]) @ geometry_normalized_affine(
                    self.image_editor.current_edits, base_shape
                )
            for layer_id, mask in masks:
                density = rasterize_layer_density(
                    layer_id,
                    mask,
                    shape,
                    self.image_editor.current_edits,
                    cache=self._local_overlay_cache,
                    base_shape=base_shape,
                    point_transform=point_transform,
                )
                if density is None:
                    continue
                # One coat shows at 35% opacity; more coats build to 80%.
                alpha = np.clip(density.arr * (0.35 * 255), 0, 0.8 * 255)
                region = overlay[density.y0 : density.y1, density.x0 : density.x1]
                region[..., 0] = b  # ARGB32 is BGRA in memory on little-endian
                region[..., 1] = g
                region[..., 2] = r
                np.maximum(region[..., 3], alpha.astype(np.uint8), out=region[..., 3])
            buf = overlay.tobytes()
            # QImage wraps buf without copying; keep it alive with the image.
            self._local_overlay_buffer = buf
            ui._local_overlay_image = QImage(
                buf, w, h, w * 4, QImage.Format.Format_ARGB32
            )
            ui.bump_local_overlay_generation()
        except Exception:
            log.exception("Failed to update dodge & burn overlay")

    # ---- Stack paint inputs ----
    #
    # An in-camera focus stack ("<prefix><number> stacked.JPG") is preceded by
    # its input frames, numbered just below it, saved as JPG (+RAW) in the
    # stack-input directories that Restack searches, or next to the stack.
    # The camera steps focus from near to far, so the highest-numbered frame
    # is focused furthest back.

    _STACKED_JPG_RE = re.compile(r"([A-Z]+)(\d+)\s+stacked\.jpe?g$", re.IGNORECASE)

    def _find_stack_input_jpgs(self, image_path: Path) -> list[Path]:
        """Input JPGs of an in-camera stacked JPG, near to far focus.

        Pure filesystem work; safe to run on a worker thread.
        """
        match = self._STACKED_JPG_RE.search(image_path.name)
        if not match:
            return []
        prefix, number = match.group(1), int(match.group(2))
        search_dirs: list[Path] = []
        main_dir = self._main_photo_directory()
        if main_dir is not None:
            try:
                relative = image_path.parent.resolve().relative_to(main_dir)
            except (OSError, ValueError):
                relative = None
            if relative is not None:
                search_dirs.extend(
                    base / relative
                    for _label, base in self._configured_restack_raw_locations()
                )
        search_dirs.append(image_path.parent)
        for directory in search_dirs:
            try:
                if not directory.is_dir():
                    continue
                found = self._find_restack_raws_in_dir(
                    directory, prefix, number, extensions=JPG_EXTENSIONS
                )
            except OSError as exc:
                log.debug("Stack input search failed in %s: %s", directory, exc)
                continue
            if found:
                return list(reversed(found))
        return []

    def _stack_source_resolver_for(self, image_path: Path):
        """Name -> input path lookup for the save worker (thread-safe)."""
        known = (
            dict(self._stack_input_paths)
            if self._stack_inputs_key == self._key(image_path)
            else {}
        )

        def resolve(name: str) -> Optional[Path]:
            if name not in known:
                known.update(
                    {p.name: p for p in self._find_stack_input_jpgs(image_path)}
                )
            return known.get(name)

        return resolve

    def _refresh_stack_inputs(self) -> None:
        """Find the current image's stack inputs in the background (once)."""
        path = self._current_image_path()
        try:
            key = self._key(path) if path is not None else None
        except (OSError, TypeError, ValueError):
            key = None
        if key == self._stack_inputs_key:
            return
        self._stack_inputs_token += 1
        token = self._stack_inputs_token
        self._stack_inputs_key = key
        self._stack_input_paths = {}
        self._stack_source_loading = set()
        if self.ui_state is not None:
            self.ui_state.stackInputs = []
            self.ui_state.stackInputName = ""
            if self.ui_state.isBrushMode and self.ui_state.brushTool == "stack":
                self.exit_local_brush_mode()
        if path is None or not self._STACKED_JPG_RE.search(path.name):
            return

        def _done(future: concurrent.futures.Future) -> None:
            try:
                result = future.result()
            except Exception:  # pylint: disable=broad-exception-caught
                log.exception("Stack input search failed for %s", path)
                result = []
            self._stackInputsReady.emit({"token": token, "paths": result})

        def _search() -> list[Path]:
            # Held-arrow navigation queues one search per step on this single
            # worker. A search whose image is no longer current is skipped
            # when its turn comes, so the directory scans for images already
            # passed do not delay the result for the one finally shown.
            if token != self._stack_inputs_token:
                return []
            return self._find_stack_input_jpgs(path)

        try:
            future = self._stack_paint_executor.submit(_search)
        except RuntimeError:
            return  # shutting down
        future.add_done_callback(_done)

    @Slot(object)
    def _on_stack_inputs_ready(self, payload) -> None:
        if self._shutting_down or payload.get("token") != self._stack_inputs_token:
            return
        paths = payload.get("paths") or []
        self._stack_input_paths = {p.name: p for p in paths}
        count = len(paths)
        rows = []
        if paths:
            # The stack itself, to paint back over frames painted below.
            rows.append(
                {"name": STACK_OUTPUT_SOURCE, "label": "S: Stack output", "short": "S"}
            )
        for i, p in enumerate(paths, start=1):
            hint = " (far)" if i == count else (" (near)" if i == 1 else "")
            rows.append(
                {"name": p.name, "label": f"{i}: {p.stem}{hint}", "short": str(i)}
            )
        self.ui_state.stackInputs = rows
        # Default to the frame focused furthest back.
        self.ui_state.stackInputName = paths[-1].name if paths else ""
        if paths:
            # Count and range, not every name: a 15-frame stack printed 15
            # file names on each visit.
            log.debug(
                "Stack inputs for current image: %d frame(s), %s .. %s",
                count,
                paths[0].name,
                paths[-1].name,
            )
        self._ensure_stack_sources_for_edits()

    @Slot(str)
    def set_stack_input(self, name: str) -> None:
        """Dropdown: choose which stack input new stack strokes paint."""
        if name not in self._stack_input_paths and not (
            name == STACK_OUTPUT_SOURCE and self._stack_input_paths
        ):
            return
        self.ui_state.stackInputName = name
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "stack":
            self._request_stack_source(name)
            self._schedule_stack_hover()

    def _ensure_stack_sources_for_edits(self) -> None:
        """Load the inputs that the current image's stack layers paint."""
        if not self._stack_input_paths:
            return
        for layer in stack_layers(self.image_editor.current_edits, include_hidden=True):
            self._request_stack_source(layer["source"])

    def _request_stack_source(self, name: str) -> None:
        """Decode and register stack input *name* in the background."""
        editor = self.image_editor
        editor_session_id = editor.session_id
        if editor_session_id is None:
            return
        loading_key = (editor_session_id, name)
        if (
            not name
            or name == STACK_OUTPUT_SOURCE
            or name in editor._stack_sources
            or loading_key in self._stack_source_loading
        ):
            return
        path = self._stack_input_paths.get(name)
        if path is None:
            self.update_status_message(f"Stack input {name} was not found")
            return
        # Register against the full-resolution master when there is one:
        # float buffers are only ever rebound, never written in place.
        output = editor.float_image
        if output is None:
            output = editor.float_preview
        if output is None:
            return
        token = self._stack_inputs_token
        editor_path = editor.current_filepath
        self._stack_source_loading.add(loading_key)
        self.update_status_message(
            f"Aligning {stack_input_label(name)} with the stack..."
        )

        def _done(future: concurrent.futures.Future) -> None:
            try:
                source, error = future.result(), None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                source, error = None, exc
            self._stackSourceReady.emit(
                {
                    "token": token,
                    "name": name,
                    "editor_path": editor_path,
                    "editor_session_id": editor_session_id,
                    "source": source,
                    "error": error,
                }
            )

        try:
            future = self._stack_paint_executor.submit(load_stack_source, path, output)
        except RuntimeError:
            self._stack_source_loading.discard(loading_key)
            return
        future.add_done_callback(_done)

    @Slot(object)
    def _on_stack_source_ready(self, payload) -> None:
        if self._shutting_down or payload.get("token") != self._stack_inputs_token:
            return
        name = payload["name"]
        editor_session_id = payload.get("editor_session_id")
        if editor_session_id is not None:
            self._stack_source_loading.discard((editor_session_id, name))
        label = stack_input_label(name)
        error = payload.get("error")
        if error is not None:
            log.warning("Could not use stack input %s: %s", name, error)
            self.update_status_message(f"Could not align {label}: {error}", 8000)
            return
        editor = self.image_editor
        if (
            editor.current_filepath != payload.get("editor_path")
            or editor.session_id != editor_session_id
            or editor._stack_sources_session_id != editor_session_id
        ):
            return
        # Keep the inputs this photo's layers use plus the selected one.
        keep = {
            layer["source"]
            for layer in stack_layers(editor.current_edits, include_hidden=True)
        }
        keep.add(self.ui_state.stackInputName)
        sources = {k: v for k, v in editor._stack_sources.items() if k in keep}
        sources[name] = payload["source"]
        # Rebind, never mutate: the render worker may be iterating the old dict.
        editor._stack_sources = sources
        editor.invalidate_render_cache()
        self._kick_preview_worker()
        # A halo correction computed while this input was missing did not
        # include the stack paint that uses it.
        self._schedule_halo_jobs()
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "stack":
            self.update_status_message(
                f"Stack paint: paint to reveal {label}. "
                "Right/Alt-drag restores the stack."
            )

    def _clear_stack_hover(self) -> None:
        self._stack_hover_timer.stop()
        self._stack_hover_sig = None
        self._stack_hover_pending = False
        if self.ui_state._stack_hover_image is not None:
            self.ui_state._stack_hover_image = None
            self._stack_hover_buffer = None
            self.ui_state.bump_stack_hover_generation(0)

    def _schedule_stack_hover(self) -> None:
        """Re-render the hover preview soon, if stack paint is active."""
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "stack":
            self._stack_hover_timer.start()
        else:
            self._clear_stack_hover()

    def _stack_hover_source(self, name: str):
        """(ready, source) for the hover render; None = the stack output."""
        if name == STACK_OUTPUT_SOURCE:
            return True, None
        source = self.image_editor._stack_sources.get(name)
        return source is not None, source

    def _stack_hover_signature(self, name: str, source) -> tuple:
        """Everything the hover render depends on (stack strokes excluded)."""
        editor = self.image_editor
        edits = editor.current_edits
        masks = editor._mask_assets
        recipe_layers = []
        for layer in effective_layers(edits):
            mask = masks.get(layer["id"])
            recipe_layers.append(
                (
                    repr(layer),
                    (mask.uid, mask.revision, len(mask.strokes)) if mask else None,
                )
            )
        return (
            name,
            id(source),
            id(editor.float_preview),
            id(editor.float_image),
            repr(sorted((k, v) for k, v in edits.items() if k != "local_layers")),
            tuple(recipe_layers),
        )

    def _start_stack_hover_render(self) -> None:
        if self._shutting_down or not (
            self.ui_state.isBrushMode and self.ui_state.brushTool == "stack"
        ):
            return
        if self._stack_hover_inflight:
            self._stack_hover_pending = True
            return
        name = self.ui_state.stackInputName
        ready, source = self._stack_hover_source(name)
        if not ready:
            self._clear_stack_hover()  # rescheduled when the input is aligned
            return
        sig = self._stack_hover_signature(name, source)
        if sig == self._stack_hover_sig:
            return
        editor = self.image_editor
        self._stack_hover_inflight = True
        self._stack_hover_pending = False

        def _done(future: concurrent.futures.Future) -> None:
            try:
                decoded, error = future.result(), None
            except Exception as exc:  # pylint: disable=broad-exception-caught
                decoded, error = None, exc
            self._stackHoverReady.emit(
                {"sig": sig, "decoded": decoded, "error": error, "editor": editor}
            )

        try:
            future = self._stack_paint_executor.submit(
                editor.render_stack_hover_preview, source
            )
        except RuntimeError:
            self._stack_hover_inflight = False
            return
        future.add_done_callback(_done)

    @Slot(object)
    def _on_stack_hover_ready(self, payload) -> None:
        self._stack_hover_inflight = False
        if self._shutting_down:
            return
        stack_mode = self.ui_state.isBrushMode and self.ui_state.brushTool == "stack"
        if payload.get("error") is not None:
            log.warning("Stack paint hover preview failed: %s", payload["error"])
        decoded = payload.get("decoded")
        current_sig = None
        if stack_mode and payload.get("editor") is self.image_editor:
            ready, source = self._stack_hover_source(self.ui_state.stackInputName)
            if ready:
                current_sig = self._stack_hover_signature(
                    self.ui_state.stackInputName, source
                )
        if (
            decoded is not None
            and current_sig is not None
            and current_sig == payload["sig"]
        ):
            from PySide6.QtGui import QImage

            buf = bytes(decoded.buffer)
            # QImage wraps buf without copying; keep it alive with the image.
            self._stack_hover_buffer = buf
            self.ui_state._stack_hover_image = QImage(
                buf,
                decoded.width,
                decoded.height,
                decoded.bytes_per_line,
                decoded.format,
            )
            self._stack_hover_sig = payload["sig"]
            self.ui_state.bump_stack_hover_generation()
        elif decoded is None and current_sig == payload["sig"]:
            # Do not retry an identical failed render forever. Any edit or
            # source change produces a new signature and remains retryable.
            self._stack_hover_sig = payload["sig"]
        if stack_mode and (
            self._stack_hover_pending or current_sig != self._stack_hover_sig
        ):
            self._schedule_stack_hover()

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

    def toggle_editor(self):
        """Toggle compact image editor. Called from keybinder E key."""
        if self.ui_state.isCropping:
            self.ui_state.statusMessage = "Apply or cancel the crop before editing"
            return
        if self.ui_state.isEditorOpen:
            self.ui_state.isEditorOpen = False
        else:
            self.ui_state.isEditorExpanded = False
            self.ui_state.isEditorOpen = True
            self.load_image_for_editing()

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

        if self.ui_state.isCropping:
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
        preview_data = (
            self._last_rendered_preview
            if self._has_current_live_preview_for_index(self.current_index)
            else None
        )
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

    def _editor_preview_master_box(self) -> Tuple[int, int]:
        """Box the editor's source-space preview master should be fitted into.

        Quick live previews (sliders, quick-adjust keys) render from that
        master, so sizing it to the image area's physical pixels makes those
        frames display-sharp on arrival instead of soft until the settled
        full-resolution pass lands. ``core.editor_preview_long_edge`` caps the
        image's long edge whichever way it is oriented; 0 means auto (follow
        the window).

        Reads the raw display size rather than ``get_display_info()``, which
        reports 0x0 while zoomed — zooming must not resize the master.
        """
        configured = config.getint("core", "editor_preview_long_edge", fallback=0)
        with self._display_lock:
            box_w = int(self.display_width)
            box_h = int(self.display_height)
        if box_w <= 0 or box_h <= 0:
            # Size unknown yet (startup): keep the historical master size, so
            # an image edited before the first resize report still gets one.
            box_w, box_h = DEFAULT_PREVIEW_MASTER_BOX

        long_edge = max(box_w, box_h)
        cap = long_edge if configured <= 0 else configured
        cap = max(EDITOR_PREVIEW_MIN_LONG_EDGE, min(cap, EDITOR_PREVIEW_MAX_LONG_EDGE))
        # Square cap box, clipped to the display. Scaling the display's own
        # shape down to the cap would let the display's *short* edge limit a
        # portrait photo — 1920px on a 3840x2160 screen yields a 1920x1080 box,
        # and a portrait image fitted into it gets only 1080px on its long
        # edge. Clipping a square box keeps the configured resolution
        # available in whichever direction the image is tall, while the
        # display bound stops either side from exceeding what can be shown.
        return (min(cap, box_w), min(cap, box_h))

    def _apply_editor_preview_master_box(self) -> None:
        """Push the configured preview-master size into the editor.

        Rebuilds the loaded image's master when the size changed (window
        resize, settings change) and re-renders so the loupe picks up the new
        resolution without waiting for the next navigation.
        """
        if self.image_editor is None:
            return
        if not self.image_editor.set_preview_master_box(
            self._editor_preview_master_box()
        ):
            return
        self._live_preview_target_dims = None
        self._live_preview_target_session_key = None
        if self._has_current_live_preview_for_index(self.current_index):
            self._kick_preview_worker()

    def _quick_preview_master_dims_if_covering(self) -> Optional[Tuple[int, int]]:
        """Master dimensions when quick renders already cover the screen.

        When the preview master is at least as large as the image area the
        loupe can show, a quick render carries every pixel the settled
        display-resolution pass would — so that pass is a no-op the user only
        experiences as a soft-then-sharp flicker, and it can be skipped.

        Returns None (refinement still needed) when the master is smaller than
        the fitted display size, when the size is unknown, while zoomed, or
        whenever geometry edits are present: crop and straighten round at
        master resolution, so their frames are not interchangeable.
        """
        display_w, display_h, _ = self.get_display_info()
        if not display_w or not display_h:
            return None

        with self.image_editor._lock:
            master = self.image_editor.float_preview
            edits = dict(self.image_editor.current_edits)
        if master is None:
            return None
        if (
            edits.get("crop_box")
            or float(edits.get("straighten_angle") or 0.0)
            or int(edits.get("rotation") or 0)
        ):
            return None

        native_w, native_h = self.get_current_display_native_size()
        if native_w <= 0 or native_h <= 0:
            return None
        scale = fit_scale_in_box(native_w, native_h, (display_w, display_h))
        fitted_w = max(1, round(native_w * scale))
        fitted_h = max(1, round(native_h * scale))
        master_h, master_w = master.shape[:2]
        # 1px of slack: the master and the fitted size are independent
        # roundings of the same aspect ratio.
        if master_w + 1 < fitted_w or master_h + 1 < fitted_h:
            return None
        return (int(master_w), int(master_h))

    def _display_preview_long_edge(self) -> Optional[int]:
        """Long-edge cap for display-only full-resolution renders.

        The cap is the long edge of the image as fitted into the image area,
        not of the area itself. A 4:3 photo fills only 2880 px of a 3840x2160
        area, and rendering it 3840 px wide made 1.8 times the pixels the
        screen can show (3.2 times for a portrait photo). A crop, straighten
        or rotation changes the output's shape, so with one in play the
        area's own long edge is used, as before.

        Returns None when the display size is unknown or the view is zoomed
        (get_display_info reports 0x0 while zoomed), which renders uncapped.
        """
        display_w, display_h, _ = self.get_display_info()
        if not (display_w and display_h):
            return None
        area_long_edge = max(int(display_w), int(display_h))
        if self._current_live_session_has_geometry_edits():
            return area_long_edge
        native_w, native_h = self.get_current_display_native_size()
        if native_w <= 0 or native_h <= 0:
            return area_long_edge
        scale = fit_scale_in_box(native_w, native_h, (display_w, display_h))
        fitted_long_edge = max(1, round(max(native_w, native_h) * scale))
        return min(area_long_edge, fitted_long_edge)

    def _live_preview_quick_target_size(self) -> Optional[Tuple[int, int]]:
        """Output size for quick (preview-master) live renders.

        Sizing quick frames to what the display-capped full-resolution
        refinement will produce keeps the loupe's sourceSize stable across
        the swap, so the refinement sharpens in place. Left unmatched, the
        rounding differences between the two render paths give the frames
        slightly different aspect ratios and every refinement nudges the
        fitted image.
        """
        cap = self._display_preview_long_edge()
        if not cap:
            return None

        # When the preview master already covers the screen there is no
        # refinement to match: the master's own size is the session's stable
        # target, and settled quick frames land on it without any resampling.
        # Drag frames are still stretched up to it, so the loupe's sourceSize
        # stays put for the whole interaction.
        covering_dims = self._quick_preview_master_dims_if_covering()
        if covering_dims is not None:
            return covering_dims

        # Prefer the recorded size of the last display-capped full-resolution
        # frame for this session: ground truth, exact for tonal-only edits.
        if (
            self._live_preview_target_dims is not None
            and self._live_preview_target_session_key is not None
            and self._live_preview_target_session_key
            == self._get_current_live_preview_session_key()
        ):
            return self._live_preview_target_dims

        # Cold start. Without geometry edits the refinement is the native
        # frame downscaled with the same rounding as _apply_edits, so its
        # size is predictable. With geometry edits the crop/straighten
        # rounding happens at master resolution; don't guess.
        if self._current_live_session_has_geometry_edits():
            return None
        native_w, native_h = self.get_current_display_native_size()
        if native_w <= 0 or native_h <= 0:
            return None
        long_edge = max(native_w, native_h)
        if long_edge <= cap:
            return (native_w, native_h)
        scale = cap / long_edge
        return (max(1, round(native_w * scale)), max(1, round(native_h * scale)))

    def _store_live_preview_target_dims(self, decoded: DecodedImage) -> None:
        """Record a display-capped full-res frame's size as the quick-render target."""
        if self._display_preview_long_edge() is None:
            # Uncapped render (display size unknown / zoomed): master-sized
            # dims must not become the quick-frame target.
            return
        self._live_preview_target_dims = (int(decoded.width), int(decoded.height))
        self._live_preview_target_session_key = (
            self._get_current_live_preview_session_key()
        )

    def _build_live_crop_preview_edits_override(self) -> Optional[dict]:
        """Snapshot a temporary crop override for live crop preview rendering."""
        if not getattr(self.ui_state, "isCropping", False):
            return None

        draft_crop_box = self._normalize_crop_box_tuple(
            getattr(self.ui_state, "currentCropBox", None)
        )
        if draft_crop_box is None:
            return None

        with self.image_editor._lock:
            preview_edits = dict(self.image_editor.current_edits)

        # Use the same draft-to-source mapping as execute_crop so the live
        # preview and the committed crop always agree.
        if self._crop_mode_has_saved_geometry:
            base_crop_box = self._crop_mode_saved_crop_box
            base_angle = self._crop_mode_saved_straighten_angle
        else:
            base_crop_box = self._normalize_crop_box_tuple(
                preview_edits.get("crop_box")
            )
            base_angle = 0.0
        composed_crop_box = self.image_editor.map_crop_draft_to_source(
            draft_crop_box,
            base_crop_box,
            base_angle,
        )
        if composed_crop_box is None:
            return None

        if preview_edits.get("crop_box") == composed_crop_box:
            return None

        preview_edits["crop_box"] = composed_crop_box
        return preview_edits

    def _preview_refinement_available(self) -> bool:
        """True when the idle pass can re-render a degraded frame at display size.

        Crop overlays and zoom both re-frame the preview per event, so
        refinement is suppressed in those states — which also means a frame
        rendered below the quick tier there would never be sharpened.
        """
        return not (
            self.ui_state is None or self.ui_state.isCropping or self.ui_state.isZoomed
        )

    def _kick_preview_worker(self, *, full_resolution: Optional[bool] = None):
        """Kicks off a background preview render task."""
        if getattr(self, "_shutting_down", False):
            return
        if self.ui_state.isBrushMode and self.ui_state.brushTool == "stack":
            self._schedule_stack_hover()

        render_full_resolution = (
            self._should_render_live_preview_full_resolution()
            if full_resolution is None
            else full_resolution
        )

        now = time.monotonic()
        frequent_quick_kick = (
            not render_full_resolution and now - self._last_quick_preview_kick_at <= 0.3
        )
        if not render_full_resolution:
            self._last_quick_preview_kick_at = now
        # Only degrade below the quick tier when the idle refinement pass can
        # sharpen the result afterwards. While cropping or zoomed it cannot, so
        # a drag-tier frame would stay soft on screen until the user navigates
        # away — and crop dragging produces back-to-back kicks, which would
        # otherwise put the whole crop interaction on the degraded path.
        drag_render = (
            not render_full_resolution
            and (self._editor_slider_drag_active or frequent_quick_kick)
            and self._preview_refinement_available()
        )

        with self._preview_lock:
            # Refinement owns a separate worker and never owns the quick gate.
            # Signal it before gate inspection so even a coalesced drag event
            # promptly stops obsolete display-resolution work.
            cancel_event = self._preview_refinement_cancel
            if cancel_event is not None:
                cancel_event.set()
            if self._preview_inflight:
                self._preview_pending = True
                return

            self._preview_inflight = True
            self._preview_pending = False
            self._preview_token += 1
            token = self._preview_token
            session_key = self._get_current_live_preview_session_key()

        # Full-resolution live previews are only consumed unzoomed (the
        # provider falls back to the decoded cache when zoomed), so cap the
        # render at the display size — a 20MP master costs several hundred ms
        # per render for pixels the screen cannot show.
        display_long_edge = (
            self._display_preview_long_edge() if render_full_resolution else None
        )
        preview_edits_override = self._build_live_crop_preview_edits_override()
        # Crop-mode overrides change the framing per frame; only steady-state
        # quick renders get sized to match the full-resolution refinement.
        quick_output_size = (
            self._live_preview_quick_target_size()
            if preview_edits_override is None
            else None
        )
        # Whether this quick frame is display-complete, i.e. the target size
        # came from a master that already covers the screen. Read from the same
        # helper the target size uses so the two can never disagree.
        covering_dims_used = (
            not render_full_resolution
            and preview_edits_override is None
            and quick_output_size is not None
            and quick_output_size == self._quick_preview_master_dims_if_covering()
        )
        quick_downscale_long_edge = None
        if drag_render:
            with self.image_editor._lock:
                preview_master = self.image_editor.float_preview
                if preview_master is not None:
                    # Keep drag renders somewhat below the quick master for
                    # responsiveness without making the displayed frame look
                    # conspicuously soft. A 4:3 1440x1080 master renders at
                    # 1080px long-edge; a 1920x1080 master is capped at 1280px.
                    quick_downscale_long_edge = min(
                        1280,
                        max(1, round(max(preview_master.shape[:2]) * 0.75)),
                    )
        if render_full_resolution and display_long_edge:
            # This kick's accepted final frame defines the quick-render target
            # size. Uncapped (zoomed) full-res renders must not: quick frames
            # would then be upscaled to master resolution.
            self._preview_full_res_token = token

        # Submit task to dedicated preview executor
        try:
            fut = self._preview_executor.submit(
                self._render_preview_worker,
                token,
                session_key,
                self.image_editor,
                render_full_resolution,
                display_long_edge,
                preview_edits_override,
                self.previewReady.emit,
                quick_output_size,
                quick_downscale_long_edge,
            )
            fut.add_done_callback(self._on_preview_done)
        except RuntimeError:
            log.warning("Preview executor failed (shutting down?)")
            with self._preview_lock:
                self._preview_inflight = False
            return

        # Schedule (or cancel) the idle-time high-quality refinement pass.
        # Always called from the main thread (slots and queued signals).
        if render_full_resolution:
            self._hq_preview_timer.stop()
        elif covering_dims_used and not drag_render:
            # The frame just submitted already carries every pixel the screen
            # can show, so there is nothing to sharpen: no soft-then-sharp
            # swap, and no full-resolution render behind it. Also cancels a
            # pass armed by an earlier drag, which this frame supersedes.
            self._hq_preview_timer.stop()
        else:
            # Sharpen sooner after a discrete edit (a keypress, a slider click,
            # Auto Levels). The long debounce exists to coalesce continuous
            # drags; spending it on a one-shot edit is the soft half-second the
            # user notices.
            self._hq_preview_timer.start(
                _HQ_PREVIEW_DEBOUNCE_DRAG_MS
                if self._editor_slider_drag_active
                else _HQ_PREVIEW_DEBOUNCE_SETTLED_MS
            )

    def _refine_preview_resolution(self):
        """Re-render the last preview-resolution frame at display resolution.

        Armed by every preview-resolution kick; fires once slider/keyboard
        input has been idle for the debounce interval. Refinement runs on its
        own cancellable worker so it can never occupy the quick-preview queue.
        """
        if getattr(self, "_shutting_down", False):
            return
        if not self._preview_refinement_available():
            return
        if not self.image_editor or self.image_editor.current_filepath is None:
            return

        with self._preview_lock:
            if self._preview_inflight:
                # A render is still running; check again once it settles.
                self._hq_preview_timer.start()
                return
            if self._preview_refinement_inflight:
                # A cancelled refinement may still be unwinding between
                # stages. Check again after it releases the dedicated worker.
                self._hq_preview_timer.start()
                return
            # Only refine a frame that is still being displayed: same index
            # and same live edit session as when the drag happened. After
            # navigation the session key changes and there is nothing to
            # sharpen (the loupe serves the decoded file again).
            has_current_buffer = (
                self._last_rendered_preview is not None
                and self._last_rendered_preview_index == self.current_index
                and self._last_rendered_preview_session_key
                == self._get_current_live_preview_session_key()
            )
        if not has_current_buffer:
            return

        self._start_preview_refinement()

    def _start_preview_refinement(self):
        """Submit an idle display-resolution render outside the quick gate."""
        display_long_edge = self._display_preview_long_edge()
        session_key = self._get_current_live_preview_session_key()
        if session_key is None:
            return

        cancel_event = threading.Event()
        image_editor = self.image_editor
        with image_editor._lock:
            edits_revision = image_editor._edits_rev
        with self._preview_lock:
            if self._preview_inflight or self._preview_refinement_inflight:
                self._hq_preview_timer.start()
                return
            self._preview_refinement_inflight = True
            self._preview_refinement_cancel = cancel_event
            self._preview_token += 1
            token = self._preview_token
            if display_long_edge:
                self._preview_full_res_token = token

        def cancelled() -> bool:
            return cancel_event.is_set() or image_editor._edits_rev != edits_revision

        # Only the expensive settled/display-resolution refinement throttles
        # speculation; lightweight quick-preview renders never do. Acquired
        # before submit so a job that finishes immediately still releases via
        # its own callback token, and released again if submission fails.
        governor_token = self._enter_prefetch_constraint("preview_refinement")

        try:
            future = self._preview_refine_executor.submit(
                self._render_preview_worker,
                token,
                session_key,
                image_editor,
                True,
                display_long_edge,
                None,
                None,
                None,
                None,
                cancelled,
            )
            future.add_done_callback(
                lambda done, tok=governor_token: self._on_preview_refinement_done(
                    done, tok
                )
            )
        except RuntimeError:
            log.warning("Preview refinement failed to start")
            self._leave_prefetch_constraint(governor_token)
            with self._preview_lock:
                if self._preview_refinement_cancel is cancel_event:
                    self._preview_refinement_cancel = None
                self._preview_refinement_inflight = False

    def _on_preview_refinement_done(self, future, governor_token=None):
        # Released first and unconditionally: success, stale result,
        # cooperative cancellation, worker exception, and shutdown all land
        # here, and the token identifies THIS refinement, so a rapidly
        # replaced job cannot restore capacity a newer one still needs.
        self._leave_prefetch_constraint(governor_token)
        if getattr(self, "_shutting_down", False):
            return
        try:
            token, session_key, decoded = future.result()
        except Exception:
            token, session_key, decoded = None, None, None

        with self._preview_lock:
            self._preview_refinement_inflight = False
            self._preview_refinement_cancel = None

        try:
            # Refinement never owns/releases _preview_inflight. The fifth
            # payload field preserves the quick gate's final-frame contract.
            self.previewReady.emit((token, session_key, decoded, True, False))
        except RuntimeError:
            pass

    @staticmethod
    def _render_preview_worker(
        token,
        session_key,
        image_editor,
        full_resolution: bool = False,
        display_long_edge=None,
        preview_edits_override=None,
        emit_intermediate=None,
        quick_output_size=None,
        quick_downscale_long_edge=None,
        cancel_check=None,
    ):
        # Heavy work (PIL apply_edits) happens here off-thread
        try:
            decoded = None
            if full_resolution:
                # Publish a cheap preview-sized frame first so keypresses give
                # immediate feedback; the high-resolution frame replaces it
                # when ready. Intermediate payloads carry final=False so the
                # gate stays inflight until the real result lands.
                if emit_intermediate is not None:
                    quick = image_editor.get_preview_data_cached(
                        allow_compute=True,
                        edits_override=preview_edits_override,
                        output_size=quick_output_size,
                    )
                    if quick is not None:
                        emit_intermediate((token, session_key, quick, False))
                decoded = image_editor.get_full_resolution_preview_data(
                    max_long_edge=display_long_edge,
                    edits_override=preview_edits_override,
                    cancel_check=cancel_check,
                )
            if decoded is None:
                # allow_compute=True ensures we actually do the work
                decoded = image_editor.get_preview_data_cached(
                    allow_compute=True,
                    edits_override=preview_edits_override,
                    output_size=quick_output_size,
                    downscale_long_edge=quick_downscale_long_edge,
                )
            return token, session_key, decoded
        except EditRenderCancelled:
            log.debug("Preview refinement cancelled (token=%s)", token)
            return token, session_key, None
        except Exception:
            log.exception("Preview render failed")
            return token, session_key, None

    def _on_preview_done(self, fut):
        if getattr(self, "_shutting_down", False):
            return

        try:
            token, session_key, decoded = fut.result()
        except Exception:
            token, session_key, decoded = None, None, None

        # Emit from worker thread; Qt will queue to UI thread
        try:
            self.previewReady.emit((token, session_key, decoded, True))
        except RuntimeError:
            # The final payload is the only thing that releases the preview
            # gate; if the emit fails (Qt object torn down mid-shutdown),
            # release it here so a surviving session cannot wedge rendering.
            with self._preview_lock:
                self._preview_inflight = False

    def _emit_preview_accepted_side_effects(self):
        self.ui_state.currentImageSourceChanged.emit()
        self.ui_state.highlightStateChanged.emit()
        self.update_histogram()

    @Slot(object)
    def _apply_preview_result(self, payload):
        if getattr(self, "_shutting_down", False):
            return

        try:
            token, session_key, decoded, is_final, owns_gate = payload
        except (TypeError, ValueError):
            try:
                token, session_key, decoded, is_final = payload
                owns_gate = True
            except (TypeError, ValueError):
                token, session_key, decoded, is_final, owns_gate = (
                    None,
                    None,
                    None,
                    True,
                    True,
                )
        should_kick = False
        should_accept = False

        with self._preview_lock:
            # If the future completed before add_done_callback registered, the
            # final frame is delivered synchronously and can be processed
            # before the queued intermediate; that late intermediate (gate
            # already released) must not overwrite the full-quality result.
            stale_intermediate = (
                owns_gate and not is_final and not self._preview_inflight
            )

            # Intermediate frames (fast preview-size pass of a full-resolution
            # render) must not release the gate: the worker is still busy
            # producing the final frame.
            if owns_gate and is_final:
                self._preview_inflight = False

            # Accept result only if:
            # 1. We got valid decoded data
            # 2. Token matches (not stale from an old request)
            # 3. No pending request waiting (avoid "snap back" stale frame flash)
            if (
                not stale_intermediate
                and decoded is not None
                and token == self._preview_token
                and (
                    not self._preview_pending
                    # Mid-stroke a newer request is almost always queued, so
                    # this rule would hide every frame until the mouse is
                    # released. Brush frames only ever lag the stroke (no
                    # value can snap back), so showing them is the point.
                    or self._local_painting_layer_id is not None
                )
                and session_key is not None
                and session_key == self._get_current_live_preview_session_key()
            ):
                self._publish_last_rendered_preview_locked(decoded, session_key)
                if is_final and token == self._preview_full_res_token:
                    # Display-capped full-res frame: its size becomes the
                    # target quick renders are matched to (shift-free swap).
                    self._live_preview_target_dims = (decoded.width, decoded.height)
                    self._live_preview_target_session_key = session_key
                self.ui_refresh_generation += 1
                self._last_rendered_preview_index = self.current_index
                self._last_rendered_preview_gen = self.ui_refresh_generation
                if (
                    self._original_compare_active
                    and self._original_compare_preview is not None
                    and self._original_compare_index == self.current_index
                    and self._original_compare_session_key
                    == self._get_current_original_compare_session_key()
                ):
                    self._original_compare_gen = self.ui_refresh_generation
                should_accept = True

            # Consume pending flag atomically before scheduling
            if owns_gate and is_final and self._preview_pending:
                self._preview_pending = False
                should_kick = True

        # Emit outside lock to avoid holding lock during UI work
        if should_accept:
            self._emit_preview_accepted_side_effects()

        # Call directly (not via singleShot) since we're on the UI thread.
        # This prevents race where a new slider event could interleave between
        # scheduling and execution, causing a spurious extra render.
        if should_kick:
            self._kick_preview_worker()

    def _can_show_original_compare(self, event) -> bool:
        if not self.image_files:
            return False
        if self._is_grid_view_active:
            return False
        if getattr(self.ui_state, "isCropping", False):
            return False
        if getattr(self.ui_state, "isEditorOpen", False) and getattr(
            self.ui_state, "isEditorExpanded", False
        ):
            return False
        blocked_modifiers = event.modifiers() & (
            Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier
        )
        return not blocked_modifiers

    @Slot()
    def start_original_compare_preview(self):
        """Show the source image with crop framing while space is held."""
        if getattr(self, "_shutting_down", False):
            return
        if not self.image_files or self._is_grid_view_active:
            log.debug("[COMPARE] start skipped: no images or grid view")
            return
        if getattr(self.ui_state, "isCropping", False):
            log.debug("[COMPARE] start skipped: cropping")
            return

        active_path = self._ensure_active_image_loaded_for_auto_adjust()
        if active_path is None:
            log.debug("[COMPARE] start skipped: no active edit path")
            return

        session_key = self._get_current_original_compare_session_key()
        if session_key is None:
            # Silent here means space does nothing at all and the matching
            # release is a no-op too, because the active flag never got set.
            log.debug(
                "[COMPARE] start skipped: no preview session " "(active=%s editor=%s)",
                active_path,
                getattr(self.image_editor, "current_filepath", None),
            )
            return

        # Skip the quick tier when the display-resolution render is the only
        # sensible frame: while zoomed a preview-master frame would be visibly
        # soft with no fit-sized frame to stand in for it, and after geometry
        # edits the quick and full paths round the crop differently.
        skip_quick_tier = (
            bool(getattr(self.ui_state, "isZoomed", False))
            or self._should_render_live_preview_full_resolution()
        )

        # Match the compare frame's dimensions to the live preview it swaps
        # with, so toggling space cannot nudge the fitted image. Both are
        # None while zoomed (display size reports 0x0), preserving the
        # uncapped native render zoom needs.
        compare_long_edge = self._display_preview_long_edge()
        compare_output_size = self._live_preview_quick_target_size()
        # The cap alone identifies which view a compare frame belongs to (it
        # is None only while zoomed), and it changes just on resize or zoom.
        # The output size deliberately stays out of the identity: it tracks
        # whatever the live preview last rendered and can change while a
        # compare render is in flight, so counting it would reject the very
        # frame the user is holding space waiting for. It is a sizing hint.
        render_target = compare_long_edge

        emit_source_changed = False
        submit_quick = False
        submit_refine = False
        index = self.current_index

        with self._preview_lock:
            was_active = self._original_compare_active
            self._original_compare_active = True
            if hasattr(self.ui_state, "originalCompareActive"):
                self.ui_state.originalCompareActive = True

            buffer_ready = (
                self._original_compare_preview is not None
                and self._original_compare_index == self.current_index
                and self._original_compare_session_key == session_key
                and self._original_compare_render_target == render_target
            )
            if buffer_ready and (
                not was_active
                or self._original_compare_gen != self.ui_refresh_generation
            ):
                # Show the cached frame immediately, then fall through: a
                # quick-tier buffer still needs its sharpening pass, or a
                # soft frame would stay soft for the rest of the session.
                # A fresh generation is required on every activation because
                # sync_ui_state() may have advanced the cached frame's marker
                # while compare was inactive and the edited frame stayed on
                # screen. While already active, avoid duplicate refreshes for
                # pixels the loupe is currently showing.
                self.ui_refresh_generation += 1
                self._original_compare_gen = self.ui_refresh_generation
                emit_source_changed = True

            # At most one render per tier is ever outstanding; the flags are
            # cleared only by that tier's completion, so tapping space while a
            # render is running reuses it instead of queueing a duplicate.
            submit_quick = (
                not buffer_ready
                and not skip_quick_tier
                and not self._original_compare_inflight
            )
            submit_refine = (
                not (buffer_ready and self._original_compare_full_resolution)
                and not self._original_compare_refine_inflight
            )
            if submit_quick:
                self._original_compare_inflight = True
            if submit_refine:
                self._original_compare_refine_inflight = True

        log.debug(
            "[COMPARE] start idx=%d was_active=%s buffer=%s full=%s "
            "emit=%s quick=%s refine=%s target=%s",
            index,
            was_active,
            buffer_ready,
            self._original_compare_full_resolution,
            emit_source_changed,
            submit_quick,
            submit_refine,
            render_target,
        )

        if emit_source_changed:
            self.ui_state.currentImageSourceChanged.emit()

        if submit_quick:
            try:
                future = self._preview_executor.submit(
                    self._render_original_compare_worker,
                    session_key,
                    index,
                    self.image_editor,
                    False,
                    compare_long_edge,
                    compare_output_size,
                    render_target,
                )
                future.add_done_callback(self._on_original_compare_done)
            except RuntimeError:
                log.warning("Original compare render failed to start")
                with self._preview_lock:
                    self._original_compare_inflight = False

        if submit_refine:
            # The display-resolution pass runs on the refinement worker, not
            # the quick-preview queue, so a long master render can never delay
            # the next interactive preview.
            try:
                future = self._preview_refine_executor.submit(
                    self._render_original_compare_worker,
                    session_key,
                    index,
                    self.image_editor,
                    True,
                    compare_long_edge,
                    compare_output_size,
                    render_target,
                )
                future.add_done_callback(self._on_original_compare_done)
            except RuntimeError:
                log.warning("Original compare refinement failed to start")
                with self._preview_lock:
                    self._original_compare_refine_inflight = False

    @Slot()
    def stop_original_compare_preview(self):
        """Return the loupe to the normal edited preview after space is released."""
        with self._preview_lock:
            if not self._original_compare_active:
                log.debug("[COMPARE] stop: was not active")
                return
            # In-flight renders are deliberately left running: their results
            # are identity-checked, so they still populate the compare buffer
            # and make the next press instant instead of being thrown away.
            self._original_compare_active = False
        log.debug("[COMPARE] stop: restoring edited preview")

        if hasattr(self.ui_state, "originalCompareActive"):
            self.ui_state.originalCompareActive = False
        self.ui_refresh_generation += 1
        # Re-point the live edited preview to the freshly bumped generation so
        # the provider's gen-gated editor-preview path stays valid when space is
        # released. Without this the loupe (at fit) fails has_valid_preview_buffer
        # and falls back to the un-edited decode, leaving the original on screen.
        # Mirrors the same fix-up done in sync_ui_state().
        if (
            self._last_rendered_preview is not None
            and self._last_rendered_preview_index == self.current_index
            and self._last_rendered_preview_session_key
            == self._get_current_live_preview_session_key()
        ):
            self._last_rendered_preview_gen = self.ui_refresh_generation
        self.ui_state.currentImageSourceChanged.emit()

    @staticmethod
    def _render_original_compare_worker(
        session_key,
        index: int,
        image_editor,
        full_resolution: bool,
        max_long_edge=None,
        output_size=None,
        render_target=None,
    ):
        try:
            decoded = image_editor.get_original_compare_preview_data(
                full_resolution=full_resolution,
                max_long_edge=max_long_edge,
                output_size=output_size,
            )
            return session_key, index, decoded, full_resolution, render_target
        except Exception:
            log.exception("Original compare render failed")
            return session_key, index, None, full_resolution, render_target

    def _on_original_compare_done(self, future):
        if getattr(self, "_shutting_down", False):
            return

        try:
            payload = future.result()
        except Exception:
            payload = None

        self.originalCompareReady.emit(payload)

    @Slot(object)
    def _apply_original_compare_result(self, payload):
        stage_known = True
        try:
            session_key, index, decoded, full_resolution, render_target = payload
        except (TypeError, ValueError):
            # Which tier produced this is unrecoverable; release both so a
            # malformed payload cannot wedge compare for the rest of the run.
            stage_known = False
            session_key, index, decoded = None, -1, None
            full_resolution, render_target = False, None

        # Read outside _preview_lock: _display_preview_long_edge() takes
        # _display_lock, and this slot runs on the UI thread, so there is no
        # reason to nest the two.
        current_target = self._display_preview_long_edge()

        should_emit = False
        needs_rekick = False
        with self._preview_lock:
            if not stage_known:
                self._original_compare_inflight = False
                self._original_compare_refine_inflight = False
            elif full_resolution:
                self._original_compare_refine_inflight = False
            else:
                self._original_compare_inflight = False

            current_session_key = self._get_current_original_compare_session_key()
            matches_current = (
                index == self.current_index
                and session_key is not None
                and session_key == current_session_key
            )
            # The tiers run on separate workers, so the quick frame can land
            # after the refinement it was standing in for. Never downgrade.
            stale_tier = (
                not full_resolution
                and self._original_compare_full_resolution
                and self._original_compare_preview is not None
                and self._original_compare_index == index
                and self._original_compare_session_key == session_key
                and self._original_compare_render_target == render_target
            )
            if decoded is not None and matches_current and not stale_tier:
                decoded.source_path = session_key[0]
                self._original_compare_preview = decoded
                self._original_compare_session_key = session_key
                self._original_compare_index = index
                self._original_compare_full_resolution = bool(full_resolution)
                self._original_compare_render_target = render_target
                # Frames that land after space was released still populate the
                # buffer above (the next press is then instant) but must not
                # repaint the loupe, which is showing the edited image again.
                # A frame for another view (the user zoomed or resized while it
                # was in flight) is kept but never shown at the wrong scale.
                if self._original_compare_active and render_target == current_target:
                    self.ui_refresh_generation += 1
                    self._original_compare_gen = self.ui_refresh_generation
                    should_emit = True

            # This render was for a view the user has already left (they
            # navigated or zoomed while it was in flight), so nothing on
            # screen improved and the tier flag it was holding blocked the
            # request for the current view. Re-kick now that the flag is
            # free. Strictly limited to superseded requests: re-kicking
            # merely because a render came back unusable would spin, since
            # the retry would fail exactly the same way.
            superseded = stage_known and (
                not matches_current or render_target != current_target
            )
            needs_rekick = (
                self._original_compare_active
                and superseded
                and not self._original_compare_refine_inflight
            )

        log.debug(
            "[COMPARE] result full=%s idx=%s ok=%s matches=%s stale_tier=%s "
            "emit=%s rekick=%s target=%s/%s",
            full_resolution,
            index,
            decoded is not None,
            matches_current,
            stale_tier,
            should_emit,
            needs_rekick,
            render_target,
            current_target,
        )

        if should_emit:
            self.ui_state.currentImageSourceChanged.emit()
        # Re-read the flag: the emit above runs QML bindings synchronously, so
        # the space release can land inside it. Restarting compare afterwards
        # would re-show the original with no release left to dismiss it.
        if needs_rekick and self._original_compare_active:
            # Outside the lock: start_original_compare_preview takes it too.
            self.start_original_compare_preview()

    @Slot()
    def cancel_crop_mode(self):
        """Cancel crop mode without applying changes."""
        if self.ui_state.isCropping:
            try:
                self._restore_crop_mode_geometry()
            except Exception:
                log.exception("cancel_crop_mode: failed to restore crop geometry")

            # Clear the mode before rendering the restored preview so the crop
            # overlay cannot stay visible during slow or failed preview work.
            self.ui_state.isCropRotating = False
            self.ui_state.isCropping = False

            try:
                # Runs synchronously on the UI thread; cap at display size.
                decoded = self.image_editor.get_full_resolution_preview_data(
                    max_long_edge=self._display_preview_long_edge()
                )
            except Exception:
                log.exception("cancel_crop_mode: restored preview render failed")
                decoded = None

            if decoded is not None:
                with self._preview_lock:
                    self._preview_token += 1
                    self._preview_pending = False
                    self._publish_last_rendered_preview_locked(decoded)
                    self._store_live_preview_target_dims(decoded)
                    self.ui_refresh_generation += 1
                    self._last_rendered_preview_index = self.current_index
                    self._last_rendered_preview_gen = self.ui_refresh_generation

            self._clear_crop_mode_snapshot()
            if decoded is not None:
                self._emit_preview_accepted_side_effects()
            else:
                self.ui_refresh_generation += 1
                self._kick_preview_worker()
            self.update_status_message("Crop cancelled")
            # Entering crop mode stopped the settled cover decode via
            # _begin_direct_image_transition; re-arm it now that the crop
            # transaction is over, so the current image still upgrades to
            # cover quality without waiting for the next navigation.
            self._restart_quality_decode_timer()

    @Slot()
    def toggle_crop_mode(self):
        """Toggle crop mode on/off."""
        if self.ui_state.isCropping:
            # Exiting crop mode: reuse the specialized cleanup
            self.cancel_crop_mode()
        else:
            if self.ui_state.isEditorOpen and self.ui_state.isEditorExpanded:
                self.update_status_message("Collapse the editor before cropping")
                return
            self.exit_local_brush_mode()

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

            self._begin_direct_image_transition("crop mode activated")
            if not self.load_image_for_editing():
                self._restart_quality_decode_timer()
                return

            self._snapshot_crop_mode_geometry()
            # Keep the committed crop in the editor; the overlay is the only
            # thing that becomes draft state during this crop transaction.
            self._set_crop_overlay_box_only((0, 0, 1000, 1000))
            if hasattr(self.ui_state, "cropRotation"):
                self.ui_state.cropRotation = 0.0
            if hasattr(self.ui_state, "currentAspectRatioIndex"):
                self.ui_state.currentAspectRatioIndex = 0
            # Set isCropping to True now that reset is done
            self.ui_state.isCropRotating = False
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
        Finds source RAW files for the current stacked JPG and launches the focus stacker.
        """
        self._restack_order_token += 1
        token = self._restack_order_token
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image selected.")
            return

        current_image_path = self.image_files[self.current_index].path
        filename = current_image_path.name

        # Ensure it's a stacked JPG (".jpg" or ".jpeg", any case), using the
        # same pattern the stack-paint detection does.
        suffix = current_image_path.suffix.lower()
        if suffix not in JPG_EXTENSIONS or not filename.lower().endswith(
            f" stacked{suffix}"
        ):
            self.update_status_message("Current image is not a stacked JPG.")
            return

        # Extract base name and number, e.g., "PB210633" from "20251121-PB210633 stacked.JPG"
        match = self._STACKED_JPG_RE.search(filename)
        if not match:
            self.update_status_message("Could not parse stacked JPG filename format.")
            log.error("Could not parse stacked JPG filename: %s", filename)
            return

        base_prefix = match.group(1)  # e.g., "PB"
        base_number_str = match.group(2)  # e.g., "210633"
        base_number = int(base_number_str)

        # Get the mirror base (main photo directory) from config
        mirror_base_str = self.get_main_photo_dir().strip().strip('"')
        if not mirror_base_str:
            self._show_missing_restack_raws_dialog(
                filename=filename,
                expected_stems=self._expected_restack_raw_stems(
                    base_prefix, base_number
                ),
                search_locations=[],
                reason="The Main Photo Directory is not configured.",
            )
            log.warning("RAW mirror base (raw.mirror_base) is not set in config.")
            return

        mirror_base_dir = self._main_photo_directory()
        if mirror_base_dir is None or not mirror_base_dir.is_dir():
            self._show_missing_restack_raws_dialog(
                filename=filename,
                expected_stems=self._expected_restack_raw_stems(
                    base_prefix, base_number
                ),
                search_locations=[],
                reason=(
                    "The configured Main Photo Directory does not exist: "
                    f"{mirror_base_str}"
                ),
            )
            log.warning(
                "Configured RAW mirror base directory does not exist: %s",
                mirror_base_str,
            )
            return

        # The date structure in the RAW directory mirrors the structure relative to the mirror_base
        try:
            relative_part = current_image_path.parent.resolve().relative_to(
                mirror_base_dir
            )
        except (OSError, ValueError):
            self._show_missing_restack_raws_dialog(
                filename=filename,
                expected_stems=self._expected_restack_raw_stems(
                    base_prefix, base_number
                ),
                search_locations=[],
                reason=(
                    f"The current image folder ({current_image_path.parent}) is "
                    "not inside the configured Main Photo Directory: "
                    f"{mirror_base_dir}"
                ),
            )
            log.error(
                "Could not find relative path for '%s' from base '%s'. Check 'mirror_base' config.",
                current_image_path.parent,
                mirror_base_dir,
            )
            return

        raw_locations = self._configured_restack_raw_locations()
        search_locations = [
            (label, base_dir, base_dir / relative_part)
            for label, base_dir in raw_locations
        ]
        if not search_locations:
            self._show_missing_restack_raws_dialog(
                filename=filename,
                expected_stems=self._expected_restack_raw_stems(
                    base_prefix, base_number
                ),
                search_locations=[],
                reason="No primary or secondary RAW source directory is configured.",
            )
            return

        expected_stems = self._expected_restack_raw_stems(base_prefix, base_number)
        self.update_status_message("Finding source RAWs...")

        def _find_and_order_restack_inputs():
            found_raw_files: List[Path] = []
            found_label = ""
            found_search_dir = None
            for label, _base_dir, raw_search_dir in search_locations:
                found_raw_files = self._find_restack_raws_in_dir(
                    raw_search_dir,
                    base_prefix,
                    base_number,
                )
                if found_raw_files:
                    found_label = label
                    found_search_dir = raw_search_dir
                    break

            # Start from deterministic filename order, then replace it with a
            # near-to-far EXIF focus order when every frame has reliable data.
            found_raw_files.sort(key=lambda path: path.name.casefold())
            focus_order = None
            error = None
            if found_raw_files:
                try:
                    found_raw_files, focus_order = order_focus_stack_inputs(
                        found_raw_files
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    error = exc
            return found_raw_files, focus_order, error, found_label, found_search_dir

        def _on_order_done(future: concurrent.futures.Future) -> None:
            try:
                (
                    ordered_files,
                    focus_order,
                    error,
                    found_label,
                    found_search_dir,
                ) = future.result()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                ordered_files = []
                focus_order = None
                error = exc
                found_label = ""
                found_search_dir = None
            self._restackOrderReady.emit(
                {
                    "token": token,
                    "source_image_path": current_image_path,
                    "files": ordered_files,
                    "focus_order": focus_order,
                    "error": error,
                    "found_label": found_label,
                    "found_search_dir": found_search_dir,
                    "filename": filename,
                    "expected_stems": expected_stems,
                    "search_locations": search_locations,
                }
            )

        try:
            future = self._exif_executor.submit(_find_and_order_restack_inputs)
        except RuntimeError:
            if not self._shutting_down:
                self.update_status_message("Could not find source RAWs.")
            return
        future.add_done_callback(_on_order_done)

    @Slot(object)
    def _on_restack_order_ready(self, payload) -> None:
        """Launch a restack after background focus ordering completes."""
        if self._shutting_down or not payload:
            return
        if payload.get("token") != self._restack_order_token:
            return

        source_image_path = payload["source_image_path"]
        if (
            not self.image_files
            or self.current_index >= len(self.image_files)
            or self._key(self.image_files[self.current_index].path)
            != self._key(source_image_path)
        ):
            log.info(
                "Discarding Restack focus order because the selected image changed."
            )
            return

        found_raw_files = payload["files"]
        focus_order = payload.get("focus_order")
        error = payload.get("error")
        if not found_raw_files:
            self._show_missing_restack_raws_dialog(
                filename=payload["filename"],
                expected_stems=payload["expected_stems"],
                search_locations=payload["search_locations"],
                reason=(f"The RAW source search failed: {error}" if error else ""),
            )
            log.info(
                "No source RAWs found for %s in %s",
                payload["filename"],
                [str(location[2]) for location in payload["search_locations"]],
            )
            return
        if error is not None:
            log.error(
                "Restack focus metadata ordering failed; retaining filename order: %s",
                error,
            )
        if focus_order:
            log.info(
                "Restack inputs ordered by %s: %s",
                focus_order,
                [path.name for path in found_raw_files],
            )
        else:
            log.info(
                "Restack focus metadata was incomplete; retaining filename order: %s",
                [path.name for path in found_raw_files],
            )

        self.update_status_message(
            f"Launching focus stacker with {len(found_raw_files)} RAWs"
            + (" in EXIF focus order..." if focus_order else "...")
        )
        log.info(
            "Launching focus stacker for %s with RAWs from %s (%s): %s",
            source_image_path.name,
            payload["found_label"],
            payload["found_search_dir"],
            [str(p) for p in found_raw_files],
        )
        success = self._launch_helicon_with_files(found_raw_files)

        if success:
            # Mark as restacked on success
            today = datetime.now().strftime("%Y-%m-%d")
            meta = self.sidecar.get_metadata(source_image_path)
            meta.restacked = True
            meta.restacked_date = today
            metadata_saved = self._persist_sidecar()
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()

            if metadata_saved:
                self.update_status_message("Focus stacker launched successfully.")
        else:
            self.update_status_message("Failed to launch focus stacker.")

    def _configured_restack_raw_locations(self) -> list[tuple[str, Path]]:
        raw_source_dir = config.get("raw", "source_dir", fallback="")
        secondary_source_dir = config.get("raw", "secondary_source_dir", fallback="")
        additional_source_dirs = config.get(
            "raw", "additional_source_dirs", fallback=""
        )
        configured = [
            ("Primary", raw_source_dir),
            ("Secondary", secondary_source_dir),
        ]
        configured.extend(
            (f"Additional {index}", raw_path)
            for index, raw_path in enumerate(
                re.split(r"[;\r\n]+", additional_source_dirs), start=1
            )
        )
        locations: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for label, raw_path in configured:
            cleaned = str(raw_path or "").strip().strip('"')
            if not cleaned:
                continue
            resolved = resolve_configured_path(cleaned)
            normalized = os.path.normcase(os.path.normpath(str(resolved)))
            if normalized in seen:
                continue
            seen.add(normalized)
            locations.append((label, resolved))
        return locations

    def _expected_restack_raw_stems(
        self,
        base_prefix: str,
        base_number: int,
        *,
        max_raw_search: int = 15,
    ) -> list[str]:
        return [
            f"{base_prefix}{raw_number:06d}"
            for raw_number in range(
                base_number - 1,
                max(base_number - max_raw_search - 1, -1),
                -1,
            )
        ]

    def _find_restack_raws_in_dir(
        self,
        raw_search_dir: Path,
        base_prefix: str,
        base_number: int,
        *,
        max_raw_search: int = 15,
        extensions: Optional[set[str]] = None,
    ) -> list[Path]:
        """Consecutive frames before *base_number*, newest (highest) first."""
        if extensions is None:
            extensions = RAW_EXTENSIONS
        expected_stems = self._expected_restack_raw_stems(
            base_prefix,
            base_number,
            max_raw_search=max_raw_search,
        )
        wanted_names = {
            f"{raw_stem}{ext}".casefold()
            for raw_stem in expected_stems
            for ext in extensions
        }
        # Camera extensions are commonly uppercase. Windows path lookup hid that
        # distinction, but Linux/WSL filesystems are case-sensitive. Filter by
        # name before is_file(), so large directories cost at most one stat per
        # relevant candidate rather than one stat per entry.
        files_by_name: dict[str, Path] = {}
        try:
            for candidate in raw_search_dir.iterdir():
                folded_name = candidate.name.casefold()
                if folded_name not in wanted_names:
                    continue
                try:
                    if candidate.is_file():
                        files_by_name[folded_name] = candidate
                except OSError as exc:
                    log.debug("Could not stat Restack candidate %s: %s", candidate, exc)
        except OSError as exc:
            log.info(
                "Could not enumerate RAW search directory %s: %s",
                raw_search_dir,
                exc,
            )
            return []

        found_raw_files: list[Path] = []
        for raw_stem in expected_stems:
            found_this_number = False
            for ext in sorted(extensions):
                potential_raw = files_by_name.get(f"{raw_stem}{ext}".casefold())
                if potential_raw is not None:
                    found_raw_files.append(potential_raw)
                    found_this_number = True
                    break

            if not found_this_number and found_raw_files:
                break

        return found_raw_files

    def _show_missing_restack_raws_dialog(
        self,
        *,
        filename: str,
        expected_stems: list[str],
        search_locations: list[tuple[str, Path, Path]],
        reason: str = "",
    ) -> None:
        raw_extensions = ", ".join(sorted(ext.upper() for ext in RAW_EXTENSIONS))
        expected_preview = ", ".join(expected_stems[:5])
        if len(expected_stems) > 5:
            expected_preview += ", ..."

        location_lines = []
        location_detail_lines = []
        for label, base_dir, search_dir in search_locations:
            if search_dir.is_dir():
                status = "searched, no matching stack input RAWs found"
            else:
                status = "folder missing or drive not available"
            location_lines.append(f"- {label}: {search_dir}\n  status: {status}")
            location_detail_lines.append(
                f"{label}\n"
                f"  Source root: {base_dir}\n"
                f"  Searched folder: {search_dir}\n"
                f"  Status: {status}"
            )
        if not location_lines:
            location_lines.append("- No RAW source folders were available to search.")
            location_detail_lines.append(
                "No RAW source folders were available to search."
            )

        summary = (
            f"Stacked image: {filename}\n\n"
            "The stack input RAW files could not be found. FastStack needs those "
            "original RAW frames to reopen this stack in the configured focus "
            "stacker.\n\n"
        )
        if reason:
            summary += f"Problem:\n{reason}\n\n"
        summary += (
            "What it looked for:\n"
            f"- RAW filenames starting before the stacked image number: {expected_preview}\n"
            f"- Extensions: {raw_extensions}\n"
            "- Up to 15 previous frame numbers, stopping after the first gap once "
            "frames are found.\n\n"
            "Where it looked:\n" + "\n".join(location_lines)
        )
        details = (
            summary
            + "\n\nConfigure these folders in Settings > Restack RAW Locations. "
            "Set the Main Photo Directory to the root folder of your stacked "
            "images, and set the primary, secondary, or additional RAW source "
            "directories to folders that mirror the same date/subfolder structure."
        )
        detailed_locations = f"Stacked image: {filename}\n\n"
        if reason:
            detailed_locations += f"Problem:\n{reason}\n\n"
        detailed_locations += (
            "Search locations:\n"
            + "\n\n".join(location_detail_lines)
            + "\n\nExpected RAW stems:\n"
            + "\n".join(expected_stems)
            + "\n\nRAW extensions:\n"
            + raw_extensions
        )

        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.setWindowTitle("Stack Input RAWs Not Found")
        msg_box.setText("FastStack could not find the stack input RAW files.")
        msg_box.setInformativeText(details)
        msg_box.setDetailedText(detailed_locations)
        settings_button = msg_box.addButton(
            "Open Settings", QMessageBox.ButtonRole.ActionRole
        )
        msg_box.addButton(QMessageBox.StandardButton.Ok)
        self._exec_modal_dialog(msg_box)
        self.update_status_message("Stack input RAW files not found.", 6000)
        if msg_box.clickedButton() is settings_button:
            self.ui_state.settingsDialogRequested.emit()

    @Slot()
    def execute_crop(self):
        """Commit the crop into the live editor session without saving yet."""
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image to crop")
            return

        if not self.ui_state.isCropping:
            return

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

        if self.view_override_path:
            filepath = Path(self.view_override_path)
        else:
            filepath = self.get_active_edit_path(self.current_index)

        editor_path = self.image_editor.current_filepath
        paths_match = False
        if editor_path:
            try:
                paths_match = Path(editor_path).resolve() == Path(filepath).resolve()
            except (OSError, ValueError):
                paths_match = str(editor_path) == str(filepath)

        # Accept preview-only sessions (auto-adjust/compact editor loads):
        # reloading here would reset live edits, and the full-resolution render
        # below materializes the float master via _ensure_float_image anyway.
        has_buffers = self.image_editor.original_image is not None and (
            self.image_editor.float_image is not None
            or self.image_editor.float_preview is not None
        )
        if not paths_match or not has_buffers:
            log.debug(
                f"execute_crop reloading image due to path mismatch. Editor: {editor_path}, File: {filepath}"
            )
            if not self.image_editor.load_image(
                str(filepath),
                cached_preview=None,
                source_exif=self._capture_source_exif_for_active_image(),
            ):
                self.update_status_message("Failed to load image for cropping")
                return
            self._ensure_live_edit_session_state(force_reset=True)
        else:
            self._ensure_live_edit_session_state()

        current_rotation = float(
            self.image_editor.current_edits.get("straighten_angle", 0.0)
        )

        committed_crop_box = self._normalize_crop_box_tuple(
            self.image_editor.get_edit_value("crop_box")
        )

        # A full-image box with no prior crop and no straighten is a no-op:
        # applying it would still mark the image edited, auto-add it to the
        # batch, and report "Crop applied". A full box remains meaningful when
        # a straighten is pending (committing the box makes the rotation
        # render) or a crop is already committed (Enter confirms and exits).
        if (
            crop_box_raw == (0, 0, 1000, 1000)
            and committed_crop_box is None
            and abs(current_rotation) <= 0.001
        ):
            self.update_status_message("No crop area selected")
            return

        # The draft overlay is normalized against the image displayed when
        # crop mode was entered (the snapshot geometry), which with a
        # committed straighten is a rotated, fill-trimmed window onto the
        # source. Map it back through that geometry; a plain linear compose
        # would pick the wrong source region.
        if self._crop_mode_has_saved_geometry:
            base_crop_box = self._crop_mode_saved_crop_box
            base_angle = self._crop_mode_saved_straighten_angle
        else:
            base_crop_box = committed_crop_box
            base_angle = 0.0
        final_crop_box = self.image_editor.map_crop_draft_to_source(
            crop_box_raw, base_crop_box, base_angle
        )
        if final_crop_box is None:
            self.update_status_message("Invalid crop selection")
            return

        log.debug(
            "execute_crop: committed=%s draft=%s base=(%s, %.4f) final=%s angle=%.4f",
            committed_crop_box,
            crop_box_raw,
            base_crop_box,
            base_angle,
            final_crop_box,
            current_rotation,
        )

        self.image_editor.set_crop_box(final_crop_box)
        self.image_editor.set_edit_param("straighten_angle", current_rotation)

        # Render the committed crop from the full-resolution master and publish
        # it before clearing crop mode, so the loupe does not keep showing the
        # preview-resolution crop used while dragging the overlay.
        try:
            # Runs synchronously on the UI thread; cap at display size.
            decoded = self.image_editor.get_full_resolution_preview_data(
                max_long_edge=self._display_preview_long_edge()
            )
        except Exception:
            log.exception("execute_crop: full-resolution render failed")
            decoded = None

        if decoded is not None:
            with self._preview_lock:
                # Older preview-worker results may still be in flight from crop
                # setup/rotation. Invalidate them so they cannot overwrite this
                # full-resolution committed crop.
                self._preview_token += 1
                self._preview_pending = False
                self._publish_last_rendered_preview_locked(decoded)
                self._store_live_preview_target_dims(decoded)
                self.ui_refresh_generation += 1
                self._last_rendered_preview_index = self.current_index
                self._last_rendered_preview_gen = self.ui_refresh_generation

        self.ui_state.isCropRotating = False
        self.ui_state.isCropping = False
        self._clear_crop_mode_snapshot()
        # Do NOT assign ui_state.currentCropBox here — its setter syncs back
        # into image_editor.set_crop_box(), which would overwrite the crop we
        # just committed. The crop overlay is already hidden by
        # `visible: isCropping` in QML, and re-entering crop mode resets the
        # box inside a new crop transaction.
        self.ui_state.resetZoomPan()
        # Cropping is a real edit, so keep batch tagging in sync immediately
        # instead of waiting for a later save/navigation refresh.
        batch_saved = self._auto_add_edited_to_batch_if_enabled(filepath)
        if decoded is not None:
            self._emit_preview_accepted_side_effects()
        else:
            self._kick_preview_worker()
        if batch_saved:
            self.update_status_message("Crop applied", timeout=5000)
        # Pairs with the _begin_direct_image_transition performed when crop mode
        # was entered, which stopped the settled cover decode.
        self._restart_quality_decode_timer()
        log.info("Crop applied to live session for %s", filepath)

    @Slot()
    def auto_levels(self):
        """Calculates and applies auto levels (preview only). Returns False if skipped."""
        if self._block_auto_adjust_during_crop():
            return False
        if not self.image_files or self.current_index >= len(self.image_files):
            self.update_status_message("No image to adjust")
            return False

        t_al_start = time.perf_counter()
        active_path = self._ensure_active_image_loaded_for_auto_adjust()
        if active_path is None:
            return False
        t_al_load = time.perf_counter()
        recommendation = self._compute_auto_levels_recommendation()
        t_al_calc = time.perf_counter()

        blacks = recommendation["base_blacks"]
        whites = recommendation["base_whites"]

        # Midtone brightness: only auto-set when the user has not touched the
        # brightness slider themselves. Applied before levels so the single
        # preview kick below renders both.
        auto_brightness_delta = 0.0
        changed_brightness = False
        try:
            current_brightness = float(
                self.image_editor.get_edit_value("brightness", 0.0)
            )
        except (TypeError, ValueError):
            current_brightness = 0.0
        base_brightness = float(recommendation.get("base_brightness", 0.0))
        if abs(current_brightness) <= 0.001 and abs(base_brightness) > 0.001:
            changed_brightness = self._apply_brightness_to_editor(
                brightness=base_brightness
            )
            auto_brightness_delta = base_brightness - current_brightness

        msg = self._format_auto_levels_detail(
            p_low=recommendation["p_low"],
            p_high=recommendation["p_high"],
            blacks=blacks,
            whites=whites,
            auto_brightness_delta=auto_brightness_delta,
        )
        changed = self._apply_levels_to_editor(
            blacks=blacks,
            whites=whites,
            kick_preview=True,
        )
        if changed_brightness and not changed:
            # Levels alone did not change anything; make sure the brightness
            # nudge still reaches the preview.
            self._kick_preview_worker()
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
        changed = changed or changed_brightness

        if not changed and recommendation["noop_reason"]:
            msg = f"Auto levels: no change ({recommendation['noop_reason']})"

        self.update_status_message(f"{msg} (preview only)", timeout=9000)
        if changed and self.image_editor and self.image_editor.current_filepath:
            self._auto_add_edited_to_batch_if_enabled(
                Path(self.image_editor.current_filepath)
            )
        log.info(
            "Auto levels preview applied to %s (clip %.2f%%, str %.2f). Msg: %s",
            active_path,
            self.auto_level_threshold,
            recommendation["strength"],
            msg,
        )
        t_al_end = time.perf_counter()
        log.debug(
            "[AUTO_LEVEL] load=%dms calc=%dms apply+ui=%dms total=%dms  %s",
            int((t_al_load - t_al_start) * 1000),
            int((t_al_calc - t_al_load) * 1000),
            int((t_al_end - t_al_calc) * 1000),
            int((t_al_end - t_al_start) * 1000),
            active_path,
        )
        # Store detail message for quick_auto_levels to pick up
        self._last_auto_levels_msg = msg
        return changed

    def _seed_active_auto_adjust_state(
        self,
        *,
        include_auto_vibrance: bool = False,
    ) -> Optional[ActiveAutoAdjustState]:
        """Create transient auto-adjust state from the current loaded image."""
        if self._block_auto_adjust_during_crop():
            return None
        recommendation = self._compute_auto_levels_recommendation()
        state = self._build_active_auto_adjust_state(
            recommendation,
            include_auto_vibrance=include_auto_vibrance,
        )
        self._active_auto_adjust_state = state
        return state

    @Slot()
    def quick_auto_levels(self):
        """Apply auto levels to the live session without saving yet."""
        if self._block_auto_adjust_during_crop():
            return
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        self._clear_active_auto_adjust_state(
            "quick auto levels starts a fresh active auto-adjust state",
            clear_editor=False,
        )
        if self._ensure_active_image_loaded_for_auto_adjust() is None:
            return

        state = self._seed_active_auto_adjust_state(include_auto_vibrance=True)
        if state is None:
            self.update_status_message("Auto levels failed")
            return

        self._apply_auto_adjust_preview(state)

    @Slot()
    def quick_auto_adjust(self):
        """Apply AWB and auto-levels to the live session without saving yet."""
        if self._block_auto_adjust_during_crop():
            return
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        self._clear_active_auto_adjust_state(
            "combined auto-adjust starts a fresh active auto-adjust state",
            clear_editor=False,
        )
        if self._ensure_active_image_loaded_for_auto_adjust() is None:
            return

        # auto_white_balance() is preview-only here: it mutates the in-memory
        # editor session and status text, but does not save or append undo.
        # It does not start a render either. One render follows, with the
        # levels in it; a white-balance-only frame would be replaced before it
        # was seen, and rendering it competed with the levels analysis below
        # and pushed the frame that mattered onto the coarser drag tier.
        awb_msg = self.auto_white_balance(kick_preview=False)
        state = self._seed_active_auto_adjust_state(include_auto_vibrance=True)
        if state is None:
            if awb_msg:
                self._kick_preview_worker()
            self.update_status_message("Auto adjust failed")
            return

        self._apply_auto_adjust_preview(state, force_preview=bool(awb_msg))
        levels_msg = self._last_auto_levels_msg
        self._last_auto_levels_msg = levels_msg

        detail_parts = [msg for msg in (awb_msg, levels_msg) if msg]
        if detail_parts:
            self.update_status_message(
                "; ".join(detail_parts),
                timeout=9000,
            )

    def _ensure_or_seed_active_auto_adjust_state(
        self,
    ) -> Optional[ActiveAutoAdjustState]:
        """Reuse the live transient state or create a fresh one from current pixels."""
        if self._block_auto_adjust_during_crop():
            return None
        if self._has_valid_active_auto_adjust_state():
            return self._active_auto_adjust_state
        if self._ensure_active_image_loaded_for_auto_adjust() is None:
            return None
        return self._seed_active_auto_adjust_state()

    @Slot()
    def reduce_auto_adjust_highlights(self):
        """Darken the highlight side by one fixed step in the live session."""
        if self._block_auto_adjust_during_crop():
            return
        state = self._ensure_or_seed_active_auto_adjust_state()
        if state is None:
            return

        state.extra_highlight_steps += 1
        self._apply_auto_adjust_preview(state)

    @Slot()
    def raise_auto_adjust_whites(self):
        """Raise the white side by one fixed step in the live session."""
        if self._block_auto_adjust_during_crop():
            return
        state = self._ensure_or_seed_active_auto_adjust_state()
        if state is None:
            return

        state.extra_highlight_steps -= 1
        self._apply_auto_adjust_preview(state)

    @Slot()
    def deepen_auto_adjust_blacks(self):
        """Deepen the shadow side by one fixed step in the live session."""
        if self._block_auto_adjust_during_crop():
            return
        state = self._ensure_or_seed_active_auto_adjust_state()
        if state is None:
            return

        state.extra_black_steps += 1
        self._apply_auto_adjust_preview(state)

    @Slot()
    def raise_auto_adjust_blacks(self):
        """Raise the shadow side by one fixed step in the live session."""
        if self._block_auto_adjust_during_crop():
            return
        state = self._ensure_or_seed_active_auto_adjust_state()
        if state is None:
            return

        state.extra_black_steps -= 1
        self._apply_auto_adjust_preview(state)

    def _apply_auto_adjust_preview(
        self, state: ActiveAutoAdjustState, *, force_preview: bool = False
    ) -> None:
        """Render the current auto-adjust state into the editor/UI immediately.

        ``force_preview`` renders even when the levels themselves did not
        move, for a caller that changed something else (white balance) and
        left the render to this call.
        """
        blacks, whites = self._derive_auto_adjust_levels(state)
        changed_levels = self._apply_levels_to_editor(
            blacks=blacks,
            whites=whites,
            kick_preview=False,
        )
        changed_vibrance = self._apply_vibrance_to_editor(
            vibrance=state.base_vibrance,
        )
        changed_brightness = self._apply_brightness_to_editor(
            brightness=state.base_brightness,
        )
        if changed_levels or changed_vibrance or changed_brightness or force_preview:
            self._kick_preview_worker()
            if self.ui_state.isHistogramVisible:
                self.update_histogram()
        detail = self._format_auto_levels_detail(
            p_low=state.p_low,
            p_high=state.p_high,
            blacks=blacks,
            whites=whites,
            extra_highlight_steps=state.extra_highlight_steps,
            extra_black_steps=state.extra_black_steps,
            auto_vibrance_delta=state.auto_vibrance_delta,
            auto_brightness_delta=state.auto_brightness_delta,
        )
        self._last_auto_levels_msg = detail
        self.update_status_message(detail, timeout=9000)
        # Every quick-adjust entry point funnels through here, so this is the
        # single place that keeps batch tagging in sync with live edits.
        if self.image_editor and self.image_editor.current_filepath:
            self._auto_add_edited_to_batch_if_enabled(
                Path(self.image_editor.current_filepath)
            )

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
            self.image_editor.load_image(
                filepath, cached_preview=None, preview_only=True
            )

        # Save current_index, temporarily set to target index for auto_levels().
        # The viewer's variant override (developed/backup) belongs to the image
        # on screen, not to the batch targets: leaving it set would load the
        # viewed variant for every item and (for backups) save it over each
        # target. Clear it per item so source and target come from `index`.
        saved_index = self.current_index
        saved_override_path = self.view_override_path
        saved_override_kind = self.view_override_kind
        self.current_index = index
        self.view_override_path = None
        self.view_override_kind = None

        try:
            try:
                expected_source = self.get_active_edit_path(index)
            except (IndexError, OSError, TypeError, ValueError):
                return False

            self._last_auto_levels_msg = ""
            applied = self.auto_levels()

            if not applied:
                return False

            loaded_path = self.image_editor.current_filepath
            if not loaded_path or normalize_path_key(loaded_path) != normalize_path_key(
                expected_source
            ):
                log.warning(
                    "batch auto levels: editor loaded %s, expected %s; skipping",
                    loaded_path,
                    expected_source,
                )
                return False

            try:
                # Overrides are cleared, so this saves in place to the batch
                # item's own edit source.
                save_target_path = None
                save_result = self.image_editor.save_image_uint8_levels(
                    save_target_path=save_target_path
                )
                if save_result is None:
                    save_result = self.image_editor.save_image_uint8_white_balance(
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
                    self.sidecar,
                    metadata_path,
                    saved_edit_state=self._build_saved_edit_state_from_editor(
                        saved_path=saved_path,
                        backup_path=backup_path,
                    ),
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
                self._clear_active_auto_adjust_state(
                    "batch auto levels saved a different image",
                    clear_editor=False,
                )
                self.image_editor.clear()
                self._invalidate_decoded_path(saved_path, force=True)
                return True

            return False
        finally:
            self.current_index = saved_index
            self.view_override_path = saved_override_path
            self.view_override_kind = saved_override_kind

    # --- Batch Auto Levels ---

    batchAutoLevelsProgress = Signal(int, int)  # (current, total)
    batchAutoLevelsFinished = Signal(int, int)  # (processed, total)

    def batch_auto_levels(self):
        """Auto-level every image in the current batch, one at a time via event loop."""
        if self._block_auto_adjust_during_crop():
            return
        batch_indices = sorted(self._get_batch_indices())
        if not batch_indices:
            self.update_status_message("No images in batch.")
            return

        self._clear_active_auto_adjust_state(
            "batch auto levels replaces the current transient auto-adjust session",
            clear_editor=True,
        )

        self._batch_al_indices = batch_indices
        self._batch_al_pos = 0
        self._batch_al_processed = 0
        self._batch_al_cancelled = False
        self._batch_al_t_start = time.perf_counter()

        self.dialog_opened("batch-auto-levels")
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

        try:
            # Refresh display
            self._bump_display_generation()
            self.prefetcher.cancel_all()
            self.prefetcher.update_prefetch(self.current_index)
            self._metadata_cache_index = (-1, -1)
            self.dataChanged.emit()
            self.sync_ui_state()
            if hasattr(self, "_thumbnail_model") and self._thumbnail_model:
                self._thumbnail_model.refresh()
        finally:
            # The progress dialog is application-modal. Neither a failed refresh
            # nor a failed QML completion handler may leave global shortcuts
            # disabled for the rest of the process.
            self.dialog_closed("batch-auto-levels")
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
        """Quickly apply auto white balance to the live session without saving yet."""
        if self._block_auto_adjust_during_crop():
            return
        if not self.image_files:
            self.update_status_message("No image to adjust")
            return

        self._clear_active_auto_adjust_state(
            "quick auto white balance starts a fresh live baseline",
            clear_editor=False,
        )
        if self._ensure_active_image_loaded_for_auto_adjust() is None:
            return
        self.auto_white_balance()

    @Slot()
    def auto_white_balance(self, kick_preview: bool = True) -> Optional[str]:
        """
        Dispatcher for auto white balance. Calls the appropriate method based on
        the mode set in the config ('lab' or 'rgb').

        Returns the detail message string if a correction was applied, or None
        if no change / error. ``kick_preview=False`` applies the correction
        without starting a preview render; the caller then owns the render.
        """
        if self._block_auto_adjust_during_crop():
            return None
        mode = config.get("awb", "mode", fallback="lab")
        if mode == "lab":
            return self.auto_white_balance_lab(kick_preview=kick_preview)
        elif mode == "rgb":
            return self.auto_white_balance_legacy(kick_preview=kick_preview)
        else:
            log.error("Unknown AWB mode: %s", mode)
            self.update_status_message(f"Error: Unknown AWB mode '{mode}'")
            return None

    def auto_white_balance_legacy(self, kick_preview: bool = True) -> Optional[str]:
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

        if kick_preview:
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

    def auto_white_balance_lab(self, kick_preview: bool = True) -> Optional[str]:
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
        tint_damp = config.getfloat("awb", "tint_damp", 0.6)
        rgb_lower_bound = config.getint("awb", "rgb_lower_bound", 5)
        rgb_upper_bound = config.getint("awb", "rgb_upper_bound", 250)
        luma_lower_bound = config.getint("awb", "luma_lower_bound", 30)
        luma_upper_bound = config.getint("awb", "luma_upper_bound", 220)

        estimate = self.image_editor.estimate_auto_white_balance(
            strength=strength,
            warm_bias=warm_bias,
            tint_bias=tint_bias,
            tint_damp=tint_damp,
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

        if kick_preview:
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
        confidence = float(estimate.get("confidence", 1.0))
        log.debug(
            "[AUTO_COLOR] total=%dms  (selected=%d stride=%d neutral<=%.3f confidence=%.2f)",
            int((t_awb_end - t_awb_start) * 1000),
            selected_pixels,
            stride,
            neutrality_limit,
            confidence,
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
        elif (
            not info
            and self.stack_end_index is not None
            and self.stack_end_index == index
        ):
            info = "Stack End Marked"
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
            now = time.monotonic()
            if now - self._last_cache_stats_update < 0.1:
                return
            self._last_cache_stats_update = now
            hits = self.image_cache.hits
            misses = self.image_cache.misses
            total = hits + misses
            hit_rate = (hits / total * 100) if total > 0 else 0
            size_mb = self.image_cache.currsize / (1024 * 1024)
            # Speculative + the reserved demand decoder, matching the
            # decode_workers=N(1 demand) notation in the NAVTRACE header.
            speculative = self.prefetcher.total_decode_workers - 1
            # Governor state confirms at a glance that demand work bypasses the
            # limit, that no more than the effective limit is running while
            # constrained, and that queued work is only delayed. All counters
            # are O(1) -- Preload All may hold thousands of queued tasks.
            governor = ""
            try:
                state = self.prefetcher.constrained_mode_snapshot()
                if state["constrained"]:
                    governor = (
                        f", governed {state['running']}/{state['effective_limit']}"
                        f" [{','.join(sorted(state['reasons']))}]"
                        f" q{state['queued_interactive']}+{state['queued_preload']}"
                        f" demand {state['running_demand']}/1"
                        f" q{state['queued_demand']}"
                    )
                elif state["queued_total"] or state["running"]:
                    governor = (
                        f", spec {state['running']}/{state['effective_limit']}"
                        f" q{state['queued_interactive']}+{state['queued_preload']}"
                        f" demand {state['running_demand']}/1"
                        f" q{state['queued_demand']}"
                    )
            except Exception:
                log.debug("Prefetch governor snapshot failed", exc_info=True)
            self.ui_state.cacheStats = (
                f"Cache: {hits} hits, {misses} misses ({hit_rate:.1f}%), "
                f"{size_mb:.1f} MB, workers {speculative}+1{governor}"
            )

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

    def cleanup_recycle_bins(self) -> bool:
        """Delete all tracked recycle bins."""
        active_bins = set(self.active_recycle_bins)
        return self._purge_recycle_bins(active_bins)

    # ---- regex for reversing UUID-suffixed recycle bin names ----
    # Current format uses ``._fs_`` marker: ``{stem}._fs_{8hex}{suffix}``
    _RECYCLE_FS_RE = re.compile(r"^(.+)\._fs_([0-9a-f]{8})$", re.IGNORECASE)

    @staticmethod
    def _recycled_name_parts(recycled_path: Path) -> Optional[tuple[str, str]]:
        """Return ``(original_name, operation_tag)`` for a current-format file."""
        match = AppController._RECYCLE_FS_RE.match(recycled_path.stem)
        if match is None:
            return None
        return match.group(1) + recycled_path.suffix, match.group(2).casefold()

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
        parts = AppController._recycled_name_parts(recycled_path)
        return parts[0] if parts is not None else None

    @staticmethod
    def _restore_destination_identity(dest_dir: Path, original_name: str) -> str:
        """Normalize restore conflicts with the destination filesystem policy."""
        return normalize_path_key(dest_dir / original_name)

    def _recycle_history_timestamps(self) -> dict[str, int]:
        """Map known recycled paths to their actual in-session delete time."""
        timestamps: dict[str, int] = {}
        for action_type, action_data, timestamp in self.undo_history:
            if action_type != "delete":
                continue
            try:
                (_, jpg_bin), (_, raw_bin) = action_data
            except (TypeError, ValueError):
                continue
            timestamp_ns = int(float(timestamp) * 1_000_000_000)
            for recycled_path in (jpg_bin, raw_bin):
                if recycled_path is not None:
                    timestamps[self._key(recycled_path)] = timestamp_ns
        return timestamps

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
            Dict with restored/skipped/superseded/ambiguous/legacy counts,
            dest_dir, and bin_path.
        """
        bin_path = Path(bin_path_str).resolve()
        dest_dir = bin_path.parent
        dest_dir_str = str(dest_dir)
        result = {
            "restored_count": 0,
            "skipped_count": 0,
            "superseded_count": 0,
            "ambiguous_count": 0,
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

        history_timestamps = self._recycle_history_timestamps()

        def destination_identity(name: str) -> str:
            return self._restore_destination_identity(dest_dir, name)

        operations: dict[tuple[str, str], list[tuple[Path, str, int, bool]]] = {}
        for p in entries:
            if not p.is_file():
                continue
            parts = self._recycled_name_parts(p)
            if parts is None:
                result["legacy_remaining_count"] += 1
                continue
            original_name, operation_tag = parts
            known_timestamp = self._key(p) in history_timestamps
            timestamp_ns = history_timestamps.get(self._key(p), 0)
            operation_key = (
                operation_tag,
                self._restore_destination_identity(dest_dir, Path(original_name).stem),
            )
            operations.setdefault(operation_key, []).append(
                (p, original_name, timestamp_ns, known_timestamp)
            )

        versions: dict[
            tuple[str, str],
            list[tuple[str, list[tuple[Path, str, int, bool]], int, bool]],
        ] = {}
        raw_primary_names: dict[str, set[str]] = {}
        for operation_entries in operations.values():
            jpeg_names = {
                destination_identity(entry[1])
                for entry in operation_entries
                if Path(entry[1]).suffix.casefold() in JPG_EXTENSIONS
            }
            if len(jpeg_names) != 1:
                continue
            primary_name = next(iter(jpeg_names))
            for entry in operation_entries:
                if Path(entry[1]).suffix.casefold() in RAW_EXTENSIONS:
                    raw_primary_names.setdefault(
                        destination_identity(entry[1]), set()
                    ).add(primary_name)

        for (operation_tag, _stem), operation_entries in operations.items():
            image_entries = [
                entry
                for entry in operation_entries
                if Path(entry[1]).suffix.casefold() in (JPG_EXTENSIONS | RAW_EXTENSIONS)
            ]
            other_entries = [
                entry for entry in operation_entries if entry not in image_entries
            ]

            if image_entries:
                jpeg_names = {
                    destination_identity(entry[1])
                    for entry in image_entries
                    if Path(entry[1]).suffix.casefold() in JPG_EXTENSIONS
                }
                primary_names = jpeg_names
                if not primary_names:
                    associated_primary_names = {
                        primary_name
                        for entry in image_entries
                        for primary_name in raw_primary_names.get(
                            destination_identity(entry[1]), set()
                        )
                    }
                    primary_names = associated_primary_names or {
                        destination_identity(entry[1]) for entry in image_entries
                    }
                if len(primary_names) != 1:
                    result["ambiguous_count"] += len(image_entries)
                    log.warning(
                        "Leaving recycle operation %s ambiguous: multiple primary names",
                        operation_tag,
                    )
                else:
                    family = ("image", next(iter(primary_names)))
                    rank = max(entry[2] for entry in image_entries)
                    all_known = all(entry[3] for entry in image_entries)
                    versions.setdefault(family, []).append(
                        (operation_tag, image_entries, rank, all_known)
                    )

            for entry in other_entries:
                family = ("file", destination_identity(entry[1]))
                versions.setdefault(family, []).append(
                    (operation_tag, [entry], entry[2], entry[3])
                )

        selected_operations: list[
            tuple[str, list[tuple[Path, str, int, bool]], int, bool]
        ] = []
        for family_versions in versions.values():
            if len(family_versions) > 1 and not all(
                version[3] for version in family_versions
            ):
                ambiguous_entries = sum(len(version[1]) for version in family_versions)
                result["ambiguous_count"] += ambiguous_entries
                log.warning(
                    "Leaving %d cross-session recycled files ambiguous; no "
                    "portable deletion chronology is available",
                    ambiguous_entries,
                )
                continue

            newest = max(
                family_versions,
                key=lambda version: (version[2], version[0]),
            )
            selected_operations.append(newest)
            for version in family_versions:
                if version is newest:
                    continue
                result["superseded_count"] += len(version[1])
                for older_path, _name, _timestamp, _known in version[1]:
                    log.info(
                        "Leaving older recycled version in place: %s",
                        older_path.name,
                    )

        # Distinct primary names (foo.jpg and foo.jpeg) remain separate
        # families. If their selected operations both contain the same RAW
        # destination, restoring either RAW would silently pair it with the
        # other operation. Leave only that conflicting member in the bin.
        destination_operations: dict[str, set[str]] = {}
        for operation_tag, operation_entries, _rank, _known in selected_operations:
            for _path, original_name, _timestamp, _entry_known in operation_entries:
                destination_operations.setdefault(
                    destination_identity(original_name), set()
                ).add(operation_tag)
        conflicting_destinations = {
            destination
            for destination, tags in destination_operations.items()
            if len(tags) > 1
        }

        selected_entries: list[tuple[Path, str, int, bool]] = []
        for _tag, operation_entries, _rank, _known in selected_operations:
            for entry in operation_entries:
                if destination_identity(entry[1]) in conflicting_destinations:
                    result["ambiguous_count"] += 1
                    log.warning(
                        "Leaving conflicting paired destination in recycle bin: %s",
                        entry[1],
                    )
                    continue
                selected_entries.append(entry)

        # Stable destination order keeps error/report ordering deterministic too.
        selected_entries.sort(key=lambda item: (item[1].casefold(), item[0].name))
        for p, original_name, _timestamp_ns, _known_timestamp in selected_entries:
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


def _directory_has_images(directory: Path) -> bool:
    """Return True if ``directory`` directly contains at least one image file.

    Stops at the first match so it stays cheap on large folders.
    """
    image_exts = JPG_EXTENSIONS | RAW_EXTENSIONS
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if os.path.splitext(entry.name)[1].lower() in image_exts:
                    try:
                        if entry.is_file():
                            return True
                    except OSError:
                        continue
    except OSError:
        return False
    return False


def _prompt_reopen_sessions(records):
    """Ask the user which crash-survivor folders to reopen.

    ``records`` are stale session dicts (see ``SessionRegistry.scan_stale``).
    Returns the selected session records, or an empty list if the user opts to
    skip (cancel / "Open default instead").

    This modal deliberately does not use AppController's gate: it runs before
    the controller, QML engine, main window, event filter, and application
    shortcuts exist, so there is no FastStack action behind it to suppress.
    """
    dialog = QDialog()
    dialog.setWindowTitle("Resume FastStack sessions")
    layout = QVBoxLayout(dialog)
    layout.addWidget(
        QLabel("FastStack didn't shut down cleanly. Reopen these folders?")
    )

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    container = QWidget()
    container_layout = QVBoxLayout(container)
    checkboxes = []
    for rec in records:
        directory = rec.get("dir", "")
        checkbox = QCheckBox(directory)
        checkbox.setChecked(True)
        container_layout.addWidget(checkbox)
        checkboxes.append((checkbox, rec))
    container_layout.addStretch(1)
    scroll.setWidget(container)
    layout.addWidget(scroll)

    buttons = QDialogButtonBox()
    open_btn = buttons.addButton(
        "Reopen Selected", QDialogButtonBox.ButtonRole.AcceptRole
    )
    buttons.addButton("Open default instead", QDialogButtonBox.ButtonRole.RejectRole)
    open_btn.setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    dialog.resize(520, 320)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return []

    return [rec for checkbox, rec in checkboxes if checkbox.isChecked()]


_dpi_notice: Optional[QMessageBox] = None


def _warn_dpi_stuck() -> None:
    """Tell the user a restart is needed when the DPI repair did not take.

    Shown at most once per run, and non-modally: the interface is drawn too
    small at this point but still usable, so this must not block work. The
    reference is kept because a QMessageBox that is only shown (not exec'd)
    is collected as soon as it goes out of scope.
    """
    global _dpi_notice
    if _dpi_notice is not None:
        return
    log.error("DPI repair did not take effect; advising restart")
    _dpi_notice = QMessageBox(
        QMessageBox.Icon.Warning,
        "FastStack Display Scaling",
        "Windows did not report this monitor's scaling to FastStack, so the "
        "interface is being drawn too small.\n\nThis usually happens after "
        "waking the computer from sleep. Restarting FastStack fixes it.",
    )
    _dpi_notice.setStandardButtons(QMessageBox.StandardButton.Ok)
    _dpi_notice.setModal(False)
    _dpi_notice.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
    _dpi_notice.show()


def _make_sigint_close_handler(app, window_holder):
    """Build a SIGINT handler that honours the vetoable window-close path.

    Ctrl-C used to call ``app.quit()`` directly, which skips
    ``ApplicationWindow.onClosing`` and therefore skips
    ``prepare_for_app_close()``. ``aboutToQuit`` can retry a failed save but
    cannot cancel the quit, so unsaved edits could be lost (FS-P1-001).

    ``window_holder`` is a list that main() appends the main window to once
    QML has loaded. Until then there is nothing to veto and nothing to save,
    so an early Ctrl-C still terminates immediately instead of being
    swallowed.
    """

    def _handle_sigint(*_args):
        window = window_holder[0] if window_holder else None
        if window is not None:
            try:
                # Runs onClosing -> prepare_for_app_close(); a rejected close
                # leaves the app running, exactly like clicking the X.
                window.close()
                return
            except Exception:
                log.exception("SIGINT: window close failed; quitting directly")
        app.quit()

    return _handle_sigint


def _unresolved_shutdown_persistence(controller: AppController) -> bool:
    """Whether accepted user data still awaits its final shutdown outcome."""
    if controller._pending_save_recovery:
        return True
    if controller._pending_edit_save_requests or controller._saves_in_flight:
        return True
    sidecar = getattr(controller, "sidecar", None)
    return bool(
        getattr(controller, "_shutting_down", False)
        and sidecar is not None
        and sidecar.dirty
        and not controller._shutdown_final_sidecar_attempted
    )


def _shutdown_force_exit_allowed(controller: AppController) -> bool:
    """Return False while destructive work or persistence remains unresolved."""
    critical_io_active = bool(controller._critical_user_data_state())
    unresolved_delete_jobs = bool(controller._pending_delete_jobs)
    unresolved_persistence = _unresolved_shutdown_persistence(controller)
    return not any((critical_io_active, unresolved_delete_jobs, unresolved_persistence))


def main(
    image_dir: Optional[str] = None,
    debug: bool = False,
    debug_cache: bool = False,
    debug_cache_trace: bool = False,
    debug_thumb_timing: bool = False,
    debug_thumb_trace: bool = False,
    start_in_loupe: bool = False,
    restore_path: Optional[str] = None,
    restore_index: Optional[int] = None,
):
    """FastStack Application Entry Point"""
    global _debug_mode, _debug_thumb_timing, _debug_thumb_trace
    global _wayland_drag_monitor
    _debug_mode = debug
    _debug_thumb_timing = debug_thumb_timing
    _debug_thumb_trace = debug_thumb_trace

    # The per-stage trace level is a strict superset of the cache summary level.
    if debug_cache_trace:
        debug_cache = True

    t0 = time.perf_counter()
    # Base cache diagnostics are deliberately concise. The explicit trace and
    # the other debug modes retain verbose per-stage records.
    debug_requested = debug or debug_cache or debug_thumb_timing or debug_thumb_trace
    verbose_debug = (
        debug or debug_cache_trace or debug_thumb_timing or debug_thumb_trace
    )
    log_file = setup_logging(verbose_debug, diagnostic=debug_requested)
    if debug_requested:
        log.info("Startup: after setup_logging: %.3fs", time.perf_counter() - t0)
    log.info("Starting FastStack")
    _wayland_drag_monitor = install_wayland_drag_monitor()

    os.environ["QT_QUICK_CONTROLS_STYLE"] = "Material"
    app_qml_dir = faststack_qml_dir()
    qt_qml_dir = pyside_qml_dir()
    qml_import_paths = [str(app_qml_dir)]
    if qt_qml_dir is not None:
        qml_import_paths.append(str(qt_qml_dir))
    existing_qml_import_path = os.environ.get("QML2_IMPORT_PATH")
    if existing_qml_import_path:
        qml_import_paths.append(existing_qml_import_path)
    os.environ["QML2_IMPORT_PATH"] = os.pathsep.join(qml_import_paths)

    app = QApplication(
        sys.argv
    )  # QApplication is correct for desktop apps with widgets

    # PyInstaller's windowed Windows build has no stdout/stderr console. Make
    # debug mode discoverable when launched by double-clicking the executable.
    if debug_requested and (sys.stdout is None or sys.stderr is None):
        # Pre-controller modal boundary: no QML engine, main window, keybinder,
        # or AppController exists yet, so application actions cannot fire behind
        # this dialog. Controller-owned native dialogs use _exec_modal_dialog.
        if log_file is not None:
            if os.name == "nt":
                # PowerShell single-quoted strings escape apostrophes by
                # doubling them. LiteralPath also avoids wildcard expansion.
                quoted_log_file = str(log_file).replace("'", "''")
                tail_command = f"Get-Content -LiteralPath '{quoted_log_file}' -Wait"
            else:
                tail_command = f"tail -f {shlex.quote(str(log_file))}"
            message = (
                "FastStack is running without a console, so debug output is "
                f"being written to:\n\n{log_file}\n\n"
                "You can view it while FastStack is running with this command:\n\n"
                f"{tail_command}"
            )
        else:
            tail_command = ""
            message = (
                "FastStack is running without a console, and no log file "
                "could be created."
            )
        debug_message_box = QMessageBox(
            QMessageBox.Icon.Information, "FastStack Debug Logging", message
        )
        if tail_command:
            copy_button = debug_message_box.addButton(
                "Copy command", QMessageBox.ButtonRole.ActionRole
            )
            copy_button.clicked.connect(
                lambda: QApplication.clipboard().setText(tail_command)
            )
        debug_message_box.addButton(QMessageBox.StandardButton.Ok)
        debug_message_box.exec()

    # Enable Ctrl-C to terminate the application. Route it through the main
    # window's close path once that window exists so a dirty editor session
    # can still veto the exit (FS-P1-001).
    import signal

    sigint_window_holder: list = []
    signal.signal(signal.SIGINT, _make_sigint_close_handler(app, sigint_window_holder))
    # Ensure Python's signal handler runs (Qt blocks main thread)
    timer = QTimer()
    timer.start(500)  # Check for signals every 500ms
    timer.timeout.connect(lambda: None)

    if debug_requested:
        log.info("Startup: after QApplication: %.3fs", time.perf_counter() - t0)

    # Windows shells escape a trailing backslash before the closing quote
    # (e.g. `faststack "C:\dir\"`), leaking a literal double-quote into argv so
    # the path arrives as `C:\dir"`. Double-quotes are never valid in a Windows
    # path, so strip stray quotes and surrounding whitespace defensively rather
    # than rejecting an otherwise-correct directory. This is gated to Windows:
    # on POSIX, quotes and leading/trailing spaces are legal in directory names.
    if image_dir and os.name == "nt":
        image_dir = image_dir.strip().strip('"').strip()

    if not image_dir:
        image_dir_str = ""
        recovery_prompt_rejected = False

        # Crash/reboot recovery: offer to reopen folders that were open when a
        # previous run died without a clean shutdown. An explicit CLI folder
        # skips this entirely (handled by the outer branch).
        try:
            stale_records, stale_paths = SessionRegistry.scan_stale()
        except Exception as e:
            log.warning("Failed to scan for previous sessions: %s", e)
            stale_records, stale_paths = [], []

        # Session files whose folder the user asked for but which we failed to
        # actually reopen. These must survive pruning so the next launch can
        # offer them again (FS-P1-006).
        unconsumed_paths: set = set()

        if stale_records:
            chosen = _prompt_reopen_sessions(stale_records)
            if chosen:
                first_record = chosen[0]
                first_dir = first_record.get("dir", "")
                first_grid = first_record.get("grid")
                restore_path = first_record.get("path")
                restore_index = first_record.get("index")
                # Reopen the rest as separate windows (one process each).
                for other_record in chosen[1:]:
                    spawned = respawn_for_directory(
                        other_record.get("dir", ""),
                        other_record.get("grid"),
                        other_record.get("path"),
                        other_record.get("index"),
                    )
                    if not spawned:
                        record_path = other_record.get("_path")
                        if record_path:
                            unconsumed_paths.add(str(record_path))
                            log.warning(
                                "Keeping session file %s: respawn for %s failed",
                                record_path,
                                other_record.get("dir", ""),
                            )
                image_dir_str = first_dir
                # Restore the first folder's view mode in this process.
                if first_grid is False:
                    start_in_loupe = True
            else:
                recovery_prompt_rejected = True

        # Consume stale records and orphan files: everything the user declined,
        # duplicate records for an already-handled folder, and corrupt/dead
        # orphans are all prunable. Only records whose respawn failed are kept.
        prunable = [p for p in stale_paths if str(p) not in unconsumed_paths]
        if prunable:
            SessionRegistry.prune(prunable)

        # A configured default directory takes precedence over resuming the
        # last folder; the last folder is only used when no default is set
        # (or it no longer exists) and no recovery prompt was declined.
        if not image_dir_str:
            default_dir = config.get("core", "default_directory")
            if default_dir and default_dir.strip():
                # Same parsing as the other configured directories: strips
                # quotes, expands %VARS%/~, maps Windows drives under WSL.
                default_dir_path = resolve_configured_path(default_dir)
                if default_dir_path.is_dir():
                    image_dir_str = str(default_dir_path.resolve())
                else:
                    log.warning(
                        "Default directory %s not found; ignoring it", default_dir
                    )
        if not image_dir_str and not recovery_prompt_rejected:
            last_dir = config.get("core", "last_directory")
            if last_dir:
                last_dir_path = Path(last_dir).expanduser()
                if (
                    last_dir_path.is_absolute()
                    and last_dir_path.is_dir()
                    and _directory_has_images(last_dir_path)
                ):
                    image_dir_str = str(last_dir_path.resolve())
                else:
                    log.info(
                        "Not resuming last directory %s: missing or has no images",
                        last_dir,
                    )
        if not image_dir_str:
            log.warning(
                "No image directory provided and no default directory set. Opening directory selection dialog."
            )
            # This is the same pre-controller boundary as the recovery/debug
            # prompts above; there are no FastStack shortcuts to gate yet.
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

    image_dir_path = image_dir_path.expanduser()
    if not image_dir_path.is_dir():
        print(f"\nDirectory not found: {image_dir_path}\n")
        # Show which part of the path exists to help the user spot the typo
        check = image_dir_path
        while check != check.parent:
            if check.exists():
                print(f"  Closest existing path: {check}")
                break
            check = check.parent
        print("\nUsage: faststack [--loupe] <directory>")
        sys.exit(1)
    image_dir_path = image_dir_path.resolve()
    app.setOrganizationName("FastStack")
    app.setOrganizationDomain("faststack.dev")
    app.setApplicationName("FastStack")

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(app_qml_dir))
    if qt_qml_dir is not None:
        engine.addImportPath(str(qt_qml_dir))
        qt5compat_path = qt_qml_dir / "Qt5Compat"
        if qt5compat_path.is_dir():
            engine.addImportPath(str(qt5compat_path))
    engine.addImportPath("qrc:/qt-project.org/imports")

    controller = AppController(
        image_dir=image_dir_path,
        engine=engine,
        debug_cache=debug_cache,
        debug_cache_trace=debug_cache_trace,
        debug_thumb_timing=debug_thumb_timing,
        debug_thumb_trace=debug_thumb_trace,
        start_in_loupe=start_in_loupe,
        app_start_t=t0,
        restore_path=restore_path,
        restore_index=restore_index,
    )
    if debug_requested:
        log.info("Startup: after AppController: %.3fs", time.perf_counter() - t0)
    image_provider = ImageProvider(controller)
    engine.addImageProvider("provider", image_provider)
    # Register thumbnail provider for grid view
    engine.addImageProvider("thumbnail", controller._thumbnail_provider)

    # Actions -> iNaturalist upload: its controller, card thumbnails and the
    # OpenStreetMap tile provider for the location picker.
    inat_upload = INatUploadController(
        config,
        parent=controller,
        user_agent=f"FastStack/{get_current_version()} (+https://github.com/AlanRockefeller/faststack)",
    )
    controller._inat_upload = inat_upload
    inat_upload.uploadFinished.connect(controller._on_inat_upload_finished)
    inat_photo_provider = CardPhotoProvider(inat_upload.photo_path)
    inat_upload.release_photo_cache = inat_photo_provider.clear_large
    engine.addImageProvider("inatphoto", inat_photo_provider)
    engine.addImageProvider("osmtile", TileImageProvider(inat_upload.tile_fetcher))
    engine.addImageProvider(
        "inatobs", ObservationThumbProvider(inat_upload.observation_thumb)
    )

    # Expose controller and UI state to QML
    context = engine.rootContext()
    context.setContextProperty("uiState", controller.ui_state)
    context.setContextProperty("controller", controller)
    context.setContextProperty("thumbnailModel", controller._thumbnail_model)
    context.setContextProperty("inatUpload", inat_upload)

    if debug_requested:
        log.info(
            "Startup: after QML providers+context: %.3fs", time.perf_counter() - t0
        )

    qml_file = app_qml_dir / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_file)))
    if debug_requested:
        log.info("Startup: after engine.load(QML): %.3fs", time.perf_counter() - t0)

    if not engine.rootObjects():
        log.error("Failed to load QML.")
        sys.exit(-1)

    # Connect key events from the main window
    main_window = engine.rootObjects()[0]
    controller.main_window = main_window
    main_window.installEventFilter(controller)
    # From here on Ctrl-C goes through the vetoable close path.
    sigint_window_holder.append(main_window)

    # Qt's Windows plugin can cache a 96 DPI fallback for a monitor it could
    # not read (seen after resuming from suspend), which leaves the whole UI
    # drawn at 1:1 on a scaled display. See faststack/util/win_dpi.py.
    dpi_watchdog = start_dpi_watchdog(app)
    if dpi_watchdog is not None:
        dpi_watchdog.repairFailed.connect(_warn_dpi_stuck)

    # Frame swaps also release the held-navigation presentation gate, so this
    # lightweight connection is required in normal (non-diagnostic) runs.
    if hasattr(main_window, "frameSwapped"):
        try:
            main_window.frameSwapped.connect(
                controller._capture_frame_swap,
                Qt.ConnectionType.DirectConnection,
            )
            controller._frame_swapped_connected = True
        except Exception:
            log.debug("Could not connect frameSwapped", exc_info=True)

    if hasattr(main_window, "afterSynchronizing"):
        try:
            main_window.afterSynchronizing.connect(
                controller._capture_frame_sync,
                Qt.ConnectionType.DirectConnection,
            )
            controller._frame_sync_connected = True
        except Exception:
            log.debug("Could not connect afterSynchronizing", exc_info=True)

    # A source is credited only after it was Ready for a render-thread scene
    # graph synchronization and the synchronized frame was subsequently
    # swapped. This avoids attributing an already-rendering old frame to a
    # source whose Image.Ready signal happened just before that old swap.
    if debug_cache:
        if hasattr(main_window, "frameSwapped"):
            if not controller._frame_sync_connected:
                log.warning(
                    "[NAVTRACE] render synchronization signal unavailable; "
                    "presentation timings will use swap-after-Ready fallback"
                )
        else:
            log.warning(
                "[NAVTRACE] frame swap signal unavailable; presentation timings "
                "will stop at Image.Ready"
            )
        present_mode = (
            "scene_sync+swap"
            if controller._frame_sync_connected and controller._frame_swapped_connected
            else (
                "swap_after_ready"
                if controller._frame_swapped_connected
                else "image_ready_fallback"
            )
        )
        log.info(
            "[NAVTRACE] enabled schema=2 mode=%s correlation=seq+generation "
            "presentation=%s navigation_fps=%.1f repeat_delay=%dms "
            "pacing=%s held_quality=%s fast_decode_max=%dpx "
            "decode_workers=%d(1 demand) constrained_speculative=%d "
            "input_settle=%dms summary_deadline=%dms",
            "trace" if debug_cache_trace else "summary",
            present_mode,
            controller._navigation_rate_fps,
            controller._navigation_repeat_delay_ms,
            "adaptive" if controller._navigation_adaptive_pacing else "fixed",
            controller._held_navigation_quality,
            controller.prefetcher.fast_decode_max_dimension,
            controller.prefetcher.total_decode_workers,
            controller.prefetcher.governor.constrained_limit,
            controller._NAV_SETTLE_MS,
            controller._NAV_SUMMARY_DEADLINE_MS,
        )

    # Defer heavy loading long enough for the mapped QML window to paint first.
    # A 0ms timer can run before the first frame is drawn, making slow folder
    # scans look like the window itself is delayed.
    QTimer.singleShot(
        50,
        lambda: controller.load(skip_thumbnail_refresh=start_in_loupe),
    )
    QTimer.singleShot(2000, controller.maybe_check_for_updates)
    if debug_requested:
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
        watchdog = {"timer": None, "cancelled": False}

        def emergency_exit_check():
            if watchdog["cancelled"]:
                return
            critical_state = controller._critical_user_data_state()
            pending_delete_ids = list(controller._pending_delete_jobs)
            if not _shutdown_force_exit_allowed(controller):
                log.critical(
                    "Shutdown timeout expired during critical user-data I/O; "
                    "continuing to wait. Active operations: %s; unresolved "
                    "delete jobs: %s",
                    critical_state,
                    pending_delete_ids,
                )
                extended = threading.Timer(30.0, emergency_exit_check)
                extended.daemon = True
                watchdog["timer"] = extended
                extended.start()
                return
            log.critical("Shutdown timeout expired with no active data commit; exiting")
            os._exit(1)

        killer = threading.Timer(7.0, emergency_exit_check)
        killer.daemon = True
        watchdog["timer"] = killer
        killer.start()

        try:
            # After 4s, dump stacks to stderr so we can see what's hung just
            # before the kill. Windowed (console=False) builds have no stderr,
            # and faulthandler raises in that case; never let diagnostics
            # prevent the real shutdown work below.
            if sys.stderr is not None:
                try:
                    faulthandler.dump_traceback_later(4.0, repeat=False)
                except (RuntimeError, ValueError, OSError) as e:
                    log.debug("faulthandler dump unavailable: %s", e)

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
            watchdog["cancelled"] = True
            active_watchdog = watchdog.get("timer")
            if active_watchdog is not None:
                active_watchdog.cancel()

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
        "--debugcache",
        action="store_true",
        help="Log concise navigation/cache summaries and invariant violations",
    )
    parser.add_argument(
        "--debugcache-trace",
        action="store_true",
        help="Enable per-image/per-stage navigation trace records (implies --debugcache)",
    )
    parser.add_argument(
        "--loupe",
        action="store_true",
        help="Start directly in loupe view and skip initial thumbnail model refresh",
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
    parser.add_argument("--restore-path", default="", help=argparse.SUPPRESS)
    parser.add_argument(
        "--restore-index",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.debug_thumbtiming or args.debug_thumbtrace:
        args.debug = True
    if args.debugcache_trace:
        args.debugcache = True
    main(
        image_dir=args.image_dir,
        debug=args.debug,
        debug_cache=args.debugcache,
        debug_cache_trace=args.debugcache_trace,
        debug_thumb_timing=args.debug_thumbtiming,
        debug_thumb_trace=args.debug_thumbtrace,
        start_in_loupe=args.loupe,
        restore_path=args.restore_path or None,
        restore_index=args.restore_index,
    )


if __name__ == "__main__":
    cli()
