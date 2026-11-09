"""QML image providers for the iNaturalist upload dialog.

* ``image://osmtile/{z}/{x}/{y}`` - a map tile from the disk cache (fetched
  from OpenStreetMap on a miss).
* ``image://inatphoto/{index}?w=320`` - a card thumbnail, EXIF-rotated.

Both are plain (synchronous) providers; QML ``Image { asynchronous: true }``
runs them on Qt's image-loading threads, so a network fetch never blocks the
UI thread.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageOps, ImageQt
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtQuick import QQuickImageProvider

from faststack.inat.tiles import TILE_SIZE, TileFetcher

log = logging.getLogger(__name__)


class TileImageProvider(QQuickImageProvider):
    def __init__(self, fetcher_getter: Callable[[], Optional[TileFetcher]]):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._fetcher_getter = fetcher_getter
        self._blank = QImage(TILE_SIZE, TILE_SIZE, QImage.Format.Format_RGB32)
        self._blank.fill(QColor("#2a2a2a"))

    def requestImage(self, id_str: str, size: QSize, _requested: QSize) -> QImage:
        size.setWidth(TILE_SIZE)
        size.setHeight(TILE_SIZE)
        try:
            z, x, y = (int(part) for part in id_str.split("?")[0].split("/")[:3])
        except ValueError:
            return self._blank
        fetcher = self._fetcher_getter()
        if fetcher is None:
            return self._blank
        data = fetcher.fetch(z, x, y)
        if not data:
            return self._blank
        image = QImage.fromData(data)
        if image.isNull():
            return self._blank
        return image


class CardPhotoProvider(QQuickImageProvider):
    """Thumbnails for the observation cards, keyed by photo index."""

    def __init__(self, path_getter: Callable[[int], Optional[Path]]):
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._path_getter = path_getter
        self._placeholder = QImage(4, 3, QImage.Format.Format_RGB32)
        self._placeholder.fill(QColor("#444444"))

    def requestImage(self, id_str: str, size: QSize, requested: QSize) -> QImage:
        parts = urlsplit(id_str)
        try:
            index = int(parts.path.strip("/").split("/")[0])
        except ValueError:
            return self._fail(size)
        query = parse_qs(parts.query)
        try:
            width = int(query.get("w", ["320"])[0])
        except ValueError:
            width = 320
        width = max(64, min(width, 1600))
        path = self._path_getter(index)
        if path is None:
            return self._fail(size)
        try:
            with Image.open(path) as img:
                w, h = img.size
                scale = width / max(w, h)
                if scale < 1:
                    try:
                        img.draft("RGB", (int(w * scale) + 1, int(h * scale) + 1))
                    except Exception:
                        pass
                img = ImageOps.exif_transpose(img) or img
                img.thumbnail((width, width), Image.Resampling.BILINEAR)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                qimg = ImageQt.ImageQt(img).copy()
        except Exception as exc:
            log.debug("Card thumbnail failed for %s: %s", path, exc)
            return self._fail(size)
        size.setWidth(qimg.width())
        size.setHeight(qimg.height())
        return qimg

    def _fail(self, size: QSize) -> QImage:
        size.setWidth(self._placeholder.width())
        size.setHeight(self._placeholder.height())
        return self._placeholder
