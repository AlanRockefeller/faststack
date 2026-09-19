"""Restart-marker-parallel baseline JPEG decoding.

Most real camera and Lightroom JPEGs carry a DRI (restart interval) marker.
Every restart interval resets the entropy decoder's DC predictors and bit
buffer, so the scan can be cut at restart-marker boundaries into independent
sub-images.  Each piece is wrapped in a copy of the original headers (with the
SOF height patched) and handed to libjpeg-turbo on its own thread; the pieces
are stacked back together.

The result is *bit-identical* to a single-threaded decode of the whole file,
at any libjpeg-turbo scaling factor, because nothing about the coefficients or
the IDCT changes -- only which thread runs which MCU rows.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
from typing import Optional

import numpy as np

from faststack.util.executors import PriorityExecutor, create_priority_executor

log = logging.getLogger(__name__)

# This was initially gated because splitting one image across cores could have
# reduced whole-folder throughput. Measurements found no gain when the outer
# prefetch pool already saturates the CPU, so callers reserve it for demand,
# settled-cover, and editor decodes. In those latency-sensitive paths, eight
# chunks cut real OM-1/Lightroom JPEG decode from 90-144 ms to 30-56 ms
# (2.6-3.0x). Comparing 150 files at three DCT scales produced zero pixel
# mismatches; eight chunks was retained as the measured default.
_MAX_CHUNKS = 8

# Chunk count and worker count are separate knobs: eight cuts are what the
# measurements above used, but running eight CPU-bound TurboJPEG jobs at once
# on a 4-thread laptop oversubscribes the cores the rest of the UI needs, which
# is why the pool was originally sized from os.cpu_count(). Keep eight chunks,
# cap concurrency at the logical CPUs, and let the extra chunks queue.
_MAX_WORKERS = max(2, min(_MAX_CHUNKS, os.cpu_count() or _MAX_CHUNKS))

# Lower runs first. A demand decode is the image the user is waiting on, so its
# chunks must not queue behind an obsolete cover decode that prefetch.py
# deliberately left running -- the reserved single demand worker would then be
# blocked on background work, which is exactly what it exists to avoid. With a
# priority queue a late demand decode waits at most for the chunks already
# executing, never for every chunk of every stale cover.
PRIORITY_FOREGROUND = 0
PRIORITY_BACKGROUND = 10

_pool: Optional[PriorityExecutor] = None
_pool_lock = threading.Lock()


def _get_pool() -> PriorityExecutor:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = create_priority_executor(
                    max_workers=_MAX_WORKERS,
                    thread_name_prefix="RstSplit",
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
                sof = (i + 5, w, h, hmax, vmax, nc)
            elif m in (
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            ):
                return None  # progressive / arithmetic / lossless
            elif m == 0xDD:
                dri = struct.unpack(">H", jpeg[i + 4 : i + 6])[0]
            elif m == 0xDA:
                if sof is None or not dri:
                    return None
                if i + 5 > n or jpeg[i + 4] != sof[5]:
                    # A sequential file may still be non-interleaved: a scan
                    # covering fewer components than the frame means more
                    # scans follow, which this splitter would swallow into the
                    # first entropy stream. Let the ordinary decoder have it.
                    return None
                return sof[:5] + (dri, i + 2 + ln)
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


def decode_parallel(
    decoder,
    jpeg,
    scaling_factor,
    pixel_format,
    flags=0,
    nchunks: int = 0,
    priority: int = PRIORITY_FOREGROUND,
):
    """Bit-identical parallel replacement for ``decoder.decode``.

    Returns ``None`` when the file is not splittable; the caller falls back to
    the ordinary single-threaded decode.

    ``priority`` orders this decode's chunks against the chunks of other
    in-flight split decodes; see ``PRIORITY_FOREGROUND``/``PRIORITY_BACKGROUND``.
    """
    built = build_chunks(jpeg, nchunks or _MAX_CHUNKS)
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
    # The caller blocks on these futures from its own thread -- never from a
    # worker of this pool -- so waiting here cannot starve the pool.
    futures = [pool.submit(work, part, priority=priority) for part in parts]
    outs = [future.result() for future in futures]
    out_h = -(-h * num // den)
    merged = np.concatenate(outs, axis=0)
    return merged[:out_h] if merged.shape[0] > out_h else merged
