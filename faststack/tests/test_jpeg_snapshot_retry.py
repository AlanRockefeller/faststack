import builtins
import warnings
from unittest.mock import patch

import numpy as np
import pytest

from faststack.imaging.jpeg import IncompleteJPEGError, _decode_with_retry
from faststack.imaging.prefetch import _decode_buffer


def _decode(path, *, is_current=None):
    return _decode_buffer(
        path, 0, 0, False, False, False, False, "cover", 0, is_current=is_current
    )


def test_truncated_snapshot_is_reread_after_file_grows(tmp_path):
    path = tmp_path / "growing.jpg"
    path.write_bytes(b"truncated")
    snapshots = []

    def decode(snapshot, **_kwargs):
        data = bytes(snapshot)
        snapshots.append(data)
        if data == b"truncated":
            raise IncompleteJPEGError("still writing")
        return np.zeros((2, 3, 3), dtype=np.uint8)

    def grow(_delay):
        path.write_bytes(b"complete snapshot")

    with (
        patch("faststack.imaging.prefetch.decode_jpeg_rgb", side_effect=decode),
        patch("faststack.imaging.prefetch.time.sleep", side_effect=grow),
    ):
        result = _decode(path)

    assert result[0] is not None
    assert snapshots == [b"truncated", b"complete snapshot"]


def test_two_truncated_snapshots_return_explicit_failure(tmp_path):
    path = tmp_path / "bad.jpg"
    path.write_bytes(b"truncated")
    with (
        patch(
            "faststack.imaging.prefetch.decode_jpeg_rgb",
            side_effect=IncompleteJPEGError("still writing"),
        ) as decode,
        patch("faststack.imaging.prefetch.time.sleep"),
    ):
        result = _decode(path)

    assert result[0] is None
    assert result[-1] == "incomplete-jpeg"
    assert decode.call_count == 2


def test_pixels_returned_with_truncation_warning_are_rejected():
    class Decoder:
        def decode(self, _data, **_kwargs):
            warnings.warn("Premature end of JPEG file", RuntimeWarning)
            return np.ones((2, 2, 3), dtype=np.uint8)

    with pytest.raises(IncompleteJPEGError):
        _decode_with_retry(b"snapshot", decoder=Decoder())


def test_stale_generation_between_attempts_stops_before_second_read(tmp_path):
    path = tmp_path / "stale.jpg"
    path.write_bytes(b"truncated")
    real_open = builtins.open
    reads = 0

    def counting_open(*args, **kwargs):
        nonlocal reads
        if args and args[0] == path and args[1] == "rb":
            reads += 1
        return real_open(*args, **kwargs)

    with (
        patch("builtins.open", side_effect=counting_open),
        patch(
            "faststack.imaging.prefetch.decode_jpeg_rgb",
            side_effect=IncompleteJPEGError("still writing"),
        ) as decode,
        patch("faststack.imaging.prefetch.time.sleep") as sleep,
    ):
        result = _decode(path, is_current=lambda: False)

    assert result[0] is None
    assert result[-1] == "stale-jpeg-retry"
    assert decode.call_count == 1
    assert reads == 1
    sleep.assert_not_called()


def test_valid_jpeg_uses_single_filesystem_snapshot(tmp_path):
    path = tmp_path / "valid.jpg"
    path.write_bytes(b"valid")
    real_open = builtins.open
    reads = 0

    def counting_open(*args, **kwargs):
        nonlocal reads
        if args and args[0] == path and args[1] == "rb":
            reads += 1
        return real_open(*args, **kwargs)

    with (
        patch("builtins.open", side_effect=counting_open),
        patch(
            "faststack.imaging.prefetch.decode_jpeg_rgb",
            return_value=np.zeros((2, 3, 3), dtype=np.uint8),
        ),
        patch("faststack.imaging.prefetch.time.sleep") as sleep,
    ):
        result = _decode(path)

    assert result[0] is not None
    assert reads == 1
    sleep.assert_not_called()
