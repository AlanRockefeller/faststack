"""Group batch photos into observations by capture time and GPS proximity.

Pure logic, no Qt. The controller feeds :class:`PhotoRecord` values in, gets
:class:`ObservationGroup` values back, and the user can then move photos
between groups, split a group into ``n`` groups, or merge groups.

Two operations matter:

* :func:`initial_grouping` - the best first guess. Photos are sorted by
  capture time and a new observation starts wherever the gap to the previous
  photo is larger than the time threshold, or the GPS distance is larger than
  the distance threshold.
* :func:`split_group` - "there are actually N observations in here". Finds the
  optimal partition of the group's photos (in time order) into ``n``
  contiguous runs by dynamic programming, minimising the within-run spread of
  (time, position). Unlike "cut at the N-1 largest gaps" this also uses how
  tight each run is, which matters when several finds were photographed in
  quick succession from one spot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

# Defaults; the controller reads the real values from config.
DEFAULT_TIME_GAP_SECONDS = 10 * 60
DEFAULT_DISTANCE_METERS = 100.0
DEFAULT_SPREAD_WARNING_METERS = 100.0

# Feature scaling for split_group(): one "unit" of difference.
_SPLIT_TIME_SCALE_SECONDS = 60.0
_SPLIT_DISTANCE_SCALE_METERS = 10.0

_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class PhotoRecord:
    """One photo in the batch, with the EXIF facts grouping needs."""

    path: Path
    order: int
    taken: Optional[datetime] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    # False when ``taken`` is the file's modification time, not EXIF.
    taken_from_exif: bool = True
    # Which camera took it (EXIF make, model and serial), so a clock offset
    # learned for one camera is not applied to another (or to a phone).
    camera: str = ""
    # Seconds to add to the EXIF time for local time: the confirmed offset of
    # a camera set to the wrong time zone (see matching.py). 0 otherwise.
    clock_offset_s: int = 0

    @property
    def time(self) -> Optional[datetime]:
        """The capture time to group, sort and upload by: ``taken`` with the
        camera's clock offset applied. ``taken`` stays the camera's own
        clock, which is what matching compares against."""
        if self.taken is None or not self.clock_offset_s or not self.taken_from_exif:
            return self.taken
        return self.taken + timedelta(seconds=self.clock_offset_s)

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def key(self) -> str:
        return str(self.path)


@dataclass
class Location:
    lat: float
    lon: float
    source: str  # "exif", "manual"
    spread_m: float = 0.0  # furthest EXIF fix from the average
    photos_with_gps: int = 0
    spread_warning_m: float = DEFAULT_SPREAD_WARNING_METERS

    @property
    def spread_warning(self) -> bool:
        """True when the EXIF fixes disagree more than a camera's GPS error
        explains - the grouping is probably wrong."""
        return self.spread_m > self.spread_warning_m


@dataclass
class ObservationGroup:
    photos: list[PhotoRecord] = field(default_factory=list)
    # True once the user has dragged the photos into an order of their own:
    # ``photos`` is then that order and is no longer sorted by capture time.
    ordered: bool = False

    def sorted_photos(self) -> list[PhotoRecord]:
        """The photos as shown and uploaded: the user's order, else by time."""
        if self.ordered:
            return list(self.photos)
        return sorted(self.photos, key=_sort_key)

    @property
    def observed_at(self) -> Optional[datetime]:
        times = [p.time for p in self.photos if p.taken is not None]
        return min(times) if times else None

    @property
    def observed_at_exif(self) -> Optional[datetime]:
        """Earliest EXIF capture time, ignoring modification-time fallbacks."""
        times = [
            p.time for p in self.photos if p.taken is not None and p.taken_from_exif
        ]
        return min(times) if times else None

    def exif_location(
        self, spread_warning_m: float = DEFAULT_SPREAD_WARNING_METERS
    ) -> Optional[Location]:
        """Average the EXIF fixes in the group; ``None`` when none has GPS."""
        fixes = [(p.lat, p.lon) for p in self.photos if p.has_gps]
        if not fixes:
            return None
        lat, lon = average_coordinates(fixes)
        spread = max(haversine_m(lat, lon, flat, flon) for flat, flon in fixes)
        return Location(
            lat=lat,
            lon=lon,
            source="exif",
            spread_m=spread,
            photos_with_gps=len(fixes),
            spread_warning_m=spread_warning_m,
        )


def _sort_key(p: PhotoRecord):
    # Photos without a capture time go after those with one, in batch order.
    if p.taken is None:
        return (1, 0.0, p.order)
    return (0, p.time.timestamp(), p.order)


def group_sort_key(group: "ObservationGroup"):
    """Where a group belongs in time order: the key of its earliest photo."""
    return min((_sort_key(p) for p in group.photos), default=(2, 0.0, 0))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def average_coordinates(fixes: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Average lat/lon pairs on the unit sphere (safe across the antimeridian)."""
    x = y = z = 0.0
    for lat, lon in fixes:
        phi = math.radians(lat)
        lam = math.radians(lon)
        x += math.cos(phi) * math.cos(lam)
        y += math.cos(phi) * math.sin(lam)
        z += math.sin(phi)
    n = len(fixes)
    x /= n
    y /= n
    z /= n
    lon = math.degrees(math.atan2(y, x))
    hyp = math.sqrt(x * x + y * y)
    lat = math.degrees(math.atan2(z, hyp))
    return lat, lon


def _gap_seconds(a: PhotoRecord, b: PhotoRecord) -> Optional[float]:
    if a.taken is None or b.taken is None:
        return None
    return abs((b.time - a.time).total_seconds())


def _gap_meters(a: PhotoRecord, b: PhotoRecord) -> Optional[float]:
    if not (a.has_gps and b.has_gps):
        return None
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def initial_grouping(
    photos: Iterable[PhotoRecord],
    time_gap_s: float = DEFAULT_TIME_GAP_SECONDS,
    distance_m: float = DEFAULT_DISTANCE_METERS,
) -> list[ObservationGroup]:
    """Best first guess: time-sorted, break on a big time gap or a big move.

    The comparison is against the *previous* photo, not the group's first, so
    a slow walk along a log keeps one observation while a jump starts a new
    one. Photos with no capture time are compared by batch order only and
    stay with their neighbour unless GPS says otherwise.
    """
    ordered = sorted(photos, key=_sort_key)
    groups: list[ObservationGroup] = []
    for photo in ordered:
        if not groups:
            groups.append(ObservationGroup([photo]))
            continue
        prev = groups[-1].photos[-1]
        if _breaks(prev, photo, time_gap_s, distance_m):
            groups.append(ObservationGroup([photo]))
        else:
            groups[-1].photos.append(photo)
    return groups


def _breaks(
    prev: PhotoRecord, cur: PhotoRecord, time_gap_s: float, distance_m: float
) -> bool:
    gap_s = _gap_seconds(prev, cur)
    if gap_s is not None and gap_s > time_gap_s:
        return True
    gap_m = _gap_meters(prev, cur)
    if gap_m is not None and gap_m > distance_m:
        return True
    return False


# --- Splitting ---------------------------------------------------------------


def _features(photos: Sequence[PhotoRecord]) -> list[tuple[float, float, float]]:
    """(t, x, y) in comparable units for the DP. Missing values are filled from
    the nearest photo that has them, so a photo without GPS neither attracts
    nor repels a cut on its own."""
    n = len(photos)
    times: list[Optional[float]] = [
        p.time.timestamp() if p.taken is not None else None for p in photos
    ]
    _fill_missing(times, fallback=[float(i) for i in range(n)])
    # Local equirectangular projection around the mean of the fixes.
    fixes = [(p.lat, p.lon) for p in photos if p.has_gps]
    if fixes:
        lat0, lon0 = average_coordinates(fixes)
        cos0 = math.cos(math.radians(lat0))
        xs: list[Optional[float]] = []
        ys: list[Optional[float]] = []
        for p in photos:
            if p.has_gps:
                xs.append(math.radians(p.lon - lon0) * cos0 * _EARTH_RADIUS_M)
                ys.append(math.radians(p.lat - lat0) * _EARTH_RADIUS_M)
            else:
                xs.append(None)
                ys.append(None)
        _fill_missing(xs, fallback=[0.0] * n)
        _fill_missing(ys, fallback=[0.0] * n)
    else:
        xs = [0.0] * n
        ys = [0.0] * n
    t0 = min(times)
    return [
        (
            (times[i] - t0) / _SPLIT_TIME_SCALE_SECONDS,
            xs[i] / _SPLIT_DISTANCE_SCALE_METERS,
            ys[i] / _SPLIT_DISTANCE_SCALE_METERS,
        )
        for i in range(n)
    ]


def _fill_missing(values: list[Optional[float]], fallback: list[float]) -> None:
    """Replace None entries with the nearest known value on either side."""
    n = len(values)
    if all(v is None for v in values):
        values[:] = fallback
        return
    last: Optional[float] = None
    forward: list[Optional[float]] = []
    for v in values:
        if v is not None:
            last = v
        forward.append(last)
    last = None
    backward: list[Optional[float]] = [None] * n
    for i in range(n - 1, -1, -1):
        if values[i] is not None:
            last = values[i]
        backward[i] = last
    for i in range(n):
        if values[i] is None:
            f, b = forward[i], backward[i]
            values[i] = f if f is not None else b


def _segment_cost_table(feats: Sequence[tuple[float, float, float]]):
    """cost[i][j] = within-segment sum of squared deviation for feats[i:j+1]."""
    n = len(feats)
    dims = 3
    # Prefix sums of x and x^2 per dimension.
    s = [[0.0] * (n + 1) for _ in range(dims)]
    s2 = [[0.0] * (n + 1) for _ in range(dims)]
    for i, f in enumerate(feats):
        for d in range(dims):
            s[d][i + 1] = s[d][i] + f[d]
            s2[d][i + 1] = s2[d][i] + f[d] * f[d]

    def cost(i: int, j: int) -> float:
        cnt = j - i + 1
        total = 0.0
        for d in range(dims):
            sm = s[d][j + 1] - s[d][i]
            sq = s2[d][j + 1] - s2[d][i]
            total += sq - sm * sm / cnt
        return max(0.0, total)

    return cost


def split_group(group: ObservationGroup, n: int) -> list[ObservationGroup]:
    """Partition the group's photos (time order) into ``n`` contiguous runs.

    Dynamic programming over segment boundaries, minimising the summed
    within-run variance of scaled (time, east, north). ``n`` is clamped to the
    photo count. ``n <= 1`` returns the group unchanged (as a new object).
    """
    photos = sorted(group.photos, key=_sort_key)
    count = len(photos)
    n = max(1, min(n, count))
    if n == 1 or count == 0:
        return [ObservationGroup(list(photos))]

    feats = _features(photos)
    cost = _segment_cost_table(feats)
    inf = float("inf")
    # best[k][j]: min cost splitting photos[0..j] into k runs; cut[k][j]: start of last run
    best = [[inf] * count for _ in range(n + 1)]
    cut = [[-1] * count for _ in range(n + 1)]
    for j in range(count):
        best[1][j] = cost(0, j)
        cut[1][j] = 0
    for k in range(2, n + 1):
        for j in range(k - 1, count):
            for i in range(k - 1, j + 1):
                c = best[k - 1][i - 1] + cost(i, j)
                if c < best[k][j]:
                    best[k][j] = c
                    cut[k][j] = i
    # Walk back.
    bounds: list[int] = []
    j = count - 1
    for k in range(n, 0, -1):
        i = cut[k][j]
        bounds.append(i)
        j = i - 1
    bounds.reverse()
    result: list[ObservationGroup] = []
    for idx, start in enumerate(bounds):
        end = bounds[idx + 1] if idx + 1 < len(bounds) else count
        result.append(ObservationGroup(list(photos[start:end])))
    return result


def merge_groups(groups: Sequence[ObservationGroup]) -> ObservationGroup:
    merged: list[PhotoRecord] = []
    for g in groups:
        merged.extend(g.photos)
    return ObservationGroup(sorted(merged, key=_sort_key))


def move_photo(
    groups: list[ObservationGroup],
    photo_key: str,
    target_index: int,
) -> list[ObservationGroup]:
    """Move one photo into ``groups[target_index]``; drop groups left empty.

    ``target_index == len(groups)`` creates a new group at the end.
    """
    photo: Optional[PhotoRecord] = None
    for g in groups:
        for p in g.photos:
            if p.key == photo_key:
                photo = p
                break
        if photo is not None:
            g.photos.remove(photo)
            break
    if photo is None:
        return groups
    if target_index >= len(groups):
        groups.append(ObservationGroup([photo]))
    else:
        target = groups[target_index]
        target.photos.append(photo)
        if not target.ordered:  # a hand-ordered card takes the photo at its end
            target.photos.sort(key=_sort_key)
    return [g for g in groups if g.photos]


def move_photos(
    groups: list[ObservationGroup],
    photo_keys: Iterable[str],
    target_index: int,
    target_key: str = "",
    after: bool = False,
) -> list[ObservationGroup]:
    """Move photos into ``groups[target_index]`` at a chosen place.

    They go just before (or ``after``) the photo ``target_key`` of that
    group, or to its end when ``target_key`` is not one of its photos, and
    keep the order they are shown in. The group keeps the resulting order
    from then on (``ordered``). Photos already in the target group move
    too, so this also reorders inside one group.

    ``target_index == len(groups)`` puts them in a new group at the end,
    sorted by capture time. Groups left empty are dropped.
    """
    wanted = set(photo_keys)
    moved = [p for g in groups for p in g.sorted_photos() if p.key in wanted]
    if not moved:
        return groups
    if target_index >= len(groups):
        for g in groups:
            g.photos = [p for p in g.photos if p.key not in wanted]
        groups.append(ObservationGroup(sorted(moved, key=_sort_key)))
        return [g for g in groups if g.photos]
    target = groups[target_index]
    shown = target.sorted_photos()
    rest = [p for p in shown if p.key not in wanted]
    index = len(rest)
    position = next((i for i, p in enumerate(shown) if p.key == target_key), None)
    if position is not None:
        cut = position + 1 if after else position
        index = sum(1 for p in shown[:cut] if p.key not in wanted)
    arranged = rest[:index] + moved + rest[index:]
    if arranged == shown:
        return groups
    for g in groups:
        if g is not target:
            g.photos = [p for p in g.photos if p.key not in wanted]
    target.photos = arranged
    target.ordered = True
    return [g for g in groups if g.photos]
