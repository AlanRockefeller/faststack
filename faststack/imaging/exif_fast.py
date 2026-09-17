"""Minimal EXIF orientation reader that never touches the caller's file object.

``PIL.Image.open(mmap).close()`` closes the mmap it was handed, so the decode
path cannot use Pillow to peek at orientation before decoding from the same
snapshot. This parses the APP1/Exif IFD0 Orientation tag straight out of the
header bytes instead: no file handles, no Pillow plugin registry, no state.
"""

from __future__ import annotations

import struct

_ORIENTATION_TAG = 0x0112


def read_orientation(data) -> int:
    """Return the EXIF orientation (1-8) from JPEG header bytes, else 1."""
    try:
        if data[0] != 0xFF or data[1] != 0xD8:
            return 1
        i = 2
        n = min(len(data), 1 << 20)
        while i < n - 3:
            if data[i] != 0xFF:
                return 1
            marker = data[i + 1]
            if marker == 0xDA or marker == 0xD9:
                return 1
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if marker == 0xE1 and data[i + 4 : i + 10] == b"Exif\x00\x00":
                return _orientation_from_tiff(bytes(data[i + 10 : i + 2 + seg_len]))
            i += 2 + seg_len
    except Exception:
        return 1
    return 1


def _orientation_from_tiff(tiff: bytes) -> int:
    if len(tiff) < 8:
        return 1
    if tiff[:2] == b"II":
        end = "<"
    elif tiff[:2] == b"MM":
        end = ">"
    else:
        return 1
    (magic,) = struct.unpack(end + "H", tiff[2:4])
    if magic != 42:
        return 1
    (ifd0,) = struct.unpack(end + "I", tiff[4:8])
    if ifd0 + 2 > len(tiff):
        return 1
    (count,) = struct.unpack(end + "H", tiff[ifd0 : ifd0 + 2])
    base = ifd0 + 2
    for k in range(count):
        off = base + 12 * k
        if off + 12 > len(tiff):
            return 1
        tag, typ, _num = struct.unpack(end + "HHI", tiff[off : off + 8])
        if tag == _ORIENTATION_TAG:
            if typ != 3 or _num != 1:
                # Anything but a single SHORT keeps its value out of line, so
                # those 4 bytes are a file offset, not an orientation.
                return 1
            (value,) = struct.unpack(end + "H", tiff[off + 8 : off + 10])
            return value if 1 <= value <= 8 else 1
    return 1
