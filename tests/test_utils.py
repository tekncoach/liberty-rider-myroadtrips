from datetime import datetime, timezone

import pytest

from utils import day_key, haversine_km, parse_iso


def test_haversine_km_one_degree_longitude_at_equator():
    # 1 degree of longitude at the equator is ~111.19 km.
    dist = haversine_km(0.0, 0.0, 0.0, 1.0)
    assert dist == pytest.approx(111.19, abs=0.5)


def test_haversine_km_one_degree_latitude():
    # 1 degree of latitude is ~111.19 km everywhere (great circle along a meridian).
    dist = haversine_km(0.0, 0.0, 1.0, 0.0)
    assert dist == pytest.approx(111.19, abs=0.5)


def test_haversine_km_known_real_world_distance():
    # Montrouge (94) to Fontainebleau castle, straight-line ~ 55 km.
    montrouge = (48.8163, 2.3184)
    fontainebleau = (48.4021, 2.7014)
    dist = haversine_km(*montrouge, *fontainebleau)
    assert dist == pytest.approx(55, abs=5)


def test_haversine_km_same_point_is_zero():
    assert haversine_km(48.8, 2.3, 48.8, 2.3) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize(
    "args",
    [
        (None, 0.0, 1.0, 1.0),
        (0.0, None, 1.0, 1.0),
        (0.0, 0.0, None, 1.0),
        (0.0, 0.0, 1.0, None),
    ],
)
def test_haversine_km_none_if_any_coord_missing(args):
    assert haversine_km(*args) is None


def test_parse_iso_handles_z_suffix():
    dt = parse_iso("2024-06-15T10:30:00Z")
    assert dt == datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_iso_handles_explicit_offset():
    dt = parse_iso("2024-06-15T10:30:00+02:00")
    assert dt.utcoffset().total_seconds() == 2 * 3600


def test_day_key_extracts_date_from_z_suffixed_timestamp():
    assert day_key("2024-06-15T23:59:59Z") == "2024-06-15"


def test_day_key_is_stable_across_times_same_day():
    assert day_key("2024-06-15T00:00:01Z") == day_key("2024-06-15T23:59:59Z")
