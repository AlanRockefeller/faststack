"""OpenStreetMap tile cache for the location picker.

Tiles live on disk under a user-selectable directory with a SQLite index that
records every use. Eviction is by a recency + frequency score, so a tile the
user keeps coming back to (home turf) outlives one seen once on a trip, and
the least recently used tiles go first among those used equally often.

The cache budget is either fixed (``budget_bytes``) or, when ``None``, chosen
from the free space on the cache volume: 2% of free space, clamped to
[200 MB, 4 GB], and never more than free space minus a 1 GB headroom.

OSM's tile usage policy asks for a real User-Agent and at most two parallel
downloads; :class:`TileFetcher` honours both. See
https://operations.osmfoundation.org/policies/tiles/
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

TILE_SIZE = 256
MIN_ZOOM = 1
MAX_ZOOM = 19

AUTO_BUDGET_FRACTION = 0.02
AUTO_BUDGET_MIN = 200 * 1024 * 1024
AUTO_BUDGET_MAX = 4 * 1024 * 1024 * 1024
FREE_SPACE_HEADROOM = 1024 * 1024 * 1024

# One extra "week of recency" per doubling of use count.
_FREQUENCY_BONUS_SECONDS = 7 * 24 * 3600
# A tile fetched within this window is never evicted (it is probably on screen).
_FRESH_GRACE_SECONDS = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    z INTEGER NOT NULL,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    size INTEGER NOT NULL,
    fetched_at REAL NOT NULL,
    last_used REAL NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (z, x, y)
);
CREATE INDEX IF NOT EXISTS tiles_last_used ON tiles(last_used);
"""


@dataclass(frozen=True)
class CacheStats:
    tiles: int
    bytes: int
    budget_bytes: int
    directory: Path


def lon_lat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Fractional tile coordinates (x, y) for a point at ``zoom``."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
    )
    return x, y


def tile_to_lon_lat(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = 2**zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


class TileCache:
    """Disk cache with a usage-aware eviction policy. Thread-safe."""

    def __init__(self, directory: Path, budget_bytes: Optional[int] = None):
        self.directory = Path(directory)
        self._tiles_dir = self.directory / "tiles"
        self._tiles_dir.mkdir(parents=True, exist_ok=True)
        self._budget_override = budget_bytes
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(self.directory / "index.sqlite"), check_same_thread=False
        )
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._reconcile()

    # --- public -----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @property
    def budget_bytes(self) -> int:
        if self._budget_override:
            return int(self._budget_override)
        return self.auto_budget(self.directory)

    @staticmethod
    def auto_budget(directory: Path) -> int:
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            return AUTO_BUDGET_MIN
        budget = int(free * AUTO_BUDGET_FRACTION)
        budget = max(AUTO_BUDGET_MIN, min(AUTO_BUDGET_MAX, budget))
        return max(0, min(budget, free - FREE_SPACE_HEADROOM))

    def stats(self) -> CacheStats:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM tiles"
            ).fetchone()
        return CacheStats(
            tiles=int(row[0]),
            bytes=int(row[1]),
            budget_bytes=self.budget_bytes,
            directory=self.directory,
        )

    def get(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Return the tile's bytes and record the use, or ``None`` on a miss."""
        path = self._tile_path(z, x, y)
        try:
            data = path.read_bytes()
        except OSError:
            self._forget(z, x, y)
            return None
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "UPDATE tiles SET last_used=?, use_count=use_count+1 WHERE z=? AND x=? AND y=?",
                (now, z, x, y),
            )
            if cur.rowcount == 0:
                # Tile file present but not indexed (copied in by hand?)
                self._db.execute(
                    "INSERT OR REPLACE INTO tiles VALUES (?,?,?,?,?,?,1)",
                    (z, x, y, len(data), now, now),
                )
            self._db.commit()
        return data

    def put(self, z: int, x: int, y: int, data: bytes) -> None:
        path = self._tile_path(z, x, y)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO tiles (z,x,y,size,fetched_at,last_used,use_count) "
                "VALUES (?,?,?,?,?,?,COALESCE((SELECT use_count FROM tiles WHERE z=? AND x=? AND y=?),0)+1)",
                (z, x, y, len(data), now, now, z, x, y),
            )
            self._db.commit()
        self.evict_if_needed()

    def evict_if_needed(self, now: Optional[float] = None) -> int:
        """Drop lowest-scoring tiles until under budget. Returns tiles removed."""
        now = time.time() if now is None else now
        budget = self.budget_bytes
        removed = 0
        with self._lock:
            total = int(
                self._db.execute("SELECT COALESCE(SUM(size),0) FROM tiles").fetchone()[
                    0
                ]
            )
            if total <= budget:
                return 0
            rows = self._db.execute(
                "SELECT z,x,y,size,fetched_at,last_used,use_count FROM tiles"
            ).fetchall()
            scored = sorted(rows, key=lambda r: self._score(r[5], r[6]))
            for z, x, y, size, fetched_at, _last_used, _count in scored:
                if total <= budget:
                    break
                if now - fetched_at < _FRESH_GRACE_SECONDS:
                    continue
                try:
                    self._tile_path(z, x, y).unlink(missing_ok=True)
                except OSError as exc:
                    log.debug("Could not delete tile %s/%s/%s: %s", z, x, y, exc)
                    continue
                self._db.execute(
                    "DELETE FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
                )
                total -= size
                removed += 1
            self._db.commit()
        if removed:
            log.info("Tile cache: evicted %d tile(s) to stay under budget", removed)
        return removed

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tiles")
            self._db.commit()
        shutil.rmtree(self._tiles_dir, ignore_errors=True)
        self._tiles_dir.mkdir(parents=True, exist_ok=True)

    # --- internals --------------------------------------------------------

    @staticmethod
    def _score(last_used: float, use_count: int) -> float:
        """Higher keeps longer: recency plus a bonus for frequent use."""
        return last_used + math.log2(1 + max(0, use_count)) * _FREQUENCY_BONUS_SECONDS

    def _tile_path(self, z: int, x: int, y: int) -> Path:
        return self._tiles_dir / str(z) / str(x) / f"{y}.png"

    def _forget(self, z: int, x: int, y: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y))
            self._db.commit()

    def _reconcile(self) -> None:
        """Drop index rows whose files vanished (user deleted the folder)."""
        with self._lock:
            rows = self._db.execute("SELECT z,x,y FROM tiles").fetchall()
            missing = [r for r in rows if not self._tile_path(*r).exists()]
            if missing:
                self._db.executemany(
                    "DELETE FROM tiles WHERE z=? AND x=? AND y=?", missing
                )
                self._db.commit()


class TileFetcher:
    """Fetch OSM tiles through the cache, two downloads at a time."""

    TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

    def __init__(
        self,
        cache: TileCache,
        user_agent: str,
        opener: Optional[Callable[[str, str], bytes]] = None,
        max_parallel: int = 2,
    ):
        self.cache = cache
        self.user_agent = user_agent
        self._open = opener or self._default_open
        self._gate = threading.BoundedSemaphore(max_parallel)
        self._failed_at: dict[tuple[int, int, int], float] = {}
        self._failed_lock = threading.Lock()

    def fetch(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Cached bytes if present, else downloaded and stored. ``None`` on failure."""
        n = 2**z
        x %= n
        if y < 0 or y >= n or z < MIN_ZOOM or z > MAX_ZOOM:
            return None
        data = self.cache.get(z, x, y)
        if data is not None:
            return data
        key = (z, x, y)
        with self._failed_lock:
            failed = self._failed_at.get(key)
            if failed is not None and time.time() - failed < 60:
                return None
        url = self.TILE_URL.format(z=z, x=x, y=y)
        with self._gate:
            try:
                data = self._open(url, self.user_agent)
            except Exception as exc:
                log.debug("Tile fetch failed %s: %s", url, exc)
                with self._failed_lock:
                    self._failed_at[key] = time.time()
                return None
        if data:
            self.cache.put(z, x, y, data)
        return data

    @staticmethod
    def _default_open(url: str, user_agent: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
