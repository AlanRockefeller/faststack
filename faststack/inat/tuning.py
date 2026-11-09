"""Remember how past batches were grouped and suggest thresholds from them.

Every upload records, for each pair of consecutive photos in time order,
the time gap, the GPS gap (when both had a fix) and whether the user left
them in the same observation. That is exactly the decision the grouping
sliders make, so the thresholds that best reproduce those decisions are
the best starting point for the next batch. A folder that was uploaded
from before gets the slider positions used then.

Stored as JSON in the app data directory; a few hundred batches is tiny.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger(__name__)

HISTORY_FILE = "inat_grouping_history.json"
MAX_BATCHES = 400
# Below this much evidence the config defaults are used instead.
MIN_BATCHES = 3
MIN_PAIRS = 20


@dataclass
class PairRecord:
    gap_s: float
    gap_m: Optional[float]
    same: bool


@dataclass
class BatchRecord:
    folder: str
    recorded_at: float
    time_gap_s: float
    distance_m: float
    photos: int
    observations: int
    pairs: list[PairRecord] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "folder": self.folder,
            "recorded_at": self.recorded_at,
            "time_gap_s": self.time_gap_s,
            "distance_m": self.distance_m,
            "photos": self.photos,
            "observations": self.observations,
            "pairs": [[p.gap_s, p.gap_m, p.same] for p in self.pairs],
        }

    @classmethod
    def from_json(cls, data: dict) -> "BatchRecord":
        pairs = []
        for row in data.get("pairs") or []:
            try:
                gap_s, gap_m, same = row
                pairs.append(
                    PairRecord(
                        float(gap_s),
                        float(gap_m) if gap_m is not None else None,
                        bool(same),
                    )
                )
            except (TypeError, ValueError):
                continue
        return cls(
            folder=str(data.get("folder") or ""),
            recorded_at=float(data.get("recorded_at") or 0),
            time_gap_s=float(data.get("time_gap_s") or 0),
            distance_m=float(data.get("distance_m") or 0),
            photos=int(data.get("photos") or 0),
            observations=int(data.get("observations") or 0),
            pairs=pairs,
        )


@dataclass
class Suggestion:
    time_gap_s: float
    distance_m: float
    source: str  # "folder", "learned", "default"
    batches: int = 0
    pairs: int = 0


def pairs_from_groups(groups: Sequence[Sequence], haversine) -> list[PairRecord]:
    """Consecutive-photo pairs across a final grouping.

    ``groups`` is a list of photo lists (any objects with ``taken``, ``lat``,
    ``lon``, ``has_gps``). Photos without a capture time are skipped.
    """
    owner: dict[int, int] = {}
    photos = []
    for gi, group in enumerate(groups):
        for p in group:
            if p.taken is None:
                continue
            owner[id(p)] = gi
            photos.append(p)
    photos.sort(key=lambda p: p.taken)
    out: list[PairRecord] = []
    for a, b in zip(photos, photos[1:]):
        gap_s = abs((b.taken - a.taken).total_seconds())
        gap_m = (
            haversine(a.lat, a.lon, b.lat, b.lon) if a.has_gps and b.has_gps else None
        )
        out.append(PairRecord(gap_s, gap_m, owner[id(a)] == owner[id(b)]))
    return out


def best_threshold(
    values: Sequence[tuple[float, bool]],
    prefer: float,
    floor: float,
) -> Optional[float]:
    """Threshold T maximising accuracy of ``same == (value <= T)``.

    Candidates are geometric midpoints between adjacent distinct values (plus
    the extremes). Ties go to the candidate nearest ``prefer`` in log space,
    so sparse evidence doesn't yank the default around.
    """
    if not values:
        return None
    xs = sorted({max(floor, v) for v, _ in values})
    candidates = [xs[0] * 0.5]
    for a, b in zip(xs, xs[1:]):
        candidates.append(math.sqrt(a * b))
    candidates.append(xs[-1] * 2.0)
    candidates.append(max(floor, prefer))  # the default itself can win a tie
    best: Optional[float] = None
    best_score = -1
    best_dist = float("inf")
    for t in candidates:
        score = sum(1 for v, same in values if (v <= t) == same)
        dist = abs(math.log(max(t, 1e-9)) - math.log(max(prefer, 1e-9)))
        if score > best_score or (score == best_score and dist < best_dist):
            best, best_score, best_dist = t, score, dist
    return best


class GroupingHistory:
    def __init__(self, path: Optional[Path] = None):
        self._path = path
        self._batches: list[BatchRecord] = []
        self._loaded = False

    # --- persistence --------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._batches = [BatchRecord.from_json(b) for b in data.get("batches", [])]
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Could not read grouping history: %s", exc)
            self._batches = []

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"version": 1, "batches": [b.to_json() for b in self._batches]}
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("Could not write grouping history: %s", exc)

    # --- public -------------------------------------------------------

    @property
    def batches(self) -> list[BatchRecord]:
        self._ensure_loaded()
        return self._batches

    def record(
        self,
        folder: str,
        time_gap_s: float,
        distance_m: float,
        pairs: Sequence[PairRecord],
        photos: int,
        observations: int,
    ) -> None:
        self._ensure_loaded()
        self._batches.append(
            BatchRecord(
                folder=folder,
                recorded_at=time.time(),
                time_gap_s=float(time_gap_s),
                distance_m=float(distance_m),
                photos=photos,
                observations=observations,
                pairs=list(pairs),
            )
        )
        if len(self._batches) > MAX_BATCHES:
            self._batches = self._batches[-MAX_BATCHES:]
        self._save()

    def suggest(
        self, folder: str, default_time_gap_s: float, default_distance_m: float
    ) -> Suggestion:
        self._ensure_loaded()
        for batch in reversed(self._batches):
            if batch.folder and batch.folder == folder and batch.time_gap_s > 0:
                return Suggestion(
                    time_gap_s=batch.time_gap_s,
                    distance_m=batch.distance_m,
                    source="folder",
                    batches=len(self._batches),
                    pairs=len(batch.pairs),
                )
        pairs = [p for b in self._batches for p in b.pairs]
        if len(self._batches) < MIN_BATCHES or len(pairs) < MIN_PAIRS:
            return Suggestion(
                default_time_gap_s,
                default_distance_m,
                "default",
                len(self._batches),
                len(pairs),
            )
        # Only pairs that the *other* dimension would not have split tell us
        # about this one: a pair 2 km apart says nothing about the time gap.
        time_values = [
            (p.gap_s, p.same)
            for p in pairs
            if p.gap_m is None or p.gap_m <= default_distance_m or p.same
        ]
        dist_values = [
            (p.gap_m, p.same)
            for p in pairs
            if p.gap_m is not None and (p.gap_s <= default_time_gap_s or p.same)
        ]
        t = best_threshold(time_values, default_time_gap_s, floor=1.0)
        d = best_threshold(dist_values, default_distance_m, floor=1.0)
        return Suggestion(
            time_gap_s=t if t is not None else default_time_gap_s,
            distance_m=d if d is not None else default_distance_m,
            source="learned",
            batches=len(self._batches),
            pairs=len(pairs),
        )
