import time

from faststack.inat.tiles import (
    TileCache,
    TileFetcher,
    lon_lat_to_tile,
    tile_to_lon_lat,
)


def test_tile_math_round_trip():
    x, y = lon_lat_to_tile(-122.273, 37.8715, 15)
    lon, lat = tile_to_lon_lat(x, y, 15)
    assert abs(lon + 122.273) < 1e-6
    assert abs(lat - 37.8715) < 1e-6
    # Tile for Berkeley at z=15 (standard slippy-map formula)
    assert int(x) == 5254
    assert int(y) == 12654


def test_cache_put_get_records_use(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=10_000)
    assert cache.get(3, 1, 2) is None
    cache.put(3, 1, 2, b"abc")
    assert cache.get(3, 1, 2) == b"abc"
    assert cache.get(3, 1, 2) == b"abc"
    row = cache._db.execute(
        "SELECT use_count, size FROM tiles WHERE z=3 AND x=1 AND y=2"
    ).fetchone()
    assert row == (3, 3)  # put counts as a use, plus two gets
    assert cache.stats().tiles == 1
    cache.close()


def test_eviction_prefers_rarely_used_old_tiles(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=25)
    now = time.time()
    old = now - 100 * 24 * 3600
    # Three 10-byte tiles, all fetched long ago (so the grace window is over).
    for i in range(3):
        cache.put(5, i, 0, b"x" * 10)
    cache._db.execute(
        "UPDATE tiles SET fetched_at=?, last_used=?, use_count=1", (old, old)
    )
    # Tile 1 used often but long ago; tile 2 used once recently; tile 0 used once long ago.
    cache._db.execute("UPDATE tiles SET use_count=40 WHERE x=1")
    cache._db.execute("UPDATE tiles SET last_used=? WHERE x=2", (now - 3600,))
    cache._db.commit()
    # Adding a fourth tile puts us at 40 bytes > 25; need to drop two.
    cache.put(5, 3, 0, b"x" * 10)
    remaining = {r[0] for r in cache._db.execute("SELECT x FROM tiles").fetchall()}
    # Tile 0 (old, rare) goes first; tile 2 (recent, rare) vs tile 1 (old,
    # frequent): the frequency bonus (log2(41)≈5.4 weeks) outweighs 100 days
    # minus an hour? 5.4 weeks < 100 days, so tile 1 goes next.
    assert 3 in remaining  # fresh, protected
    assert 2 in remaining
    assert 0 not in remaining
    assert 1 not in remaining
    cache.close()


def test_fresh_tiles_survive_eviction(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=5)
    cache.put(2, 0, 0, b"aaaa")
    cache.put(2, 1, 0, b"bbbb")  # over budget, but both are fresh
    assert cache.stats().tiles == 2
    cache.close()


def test_reconcile_drops_missing_files(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=1000)
    cache.put(4, 1, 1, b"zz")
    cache.close()
    (tmp_path / "tiles" / "4" / "1" / "1.png").unlink()
    cache = TileCache(tmp_path, budget_bytes=1000)
    assert cache.stats().tiles == 0
    cache.close()


def test_fetcher_uses_cache_then_network(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=1000)
    calls = []

    def opener(url, ua):
        calls.append((url, ua))
        return b"png-bytes"

    fetcher = TileFetcher(cache, "FastStack-test", opener=opener)
    assert fetcher.fetch(10, 5, 6) == b"png-bytes"
    assert fetcher.fetch(10, 5, 6) == b"png-bytes"
    assert len(calls) == 1
    assert calls[0] == ("https://tile.openstreetmap.org/10/5/6.png", "FastStack-test")
    cache.close()


def test_fetcher_wraps_x_and_rejects_bad_y(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=1000)
    seen = []

    def opener(url, ua):
        seen.append(url)
        return b"p"

    fetcher = TileFetcher(cache, "ua", opener=opener)
    fetcher.fetch(3, 9, 2)  # 9 wraps to 1 at z=3
    assert seen[-1].endswith("/3/1/2.png")
    assert fetcher.fetch(3, 0, 8) is None
    assert fetcher.fetch(3, 0, -1) is None
    cache.close()


def test_fetcher_backs_off_after_failure(tmp_path):
    cache = TileCache(tmp_path, budget_bytes=1000)
    calls = []

    def opener(url, ua):
        calls.append(url)
        raise OSError("offline")

    fetcher = TileFetcher(cache, "ua", opener=opener)
    assert fetcher.fetch(5, 1, 1) is None
    assert fetcher.fetch(5, 1, 1) is None
    assert len(calls) == 1
    cache.close()
