"""Quick test to check cachetools.LRUCache API."""
from cachetools import LRUCache

# Create a basic LRUCache
cache = LRUCache(maxsize=100)

# Check if maxsize is a property or method
print(f"Type of maxsize: {type(cache.maxsize)}")
print(f"maxsize value: {cache.maxsize}")

# Check if we can access the internal attribute
if hasattr(cache, '_Cache__maxsize'):
    print(f"Internal _Cache__maxsize: {cache._Cache__maxsize}")

# List all attributes
print(f"\nAll cache attributes: {[attr for attr in dir(cache) if not attr.startswith('_')]}")
