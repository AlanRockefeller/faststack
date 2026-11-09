from datetime import datetime, timedelta
from pathlib import Path

from faststack.inat.grouping import PhotoRecord, haversine_m
from faststack.inat.tuning import (
    GroupingHistory,
    PairRecord,
    best_threshold,
    pairs_from_groups,
)

T0 = datetime(2026, 11, 1, 10, 0, 0)


def _p(i, seconds, lat=None, lon=None):
    return PhotoRecord(
        path=Path(f"/x/{i}.jpg"),
        order=i,
        taken=T0 + timedelta(seconds=seconds) if seconds is not None else None,
        lat=lat,
        lon=lon,
    )


def test_pairs_from_groups_marks_same_and_gaps():
    a = [_p(1, 0, 37.0, -122.0), _p(2, 30, 37.0, -122.0)]
    b = [_p(3, 400), _p(4, None)]
    pairs = pairs_from_groups([a, b], haversine_m)
    assert [(round(p.gap_s), p.same) for p in pairs] == [(30, True), (370, False)]
    assert pairs[0].gap_m == 0.0
    assert pairs[1].gap_m is None


def test_best_threshold_separates_clusters_and_prefers_default_on_ties():
    values = [(10, True), (20, True), (40, True), (900, False), (1500, False)]
    t = best_threshold(values, prefer=600, floor=1)
    assert 40 < t < 900
    # Any T in (40, 900) scores the same; the geometric midpoint candidate is
    # sqrt(40*900)=189.7, the nearest to the preferred 600 among candidates.
    assert abs(t - (40 * 900) ** 0.5) < 1e-6
    assert best_threshold([], 1, 1) is None


def test_history_suggests_folder_values_then_learned(tmp_path):
    hist = GroupingHistory(tmp_path / "h.json")
    assert hist.suggest("/f", 600, 100).source == "default"

    same_pairs = [PairRecord(20, 5, True)] * 8
    diff_pairs = [PairRecord(1200, 300, False)] * 4
    for i in range(3):
        hist.record(f"/folder{i}", 600 + i, 100, same_pairs + diff_pairs, 12, 4)

    # Same folder -> that folder's sliders.
    s = hist.suggest("/folder1", 600, 100)
    assert s.source == "folder" and s.time_gap_s == 601

    # New folder -> learned thresholds that separate 20s/5m from 1200s/300m.
    s = hist.suggest("/new", 600, 100)
    assert s.source == "learned"
    assert 20 < s.time_gap_s < 1200
    assert 5 < s.distance_m < 300

    # Persisted.
    again = GroupingHistory(tmp_path / "h.json")
    assert len(again.batches) == 3
    assert again.suggest("/new", 600, 100).source == "learned"


def test_history_learns_user_who_groups_long_sessions(tmp_path):
    # A user who keeps photos 20 minutes apart in one observation should get a
    # longer time gap than the 10-minute default.
    hist = GroupingHistory(tmp_path / "h.json")
    pairs = [PairRecord(1200, 10, True)] * 10 + [PairRecord(3600, 500, False)] * 10
    for i in range(3):
        hist.record(f"/f{i}", 600, 100, pairs, 20, 2)
    s = hist.suggest("/other", 600, 100)
    assert s.time_gap_s > 1200
