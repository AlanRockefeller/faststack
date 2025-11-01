"""Tests for the byte-aware LRU cache."""

import pytest

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
    assert cache.maxsize == 1000
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
    cache["a"] = MockItem(50) # a is oldest
    cache["b"] = MockItem(40)
    cache["c"] = MockItem(30) # This should evict 'a'

    assert "a" not in cache
    assert "b" in cache
    assert "c" in cache
    assert cache.currsize == 70 # 40 + 30

    cache["d"] = MockItem(50) # This should evict 'b'
    assert "b" not in cache
    assert "c" in cache
    assert "d" in cache
    assert cache.currsize == 80 # 30 + 50

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
