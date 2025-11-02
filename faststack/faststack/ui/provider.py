"""QML Image Provider and application state bridge."""

import logging
from typing import Optional

import numpy as np
from PySide6.QtCore import QObject, Signal, Property, QUrl, Slot, Qt
from PySide6.QtGui import QImage
from PySide6.QtQuick import QQuickImageProvider

from faststack.models import DecodedImage

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

        # The ID is expected to be the image index
        try:
            image_index_str = id.split('/')[0]
            index = int(image_index_str)
            image_data = self.app_controller.get_decoded_image(index)

            if image_data:
                # Zero-copy QImage from numpy buffer
                qimg = QImage(
                    image_data.buffer,
                    image_data.width,
                    image_data.height,
                    image_data.bytes_per_line,
                    QImage.Format.Format_RGB888
                )
                # Keep a reference to the original buffer to prevent garbage collection
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

    def __init__(self, app_controller):
        super().__init__()
        self.app_controller = app_controller

    @Property(int, notify=currentIndexChanged)
    def currentIndex(self):
        return self.app_controller.current_index

    @Property(int, notify=imageCountChanged)
    def imageCount(self):
        return len(self.app_controller.image_files)

    @Property(str, notify=currentImageSourceChanged)
    def currentImageSource(self):
        # The source is the provider ID, which we tie to the index and a generation counter
        # to force QML to request a new image even if the index is the same.
        return f"image://provider/{self.app_controller.current_index}/{self.app_controller.ui_refresh_generation}"

    # --- Metadata Properties ---
    @Property(str, notify=metadataChanged)
    def currentFilename(self):
        return self.app_controller.get_current_metadata().get("filename", "")

    @Property(bool, notify=metadataChanged)
    def isFlagged(self):
        return self.app_controller.get_current_metadata().get("flag", False)

    @Property(bool, notify=metadataChanged)
    def isRejected(self):
        return self.app_controller.get_current_metadata().get("reject", False)

    @Property(str, notify=metadataChanged)
    def stackInfoText(self):
        return self.app_controller.get_current_metadata().get("stack_info_text", "")

    @Property(str, notify=metadataChanged)
    def get_stack_summary(self):
        if not self.app_controller.stacks:
            return "No stacks defined."

        summary = f"Found {len(self.app_controller.stacks)} stacks:\n\n"
        for i, (start, end) in enumerate(self.app_controller.stacks):
            count = end - start + 1
            summary += f"Stack {i+1}: {count} photos (indices {start}-{end})\n"
        return summary

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
