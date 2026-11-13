"""Read the capture time and GPS fix grouping needs from a photo's EXIF."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import ExifTags, Image

from faststack.imaging.metadata import (
    _GPS_IFD_TAG,
    _gps_coordinates_from_info,
    clean_exif_value,
)
from faststack.inat.grouping import PhotoRecord

log = logging.getLogger(__name__)

_DATETIME_ORIGINAL = 0x9003
_DATETIME = 0x0132
_MAKE = 0x010F
_MODEL = 0x0110
_BODY_SERIAL = 0xA431
_EXIF_DT_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_exif_datetime(value: object) -> Optional[datetime]:
    """Parse ``YYYY:MM:DD HH:MM:SS`` (and close variants); ``None`` if unusable."""
    if value is None:
        return None
    text = clean_exif_value(value) if not isinstance(value, str) else value
    match = _EXIF_DT_RE.search(text or "")
    if not match:
        return None
    y, mo, d, h, mi, s = (int(x) for x in match.groups())
    try:
        dt = datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    if dt.year <= 1970:
        return None
    return dt


def camera_identity(exif, sub: dict) -> str:
    """"Make Model #serial" from EXIF, as far as the camera wrote it."""
    parts = []
    for raw in (exif.get(_MAKE), exif.get(_MODEL)):
        text = clean_exif_value(raw) if raw is not None else ""
        text = " ".join(str(text or "").replace("\x00", " ").split())
        if text and text not in parts:
            parts.append(text)
    name = " ".join(parts)
    serial = sub.get(_BODY_SERIAL)
    serial = " ".join(str(clean_exif_value(serial) or "").split()) if serial else ""
    return f"{name} #{serial}" if name and serial else name


def read_photo_facts(path: Path, order: int) -> PhotoRecord:
    """Build a :class:`PhotoRecord` for ``path``; missing EXIF leaves fields None.

    Falls back to the file's modification time when the EXIF has no capture
    time, so a batch of EXIF-less files still groups sensibly.
    """
    taken: Optional[datetime] = None
    coords: Optional[tuple[float, float]] = None
    camera = ""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if exif:
                sub = {}
                try:
                    sub = dict(exif.get_ifd(ExifTags.IFD.Exif) or {})
                except Exception:
                    sub = {}
                raw_dt = sub.get(_DATETIME_ORIGINAL) or exif.get(_DATETIME)
                taken = parse_exif_datetime(raw_dt)
                try:
                    camera = camera_identity(exif, sub)
                except Exception:
                    camera = ""
                gps_ifd = {}
                try:
                    gps_ifd = dict(exif.get_ifd(_GPS_IFD_TAG) or {})
                except Exception:
                    gps_ifd = {}
                if not gps_ifd:
                    raw = dict(exif).get(_GPS_IFD_TAG)
                    gps_ifd = raw if isinstance(raw, dict) else {}
                coords = _gps_coordinates_from_info(gps_ifd) if gps_ifd else None
    except Exception as exc:
        log.debug("EXIF read failed for %s: %s", path, exc)

    from_exif = taken is not None
    if taken is None:
        try:
            taken = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            taken = None

    if coords is not None and (coords[0] == 0.0 and coords[1] == 0.0):
        # Null Island means "no fix" on many cameras.
        coords = None

    return PhotoRecord(
        path=path,
        order=order,
        taken=taken,
        lat=coords[0] if coords else None,
        lon=coords[1] if coords else None,
        taken_from_exif=from_exif,
        camera=camera,
    )
