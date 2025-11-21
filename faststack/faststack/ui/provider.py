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

    def __init__(self, app_controller):
        super().__init__()
        self.app_controller = app_controller
        self._is_preloading = False
        self._preload_progress = 0
        # 1 = light, 0 = dark (controller will overwrite this on startup)
        self._theme = 1
        self._status_message = "" # New private variable for status message

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
    def isFlagged(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("flag", False)

    @Property(bool, notify=metadataChanged)
    def isRejected(self):
        if not self.app_controller.image_files:
            return False
        return self.app_controller.get_current_metadata().get("reject", False)

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

    @Property(str, constant=True)
    def currentDirectory(self):
        """Returns the path of the current working directory."""
        return str(self.app_controller.image_dir)

    # --- Slots for QML to call ---
    @Slot()
    def nextImage(self):
        self.app_controller.next_image()

    @Slot()
    def prevImage(self):
        self.app_controller.prev_image()

    @Slot()
    def toggleFlag(self):
        self.app_controller.toggle_current_flag()

    @Slot()
    def launch_helicon(self):
        self.app_controller.launch_helicon()

    @Slot()
    def clear_all_stacks(self):
        self.app_controller.clear_all_stacks()

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
    def open_directory_dialog(self):
        return self.app_controller.open_directory_dialog()

    @Slot()
    def preloadAllImages(self):
        self.app_controller.preload_all_images()

    @Slot(int, int)
    def onDisplaySizeChanged(self, width: int, height: int):
        self.app_controller.on_display_size_changed(width, height)

    @Slot()
    def resetZoomPan(self):
        """Triggers a reset of zoom and pan in QML."""
        self.resetZoomPanRequested.emit()

