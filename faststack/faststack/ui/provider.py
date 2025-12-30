"""QML Image Provider and application state bridge."""

import logging
from PySide6.QtCore import QObject, Signal, Property, Slot, Qt
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from faststack.models import DecodedImage
from faststack.config import config

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

    def requestImage(self, id: str, size: object, requestedSize: object) -> QImage:
        """Handles image requests from QML."""
        if not id:
            return self.placeholder

        try:
            image_index_str = id.split('/')[0]
            index = int(image_index_str)
            image_data = self.app_controller.get_decoded_image(index)

            if image_data:
                qimg = QImage(
                    image_data.buffer,
                    image_data.width,
                    image_data.height,
                    image_data.bytes_per_line,
                    QImage.Format.Format_RGB888
                )
                # Set sRGB color space for proper color management (if available)
                # Skip this when using ICC mode - pixels are already in monitor space
                color_mode = config.get('color', 'mode', fallback="none").lower()
                if HAS_COLOR_SPACE and color_mode != "icc":
                    try:
                        # Create sRGB color space using constructor with NamedColorSpace enum
                        cs = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
                        qimg.setColorSpace(cs)
                        log.debug("Applied sRGB color space to image")
                    except (RuntimeError, ValueError) as e:
                        log.warning(f"Failed to set color space: {e}")
                elif color_mode == "icc":
                    log.debug("ICC mode: skipping Qt color space (pixels already in monitor space)")
                # keep buffer alive
                qimg.original_buffer = image_data.buffer
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
    isZoomedChanged = Signal()
    statusMessageChanged = Signal() # New signal for status messages
    resetZoomPanRequested = Signal() # Signal to tell QML to reset zoom/pan
    stackSummaryChanged = Signal() # Signal for stack summary updates
    filterStringChanged = Signal() # Signal for filter string updates
    colorModeChanged = Signal() # Signal for color mode updates
    saturationFactorChanged = Signal() # Signal for saturation factor updates
    awbModeChanged = Signal()
    awbStrengthChanged = Signal()
    awbWarmBiasChanged = Signal()
    awbTintBiasChanged = Signal()
    awbLumaLowerBoundChanged = Signal()
    awbLumaUpperBoundChanged = Signal()
    awbRgbLowerBoundChanged = Signal()
    awbRgbUpperBoundChanged = Signal()
    default_directory_changed = Signal(str)
    isStackedJpgChanged = Signal() # New signal for isStackedJpg
    autoLevelClippingThresholdChanged = Signal(float)
    autoLevelStrengthChanged = Signal(float)
    autoLevelStrengthAutoChanged = Signal(bool)
    # Image Editor Signals
    is_editor_open_changed = Signal(bool)
    is_cropping_changed = Signal(bool)
    is_histogram_visible_changed = Signal(bool)
    histogram_data_changed = Signal()
    brightness_changed = Signal(float)
    contrast_changed = Signal(float)
    saturation_changed = Signal(float)
    white_balance_by_changed = Signal(float)
    white_balance_mg_changed = Signal(float)
    aspect_ratio_names_changed = Signal(list)
    current_aspect_ratio_index_changed = Signal(int)
    current_crop_box_changed = Signal(tuple) # (left, top, right, bottom) normalized to 0-1000
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
    
    # Debug Cache Signals
    debugCacheChanged = Signal(bool)
    cacheStatsChanged = Signal(str)
    isDecodingChanged = Signal(bool)
    debugModeChanged = Signal(bool) # General debug mode signal

    def __init__(self, app_controller):
        super().__init__()
        self.app_controller = app_controller
        self._is_preloading = False
        self._preload_progress = 0
        # 1 = light, 0 = dark (controller will overwrite this on startup)
        self._theme = 1
        self._status_message = "" # New private variable for status message
        # Image Editor State
        self._is_editor_open = False
        self._is_cropping = False
        self._is_histogram_visible = False
        self._histogram_data = None  # Will be a dict with 'r', 'g', 'b' arrays
        self._brightness = 0.0
        self._contrast = 0.0
        self._saturation = 0.0
        self._white_balance_by = 0.0
        self._white_balance_mg = 0.0
        self._current_crop_box = (0, 0, 1000, 1000)
        self._crop_rotation = 0.0
        self._debug_mode = False
        self._aspect_ratio_names = []
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
        
        # Debug Cache State
        self._debug_cache = False
        self._cache_stats = ""
        self._is_decoding = False

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
        return f"image://provider/{self.app_controller.current_index}/{self.app_controller.ui_refresh_generation}"

    @Property(str, notify=metadataChanged)
    def currentFilename(self):
        if not self.app_controller.image_files:
            return ""
        return self.app_controller.get_current_metadata().get("filename", "")

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

    @Property(str, notify=stackSummaryChanged)
    def stackSummary(self):
        if not self.app_controller.stacks:
            return "No stacks defined."
        summary = f"Found {len(self.app_controller.stacks)} stacks:\n\n"
        for i, (start, end) in enumerate(self.app_controller.stacks):
            count = end - start + 1
            summary += f"Stack {i+1}: {count} photos (indices {start}-{end})\n"
        return summary

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self):
        return self._status_message

    @statusMessage.setter
    def statusMessage(self, value: str):
        if self._status_message != value:
            self._status_message = value
            self.statusMessageChanged.emit()

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

    @Property(str, constant=True)
    def currentDirectory(self):
        """Returns the path of the current working directory."""
        return str(self.app_controller.image_dir)

    @Property(bool, notify=metadataChanged)
    def isStackedJpg(self):
        """Returns True if the current image is a stacked JPG."""
        return self.currentFilename.lower().endswith(" stacked.jpg")

    # --- Slots for QML to call ---
    @Slot()
    def nextImage(self):
        self.app_controller.next_image()

    @Slot()
    def prevImage(self):
        self.app_controller.prev_image()


    @Slot()
    def launch_helicon(self):
        self.app_controller.launch_helicon()

    @Slot()
    def clear_all_stacks(self):
        self.app_controller.clear_all_stacks()

    @Slot()
    def clear_all_batches(self):
        self.app_controller.clear_all_batches()

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

    @Slot(str)
    def applyFilter(self, filter_string: str):
        """Applies a filter string to the image list."""
        self.app_controller.apply_filter(filter_string)

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
                self.app_controller.update_histogram()
    
    @Property('QVariant', notify=histogram_data_changed)
    def histogramData(self):
        """Returns histogram data as a dict with 'r', 'g', 'b' keys, each containing a list of 256 values."""
        return self._histogram_data
    
    @histogramData.setter
    def histogramData(self, new_value):
        if self._histogram_data != new_value:
            self._histogram_data = new_value
            self.histogram_data_changed.emit()

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

    @Property('QVariantList', notify=aspect_ratio_names_changed)
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

    @Property('QVariant', notify=current_crop_box_changed)
    def currentCropBox(self) -> tuple:
        # QML will receive this as a list
        return self._current_crop_box

    @currentCropBox.setter
    def currentCropBox(self, new_value):
        # Convert QJSValue or list to tuple if needed
        original_value = new_value
        try:
            if hasattr(new_value, 'toVariant'):
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
            log.warning("UIState.currentCropBox: ignoring invalid crop box %r", new_value)
            return 
        if self._current_crop_box != new_value:
            self._current_crop_box = new_value
            self.current_crop_box_changed.emit(new_value)
            # Sync with ImageEditor
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
