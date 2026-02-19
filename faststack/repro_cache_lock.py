import threading
from faststack.imaging.cache import ByteLRUCache


def repro_lock_contention():
    lock_held_during_callback = False

    def on_evict_callback(key, value):
        nonlocal lock_held_during_callback
        # Try to acquire the same lock. If it's held by the current thread (RLock),
        # we can check if it would block others or if we can detect it's held.
        # Since it's an RLock, current thread can re-acquire it.
        # But we can check if the lock is "locked" by looking at internal state
        # or just by the fact that we know we are in the callback.

        # A better way to check if the lock is held:
        # Since it's an RLock, it doesn't expose a simple "is_locked" that works across threads easily
        # but we can try to acquire it in a DIFFERENT thread.

        def check_lock():
            nonlocal lock_held_during_callback
            if not cache._lock.acquire(blocking=False):
                lock_held_during_callback = True
            else:
                cache._lock.release()

        t = threading.Thread(target=check_lock)
        t.start()
        t.join()

    cache = ByteLRUCache(max_bytes=100, size_of=lambda x: x, on_evict=on_evict_callback)

    print("Adding item 'a' (50 bytes)")
    cache["a"] = 50
    print("Adding item 'b' (50 bytes)")
    cache["b"] = 50

    print("Adding item 'c' (50 bytes) -> should trigger eviction of 'a'")
    cache["c"] = 50

    if lock_held_during_callback:
        print("FAILED: Lock was HELD during on_evict callback!")
    else:
        print("SUCCESS: Lock was NOT held during on_evict callback.")


if __name__ == "__main__":
    repro_lock_contention()
