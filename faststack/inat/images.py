"""Shrink photos for upload, keeping EXIF (date, GPS, orientation) and ICC."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

VISION_LONG_EDGE = 1024  # MycoMap Vision: "a long side of 640-1024 px is plenty"
INAT_LONG_EDGE = 2048  # iNaturalist stores at most 2048 px
JPEG_QUALITY = 85


def prepare_jpeg(path: Path, long_edge: int, quality: int = JPEG_QUALITY) -> bytes:
    """Return JPEG bytes no larger than ``long_edge`` on the longest side.

    A file already within the limit is returned untouched (no re-encode). The
    EXIF block is carried over verbatim so Vision can read DateTimeOriginal
    and iNaturalist the GPS fix; the orientation tag stays valid because the
    pixels are not rotated here.
    """
    with Image.open(path) as img:
        width, height = img.size
        if max(width, height) <= long_edge and _is_jpeg(img):
            return path.read_bytes()
        exif = img.info.get("exif")
        icc = img.info.get("icc_profile")
        # Let libjpeg decode at a reduced DCT scale: far faster than a full
        # decode followed by a resize for 24-50 MP sources.
        scale = long_edge / max(width, height)
        try:
            img.draft("RGB", (int(width * scale), int(height * scale)))
        except Exception:
            pass
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        kwargs = {"quality": quality, "optimize": True}
        if exif:
            kwargs["exif"] = exif
        if icc:
            kwargs["icc_profile"] = icc
        img.save(buf, format="JPEG", **kwargs)
        return buf.getvalue()


def _is_jpeg(img: Image.Image) -> bool:
    return (img.format or "").upper() in ("JPEG", "MPO")
