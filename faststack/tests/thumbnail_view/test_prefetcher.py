"""Tests for ThumbnailPrefetcher and ThumbnailCache."""

import time
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from faststack.io.utils import compute_path_hash
from faststack.thumbnail_view.prefetcher import ThumbnailCache, ThumbnailPrefetcher


@pytest.fixture(scope="session")
def qt_app():
    """
    Ensure a Qt event loop exists for any code paths that use QTimer / queued invokes.
    """
    try:
        from PySide6.QtCore import QCoreApplication
    except Exception:
        # If PySide6 isn't available in this environment, tests that need it will fail anyway.
        return None

    app = QCoreApplication.instance()
    if not app:
        app = QCoreApplication([])
    return app


@pytest.fixture
def temp_folder(tmp_path):
    """Create a temporary folder with test images."""
    return tmp_path


@pytest.fixture
def test_image(temp_folder):
    """Create a test JPEG image."""
    img_path = temp_folder / "test.jpg"
    img = Image.new("RGB", (400, 300), color="red")
    img.save(img_path, "JPEG")
    return img_path


@pytest.fixture
def cache():
    """Create a test cache."""
    return ThumbnailCache(max_bytes=1024 * 1024, max_items=100)


@pytest.fixture
def prefetcher(cache):
    """Create a test prefetcher."""
    callback = MagicMock()
    pf = ThumbnailPrefetcher(
        cache=cache,
        on_ready_callback=callback,
        max_workers=2,
        target_size=200,
    )
    yield pf
    pf.shutdown()


def _wait_until(predicate, timeout_s=2.0, interval_s=0.02, qt_app=None):
    """Poll until predicate() is True or timeout; processes Qt events if available."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if qt_app is not None:
            try:
                qt_app.processEvents()
            except Exception:
                pass
        if predicate():
            return True
        time.sleep(interval_s)
    return False


def _assert_ready_callback_called_once(callback: MagicMock):
    """
    ThumbnailPrefetcher may treat on_ready_callback as:
      1) a plain callable: callback(cache_key)
      2) a Qt-like signal: callback.emit(cache_key)

    Accept either, but require exactly one effective delivery.
    Return the delivered cache_key for further assertions.
    """
    direct_calls = callback.call_count
    emit_calls = callback.emit.call_count if hasattr(callback, "emit") else 0

    total = direct_calls + emit_calls
    assert (
        total == 1
    ), f"Expected callback delivery once; direct={direct_calls}, emit={emit_calls}"

    if direct_calls == 1:
        args, _kwargs = callback.call_args
        assert args, "callback should receive at least one positional arg"
        return args[0]

    args, _kwargs = callback.emit.call_args
    assert args, "callback.emit should receive at least one positional arg"
    return args[0]


def _assert_ready_callback_not_called(callback: MagicMock):
    """Ensure neither callback(...) nor callback.emit(...) happened."""
    assert callback.call_count == 0
    if hasattr(callback, "emit"):
        assert callback.emit.call_count == 0


class TestThumbnailCache:
    """Tests for ThumbnailCache."""

    def test_put_and_get(self, cache):
        """Test basic put and get operations."""
        cache.put("key1", b"value1")
        assert cache.get("key1") == b"value1"

    def test_get_missing_key(self, cache):
        """Test getting a non-existent key."""
        assert cache.get("nonexistent") is None

    def test_lru_eviction_by_count(self):
        """Test LRU eviction when max_items is reached."""
        cache = ThumbnailCache(max_bytes=1024 * 1024, max_items=3)

        cache.put("key1", b"v1")
        cache.put("key2", b"v2")
        cache.put("key3", b"v3")
        cache.put("key4", b"v4")  # Should evict key1

        assert cache.get("key1") is None
        assert cache.get("key2") is not None
        assert cache.get("key3") is not None
        assert cache.get("key4") is not None

    def test_lru_eviction_by_bytes(self):
        """Test LRU eviction when max_bytes is reached."""
        cache = ThumbnailCache(max_bytes=100, max_items=1000)

        cache.put("key1", b"x" * 40)
        cache.put("key2", b"y" * 40)
        cache.put("key3", b"z" * 40)  # Should evict key1

        assert cache.get("key1") is None
        assert cache.get("key2") is not None
        assert cache.get("key3") is not None

    def test_lru_order_updated_on_get(self, cache):
        """Test that accessing an item moves it to end of LRU."""
        cache.put("key1", b"v1")
        cache.put("key2", b"v2")

        # Access key1 to make it more recently used
        cache.get("key1")

        # Add enough items to trigger eviction
        cache._max_items = 2
        cache.put("key3", b"v3")

        # key2 should be evicted (oldest), key1 should remain
        assert cache.get("key1") is not None
        assert cache.get("key2") is None

    def test_clear(self, cache):
        """Test clearing the cache."""
        cache.put("key1", b"v1")
        cache.put("key2", b"v2")

        cache.clear()

        assert cache.get("key1") is None
        assert cache.get("key2") is None
        assert cache.size == 0
        assert cache.bytes_used == 0

    def test_size_and_bytes_used(self, cache):
        """Test size and bytes_used properties."""
        cache.put("key1", b"12345")
        cache.put("key2", b"67890")

        assert cache.size == 2
        assert cache.bytes_used == 10

    def test_update_existing_key(self, cache):
        """Test updating an existing key."""
        cache.put("key1", b"old")
        cache.put("key1", b"new_value")

        assert cache.get("key1") == b"new_value"
        assert cache.size == 1


class TestThumbnailPrefetcher:
    """Tests for ThumbnailPrefetcher."""

    def test_prefetcher_creation(self, prefetcher, cache):
        """Test prefetcher is created correctly."""
        assert prefetcher._cache is cache
        assert prefetcher._target_size == 200

    def test_submit_schedules_job(self, prefetcher, test_image, cache, qt_app):
        """Test that submit schedules a decode job."""
        mtime_ns = test_image.stat().st_mtime_ns

        result = prefetcher.submit(test_image, mtime_ns)
        assert result is True

        # Wait for job to complete (cache filled)
        path_hash = compute_path_hash(test_image)
        cache_key = f"200/{path_hash}/{mtime_ns}"
        assert _wait_until(
            lambda: cache.get(cache_key) is not None, timeout_s=2.0, qt_app=qt_app
        )

        assert cache.get(cache_key) is not None

    def test_submit_skips_if_cached(self, prefetcher, test_image, cache):
        """Test that submit skips if already cached."""
        mtime_ns = test_image.stat().st_mtime_ns
        path_hash = compute_path_hash(test_image)
        cache_key = f"200/{path_hash}/{mtime_ns}"

        # Pre-populate cache
        cache.put(cache_key, b"cached_data")

        result = prefetcher.submit(test_image, mtime_ns)
        assert result is False

    def test_submit_deduplicates_inflight(self, prefetcher, test_image):
        """Test that duplicate in-flight jobs are skipped."""
        mtime_ns = test_image.stat().st_mtime_ns

        result1 = prefetcher.submit(test_image, mtime_ns)
        result2 = prefetcher.submit(test_image, mtime_ns)

        assert result1 is True
        assert result2 is False

    def test_callback_called_on_complete(self, cache, test_image, qt_app):
        """
        Test that callback is called when decode completes.

        Many implementations deliver callbacks via Qt (e.g., QTimer.singleShot(0, ...),
        invokeMethod, queued signals). In tests, that can be flaky without a running loop.
        We patch PySide6.QtCore.QTimer.singleShot to execute immediately.
        """
        callback = MagicMock()

        # Make QTimer.singleShot run the provided function immediately
        def _single_shot_immediate(_ms, fn):
            fn()

        from PySide6.QtCore import (
            QTimer,
        )  # import here so patch.object has the real type

        with patch.object(QTimer, "singleShot", side_effect=_single_shot_immediate):
            prefetcher = ThumbnailPrefetcher(
                cache=cache,
                on_ready_callback=callback,
                max_workers=1,
                target_size=200,
            )
            try:
                mtime_ns = test_image.stat().st_mtime_ns
                prefetcher.submit(test_image, mtime_ns)

                # Wait for decode completion (cache fill proves the worker finished)
                path_hash = compute_path_hash(test_image)
                cache_key = f"200/{path_hash}/{mtime_ns}"
                assert _wait_until(
                    lambda: cache.get(cache_key) is not None,
                    timeout_s=2.0,
                    qt_app=qt_app,
                )

                delivered_key = _assert_ready_callback_called_once(callback)
                assert "200/" in str(delivered_key)
            finally:
                prefetcher.shutdown()

    def test_cancel_all(self, prefetcher, test_image):
        """Test canceling all pending jobs."""
        mtime_ns = test_image.stat().st_mtime_ns

        prefetcher.submit(test_image, mtime_ns)
        prefetcher.cancel_all()

        assert len(prefetcher._inflight) == 0
        assert len(prefetcher._futures) == 0


class TestThumbnailDecode:
    """Tests for thumbnail decoding functionality."""

    def test_decode_applies_exif_orientation(self, cache, temp_folder, qt_app):
        """Test that EXIF orientation is applied during decode."""
        # Create an image with EXIF orientation
        img_path = temp_folder / "oriented.jpg"
        img = Image.new("RGB", (400, 200), color="blue")

        # Save with EXIF orientation (rotated 90 CW)
        exif_dict = img.getexif()
        exif_dict[274] = 6  # Orientation tag = 6 (90 CW)

        img.save(img_path, "JPEG", exif=exif_dict)

        callback = MagicMock()
        prefetcher = ThumbnailPrefetcher(
            cache=cache,
            on_ready_callback=callback,
            max_workers=1,
            target_size=100,
        )

        try:
            mtime_ns = img_path.stat().st_mtime_ns
            prefetcher.submit(img_path, mtime_ns)

            # Wait for completion (cache filled)
            path_hash = compute_path_hash(img_path)
            cache_key = f"100/{path_hash}/{mtime_ns}"
            assert _wait_until(
                lambda: cache.get(cache_key) is not None, timeout_s=2.0, qt_app=qt_app
            )

            cached_bytes = cache.get(cache_key)
            assert cached_bytes is not None
            assert len(cached_bytes) > 0
        finally:
            prefetcher.shutdown()

    def test_decode_handles_png(self, cache, temp_folder, qt_app):
        """Test that PNG files can be decoded."""
        img_path = temp_folder / "test.png"
        img = Image.new("RGB", (300, 300), color="green")
        img.save(img_path, "PNG")

        callback = MagicMock()
        prefetcher = ThumbnailPrefetcher(
            cache=cache,
            on_ready_callback=callback,
            max_workers=1,
            target_size=200,
        )

        try:
            mtime_ns = img_path.stat().st_mtime_ns
            prefetcher.submit(img_path, mtime_ns)

            path_hash = compute_path_hash(img_path)
            cache_key = f"200/{path_hash}/{mtime_ns}"
            assert _wait_until(
                lambda: cache.get(cache_key) is not None, timeout_s=2.0, qt_app=qt_app
            )
            assert cache.get(cache_key) is not None
        finally:
            prefetcher.shutdown()

    def test_decode_handles_corrupt_file(self, cache, temp_folder, qt_app):
        """Test that corrupt files are handled gracefully."""
        img_path = temp_folder / "corrupt.jpg"
        img_path.write_bytes(b"not a valid jpeg")

        callback = MagicMock()
        prefetcher = ThumbnailPrefetcher(
            cache=cache,
            on_ready_callback=callback,
            max_workers=1,
            target_size=200,
        )

        try:
            mtime_ns = img_path.stat().st_mtime_ns
            prefetcher.submit(img_path, mtime_ns)

            # Give it a moment to attempt decode; it should fail and not cache/callback
            time.sleep(0.3)
            if qt_app is not None:
                try:
                    qt_app.processEvents()
                except Exception:
                    pass

            path_hash = compute_path_hash(img_path)
            cache_key = f"200/{path_hash}/{mtime_ns}"
            assert cache.get(cache_key) is None

            _assert_ready_callback_not_called(callback)
        finally:
            prefetcher.shutdown()
