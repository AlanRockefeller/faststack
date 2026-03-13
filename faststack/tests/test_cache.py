"""Tests for the byte-aware LRU cache."""

from faststack.imaging.cache import ByteLRUCache


class MockItem:
    """A mock object with a settable size."""

    def __init__(self, size: int):
        self._size = size

    def __sizeof__(self) -> int:
        return self._size


def test_cache_init():
    """Tests cache initialization."""
    cache = ByteLRUCache(max_bytes=1000, size_of=lambda x: x.__sizeof__())
    assert cache.max_bytes == 1000
    assert cache.currsize == 0


def test_cache_add_items():
    """Tests adding items and tracking size."""
    cache = ByteLRUCache(max_bytes=100, size_of=lambda x: x.__sizeof__())
    cache["a"] = MockItem(20)
    assert cache.currsize == 20
    cache["b"] = MockItem(30)
    assert cache.currsize == 50
    assert "a" in cache
    assert "b" in cache


def test_cache_eviction():
    """Tests that the least recently used item is evicted when full."""
    cache = ByteLRUCache(max_bytes=100, size_of=lambda x: x.__sizeof__())
    cache["a"] = MockItem(50)  # a is oldest
    cache["b"] = MockItem(40)
    cache["c"] = MockItem(30)  # This should evict 'a'

    assert "a" not in cache
    assert "b" in cache
    assert "c" in cache
    assert cache.currsize == 70  # 40 + 30

    cache["d"] = MockItem(50)  # This should evict 'b'
    assert "b" not in cache
    assert "c" in cache
    assert "d" in cache
    assert cache.currsize == 80  # 30 + 50


def test_cache_update_item():
    """Tests that updating an item adjusts the cache size."""
    cache = ByteLRUCache(max_bytes=100, size_of=lambda x: x.__sizeof__())
    cache["a"] = MockItem(20)
    assert cache.currsize == 20

    # Replace with a larger item
    cache["a"] = MockItem(50)
    assert cache.currsize == 50

    # Replace with a smaller item
    cache["a"] = MockItem(10)
    assert cache.currsize == 10


def test_get_decoded_image_size_with_nbytes():
    """Tests when buffer has nbytes."""
    from faststack.imaging.cache import get_decoded_image_size
    from faststack.models import DecodedImage

    class MockBuffer:
        def __init__(self, nbytes):
            self.nbytes = nbytes

    buffer = MockBuffer(nbytes=100)
    item = DecodedImage(
        buffer=buffer, width=10, height=10, bytes_per_line=40, format=None
    )
    assert get_decoded_image_size(item) == 100


def test_get_decoded_image_size_fallback_metadata():
    """Tests fallback when buffer lacks nbytes but has metadata."""
    from faststack.imaging.cache import get_decoded_image_size
    from faststack.models import DecodedImage

    class MockBuffer:
        pass

    buffer = MockBuffer()
    item = DecodedImage(
        buffer=buffer, width=10, height=10, bytes_per_line=30, format=None
    )
    # bytes_per_pixel = 30 // 10 = 3 (RGB, no overcounting)
    # size = 10 * 10 * 3 = 300
    assert get_decoded_image_size(item) == 300


def test_get_decoded_image_size_fallback_default():
    """Tests fallback when metadata is missing (should default to 4)."""
    from faststack.imaging.cache import get_decoded_image_size
    from types import SimpleNamespace

    class MockBuffer:
        pass

    buffer = MockBuffer()
    # Use SimpleNamespace to build a minimal object that lacks bytes_per_line
    item = SimpleNamespace(buffer=buffer, width=10, height=10)

    # size = 10 * 10 * 4 = 400
    assert get_decoded_image_size(item) == 400
