"""Ctrl+Shift+C: put the current photo itself on the clipboard.

Mixed into AppController (faststack/app.py); every method runs on the
controller instance.  Ctrl+C still copies the path as text.

The photo is decoded and encoded to PNG on a daemon worker: Qt's own image
clipboard (``setImageData``) encodes on the UI thread each time a program
asks for the image, which for a 20 MP photo is a few seconds, and on Omarchy
the clipboard history asks straight away and gives up after 2 seconds.  So on
Linux the clipboard is offered ready-made bytes only: ``image/png`` (what most
programs paste) plus ``image/jpeg`` with the file's own bytes when it is a JPG.
Windows and macOS programs read images through Qt's native conversions, which
need a QImage, so there the QImage is set as well.

Only the latest Ctrl+Shift+C lands: a slower earlier copy that finishes later
is dropped.  The clipboard holds the photo as it is on disk, so unsaved edits
are saved first, as for drag-and-drop.
"""

from __future__ import annotations

import io
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, Slot
from PySide6.QtGui import QColorSpace, QImage, QImageReader
from PySide6.QtWidgets import QApplication

from faststack.io.indexer import JPG_EXTENSIONS, RAW_EXTENSIONS

log = logging.getLogger(__name__)

# Qt's PNG "quality" is zlib effort: 85 is zlib level 1.  A 17.9 MP photo
# encodes in about 2 s to 21 MB; 90 and above store it uncompressed (1 s but
# 53 MB, which is slow to paste into anything that uploads it).
_PNG_QUALITY = 85


@dataclass
class ClipboardImage:
    """A photo ready for the clipboard, built off the UI thread."""

    token: int
    path: Path
    png: Optional[bytes] = None
    jpeg: Optional[bytes] = None
    image: Optional[QImage] = None
    error: Optional[str] = None


def _embedded_icc(data: bytes) -> Optional[bytes]:
    """The JPEG's ICC profile, read from its header only."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.info.get("icc_profile") or None
    except Exception:
        return None


def _decode(path: Path, data: bytes) -> QImage:
    """The photo upright, as Format_RGB888 (owns its pixels)."""
    if path.suffix.lower() in JPG_EXTENSIONS:
        from faststack.imaging import rst_parallel
        from faststack.imaging.exif_fast import read_orientation
        from faststack.imaging.jpeg import decode_jpeg_rgb
        from faststack.imaging.orientation import apply_orientation_to_np

        rgb = decode_jpeg_rgb(
            data,
            source_path=str(path),
            rst_parallel_priority=rst_parallel.PRIORITY_BACKGROUND,
        )
        if rgb is not None:
            rgb = apply_orientation_to_np(rgb, read_orientation(data))
            height, width = rgb.shape[:2]
            # copy(): the QImage must not point into the numpy buffer.
            image = QImage(
                rgb.data, width, height, rgb.strides[0], QImage.Format_RGB888
            ).copy()
            # The pixels are as stored, not converted to sRGB, so the PNG
            # carries the JPG's profile (Adobe RGB, say) for them to mean
            # the same colours. Qt writes it as the PNG's iCCP chunk.
            icc = _embedded_icc(data)
            if icc is not None:
                space = QColorSpace.fromIccProfile(icc)
                if space.isValid():
                    image.setColorSpace(space)
            return image
    # Anything else Qt can read (and a JPG turbojpeg refused).
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "unreadable image")
    return image


def build_clipboard_image(token: int, path: Path) -> ClipboardImage:
    """Read and encode *path* for the clipboard.  Never raises."""
    result = ClipboardImage(token=token, path=path)
    try:
        data = path.read_bytes()
        image = _decode(path, data)
        if image.format() not in (QImage.Format_RGB888, QImage.Format_ARGB32):
            # RGB888 encodes about twice as fast as Qt's default RGB32.
            image = image.convertToFormat(
                QImage.Format_ARGB32
                if image.hasAlphaChannel()
                else QImage.Format_RGB888
            )
        png = QByteArray()
        buffer = QBuffer(png)
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG", _PNG_QUALITY):
            raise ValueError("PNG encoding failed")
        buffer.close()
        result.png = png.data()
        if path.suffix.lower() in JPG_EXTENSIONS:
            result.jpeg = data
        if not sys.platform.startswith("linux"):
            result.image = image
    except Exception as exc:  # reported in the status bar, never raised
        log.warning("Could not prepare %s for the clipboard", path, exc_info=True)
        result.error = str(exc) or type(exc).__name__
    return result


class ClipboardImageMixin:
    """Ctrl+Shift+C (``copy_image_to_clipboard``)."""

    def _init_clipboard_image(self) -> None:
        from faststack.util.executors import create_daemon_threadpool_executor

        self._clipboard_image_executor = create_daemon_threadpool_executor(
            max_workers=1, thread_name_prefix="ClipboardImage"
        )
        # Bumped by every Ctrl+Shift+C; an older result is dropped.
        self._clipboard_image_token = 0
        self._clipboardImageReady.connect(self._on_clipboard_image_ready)

    def _shutdown_clipboard_image(self) -> None:
        executor = getattr(self, "_clipboard_image_executor", None)
        if executor is not None:
            self._safe_shutdown_executor(executor, "clipboard image", wait=False)

    @Slot()
    def copy_image_to_clipboard(self) -> None:
        if not self.image_files or not (
            0 <= self.current_index < len(self.image_files)
        ):
            self.update_status_message("No image to copy.")
            return
        img = self.image_files[self.current_index]
        if self._is_current_live_edit_session_dirty():
            if self._block_if_saving(img.path):
                return
            if not self._flush_current_live_edit_session_for_drag(
                saving_status="Saving edits before copying..."
            ):
                return
        # The file on screen: a backup picked with the variant badges, else
        # the developed JPG in RAW mode, else the photo.
        path = (
            Path(self.view_override_path)
            if getattr(self, "view_override_path", None)
            else self._export_path_for(img)
        )
        if self._block_if_saving(img.path, path):
            return
        if path.suffix.lower() in RAW_EXTENSIONS:
            self.update_status_message(
                f"{path.name} is a RAW file; develop it to a JPG to copy the image."
            )
            return

        self._clipboard_image_token += 1
        token = self._clipboard_image_token
        self.update_status_message(f"Copying image {path.name}...")

        def work() -> None:
            # A newer Ctrl+Shift+C is already queued: skip the encode.
            if token != self._clipboard_image_token:
                return
            result = build_clipboard_image(token, path)
            try:
                self._clipboardImageReady.emit(result)
            except RuntimeError:
                pass  # The controller is gone (app quitting).

        try:
            self._clipboard_image_executor.submit(work)
        except RuntimeError:
            log.debug("Clipboard image worker is shut down", exc_info=True)

    def _cancel_pending_clipboard_image(self) -> None:
        """A later clipboard copy (Ctrl+C) wins over an image still encoding."""
        self._clipboard_image_token += 1

    def _on_clipboard_image_ready(self, result: ClipboardImage) -> None:
        if result.token != self._clipboard_image_token:
            return
        if result.error is not None:
            self.update_status_message(
                f"Could not copy image {result.path.name}: {result.error}"
            )
            return
        mime = QMimeData()
        if result.image is not None:
            mime.setImageData(result.image)
        mime.setData("image/png", QByteArray(result.png))
        if result.jpeg is not None:
            mime.setData("image/jpeg", QByteArray(result.jpeg))
        QApplication.clipboard().setMimeData(mime)
        self.update_status_message(f"Copied image: {result.path.name}")
        log.info(
            "Copied image to clipboard: %s (PNG %.1f MB)",
            result.path,
            len(result.png) / 1e6,
        )
