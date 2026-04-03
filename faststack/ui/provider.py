"""QML Image Provider and application state bridge."""

import collections
import logging
import threading
from pathlib import Path

from PySide6.QtCore import Property, QObject, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from faststack.config import config
from faststack.imaging.optional_deps import HAS_OPENCV

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
        self._app_controller = app_controller  # Backward compatibility alias
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

    def requestImage(self, id: str, size: object, requestedSize: object) -> QImage:
        """Handles image requests from QML."""
        import time

        _debug = getattr(self.app_controller, "debug_cache", False)
        if _debug:
            _t_start = time.perf_counter()
            print(f"[DBGCACHE] {_t_start*1000:.3f} requestImage: START id={id}")

        if not id:
            return self.placeholder

        try:
            # Handle mask overlay requests
            if id.startswith("mask_overlay/"):
                overlay = getattr(
                    self.app_controller.ui_state, "_darken_overlay_image", None
                )
                if overlay is not None:
                    return overlay
                return self._transparent

            # Parse index and optional generation
            parts = id.split("/")
            index = int(parts[0])
            gen = int(parts[1]) if len(parts) > 1 else None

            # If editor is open, use the background-rendered preview buffer
            # BUT only if the requested index matches the currently edited index!
            # AND the generation matches (to avoid stale frames during rotation/param changes)
            # FIX: If zoomed in, force full-res image instead of low-res preview

            use_editor_preview = (
                self.app_controller.ui_state.isEditorOpen
                and index == self.app_controller.current_index
                and not self.app_controller.ui_state.isZoomed
                and self.app_controller._last_rendered_preview is not None
                and getattr(self.app_controller, "_last_rendered_preview_index", None)
                == index
                and (
                    gen is None
                    or getattr(self.app_controller, "_last_rendered_preview_gen", None)
                    == gen
                )
            )

            if _debug:
                _t_get = time.perf_counter()

            image_data = (
                self.app_controller._last_rendered_preview
                if use_editor_preview
                else self.app_controller.get_decoded_image(index)
            )

            if _debug:
                _t_got = time.perf_counter()
                print(
                    f"[DBGCACHE] {_t_got*1000:.3f} requestImage: got image_data in {(_t_got - _t_get)*1000:.2f}ms"
                )

            if image_data:
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

                # Detach from Python buffer to prevent ownership issues and force proper texture upload
                # OPTIMIZATION: Only do this expensive copy when serving the live editor preview,
                # where we need to detach from the shared memory buffer that might change.
                # For standard browsing/prefetch, the buffer is stable enough.
                if (
                    self.app_controller.ui_state.isEditorOpen
                    and index == self.app_controller.current_index
                ):
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
                        # Create sRGB color space using constructor with NamedColorSpace enum
                        cs = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
                        qimg.setColorSpace(cs)
                        log.debug("Applied sRGB color space to image")
                    except (RuntimeError, ValueError) as e:
                        log.warning(f"Failed to set color space: {e}")
                elif color_mode == "icc":
                    log.debug(
                        "ICC mode: skipping Qt color space (pixels already in monitor space)"
                    )

                if _debug:
                    _t_end = time.perf_counter()
                    print(
                        f"[DBGCACHE] {_t_end*1000:.3f} requestImage: DONE id={id} total={(_t_end - _t_start)*1000:.2f}ms"
                    )

                # Buffer is now safe to release (handled by copy), but original_buffer ref in Python object stays
                # We don't need to manually attach original_buffer to qimg anymore since we copied.
                return qimg

        except (ValueError, IndexError) as e:
            log.error(f"Invalid image ID requested from QML: {id}. Error: {e}")

        return self.placeholder


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
    resetZoomPanRequested = Signal()  # Signal to tell QML to reset zoom/pan
    absoluteZoomRequested = Signal(
        float
    )  # New: Request absolute zoom level (1.0, 2.0, etc.)
    stackSummaryChanged = Signal()  # Signal for stack summary updates
    filterStringChanged = Signal()  # Signal for filter string updates
    colorModeChanged = Signal()  # Signal for color mode updates
    saturationFactorChanged = Signal()  # Signal for saturation factor updates
    awbModeChanged = Signal()
    awbStrengthChanged = Signal()
    awbWarmBiasChanged = Signal()
    awbTintBiasChanged = Signal()
    awbLumaLowerBoundChanged = Signal()
    awbLumaUpperBoundChanged = Signal()
    awbRgbLowerBoundChanged = Signal()
    awbRgbUpperBoundChanged = Signal()
    default_directory_changed = Signal(str)
    currentDirectoryChanged = Signal()  # Signal when working directory changes
    isStackedJpgChanged = Signal()  # New signal for isStackedJpg
    autoLevelClippingThresholdChanged = Signal(float)
    autoLevelStrengthChanged = Signal(float)
    autoLevelStrengthAutoChanged = Signal(bool)
    # Image Editor Signals
    is_editor_open_changed = Signal(bool)
    editorImageChanged = (
        Signal()
    )  # New signal for when the image loaded in editor changes
    is_cropping_changed = Signal(bool)

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
    anySliderPressedChanged = Signal(bool)
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
    saveBehaviorMessageChanged = Signal()  # Signal for save behavior message updates
    isSavingChanged = Signal(bool)  # Signal for save operation in progress
    batchAutoLevelsProgressChanged = Signal()
    batchAutoLevelsActiveChanged = Signal()

    # Variant badges
    variantBadgesChanged = Signal()
    variantSaveHintChanged = Signal()

    def __init__(self, app_controller, clock_func=None):
        super().__init__()
        self.app_controller = app_controller
        self._app_controller = app_controller  # Backward compatibility alias
        self._clock = clock_func or (lambda: __import__("time").monotonic())
        self._last_prefetch_data = None  # (startIndex, endIndex, maxCount)
        self._last_prefetch_time = 0
        self._is_preloading = False
        self._preload_progress = 0
        # 1 = light, 0 = dark (controller will overwrite this on startup)
        self._theme = 1
        self._status_message = ""  # New private variable for status message
        # Image Editor State
        self._is_editor_open = False
        self._is_cropping = False
        self._is_histogram_visible = False
        self._histogram_data = {}  # Will be a dict with 'r', 'g', 'b' arrays
        self._brightness = 0.0
        self._contrast = 0.0
        self._saturation = 0.0
        self._white_balance_by = 0.0
        self._white_balance_mg = 0.0
        self._current_crop_box = [0, 0, 1000, 1000]
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
        self._any_slider_pressed = False
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
            self.app_controller.editSourceModeChanged.connect(
                lambda _: self.metadataChanged.emit()
            )  # Also update metadata binding if needed

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
        return f"image://provider/{self.app_controller.current_index}/{self.app_controller.ui_refresh_generation}"

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
        if (
            not self.app_controller.image_files
            or self.app_controller.current_index >= len(self.app_controller.image_files)
        ):
            return False
        return self.app_controller.image_files[
            self.app_controller.current_index
        ].has_raw

    @Property(bool, notify=metadataChanged)
    def hasWorkingTif(self):
        if (
            not self.app_controller.image_files
            or self.app_controller.current_index >= len(self.app_controller.image_files)
        ):
            return False
        return self.app_controller.image_files[
            self.app_controller.current_index
        ].has_working_tif

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
        summary = f"Found {len(self.app_controller.stacks)} stacks:\n\n"
        for i, (start, end) in enumerate(self.app_controller.stacks):
            count = end - start + 1
            summary += f"Stack {i + 1}: {count} photos (indices {start}-{end})\n"
        return summary

    @Property(str, notify=saveBehaviorMessageChanged)
    def saveBehaviorMessage(self):
        """Returns a string describing what files will be affected by saving."""
        if not hasattr(self.app_controller, "current_edit_source_mode"):
            return ""

        if self.app_controller.current_edit_source_mode == "raw":
            return "Editing: RAW (writes working .tif + creates -developed.jpg; original JPG untouched)"
        else:
            return "Editing: JPEG (will overwrite JPG)"

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self):
        return self._status_message

    @statusMessage.setter
    def statusMessage(self, value: str):
        if self._status_message != value:
            self._status_message = value
            self.statusMessageChanged.emit()

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

    @Property(str, notify=colorModeChanged)
    def colorMode(self):
        """Returns the current color mode."""
        return self.app_controller.get_color_mode()

    @Property(float, notify=saturationFactorChanged)
    def saturationFactor(self):
        """Returns the current saturation factor."""
        return self.app_controller.get_saturation_factor()

    @Property(str, notify=awbModeChanged)
    def awbMode(self):
        return self.app_controller.get_awb_mode()

    @awbMode.setter
    def awbMode(self, mode: str):
        self.app_controller.set_awb_mode(mode)
        self.awbModeChanged.emit()

    @Property(float, notify=awbStrengthChanged)
    def awbStrength(self):
        return self.app_controller.get_awb_strength()

    @awbStrength.setter
    def awbStrength(self, value: float):
        self.app_controller.set_awb_strength(value)
        self.awbStrengthChanged.emit()

    @Property(int, notify=awbWarmBiasChanged)
    def awbWarmBias(self):
        return self.app_controller.get_awb_warm_bias()

    @awbWarmBias.setter
    def awbWarmBias(self, value: int):
        self.app_controller.set_awb_warm_bias(value)
        self.awbWarmBiasChanged.emit()

    @Property(int, notify=awbTintBiasChanged)
    def awbTintBias(self):
        return self.app_controller.get_awb_tint_bias()

    @awbTintBias.setter
    def awbTintBias(self, value: int):
        self.app_controller.set_awb_tint_bias(value)
        self.awbTintBiasChanged.emit()

    @Property(int, notify=awbLumaLowerBoundChanged)
    def awbLumaLowerBound(self):
        return self.app_controller.get_awb_luma_lower_bound()

    @awbLumaLowerBound.setter
    def awbLumaLowerBound(self, value: int):
        self.app_controller.set_awb_luma_lower_bound(value)
        self.awbLumaLowerBoundChanged.emit()

    @Property(int, notify=awbLumaUpperBoundChanged)
    def awbLumaUpperBound(self):
        return self.app_controller.get_awb_luma_upper_bound()

    @awbLumaUpperBound.setter
    def awbLumaUpperBound(self, value: int):
        self.app_controller.set_awb_luma_upper_bound(value)
        self.awbLumaUpperBoundChanged.emit()

    @Property(int, notify=awbRgbLowerBoundChanged)
    def awbRgbLowerBound(self):
        return self.app_controller.get_awb_rgb_lower_bound()

    @awbRgbLowerBound.setter
    def awbRgbLowerBound(self, value: int):
        self.app_controller.set_awb_rgb_lower_bound(value)
        self.awbRgbLowerBoundChanged.emit()

    @Property(int, notify=awbRgbUpperBoundChanged)
    def awbRgbUpperBound(self):
        return self.app_controller.get_awb_rgb_upper_bound()

    @awbRgbUpperBound.setter
    def awbRgbUpperBound(self, value: int):
        self.app_controller.set_awb_rgb_upper_bound(value)
        self.awbRgbUpperBoundChanged.emit()

    @Property(str, notify=currentDirectoryChanged)
    def currentDirectory(self):
        """Returns the path of the current working directory."""
        return str(self.app_controller.image_dir)

    @Property(bool, notify=metadataChanged)
    def isStackedJpg(self):
        """Returns True if the current image is a stacked JPG."""
        return self.currentFilename.lower().endswith(" stacked.jpg")

    @Property(bool, constant=True)
    def hasOpenCV(self):
        """Returns True if OpenCV is available."""
        return HAS_OPENCV

    # --- Slots for QML to call ---
    @Slot()
    def nextImage(self):
        self.app_controller.next_image()

    @Slot()
    def prevImage(self):
        self.app_controller.prev_image()

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
    def jumpToLastUploaded(self):
        self.app_controller.jump_to_last_uploaded()

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
    def open_file_dialog(self):
        return self.app_controller.open_file_dialog()

    @Slot(str, result=bool)
    def check_path_exists(self, path):
        return self.app_controller.check_path_exists(path)

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

    @Slot(result=str)
    def open_directory_dialog(self):
        return self.app_controller.open_directory_dialog()

    @Property(float, notify=autoLevelClippingThresholdChanged)
    def autoLevelClippingThreshold(self):
        return self.app_controller.get_auto_level_clipping_threshold()

    @autoLevelClippingThreshold.setter
    def autoLevelClippingThreshold(self, value):
        self.app_controller.set_auto_level_clipping_threshold(value)
        self.autoLevelClippingThresholdChanged.emit(value)

    @Property(float, notify=autoLevelStrengthChanged)
    def autoLevelStrength(self):
        return self.app_controller.get_auto_level_strength()

    @autoLevelStrength.setter
    def autoLevelStrength(self, value):
        self.app_controller.set_auto_level_strength(value)
        self.autoLevelStrengthChanged.emit(value)

    @Property(bool, notify=autoLevelStrengthAutoChanged)
    def autoLevelStrengthAuto(self):
        return self.app_controller.get_auto_level_strength_auto()

    @autoLevelStrengthAuto.setter
    def autoLevelStrengthAuto(self, value):
        self.app_controller.set_auto_level_strength_auto(value)
        self.autoLevelStrengthAutoChanged.emit(value)

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

    @Property(bool, notify=anySliderPressedChanged)
    def anySliderPressed(self):
        return self._any_slider_pressed

    @anySliderPressed.setter
    def anySliderPressed(self, value):
        if self._any_slider_pressed != value:
            self._any_slider_pressed = value
            self.anySliderPressedChanged.emit(value)

    @Slot(bool)
    def setAnySliderPressed(self, pressed: bool):
        self.anySliderPressed = pressed

    @Property(bool, notify=is_cropping_changed)
    def isCropping(self) -> bool:
        return self._is_cropping

    @isCropping.setter
    def isCropping(self, new_value: bool):
        if self._is_cropping != new_value:
            self._is_cropping = new_value
            self.is_cropping_changed.emit(new_value)

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

    @Property("QVariant", notify=histogram_data_changed)
    def histogramData(self):
        """Returns histogram data as a dict with 'r', 'g', 'b' keys, each containing a list of 256 values."""
        return self._histogram_data

    @histogramData.setter
    def histogramData(self, new_value):
        if self._histogram_data != new_value:
            self._histogram_data = new_value
            self.histogram_data_changed.emit()

    @Property("QVariant", notify=highlightStateChanged)
    def highlightState(self):
        """Returns highlight analysis state for UI display.

        Returns dict with:
        - headroom_pct: Fraction of pixels with recoverable data above 1.0 (16-bit sources)
        - source_clipped_pct: Fraction of pixels clipped in the SOURCE image (JPEG flat-top @ 254+)
        - current_nearwhite_pct: Fraction of pixels currently near white in the processed state.
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
        # Normalize for QML robustness: ensure stable keys exist
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
    def whiteBalanceBY(self) -> float:
        return self._white_balance_by

    @whiteBalanceBY.setter
    def whiteBalanceBY(self, new_value: float):
        if self._white_balance_by != new_value:
            self._white_balance_by = new_value
            self.white_balance_by_changed.emit(new_value)

    @Property(float, notify=white_balance_mg_changed)
    def whiteBalanceMG(self) -> float:
        return self._white_balance_mg

    @whiteBalanceMG.setter
    def whiteBalanceMG(self, new_value: float):
        if self._white_balance_mg != new_value:
            self._white_balance_mg = new_value
            self.white_balance_mg_changed.emit(new_value)

    # Snake_case aliases for QML bracket notation access
    @Property(float, notify=white_balance_by_changed)
    def white_balance_by(self) -> float:
        return self._white_balance_by

    @white_balance_by.setter
    def white_balance_by(self, new_value: float):
        self.whiteBalanceBY = new_value

    @Property(float, notify=white_balance_mg_changed)
    def white_balance_mg(self) -> float:
        return self._white_balance_mg

    @white_balance_mg.setter
    def white_balance_mg(self, new_value: float):
        self.whiteBalanceMG = new_value

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

    @Property("QVariant", notify=current_crop_box_changed)
    def currentCropBox(self) -> tuple:
        # QML will receive this as a list
        return self._current_crop_box

    @currentCropBox.setter
    def currentCropBox(self, new_value):
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

        # only accept 4-element tuples
        if (
            not isinstance(new_value, tuple)
            or len(new_value) != 4
            or not all(isinstance(v, (int, float)) for v in new_value)
        ):
            log.warning(
                "UIState.currentCropBox: ignoring invalid crop box %r", new_value
            )
            return
        if self._current_crop_box != new_value:
            self._current_crop_box = new_value
            self.current_crop_box_changed.emit(new_value)
            # Sync with ImageEditor
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
    isFolderLoadedChanged = Signal()  # Emitted after first model refresh

    @Property(bool, notify=isFolderLoadedChanged)
    def isFolderLoaded(self) -> bool:
        """Returns True after the folder has been scanned at least once.

        Used by QML to avoid showing 'No images' message during initial load.
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

    @Slot(int, int, int)
    def gridPrefetchRange(self, startIndex: int, endIndex: int, maxCount: int = 800):
        """Prefetch thumbnails for the given index range with budget and duplicate suppression."""
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

        # 2. Duplicate Suppression
        now = self._clock()
        current_req = (startIndex, endIndex, maxCount)
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

        # Trim endIndex if the requested range exceeds the budget
        if (endIndex - startIndex + 1) > budget:
            endIndex = startIndex + budget - 1

        # Submit prefetch jobs for visible range
        # Defensive fallback if thumbnail_size is refactored away
        size = getattr(model, "thumbnail_size", None) or getattr(
            prefetcher, "_target_size", None
        )
        for i in range(startIndex, endIndex + 1):
            entry = model.get_entry(i)
            if entry and not entry.is_folder:
                prefetcher.submit(
                    entry.path, entry.mtime_ns, size=size, priority=prefetcher.PRIO_MED
                )

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
