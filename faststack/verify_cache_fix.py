import sys
import os
from pathlib import Path

# Add current dir to path
sys.path.append(os.getcwd())

from imaging.cache import ByteLRUCache
from models import DecodedImage
import numpy as np


def test_cache():
    evicted = []

    def on_evict(k, v):
        evicted.append((k, v))
        print(f"Evicted: {k}")

    cache = ByteLRUCache(max_bytes=100, size_of=sys.getsizeof, on_evict=on_evict)
    img1 = DecodedImage(
        buffer=memoryview(np.zeros(60, dtype=np.uint8)),
        width=60,
        height=1,
        bytes_per_line=60,
        format="dummy_format",
    )
    img2 = DecodedImage(
        buffer=memoryview(np.zeros(60, dtype=np.uint8)),
        width=60,
        height=1,
        bytes_per_line=60,
        format="dummy_format",
    )

    cache["k1"] = img1
    print("Added k1")
    cache["k2"] = img2
    print("Added k2")

    assert len(evicted) == 1
    assert evicted[0][0] == "k1"
    print("Eviction verified!")

    cache.popitem()
    assert len(evicted) == 2
    assert evicted[1][0] == "k2"
    print("Popitem verification passed!")


if __name__ == "__main__":
    try:
        test_cache()
        print("STANDALONE TEST PASSED")
    except Exception as e:
        print(f"TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
