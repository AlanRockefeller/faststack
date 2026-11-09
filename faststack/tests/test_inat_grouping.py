from datetime import datetime, timedelta
from pathlib import Path

import pytest

from faststack.inat.grouping import (
    ObservationGroup,
    PhotoRecord,
    average_coordinates,
    haversine_m,
    initial_grouping,
    merge_groups,
    move_photo,
    split_group,
)

T0 = datetime(2026, 11, 1, 10, 0, 0)
BASE_LAT, BASE_LON = 37.8715, -122.2730  # Berkeley


def _photo(i, seconds, lat=None, lon=None, offset_m=(0, 0)):
    if lat is not None:
        # Nudge by metres: 1 deg lat ~ 111 km; lon scaled by cos(lat).
        lat = lat + offset_m[1] / 111_320.0
        lon = lon + offset_m[0] / (111_320.0 * 0.79)
    return PhotoRecord(
        path=Path(f"/x/IMG_{i:04d}.jpg"),
        order=i,
        taken=T0 + timedelta(seconds=seconds) if seconds is not None else None,
        lat=lat,
        lon=lon,
    )


def test_haversine_known_distance():
    # ~1 km north
    d = haversine_m(BASE_LAT, BASE_LON, BASE_LAT + 0.009, BASE_LON)
    assert 990 < d < 1010


def test_average_coordinates_across_antimeridian():
    lat, lon = average_coordinates([(0.0, 179.9), (0.0, -179.9)])
    assert abs(lat) < 1e-6
    assert abs(abs(lon) - 180.0) < 0.01


def test_initial_grouping_breaks_on_time_gap():
    photos = [
        _photo(1, 0),
        _photo(2, 30),
        _photo(3, 60),
        _photo(4, 60 + 15 * 60),  # 15 min later
        _photo(5, 60 + 15 * 60 + 20),
    ]
    groups = initial_grouping(photos, time_gap_s=600, distance_m=100)
    assert [[p.order for p in g.photos] for g in groups] == [[1, 2, 3], [4, 5]]


def test_initial_grouping_breaks_on_distance():
    photos = [
        _photo(1, 0, BASE_LAT, BASE_LON),
        _photo(2, 20, BASE_LAT, BASE_LON, offset_m=(5, 5)),
        _photo(3, 40, BASE_LAT, BASE_LON, offset_m=(300, 0)),  # 300 m away
        _photo(4, 60, BASE_LAT, BASE_LON, offset_m=(305, 2)),
    ]
    groups = initial_grouping(photos, time_gap_s=600, distance_m=100)
    assert [[p.order for p in g.photos] for g in groups] == [[1, 2], [3, 4]]


def test_initial_grouping_sorts_by_time_not_filename():
    # Filenames out of order (two cameras, say); time wins.
    photos = [
        _photo(9, 0),
        _photo(1, 5),
        _photo(5, 2000),
        _photo(2, 10),
    ]
    groups = initial_grouping(photos, time_gap_s=600, distance_m=100)
    assert [[p.order for p in g.photos] for g in groups] == [[9, 1, 2], [5]]


def test_photos_without_time_trail_in_batch_order():
    photos = [_photo(3, None), _photo(1, 0), _photo(2, None)]
    groups = initial_grouping(photos, time_gap_s=600, distance_m=100)
    assert [p.order for p in groups[0].photos] == [1, 2, 3]


def test_exif_location_averages_and_reports_spread():
    g = ObservationGroup(
        [
            _photo(1, 0, BASE_LAT, BASE_LON),
            _photo(2, 1, BASE_LAT, BASE_LON, offset_m=(20, 0)),
            _photo(3, 2),  # no GPS
        ]
    )
    loc = g.exif_location(spread_warning_m=100)
    assert loc is not None
    assert loc.photos_with_gps == 2
    assert 8 < loc.spread_m < 12
    assert not loc.spread_warning
    assert abs(loc.lon - (BASE_LON + 10 / (111_320.0 * 0.79))) < 1e-6


def test_exif_location_warns_on_wide_spread():
    g = ObservationGroup(
        [
            _photo(1, 0, BASE_LAT, BASE_LON),
            _photo(2, 1, BASE_LAT, BASE_LON, offset_m=(400, 0)),
        ]
    )
    loc = g.exif_location(spread_warning_m=100)
    assert loc.spread_warning


def test_exif_location_none_without_gps():
    assert ObservationGroup([_photo(1, 0)]).exif_location() is None


def test_split_group_finds_two_time_clusters():
    g = ObservationGroup(
        [
            _photo(1, 0),
            _photo(2, 20),
            _photo(3, 40),
            _photo(4, 300),
            _photo(5, 320),
        ]
    )
    parts = split_group(g, 2)
    assert [[p.order for p in part.photos] for part in parts] == [[1, 2, 3], [4, 5]]


def test_split_group_three_finds_same_spot_quick_succession():
    # Three finds within ~2 minutes from one spot: cuts fall at the two
    # larger gaps even though all gaps are short.
    secs = [0, 8, 16, 60, 70, 78, 130, 140]
    g = ObservationGroup(
        [_photo(i + 1, s, BASE_LAT, BASE_LON) for i, s in enumerate(secs)]
    )
    parts = split_group(g, 3)
    assert [[p.order for p in part.photos] for part in parts] == [
        [1, 2, 3],
        [4, 5, 6],
        [7, 8],
    ]


def test_split_group_uses_position_when_time_is_uniform():
    # Evenly spaced in time; position separates them.
    g = ObservationGroup(
        [
            _photo(1, 0, BASE_LAT, BASE_LON),
            _photo(2, 10, BASE_LAT, BASE_LON, offset_m=(1, 0)),
            _photo(3, 20, BASE_LAT, BASE_LON, offset_m=(60, 0)),
            _photo(4, 30, BASE_LAT, BASE_LON, offset_m=(61, 1)),
        ]
    )
    parts = split_group(g, 2)
    assert [[p.order for p in part.photos] for part in parts] == [[1, 2], [3, 4]]


def test_split_group_clamps_n():
    g = ObservationGroup([_photo(1, 0), _photo(2, 5)])
    assert len(split_group(g, 5)) == 2
    assert len(split_group(g, 0)) == 1
    assert len(split_group(ObservationGroup([]), 3)) == 1


def test_merge_and_move():
    a = ObservationGroup([_photo(1, 0), _photo(3, 50)])
    b = ObservationGroup([_photo(2, 25)])
    merged = merge_groups([a, b])
    assert [p.order for p in merged.photos] == [1, 2, 3]

    groups = [
        ObservationGroup([_photo(1, 0), _photo(2, 5)]),
        ObservationGroup([_photo(3, 9)]),
    ]
    groups = move_photo(groups, "/x/IMG_0002.jpg", 1)
    assert [[p.order for p in g.photos] for g in groups] == [[1], [2, 3]]
    # Moving into a new group at the end
    groups = move_photo(groups, "/x/IMG_0001.jpg", len(groups))
    assert [[p.order for p in g.photos] for g in groups] == [[2, 3], [1]]
    # Unknown key is a no-op
    assert move_photo(groups, "/nope.jpg", 0) == groups


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_split_partition_covers_all_photos(n):
    g = ObservationGroup([_photo(i, i * 7) for i in range(1, 9)])
    parts = split_group(g, n)
    assert len(parts) == n
    flat = [p.order for part in parts for p in part.photos]
    assert flat == list(range(1, 9))


def test_observed_at_exif_ignores_mtime_fallback():
    exif = _photo(1, 100)
    fallback = PhotoRecord(
        path=Path("/x/IMG_0002.jpg"), order=2, taken=T0, taken_from_exif=False
    )
    g = ObservationGroup([exif, fallback])
    assert g.observed_at == T0  # grouping still uses the fallback
    assert g.observed_at_exif == exif.taken  # uploads only trust EXIF
    assert ObservationGroup([fallback]).observed_at_exif is None
