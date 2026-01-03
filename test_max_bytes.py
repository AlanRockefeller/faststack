"""Quick test to verify ByteLRUCache.max_bytes works correctly."""
from faststack.imaging.cache import ByteLRUCache

class MockItem:
    def __init__(self, size: int):
        self._size = size
    
    def __sizeof__(self) -> int:
        return self._size

# Test 1: Initialize cache
cache = ByteLRUCache(max_bytes=1000, size_of=lambda x: x.__sizeof__())
print(f"Initial max_bytes: {cache.max_bytes}")
assert cache.max_bytes == 1000, "Initial max_bytes should be 1000"

# Test 2: Add items
cache["a"] = MockItem(50)
cache["b"] = MockItem(40)
print(f"Current size: {cache.currsize}, Max bytes: {cache.max_bytes}")
assert cache.currsize == 90, "Current size should be 90"

# Test 3: Change max_bytes and verify eviction works
cache.max_bytes = 80
print(f"New max_bytes: {cache.max_bytes}")
assert cache.max_bytes == 80, "max_bytes should be updated to 80"

# Test 4: Add an item that triggers eviction
cache["c"] = MockItem(50)
print(f"After eviction - Current size: {cache.currsize}, Items: {list(cache.keys())}")

# "a" should have been evicted (LRU)
assert "a" not in cache, "Item 'a' should have been evicted"
assert "b" in cache or "c" in cache, "At least one of 'b' or 'c' should be in cache"
assert cache.currsize <= cache.max_bytes, f"Current size {cache.currsize} should be <= max_bytes {cache.max_bytes}"

print("\n✓ All tests passed! ByteLRUCache.max_bytes works correctly.")
