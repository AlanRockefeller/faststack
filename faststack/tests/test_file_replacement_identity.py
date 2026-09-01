import os
from unittest.mock import MagicMock


def test_equal_size_equal_mtime_replacement_is_a_new_cache_identity(
    app_controller, tmp_path
):
    path = tmp_path / "photo.jpg"
    replacement = tmp_path / "replacement.jpg"
    path.write_bytes(b"AAAA")
    original_time = path.stat().st_mtime_ns
    replacement.write_bytes(b"BBBB")
    os.utime(replacement, ns=(original_time, original_time))

    first_identity = app_controller._file_state_fingerprint(path)
    app_controller.image_cache = MagicMock()
    app_controller.prefetcher = MagicMock()
    app_controller._invalidate_decoded_path(path)

    os.replace(replacement, path)
    os.utime(path, ns=(original_time, original_time))
    second_identity = app_controller._file_state_fingerprint(path)
    app_controller._invalidate_decoded_path(path)

    assert path.read_bytes() == b"BBBB"
    assert first_identity != second_identity
    assert app_controller.image_cache.pop_path.call_count == 2
    assert app_controller.prefetcher.invalidate_path.call_count == 2
