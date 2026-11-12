"""Find observations already on iNaturalist that a card's photos belong to.

Pure logic, no Qt. The case this serves: the user photographs a find with
the phone (the iNaturalist app makes the observation there and then) and
with the camera, or uploads some camera photos of a find now and more of it
later. Uploading the camera photos as a new observation makes a duplicate;
they belong in the observation that already exists.

Time and place say which one. The place is easy: phone and camera fixes of
one find are a few tens of metres apart. The time is not, because a camera
clock is often on the wrong time zone (the date is right, the hour is not),
and wrong by the same amount for days or weeks. So the camera's clock offset
is part of the answer:

* :func:`detect_offsets` looks for the offset (a whole number of quarter
  hours, as time zones are) under which the most cards land next to one of
  the user's observations, in time and in GPS distance. Only observations
  whose time came from a device that knows the local time count as
  evidence; see ``ExistingObservation.camera_clock``.
* :func:`match_cards` lists, per card, the observations it may belong to
  under those offsets.
* :class:`ClockOffsetStore` remembers an offset the user confirmed, per
  camera and date, so the next batch from the same trip starts from it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

from faststack.inat.grouping import PhotoRecord, haversine_m

log = logging.getLogger(__name__)

QUARTER_HOUR_S = 15 * 60
# Time zones run from UTC-12 to UTC+14.
MAX_OFFSET_S = 26 * 3600
# How far outside a card's photo times an observation may be and still be
# the same find: the phone shot can come a while before or after the camera.
MATCH_WINDOW_S = 30 * 60
# Without GPS on one side, time is the only evidence; ask for more of it.
MATCH_WINDOW_NO_GPS_S = 15 * 60
# Phone and camera fixes of one find; the observation's own accuracy and the
# card's GPS spread are added on top.
NEAR_M = 250.0
MAX_ACCURACY_ALLOWANCE_M = 1000.0
MAX_MATCHES_PER_CARD = 3
# Each hour of offset counts as this much time apart, per card: between
# two offsets that line up as many cards, the smaller wins, so a camera on
# the neighbouring zone beats an unrelated observation at the same spot
# eight hours later.
_PER_HOUR_PENALTY_S = 5 * 60

OFFSETS_FILE = "inat_clock_offsets.json"
MAX_OFFSET_RECORDS = 500
# A confirmed offset is assumed to still hold this long either side of the
# date it was confirmed on (a trip lasts days or weeks).
OFFSET_VALID_DAYS = 30

_BARE_TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$")
_LOOSE_TIME_RE = re.compile(
    r"(\d{4})[-/:](\d{1,2})[-/:](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)


@dataclass(frozen=True)
class ExistingPhoto:
    """One photo of an observation already on iNaturalist."""

    op_id: int  # the observation photo's id (what a position update needs); 0 = unknown
    position: Optional[int]
    url: str  # square thumbnail


def _size_url(url: str, size: str) -> str:
    return re.sub(r"/square(\.\w+)(\?.*)?$", rf"/{size}\1\2", url)


@dataclass(frozen=True)
class ExistingObservation:
    """One of the user's observations on iNaturalist, for matching."""

    id: int
    wall_time: Optional[datetime] = None  # local clock time where observed
    observed_on: Optional[date] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    accuracy_m: Optional[float] = None
    # The time was sent as a bare "YYYY-MM-DD HH:MM:SS", the way FastStack
    # sends a camera's EXIF time: it may be a camera clock, not local time.
    camera_clock: bool = False
    name: str = ""
    photo_count: int = 0
    photo_url: str = ""  # square thumbnail; "" when it has no photo
    photos: tuple[ExistingPhoto, ...] = ()  # in the order iNaturalist shows them

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None

    def photo(self, size: str, index: int = 0) -> str:
        """Photo ``index`` at another of iNaturalist's sizes ("small")."""
        if index == 0 and not self.photos:
            return _size_url(self.photo_url, size)
        if 0 <= index < len(self.photos):
            return _size_url(self.photos[index].url, size)
        return ""


def _existing_photos(row: dict) -> tuple[ExistingPhoto, ...]:
    """The observation's photos in display order: by position, then id
    (iNaturalist puts photos without a position last)."""
    out = []
    links = row.get("observation_photos")
    if isinstance(links, list) and links:
        for link in links:
            if not isinstance(link, dict):
                continue
            photo = link.get("photo") if isinstance(link.get("photo"), dict) else {}
            url = str(photo.get("url") or "")
            try:
                op_id = int(link.get("id") or 0)
            except (TypeError, ValueError):
                op_id = 0
            try:
                position = int(link["position"]) if link.get("position") is not None else None
            except (TypeError, ValueError):
                position = None
            out.append(
                ExistingPhoto(op_id, position, url if url.startswith("https://") else "")
            )
        out.sort(
            key=lambda p: (p.position is None, p.position or 0, p.op_id)
        )
    else:
        for photo in row.get("photos") or []:
            if isinstance(photo, dict):
                url = str(photo.get("url") or "")
                out.append(
                    ExistingPhoto(0, None, url if url.startswith("https://") else "")
                )
    return tuple(out)


def parse_observation_time(row: dict) -> tuple[Optional[datetime], bool]:
    """(local wall-clock time, came as a bare camera-style string) of a row.

    ``observed_on_string`` is what the uploader sent. A bare
    "YYYY-MM-DD HH:MM:SS" is taken as it is (iNaturalist reads it as local
    time, so that is also its wall time). Otherwise ``time_observed_at`` is
    the instant, shown in the observation's own time zone.
    """
    raw = str(row.get("observed_on_string") or "").strip()
    bare = _BARE_TIME_RE.fullmatch(raw)
    if bare:
        try:
            return datetime(*(int(x) for x in bare.groups())), True
        except ValueError:
            pass
    stamp = row.get("time_observed_at")
    if stamp:
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            zone = row.get("observed_time_zone")
            if when.tzinfo is not None and zone:
                try:
                    from zoneinfo import ZoneInfo

                    when = when.astimezone(ZoneInfo(str(zone)))
                except Exception:
                    pass  # unknown zone name or no tz database: keep its offset
            return when.replace(tzinfo=None), False
    loose = _LOOSE_TIME_RE.search(raw)
    if loose:
        parts = [int(x) if x else 0 for x in loose.groups()]
        try:
            return datetime(*parts), False
        except ValueError:
            pass
    return None, False


def _parse_date(text) -> Optional[date]:
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def observation_from_row(row: dict) -> Optional[ExistingObservation]:
    """An API v1 observation row as an :class:`ExistingObservation`."""
    try:
        obs_id = int(row.get("id"))
    except (TypeError, ValueError):
        return None
    wall, camera_clock = parse_observation_time(row)
    observed_on = _parse_date(row.get("observed_on"))
    if observed_on is None and wall is not None:
        observed_on = wall.date()
    point = _lat_lon(row.get("private_location"))
    accuracy = row.get("positional_accuracy")
    if point is None and not row.get("obscured"):
        # Without the owner's token an obscured one has a made-up point.
        point = _lat_lon(row.get("location"))
    taxon = row.get("taxon") if isinstance(row.get("taxon"), dict) else {}
    photos = _existing_photos(row)
    url = photos[0].url if photos else ""
    try:
        accuracy_m = float(accuracy) if accuracy is not None else None
    except (TypeError, ValueError):
        accuracy_m = None
    return ExistingObservation(
        id=obs_id,
        wall_time=wall,
        observed_on=observed_on,
        lat=point[0] if point else None,
        lon=point[1] if point else None,
        accuracy_m=accuracy_m,
        camera_clock=camera_clock,
        name=str(taxon.get("name") or row.get("species_guess") or ""),
        photo_count=len(photos),
        photo_url=url,
        photos=photos,
    )


def _lat_lon(text) -> Optional[tuple[float, float]]:
    try:
        lat, lon = (float(part) for part in str(text).split(","))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# --- Cards ------------------------------------------------------------------


@dataclass
class CardFacts:
    """What matching needs from one card: its photos and where it is."""

    key: int
    photos: Sequence[PhotoRecord]
    lat: Optional[float] = None
    lon: Optional[float] = None
    spread_m: float = 0.0

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None

    def timed(self) -> list[PhotoRecord]:
        """Photos with an EXIF capture time (a file date says nothing)."""
        return [p for p in self.photos if p.taken is not None and p.taken_from_exif]

    def main_camera(self) -> Optional[str]:
        counts: dict[str, int] = {}
        for p in self.timed():
            counts[p.camera] = counts.get(p.camera, 0) + 1
        if not counts:
            return None
        return max(counts, key=lambda c: (counts[c], c))

    def span(
        self, offsets: Optional[dict[str, int]] = None, camera: Optional[str] = None
    ) -> Optional[tuple[datetime, datetime]]:
        """Earliest and latest capture time, each shifted by its camera's
        offset; only ``camera``'s photos when given."""
        offsets = offsets or {}
        times = [
            p.taken + timedelta(seconds=offsets.get(p.camera, 0))
            for p in self.timed()
            if camera is None or p.camera == camera
        ]
        if not times:
            return None
        return min(times), max(times)


def _gap_to_span(when: datetime, span: tuple[datetime, datetime]) -> float:
    """Seconds from ``when`` to the nearest moment of ``span`` (0 inside)."""
    lo, hi = span
    if when < lo:
        return (lo - when).total_seconds()
    if when > hi:
        return (when - hi).total_seconds()
    return 0.0


def _distance(card: CardFacts, obs: ExistingObservation) -> Optional[float]:
    if not (card.has_location and obs.has_location):
        return None
    return haversine_m(card.lat, card.lon, obs.lat, obs.lon)


def _near_enough(card: CardFacts, obs: ExistingObservation, dist: float) -> bool:
    allowance = NEAR_M + card.spread_m
    if obs.accuracy_m:
        allowance += min(obs.accuracy_m, MAX_ACCURACY_ALLOWANCE_M)
    return dist <= allowance


def _plausible_day(span: tuple[datetime, datetime], obs: ExistingObservation) -> bool:
    """Cheap first cut: the observation is within a day of the photos."""
    if obs.wall_time is not None:
        slack = timedelta(seconds=MAX_OFFSET_S + MATCH_WINDOW_S)
        return span[0] - slack <= obs.wall_time <= span[1] + slack
    if obs.observed_on is not None:
        return span[0].date() - timedelta(days=1) <= obs.observed_on <= (
            span[1].date() + timedelta(days=1)
        )
    return False


# --- Clock offsets ----------------------------------------------------------


@dataclass
class OffsetGuess:
    """The clock offset matching uses for one camera, and where it came from.

    ``offset_s`` is added to the camera's EXIF time to get local time: a
    camera three hours behind has +10800.
    """

    offset_s: int = 0
    source: str = "none"  # "none", "detected", "saved", "confirmed", "ignored"
    support: int = 0  # cards that sit next to an observation at this offset
    from_obs: list[int] = field(default_factory=list)
    saved_on: Optional[date] = None

    @property
    def corrects_times(self) -> bool:
        """Applied to the times of new observations, not only to matching."""
        return self.source in ("saved", "confirmed") and self.offset_s != 0


def _quarter_hours(lo: float, hi: float) -> range:
    """Every whole quarter hour from ``lo`` to ``hi`` seconds, within limits."""
    lo = max(lo, -MAX_OFFSET_S)
    hi = min(hi, MAX_OFFSET_S)
    if hi < lo:
        return range(0)
    first = math.ceil(lo / QUARTER_HOUR_S)
    last = math.floor(hi / QUARTER_HOUR_S)
    return range(first * QUARTER_HOUR_S, last * QUARTER_HOUR_S + 1, QUARTER_HOUR_S)


def _penalty(offset_s: int) -> float:
    return abs(offset_s) / 3600.0 * _PER_HOUR_PENALTY_S


def _granularity(offset_s: int) -> int:
    """0 for a whole hour, 1 for a half hour, 2 for a quarter hour.

    Most time zones are whole hours. A finer offset fitting a little
    better usually just absorbs the minutes between the phone shot and the
    camera shots, so it is only chosen when no coarser one fits at all
    (or when it lines up more cards).
    """
    if offset_s % 3600 == 0:
        return 0
    if offset_s % 1800 == 0:
        return 1
    return 2


def offset_for_pair(
    card: CardFacts,
    obs: ExistingObservation,
    camera: str,
    current_s: int = 0,
) -> Optional[int]:
    """The camera offset that puts ``obs`` among ``camera``'s photos of
    ``card``: ``current_s`` when it already does, else the best quarter hour.
    None when the observation has no usable time."""
    if obs.wall_time is None or obs.camera_clock:
        return None
    span = card.span(camera=camera)
    if span is None:
        return None
    current = (span[0] + timedelta(seconds=current_s), span[1] + timedelta(seconds=current_s))
    if _gap_to_span(obs.wall_time, current) <= MATCH_WINDOW_S:
        return current_s
    lo = (obs.wall_time - span[1]).total_seconds() - MATCH_WINDOW_S
    hi = (obs.wall_time - span[0]).total_seconds() + MATCH_WINDOW_S
    best: Optional[tuple[int, float, int, int]] = None
    for k in _quarter_hours(lo, hi):
        shifted = (span[0] + timedelta(seconds=k), span[1] + timedelta(seconds=k))
        cost = _gap_to_span(obs.wall_time, shifted) + _penalty(k)
        key = (_granularity(k), cost, abs(k), k)
        if best is None or key < best:
            best = key
    return best[3] if best else None


def _one_to_one(
    triples: Sequence[tuple[float, int, int]],
) -> list[tuple[float, int, int]]:
    """Pair cards with observations, closest first, each used at most once.

    One observation is one find: lined up with two cards it is evidence
    for one of them, not for both (which would let an offset that puts a
    single phone observation between two cards beat the true one).
    """
    used_cards: set[int] = set()
    used_obs: set[int] = set()
    out = []
    for residual, card_key, obs_id in sorted(triples):
        if card_key in used_cards or obs_id in used_obs:
            continue
        used_cards.add(card_key)
        used_obs.add(obs_id)
        out.append((residual, card_key, obs_id))
    return out


def detect_offsets(
    cards: Sequence[CardFacts],
    observations: Sequence[ExistingObservation],
    priors: Optional[dict[str, OffsetGuess]] = None,
    skip_pairs: Iterable[tuple[int, int]] = (),
) -> dict[str, OffsetGuess]:
    """Each camera's most likely clock offset, from where its cards meet the
    user's observations.

    Evidence is a card with GPS next to an observation with GPS whose time
    is local time (not a camera clock), each observation counting for one
    card at most. Each such pair votes for every
    quarter-hour offset that puts the observation within
    :data:`MATCH_WINDOW_S` of the camera's photos. The offset that lines up
    the most cards wins; on a tie the prior (an offset saved earlier), then
    a whole hour, then a half hour, then the one with the cards closest in
    time (a smaller offset counting as closer). Cameras with no evidence keep their prior,
    or zero.

    A prior whose source is "confirmed" or "ignored" is the user's decision
    for this session and is returned unchanged. ``skip_pairs`` holds
    (card key, observation id) pairs the user said do not belong together.
    """
    priors = priors or {}
    skip = set(skip_pairs)
    cameras = {p.camera for c in cards for p in c.timed()}
    evidence = [
        o
        for o in observations
        if o.wall_time is not None and not o.camera_clock and o.has_location
    ]
    out: dict[str, OffsetGuess] = {}
    for camera in sorted(cameras):
        prior = priors.get(camera)
        if prior is not None and prior.source in ("confirmed", "ignored"):
            out[camera] = prior
            continue
        # offset -> every (residual, card key, observation id) it lines up
        votes: dict[int, list[tuple[float, int, int]]] = {}
        for card in cards:
            span = card.span(camera=camera)
            if span is None or not card.has_location:
                continue
            for obs in evidence:
                if (card.key, obs.id) in skip or not _plausible_day(span, obs):
                    continue
                dist = _distance(card, obs)
                if dist is None or not _near_enough(card, obs, dist):
                    continue
                lo = (obs.wall_time - span[1]).total_seconds() - MATCH_WINDOW_S
                hi = (obs.wall_time - span[0]).total_seconds() + MATCH_WINDOW_S
                for k in _quarter_hours(lo, hi):
                    shifted = (
                        span[0] + timedelta(seconds=k),
                        span[1] + timedelta(seconds=k),
                    )
                    residual = _gap_to_span(obs.wall_time, shifted)
                    votes.setdefault(k, []).append((residual, card.key, obs.id))
        prior_k = prior.offset_s if prior is not None else None
        best_key = None
        best_k = None
        best_pairs: list[tuple[float, int, int]] = []
        for k, triples in votes.items():
            pairs = _one_to_one(triples)
            support = len(pairs)
            cost = sum(r for r, _, _ in pairs) + support * _penalty(k)
            key = (-support, 0 if k == prior_k else 1, _granularity(k), cost, abs(k))
            if best_key is None or key < best_key:
                best_key, best_k, best_pairs = key, k, pairs
        if best_k is None:
            out[camera] = prior if prior is not None else OffsetGuess()
            continue
        support = len(best_pairs)
        from_obs = sorted({oid for _, _, oid in best_pairs})
        if prior is not None and best_k == prior_k:
            out[camera] = OffsetGuess(
                offset_s=prior_k,
                source=prior.source,
                support=support,
                from_obs=from_obs,
                saved_on=prior.saved_on,
            )
        else:
            out[camera] = OffsetGuess(
                offset_s=best_k,
                source="detected" if best_k != 0 else "none",
                support=support,
                from_obs=from_obs,
            )
    return out


# --- Matching ---------------------------------------------------------------


@dataclass
class Match:
    obs: ExistingObservation
    time_gap_s: Optional[float]  # None: the observation has a date only
    distance_m: Optional[float]  # None: one side has no GPS
    # Matched on the camera's own clock (an observation FastStack uploaded
    # from the same camera before), not on local time.
    camera_clock: bool = False
    score: float = 0.0


def match_card(
    card: CardFacts,
    observations: Iterable[ExistingObservation],
    offsets: dict[str, int],
    exclude: Iterable[int] = (),
) -> list[Match]:
    """The observations ``card`` may belong to, best first."""
    local = card.span(offsets)
    raw = card.span()
    if local is None or raw is None:
        return []
    skip = set(exclude)
    shifted = any(offsets.get(p.camera, 0) for p in card.timed())
    found: list[Match] = []
    for obs in observations:
        if obs.id in skip or not _plausible_day(raw, obs):
            continue
        dist = _distance(card, obs)
        if dist is not None and not _near_enough(card, obs, dist):
            continue
        if obs.wall_time is None:
            # A date and nothing else: only with GPS agreeing, on that day.
            if dist is None or obs.observed_on not in (
                local[0].date(),
                local[1].date(),
            ):
                continue
            gap: Optional[float] = None
            on_camera_clock = False
        else:
            # Local time, and for a bare string also the camera's own clock.
            spans = [(local, False)]
            if obs.camera_clock and shifted:
                spans.append((raw, True))
            gap, on_camera_clock = min(
                ((_gap_to_span(obs.wall_time, s), cam) for s, cam in spans),
                key=lambda pair: pair[0],
            )
            window = MATCH_WINDOW_S if dist is not None else MATCH_WINDOW_NO_GPS_S
            if gap > window:
                continue
        # Lower is better: a minute apart weighs like 25 m apart.
        score = (gap if gap is not None else MATCH_WINDOW_S) / 60.0
        score += (dist / 25.0) if dist is not None else 10.0
        found.append(
            Match(
                obs=obs,
                time_gap_s=gap,
                distance_m=dist,
                camera_clock=on_camera_clock,
                score=score,
            )
        )
    found.sort(key=lambda m: (m.score, m.obs.id))
    return found[:MAX_MATCHES_PER_CARD]


def match_cards(
    cards: Sequence[CardFacts],
    observations: Sequence[ExistingObservation],
    offsets: dict[str, int],
    exclude: Iterable[int] = (),
) -> dict[int, list[Match]]:
    skip = set(exclude)
    return {c.key: match_card(c, observations, offsets, skip) for c in cards}


def fetch_window(
    photos: Iterable[PhotoRecord], max_days: int = 62
) -> Optional[tuple[date, date]]:
    """The observed-on dates to ask iNaturalist about for these photos.

    A day either side, for a camera clock off by a time zone across
    midnight. A batch spanning months asks about its newest ``max_days``.
    """
    days = [p.taken.date() for p in photos if p.taken is not None and p.taken_from_exif]
    if not days:
        return None
    first, last = min(days), max(days)
    if (last - first).days > max_days:
        first = last - timedelta(days=max_days)
    return first - timedelta(days=1), last + timedelta(days=1)


# --- Remembered offsets -----------------------------------------------------


@dataclass
class OffsetRecord:
    camera: str
    offset_s: int
    on: date  # the photos' date when it was confirmed
    obs_id: Optional[int] = None
    recorded_at: float = 0.0


class ClockOffsetStore:
    """Camera clock offsets the user confirmed, kept in the app data folder."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._records: list[OffsetRecord] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for row in data.get("offsets", []):
                on = _parse_date(row.get("on"))
                if on is None:
                    continue
                self._records.append(
                    OffsetRecord(
                        camera=str(row.get("camera") or ""),
                        offset_s=int(row.get("offset_s") or 0),
                        on=on,
                        obs_id=(
                            int(row["obs_id"]) if row.get("obs_id") is not None else None
                        ),
                        recorded_at=float(row.get("recorded_at") or 0),
                    )
                )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("Could not read camera clock offsets: %s", exc)
            self._records = []

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "offsets": [
                            {
                                "camera": r.camera,
                                "offset_s": r.offset_s,
                                "on": r.on.isoformat(),
                                "obs_id": r.obs_id,
                                "recorded_at": r.recorded_at,
                            }
                            for r in self._records
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("Could not write camera clock offsets: %s", exc)

    def record(
        self, camera: str, offset_s: int, on: date, obs_id: Optional[int] = None
    ) -> None:
        """Remember ``camera``'s offset on day ``on``; replaces that day's."""
        self._ensure_loaded()
        self._records = [
            r for r in self._records if not (r.camera == camera and r.on == on)
        ]
        self._records.append(
            OffsetRecord(camera, int(offset_s), on, obs_id, time.time())
        )
        if len(self._records) > MAX_OFFSET_RECORDS:
            self._records.sort(key=lambda r: r.recorded_at)
            self._records = self._records[-MAX_OFFSET_RECORDS:]
        self._save()

    def lookup(self, camera: str, on: date) -> Optional[OffsetRecord]:
        """The record for ``camera`` closest to ``on``, within the valid days;
        the newer one wins a tie."""
        self._ensure_loaded()
        best: Optional[OffsetRecord] = None
        for r in self._records:
            if r.camera != camera:
                continue
            distance = abs((r.on - on).days)
            if distance > OFFSET_VALID_DAYS:
                continue
            if best is None or (distance, -r.recorded_at) < (
                abs((best.on - on).days),
                -best.recorded_at,
            ):
                best = r
        return best


def format_offset(offset_s: int) -> str:
    """"3 h", "5 h 30 min", "45 min" (no sign)."""
    total = abs(int(offset_s)) // 60
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"


def describe_offset(offset_s: int) -> str:
    """"3 h behind local time" for +3 h (the camera shows an earlier time)."""
    if offset_s == 0:
        return "right"
    side = "behind" if offset_s > 0 else "ahead of"
    return f"{format_offset(offset_s)} {side} local time"
