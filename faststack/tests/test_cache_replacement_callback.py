"""Tests for ByteLRUCache eviction callbacks."""

import threading
from faststack.imaging.cache import ByteLRUCache


def _make_cache(max_bytes, on_evict):
    """Helper: integer-sized cache with eviction callback."""
    return ByteLRUCache(max_bytes=max_bytes, size_of=lambda x: x, on_evict=on_evict)


# ── Replacement ──────────────────────────────────────────────────────


def test_replacement_fires_callback_exactly_once():
    """Put key A, then put key A again -> callback called once with old value."""
    evicted = []
    cache = _make_cache(200, lambda k, v: evicted.append((k, v)))

    cache["a"] = 40
    cache["a"] = 60  # replace

    assert evicted == [("a", 40)]
    assert cache["a"] == 60
    assert cache.currsize == 60


def test_replacement_plus_lru_eviction():
    """Replace key A with a larger value that forces eviction of key B."""
    evicted = []
    cache = _make_cache(100, lambda k, v: evicted.append((k, v)))

    cache["a"] = 40
    cache["b"] = 40
    # a(40) + b(40) = 80.  Now replace a with 70 -> a(70) + b(40) = 110 > 100.
    cache["a"] = 70

    from collections import defaultdict
    evicted_map = defaultdict(list)
    for k, v in evicted:
        evicted_map[k].append(v)

    assert "a" in evicted_map, "old value of 'a' should be reported"
    assert "b" in evicted_map, "'b' should be evicted by LRU pressure"
    assert 40 in evicted_map["a"]
    assert 40 in evicted_map["b"]


# ── LRU eviction ────────────────────────────────────────────────────


def test_lru_eviction_fires_callback():
    """Fill cache past maxsize -> LRU item evicted with callback."""
    evicted = []
    cache = _make_cache(100, lambda k, v: evicted.append((k, v)))

    cache["a"] = 60
    cache["b"] = 60  # total 120 > 100: evicts "a"

    assert evicted == [("a", 60)]
    assert "a" not in cache
    assert "b" in cache


def test_multiple_evictions_in_one_put():
    """A single large insert can evict multiple items."""
    evicted = []
    cache = _make_cache(100, lambda k, v: evicted.append((k, v)))

    cache["a"] = 30
    cache["b"] = 30
    cache["c"] = 30
    # a+b+c = 90.  Insert d=80 -> must evict a, b, c to make room.
    cache["d"] = 80

    evicted_keys = {k for k, _ in evicted}
    assert {"a", "b", "c"} <= evicted_keys


# ── Eviction order ──────────────────────────────────────────────────


def test_replacement_peek_does_not_change_eviction_order():
    """Peeking at old value during replacement must not alter LRU eviction order.

    Scenario: a(LRU), b, c(MRU) near capacity.  Replace c → old-c is peeked.
    If the peek moved c to MRU, that's fine (c is being replaced anyway).
    The critical thing: a must still be evicted first, not b.
    """
    evicted = []
    cache = _make_cache(100, lambda k, v: evicted.append((k, v)))

    cache["a"] = 30  # LRU
    cache["b"] = 30
    cache["c"] = 30  # MRU  (total 90)

    # Touch "a" to make it NOT the LRU — now b is LRU
    _ = cache["a"]

    # Replace "c" with larger value that forces an eviction.
    # Total would be 30(a) + 30(b) + 50(c) = 110 > 100.
    # b is LRU and should be evicted, NOT a.
    cache["c"] = 50

    evicted_keys = [k for k, _ in evicted]
    # "c" old value reported as replacement
    assert "c" in evicted_keys
    # "b" evicted by LRU pressure (it was least recently used)
    assert "b" in evicted_keys
    # "a" must NOT be evicted (it was touched more recently than b)
    assert "a" not in evicted_keys
    assert "a" in cache


# ── Eviction path verification ──────────────────────────────────────


def test_eviction_goes_through_delitem():
    """Prove that LRU eviction during __setitem__ routes through __delitem__.

    If cachetools ever changes popitem() to bypass __delitem__, this test
    will catch the regression and we'll need to reintroduce a popitem() override.
    """
    delitem_keys = []

    class TracingCache(ByteLRUCache):
        def __delitem__(self, key):
            delitem_keys.append(key)
            super().__delitem__(key)

    cache = TracingCache(max_bytes=100, size_of=lambda x: x, on_evict=None)
    cache["a"] = 60
    cache["b"] = 60  # total 120 > 100: must evict "a" via __delitem__

    assert "a" in delitem_keys, (
        "LRU eviction did NOT route through __delitem__. "
        "Reintroduce popitem() override to restore eviction callbacks."
    )


def test_on_evict_fires_for_both_overflow_and_replacement():
    """Combined test: on_evict fires for both LRU overflow and key replacement."""
    evicted = []
    cache = _make_cache(100, lambda k, v: evicted.append((k, v)))

    # Phase 1: LRU overflow eviction
    cache["a"] = 60
    cache["b"] = 60  # evicts "a"
    assert ("a", 60) in evicted

    # Phase 2: replacement overwrite
    evicted.clear()
    cache["b"] = 50  # replaces old "b" (60) with new value (50)
    assert ("b", 60) in evicted
    assert cache["b"] == 50


# ── evict_paths + tombstones ────────────────────────────────────────


def test_evict_paths_suppresses_callbacks():
    """evict_paths() should NOT trigger on_evict (intentional removal, not LRU)."""
    evicted = []
    cache = _make_cache(10_000, lambda k, v: evicted.append((k, v)))

    cache["photo.jpg::0"] = 100
    cache["photo.jpg::1"] = 200
    cache["other.jpg::0"] = 300

    from pathlib import Path

    cache.evict_paths([Path("photo.jpg")])

    # Keys should be removed from cache
    assert "photo.jpg::0" not in cache
    assert "photo.jpg::1" not in cache
    assert "other.jpg::0" in cache

    # But on_evict should NOT have been called (intentional removal)
    assert len(evicted) == 0


def test_evict_paths_tombstone_blocks_reinsert():
    """After evict_paths(), re-caching the same prefix is silently blocked."""
    cache = _make_cache(10_000, None)

    cache["photo.jpg::0"] = 100
    from pathlib import Path

    cache.evict_paths([Path("photo.jpg")])
    assert "photo.jpg::0" not in cache

    # Tombstone should block re-insertion
    cache["photo.jpg::0"] = 100
    assert "photo.jpg::0" not in cache


def test_evict_paths_no_deadlock_under_contention():
    """evict_paths() must not deadlock when another thread is writing."""
    cache = _make_cache(1_000_000, None)

    # Pre-populate
    for i in range(50):
        cache[f"img_{i}.jpg::0"] = 100

    errors = []
    barrier = threading.Barrier(2, timeout=5)

    def writer():
        try:
            barrier.wait()
            for i in range(50, 150):
                cache[f"new_{i}.jpg::0"] = 100
        except Exception as e:
            errors.append(e)

    def evictor():
        try:
            barrier.wait()
            from pathlib import Path

            paths = [Path(f"img_{i}.jpg") for i in range(50)]
            cache.evict_paths(paths)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=evictor)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Errors during concurrent access: {errors}"
    assert not t1.is_alive(), "Writer thread deadlocked"
    assert not t2.is_alive(), "Evictor thread deadlocked"
