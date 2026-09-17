"""EXPERIMENTAL: restart-marker-parallel baseline JPEG decoding.

Most real camera and Lightroom JPEGs carry a DRI (restart interval) marker.
Every restart interval resets the entropy decoder's DC predictors and bit
buffer, so the scan can be cut at restart-marker boundaries into independent
sub-images.  Each piece is wrapped in a copy of the original headers (with the
SOF height patched) and handed to libjpeg-turbo on its own thread; the pieces
are stacked back together.

The result is *bit-identical* to a single-threaded decode of the whole file,
at any libjpeg-turbo scaling factor, because nothing about the coefficients or
the IDCT changes -- only which thread runs which MCU rows.

Enabled by FASTSTACK_RST_PARALLEL=1 (prototype gate).
"""

from __future__ import annotations

import logging
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

ENABLED = os.environ.get("FASTSTACK_RST_PARALLEL", "") not in ("", "0", "false")
MAX_CHUNKS = int(os.environ.get("FASTSTACK_RST_CHUNKS", "8"))

_pool: Optional[ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(
                max_workers=max(2, (os.cpu_count() or 8)),
                thread_name_prefix="rstsplit",
            )
        return _pool


def _parse(jpeg) -> Optional[tuple]:
    """Return (sof_height_offset, w, h, hmax, vmax, dri, scan_start) or None."""
    try:
        if jpeg[0] != 0xFF or jpeg[1] != 0xD8:
            return None
        i = 2
        sof = None
        dri = 0
        n = len(jpeg)
        while i < n - 1:
            if jpeg[i] != 0xFF:
                return None
            m = jpeg[i + 1]
            if m in (0xD8, 0xD9):
                i += 2
                continue
            if i + 4 > n:
                return None
            ln = struct.unpack(">H", jpeg[i + 2 : i + 4])[0]
            if m in (0xC0, 0xC1):  # baseline / extended sequential only
                h = struct.unpack(">H", jpeg[i + 5 : i + 7])[0]
                w = struct.unpack(">H", jpeg[i + 7 : i + 9])[0]
                nc = jpeg[i + 9]
                if nc not in (1, 3):
                    return None
                samp = [jpeg[i + 11 + 3 * c] for c in range(nc)]
                hmax = max(s >> 4 for s in samp)
                vmax = max(s & 15 for s in samp)
                if vmax != 1:
                    # Vertically subsampled chroma (4:2:0 and friends): at
                    # scale 1/1 libjpeg-turbo's fancy upsampler reads the
                    # chroma row above and below, so a chunk boundary is not
                    # independent and the seam differs from a whole-image
                    # decode by up to ~40/255. Measured, not theoretical.
                    # 4:4:4 and 4:2:2 have no vertical chroma dependency and
                    # are bit-identical at every scaling factor.
                    return None
                sof = (i + 5, w, h, hmax, vmax)
            elif m in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD,
                       0xCE, 0xCF):
                return None  # progressive / arithmetic / lossless
            elif m == 0xDD:
                dri = struct.unpack(">H", jpeg[i + 4 : i + 6])[0]
            elif m == 0xDA:
                if sof is None or not dri:
                    return None
                return sof + (dri, i + 2 + ln)
            i += 2 + ln
    except Exception:
        return None
    return None


def build_chunks(jpeg, nchunks: int):
    """Split ``jpeg`` into independently decodable sub-JPEGs.

    Returns ``(parts, width, height)`` where ``parts`` is a list of byte
    strings, or ``None`` when the file cannot be split.
    """
    parsed = _parse(jpeg)
    if parsed is None:
        return None
    sof_h_off, w, h, hmax, vmax, dri, scan_start = parsed

    mcu_w, mcu_h = hmax * 8, vmax * 8
    mcus_x = (w + mcu_w - 1) // mcu_w
    mcu_rows = (h + mcu_h - 1) // mcu_h
    if mcus_x % dri:
        # A restart interval straddles the end of an MCU row, so row-aligned
        # cuts are not restart-aligned.  Bail rather than guess.
        return None
    iv_per_row = mcus_x // dri
    # RSTn markers cycle 0..7 and a fresh decoder expects RST0 first, so a cut
    # may only land where the interval index is a multiple of 8.
    step = 8 // np.gcd(iv_per_row, 8)
    rows_per_chunk = max(
        step,
        ((mcu_rows + nchunks - 1) // nchunks + step - 1) // step * step,
    )
    if rows_per_chunk >= mcu_rows:
        return None

    buf = np.frombuffer(jpeg, dtype=np.uint8)[scan_start:]
    ff = np.flatnonzero(buf[:-1] == 0xFF)
    nxt = buf[ff + 1]
    rst = ff[(nxt >= 0xD0) & (nxt <= 0xD7)] + scan_start

    needed = ((mcu_rows - 1) // rows_per_chunk) * rows_per_chunk * iv_per_row
    if needed > len(rst):
        # Fewer restart markers than the header implies -- a truncated or
        # malformed scan. Let the ordinary decoder produce the (possibly
        # IncompleteJPEG) result instead of indexing past the end.
        return None

    starts = range(0, mcu_rows, rows_per_chunk)
    header = bytes(jpeg[:scan_start])
    raw = bytes(jpeg) if not isinstance(jpeg, bytes) else jpeg
    parts = []
    for r0 in starts:
        r1 = min(r0 + rows_per_chunk, mcu_rows)
        iv0 = r0 * iv_per_row
        if iv0 and iv0 > len(rst):
            return None
        b0 = scan_start if r0 == 0 else int(rst[iv0 - 1]) + 2
        hdr = bytearray(header)
        struct.pack_into(">H", hdr, sof_h_off, (r1 - r0) * mcu_h)
        if r1 == mcu_rows:
            parts.append(bytes(hdr) + raw[b0:])
        else:
            b1 = int(rst[r1 * iv_per_row - 1])
            parts.append(bytes(hdr) + raw[b0:b1] + b"\xff\xd9")
    return parts, w, h


def decode_parallel(decoder, jpeg, scaling_factor, pixel_format, flags=0,
                    nchunks: int = 0):
    """Bit-identical parallel replacement for ``decoder.decode``.

    Returns ``None`` when the file is not splittable; the caller falls back to
    the ordinary single-threaded decode.
    """
    built = build_chunks(jpeg, nchunks or MAX_CHUNKS)
    if built is None:
        return None
    parts, w, h = built
    num, den = scaling_factor if scaling_factor else (1, 1)

    def work(part):
        return decoder.decode(
            part,
            scaling_factor=scaling_factor,
            pixel_format=pixel_format,
            flags=flags,
        )

    pool = _get_pool()
    outs = list(pool.map(work, parts))
    out_h = -(-h * num // den)
    merged = np.concatenate(outs, axis=0)
    return merged[:out_h] if merged.shape[0] > out_h else merged
