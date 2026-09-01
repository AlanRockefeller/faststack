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
    from types import SimpleNamespace

    from faststack.imaging.cache import get_decoded_image_size

    class MockBuffer:
        pass

    buffer = MockBuffer()
    # Use SimpleNamespace to build a minimal object that lacks bytes_per_line
    item = SimpleNamespace(buffer=buffer, width=10, height=10)

    # size = 10 * 10 * 4 = 400
    assert get_decoded_image_size(item) == 400


def _decoded_with_mask(width=64, height=48):
    """A DecodedImage shaped like one a darkened preview render produces."""
    import numpy as np

    from faststack.models import DecodedImage, ResolvedDarkenMask

    pixels = bytes(width * height * 3)
    mask = np.zeros((height, width), dtype=np.float32)
    mask.flags.writeable = False
    return (
        DecodedImage(
            buffer=memoryview(pixels),
            width=width,
            height=height,
            bytes_per_line=width * 3,
            format=None,
            darken_mask=ResolvedDarkenMask(
                mask=mask,
                width=width,
                height=height,
                mask_id="darken",
                mask_revision=1,
            ),
        ),
        width * height * 3,
        mask.nbytes,
    )


def test_decoded_image_size_counts_the_published_darken_mask():
    """A mask-bearing frame is 7/3 of its buffer, not 3/3.

    _seed_decode_cache_from_live_preview puts the last rendered preview into
    the byte-budgeted image cache on navigate-away, and that frame carries the
    float32 mask its render resolved. Charging only the RGB888 buffer would
    under-report the entry by 57%.
    """
    from faststack.imaging.cache import get_decoded_image_size

    item, buffer_bytes, mask_bytes = _decoded_with_mask()
    assert mask_bytes == buffer_bytes * 4 // 3  # float32 plane vs RGB888
    assert get_decoded_image_size(item) == buffer_bytes + mask_bytes
    # __sizeof__ is not what production budgets on, but must not disagree.
    assert item.__sizeof__() == buffer_bytes + mask_bytes


def test_decoded_image_size_unchanged_without_a_mask():
    """Ordinary decodes carry no mask and must not be charged for one."""
    from faststack.imaging.cache import get_decoded_image_size
    from faststack.models import DecodedImage

    pixels = bytes(64 * 48 * 3)
    item = DecodedImage(
        buffer=memoryview(pixels),
        width=64,
        height=48,
        bytes_per_line=64 * 3,
        format=None,
    )
    assert item.darken_mask is None
    assert get_decoded_image_size(item) == len(pixels)
    assert item.__sizeof__() == len(pixels)


def test_cache_budget_accounts_for_retained_masks():
    """The budget must evict on real retained bytes, not just pixel bytes."""
    from faststack.imaging.cache import ByteLRUCache

    item, buffer_bytes, mask_bytes = _decoded_with_mask()
    entry_bytes = buffer_bytes + mask_bytes

    cache = ByteLRUCache(max_bytes=entry_bytes)
    cache["a"] = item
    assert cache.currsize == entry_bytes

    # A second identical entry does not fit. Counting only the buffer would
    # have let both in and overshot the budget by a whole mask.
    other, _, _ = _decoded_with_mask()
    cache["b"] = other
    assert cache.currsize == entry_bytes
    assert "a" not in cache
    assert "b" in cache


def test_eviction_releases_the_charged_bytes():
    """Sizes stay stable across insert and delete, so currsize returns to 0."""
    from faststack.imaging.cache import ByteLRUCache

    item, buffer_bytes, mask_bytes = _decoded_with_mask()
    cache = ByteLRUCache(max_bytes=10 * (buffer_bytes + mask_bytes))
    cache["a"] = item
    assert cache.currsize == buffer_bytes + mask_bytes
    del cache["a"]
    assert cache.currsize == 0
