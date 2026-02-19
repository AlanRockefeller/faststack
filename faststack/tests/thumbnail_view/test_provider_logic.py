"""Tests for _parse_id logic and cache-hit decode recovery in ThumbnailProvider."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from faststack.thumbnail_view.provider import ThumbnailProvider, PLACEHOLDER_COLOR, ERROR_COLOR
from faststack.thumbnail_view.prefetcher import ThumbnailCache
from PySide6.QtCore import QSize


class TestProviderLogic:
    @pytest.fixture
    def provider(self):
        p = ThumbnailProvider.__new__(ThumbnailProvider)
        p._default_size = 200
        return p

    def test_parse_id_file_success(self, provider):
        id_str = "256/pathhash123/123456789?r=1&reason=scroll"
        parsed = provider._parse_id(id_str)

        assert parsed.id_clean == "256/pathhash123/123456789"
        assert parsed.parts == ["256", "pathhash123", "123456789"]
        assert parsed.thumb_size == 256
        assert parsed.path_hash == "pathhash123"
        assert parsed.mtime_ns == 123456789
        assert parsed.reason == "scroll"
        assert parsed.is_folder is False
        assert parsed.is_valid is True

    def test_parse_id_folder_success(self, provider):
        id_str = "folder/pathhash456/987654321?r=2"
        parsed = provider._parse_id(id_str)

        assert parsed.id_clean == "folder/pathhash456/987654321"
        assert parsed.parts == ["folder", "pathhash456", "987654321"]
        assert parsed.thumb_size == 200  # Default size
        assert parsed.path_hash == "pathhash456"
        assert parsed.mtime_ns == 987654321
        assert parsed.reason == "unknown"
        assert parsed.is_folder is True
        assert parsed.is_valid is True

    def test_parse_id_invalid_format(self, provider):
        id_str = "invalid/id"
        parsed = provider._parse_id(id_str)

        assert parsed.is_valid is False

    def test_parse_id_invalid_number(self, provider):
        id_str = "abc/pathhash/123"
        parsed = provider._parse_id(id_str)

        assert parsed.is_valid is False


class TestCacheDiscard:
    """Tests for ThumbnailCache.discard()."""

    def test_discard_existing_key(self):
        cache = ThumbnailCache(max_bytes=1024, max_items=10)
        cache.put("a", b"hello")
        assert cache.size == 1
        assert cache.bytes_used == 5

        assert cache.discard("a") is True
        assert cache.size == 0
        assert cache.bytes_used == 0
        assert cache.get("a") is None

    def test_discard_missing_key_returns_false(self):
        cache = ThumbnailCache(max_bytes=1024, max_items=10)
        assert cache.discard("nonexistent") is False
        assert cache.size == 0
        assert cache.bytes_used == 0

    def test_discard_updates_bytes_correctly(self):
        cache = ThumbnailCache(max_bytes=1024, max_items=10)
        cache.put("a", b"12345")  # 5 bytes
        cache.put("b", b"67890ab")  # 7 bytes
        assert cache.bytes_used == 12

        cache.discard("a")
        assert cache.bytes_used == 7
        assert cache.size == 1


class TestDecodeFailureRecovery:
    """Cache-hit decode failure should behave like a cache miss."""

    @pytest.fixture
    def wired_provider(self):
        """Build a ThumbnailProvider with mock cache, prefetcher, and path_resolver."""
        cache = ThumbnailCache(max_bytes=1024, max_items=10)
        prefetcher = MagicMock()
        prefetcher.PRIO_HIGH = 0
        prefetcher.submit = MagicMock(return_value=True)

        resolver = MagicMock(return_value=Path("/fake/photo.jpg"))

        provider = ThumbnailProvider(
            cache=cache,
            prefetcher=prefetcher,
            path_resolver=resolver,
            default_size=200,
        )
        return provider, cache, prefetcher

    def test_bad_cached_bytes_returns_placeholder_not_error(self, wired_provider):
        provider, cache, _prefetcher = wired_provider

        # Inject invalid JPEG bytes into the cache
        cache_key = "200/abc123/999"
        cache.put(cache_key, b"NOT-VALID-JPEG-DATA")

        out_size = QSize()
        result = provider.requestImage(f"{cache_key}?r=1", out_size, QSize())

        # Should return the loading placeholder (neutral gray), NOT error (dark red).
        # Compare RGB components to avoid QColor format/alpha surprises.
        assert not result.isNull()
        pixel = result.pixelColor(0, 0)
        assert (pixel.red(), pixel.green(), pixel.blue()) == (
            PLACEHOLDER_COLOR.red(),
            PLACEHOLDER_COLOR.green(),
            PLACEHOLDER_COLOR.blue(),
        )
        assert (pixel.red(), pixel.green(), pixel.blue()) != (
            ERROR_COLOR.red(),
            ERROR_COLOR.green(),
            ERROR_COLOR.blue(),
        )

    def test_bad_cached_bytes_evicts_and_submits(self, wired_provider):
        provider, cache, prefetcher = wired_provider

        cache_key = "200/abc123/999"
        cache.put(cache_key, b"NOT-VALID-JPEG-DATA")

        provider.requestImage(f"{cache_key}?r=1", QSize(), QSize())

        # The bad entry should have been evicted
        assert cache.get(cache_key) is None

        # Prefetcher should have been asked to re-decode with correct args
        prefetcher.submit.assert_called_once()
        args, kwargs = prefetcher.submit.call_args
        assert args[0] == Path("/fake/photo.jpg")
        assert args[1] == 999  # mtime_ns
        assert args[2] == 200  # thumb_size
        assert kwargs["priority"] == prefetcher.PRIO_HIGH
