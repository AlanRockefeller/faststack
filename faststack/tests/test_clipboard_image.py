"""Ctrl+Shift+C: copy the photo itself to the clipboard."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from faststack.controller import clipboard_image
from faststack.controller.clipboard_image import build_clipboard_image
from faststack.models import ImageFile
from faststack.ui.keystrokes import Keybinder


def _write_jpeg(path: Path, width: int, height: int, orientation: int = 1) -> bytes:
    # Left half red, right half blue, so a rotation is visible.
    pixels = np.zeros((height, width, 3), np.uint8)
    pixels[:, : width // 2] = (255, 0, 0)
    pixels[:, width // 2 :] = (0, 0, 255)
    image = Image.fromarray(pixels)
    exif = Image.Exif()
    if orientation != 1:
        exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95, exif=exif.tobytes())
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def _png_image(png: bytes) -> QImage:
    image = QImage()
    assert image.loadFromData(png, "PNG")
    return image


def test_jpeg_becomes_upright_png_plus_original_bytes(tmp_path):
    path = tmp_path / "A.jpg"
    original = _write_jpeg(path, 64, 32, orientation=6)  # rotate 90 CW to view

    result = build_clipboard_image(7, path)

    assert result.error is None
    assert result.token == 7
    assert result.jpeg == original  # the file's own bytes, not re-encoded
    image = _png_image(result.png)
    assert (image.width(), image.height()) == (32, 64)
    # Rotated 90 CW: the red left half is now on top.
    assert QImage(image).pixelColor(16, 4).red() > 200
    assert QImage(image).pixelColor(16, 60).blue() > 200
    # Linux offers bytes only; Qt would otherwise re-encode on the UI thread.
    assert (result.image is None) == sys.platform.startswith("linux")


def test_non_jpeg_is_read_by_qt_and_has_no_jpeg_bytes(tmp_path):
    path = tmp_path / "A.png"
    Image.new("RGB", (20, 10), (0, 255, 0)).save(path)

    result = build_clipboard_image(1, path)

    assert result.error is None
    assert result.jpeg is None
    assert _png_image(result.png).size().toTuple() == (20, 10)


def test_unreadable_file_reports_an_error(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not a jpeg at all")

    result = build_clipboard_image(1, path)

    assert result.png is None
    assert result.error


def test_missing_file_reports_an_error(tmp_path):
    result = build_clipboard_image(1, tmp_path / "gone.jpg")
    assert result.png is None
    assert result.error


@pytest.mark.parametrize(
    ("modifiers", "expected"),
    [
        (Qt.ControlModifier | Qt.ShiftModifier, "copy_image_to_clipboard"),
        (Qt.ControlModifier, "copy_path_to_clipboard"),
        (Qt.NoModifier, "clear_all_stacks"),
    ],
)
def test_key_bindings(modifiers, expected):
    keybinder = Keybinder(MagicMock())
    assert keybinder._binding_for(Qt.Key_C, "", modifiers) == expected


def _copy_and_finish(controller):
    """Run copy_image_to_clipboard with its worker inline; return the result."""
    results = []
    controller._clipboard_image_executor = MagicMock()
    controller._clipboard_image_executor.submit.side_effect = lambda fn: fn()
    controller._clipboardImageReady.disconnect(controller._on_clipboard_image_ready)
    controller._clipboardImageReady.connect(results.append)
    controller.copy_image_to_clipboard()
    return results


def test_copy_puts_png_and_jpeg_on_the_clipboard(app_controller, tmp_path):
    path = tmp_path / "A.jpg"
    original = _write_jpeg(path, 40, 30)
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0

    results = _copy_and_finish(app_controller)
    assert len(results) == 1

    clipboard = MagicMock()
    with patch.object(clipboard_image, "QApplication") as qapp:
        qapp.clipboard.return_value = clipboard
        app_controller._on_clipboard_image_ready(results[0])

    mime = clipboard.setMimeData.call_args.args[0]
    assert "image/png" in mime.formats()
    assert mime.data("image/jpeg").data() == original
    assert "text/plain" not in mime.formats()  # Ctrl+C is the path copy
    app_controller.update_status_message.assert_called_with("Copied image: A.jpg")


def test_older_copy_finishing_late_is_dropped(app_controller, tmp_path):
    path = tmp_path / "A.jpg"
    _write_jpeg(path, 8, 8)
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0
    results = _copy_and_finish(app_controller)
    app_controller._clipboard_image_token += 1  # a newer Ctrl+Shift+C

    with patch.object(clipboard_image, "QApplication") as qapp:
        app_controller._on_clipboard_image_ready(results[0])
    qapp.clipboard.assert_not_called()


def test_raw_without_developed_jpg_is_refused(app_controller, tmp_path):
    path = tmp_path / "A.orf"
    path.write_bytes(b"raw")
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0

    assert _copy_and_finish(app_controller) == []
    message = app_controller.update_status_message.call_args.args[0]
    assert "RAW" in message


def test_unsaved_edits_are_saved_first(app_controller, tmp_path):
    path = tmp_path / "A.jpg"
    _write_jpeg(path, 8, 8)
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0
    app_controller._is_current_live_edit_session_dirty = lambda: True
    app_controller._flush_current_live_edit_session_for_drag = MagicMock(
        return_value=False  # the save failed: nothing is copied
    )

    assert _copy_and_finish(app_controller) == []
    app_controller._flush_current_live_edit_session_for_drag.assert_called_once()


def test_no_images(app_controller):
    app_controller.image_files = []
    assert _copy_and_finish(app_controller) == []
    app_controller.update_status_message.assert_called_with("No image to copy.")


def test_real_worker_hands_the_result_to_the_gui_thread(app_controller, tmp_path):
    """The encode runs on the worker; setMimeData runs back on this thread."""
    import threading
    import time

    from PySide6.QtCore import QCoreApplication

    path = tmp_path / "A.jpg"
    _write_jpeg(path, 16, 16)
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0
    calls = []
    clipboard = MagicMock()
    clipboard.setMimeData.side_effect = lambda mime: calls.append(
        (threading.get_ident(), list(mime.formats()))
    )
    with patch.object(clipboard_image, "QApplication") as qapp:
        qapp.clipboard.return_value = clipboard
        app_controller.copy_image_to_clipboard()
        deadline = time.monotonic() + 10
        while not calls and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    assert calls, "the clipboard was never set"
    thread_id, formats = calls[0]
    assert thread_id == threading.get_ident()
    assert "image/png" in formats and "image/jpeg" in formats


def test_jpeg_colour_profile_is_kept_in_the_png(tmp_path):
    from PySide6.QtGui import QColorSpace

    icc = QColorSpace(QColorSpace.AdobeRgb).iccProfile().data()
    path = tmp_path / "adobe.jpg"
    Image.new("RGB", (8, 8), (40, 120, 200)).save(path, "JPEG", icc_profile=icc)

    result = build_clipboard_image(1, path)

    assert Image.open(io.BytesIO(result.png)).info.get("icc_profile") == icc


def test_jpeg_without_profile_gives_png_without_one(tmp_path):
    path = tmp_path / "A.jpg"
    _write_jpeg(path, 8, 8)
    result = build_clipboard_image(1, path)
    assert not Image.open(io.BytesIO(result.png)).info.get("icc_profile")


def test_copies_the_backup_on_screen(app_controller, tmp_path):
    path = tmp_path / "A.jpg"
    backup = tmp_path / "A-backup.jpg"
    _write_jpeg(path, 8, 8)
    backup_bytes = _write_jpeg(backup, 12, 6)
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0
    app_controller.view_override_path = str(backup)

    results = _copy_and_finish(app_controller)

    assert results[0].path == backup
    assert results[0].jpeg == backup_bytes


def test_ctrl_c_during_an_image_copy_keeps_the_path(app_controller, tmp_path):
    path = tmp_path / "A.jpg"
    _write_jpeg(path, 8, 8)
    app_controller.image_files = [ImageFile(path=path)]
    app_controller.current_index = 0
    results = _copy_and_finish(app_controller)  # still "encoding"

    with patch("faststack.app.QApplication") as app_qapp:
        app_controller.copy_path_to_clipboard()
    app_qapp.clipboard.return_value.setText.assert_called_once_with(str(path))

    with patch.object(clipboard_image, "QApplication") as qapp:
        app_controller._on_clipboard_image_ready(results[0])
    qapp.clipboard.assert_not_called()
