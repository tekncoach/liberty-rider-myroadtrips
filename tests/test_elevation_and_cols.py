"""elevation.py, mountain_pass.py, and the peak-detection/refinement/
resampling helpers in app.py behind the elevation chart and named cols.

Both elevation.py and mountain_pass.py hit real external APIs
(open-elevation, OpenStreetMap Overpass) — every test here monkeypatches
`requests.post` on the relevant module so the suite never makes a real
network call.
"""
from __future__ import annotations

import polyline as polyline_lib
import pytest
import requests

import db as db_module
import elevation as elevation_module
import mountain_pass as mountain_pass_module
from conftest import make_ride


@pytest.fixture
def conn(temp_db):
    db_module.init_db()
    connection = db_module.connect()
    yield connection
    connection.close()


class FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise requests.RequestException("boom")

    def json(self):
        return self._payload


# --- elevation.get_elevations ------------------------------------------------


def test_get_elevations_caches_and_avoids_refetching(conn, monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["locations"])
        return FakeResponse({"results": [{"elevation": 123.0} for _ in json["locations"]]})

    monkeypatch.setattr(elevation_module.requests, "post", fake_post)

    result = elevation_module.get_elevations(conn, [(45.0, 6.0), (45.1, 6.1)])
    assert result == [123.0, 123.0]
    assert len(calls) == 1

    # Same points again — must come entirely from cache, no second request.
    result2 = elevation_module.get_elevations(conn, [(45.0, 6.0), (45.1, 6.1)])
    assert result2 == [123.0, 123.0]
    assert len(calls) == 1


def test_get_elevations_returns_none_on_request_failure(conn, monkeypatch):
    monkeypatch.setattr(
        elevation_module.requests, "post",
        lambda *a, **kw: FakeResponse({}, status_ok=False),
    )
    result = elevation_module.get_elevations(conn, [(45.0, 6.0)])
    assert result == [None]
    # A failed lookup must not be cached — a later successful call should
    # still try the network again for the same point.
    assert conn.execute("SELECT COUNT(*) AS n FROM elevation_cache").fetchone()["n"] == 0


def test_get_elevations_preserves_input_order_with_duplicates(conn, monkeypatch):
    monkeypatch.setattr(
        elevation_module.requests, "post",
        lambda url, json, timeout: FakeResponse({
            "results": [{"elevation": 10.0 * (i + 1)} for i in range(len(json["locations"]))]
        }),
    )
    points = [(45.0, 6.0), (46.0, 7.0), (45.0, 6.0)]  # first point repeated
    result = elevation_module.get_elevations(conn, points)
    assert result[0] == result[2]  # same coordinate, same elevation, from cache the second time


# --- mountain_pass.get_pass_name ---------------------------------------------


def test_get_pass_name_caches_successful_no_result(conn, monkeypatch):
    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append(data)
        return FakeResponse({"elements": []})

    monkeypatch.setattr(mountain_pass_module.requests, "post", fake_post)

    assert mountain_pass_module.get_pass_name(conn, 45.0, 6.0) is None
    assert len(calls) == 1
    # Cached — a second call for the same spot must not hit the network again.
    assert mountain_pass_module.get_pass_name(conn, 45.0, 6.0) is None
    assert len(calls) == 1


def test_get_pass_name_does_not_cache_request_failure(conn, monkeypatch):
    monkeypatch.setattr(
        mountain_pass_module.requests, "post",
        lambda *a, **kw: FakeResponse({}, status_ok=False),
    )
    assert mountain_pass_module.get_pass_name(conn, 45.0, 6.0) is None
    assert conn.execute("SELECT COUNT(*) AS n FROM mountain_pass_cache").fetchone()["n"] == 0


def test_get_pass_name_disambiguates_by_elevation_hint(conn, monkeypatch):
    """Two named candidates within radius (a col and a summit, the Mont
    Ventoux / Col des Tempêtes case) — the one whose OSM `ele` tag is
    closest to our measured elevation must win, not just the nearest one."""
    elements = [
        {"lat": 45.0001, "lon": 6.0001, "tags": {"name": "Col des Tempêtes", "ele": "1827"}},
        {"lat": 45.0002, "lon": 6.0002, "tags": {"name": "Mont Ventoux", "ele": "1910"}},
    ]
    monkeypatch.setattr(
        mountain_pass_module.requests, "post",
        lambda *a, **kw: FakeResponse({"elements": elements}),
    )
    name = mountain_pass_module.get_pass_name(conn, 45.0, 6.0, hint_elevation=1901.0)
    assert name == "Mont Ventoux"


def test_get_pass_name_falls_back_to_distance_without_hint(conn, monkeypatch):
    elements = [
        {"lat": 45.01, "lon": 6.01, "tags": {"name": "Far one", "ele": "1000"}},
        {"lat": 45.0001, "lon": 6.0001, "tags": {"name": "Near one", "ele": "9999"}},
    ]
    monkeypatch.setattr(
        mountain_pass_module.requests, "post",
        lambda *a, **kw: FakeResponse({"elements": elements}),
    )
    name = mountain_pass_module.get_pass_name(conn, 45.0, 6.0)
    assert name == "Near one"


# --- app.py: _resample_indices / _detect_peaks / _refine_peak_point --------


def test_resample_indices_includes_first_and_last(app_module):
    cum = [0.0, 1.0, 2.0, 3.0, 10.0, 20.0, 21.0, 22.0]
    idx = app_module._resample_indices(cum, target_count=4)
    assert idx[0] == 0
    assert idx[-1] == len(cum) - 1
    assert idx == sorted(set(idx))


def test_resample_indices_returns_everything_when_fewer_than_target(app_module):
    cum = [0.0, 1.0, 2.0]
    assert app_module._resample_indices(cum, target_count=60) == [0, 1, 2]


def test_detect_peaks_requires_prominence(app_module):
    # A tiny 20m bump shouldn't register as a peak at the default 100m
    # prominence threshold, but a real 500m one should.
    flat = [{"elevation": 100.0} for _ in range(5)]
    profile = flat + [{"elevation": 120.0}] + flat
    assert app_module._detect_peaks(profile) == []

    profile2 = flat + [{"elevation": 700.0}] + flat
    peaks = app_module._detect_peaks(profile2)
    assert peaks == [5]


def test_detect_peaks_ignores_none_elevations(app_module):
    profile = [{"elevation": 100.0}, {"elevation": None}, {"elevation": 700.0}, {"elevation": None}, {"elevation": 100.0}]
    # Must not raise on None neighbors, and shouldn't detect a peak it can't
    # actually confirm climbs-then-descends around.
    assert app_module._detect_peaks(profile) == []


def test_refine_peak_point_prefers_a_nearby_pause_over_the_coarse_sample(app_module, conn, monkeypatch):
    points = [(45.0, 6.0), (45.001, 6.001), (45.002, 6.002), (45.003, 6.003), (45.004, 6.004)]
    sample_idx = [0, 2, 4]  # coarse profile only sampled every other point
    pauses = [{"lat": 45.0025, "lon": 6.0025}]  # near index 2-3, slightly off-path

    # Fake elevations: the window's raw points all read low, but the pause
    # location reads as the true high point.
    def fake_get_elevations(conn_, pts):
        return [999.0 if pt == (45.0025, 6.0025) else 50.0 for pt in pts]

    monkeypatch.setattr(app_module.elevation, "get_elevations", fake_get_elevations)

    (lat, lon), elev = app_module._refine_peak_point(conn, points, sample_idx, pauses, peak_i=1)
    assert (lat, lon) == (45.0025, 6.0025)
    assert elev == 999.0


# --- integration: /elevation and /cols endpoints, and ride_cols persistence --


def _mountain_profile_points():
    """A simple up-then-down track, encoded as a real polyline — enough
    points that _resample_indices/_detect_peaks have something to chew on."""
    lats = [45.000 + i * 0.001 for i in range(10)] + [45.010 - i * 0.001 for i in range(10)]
    lons = [6.000] * 20
    return list(zip(lats, lons))


def test_elevation_endpoint_is_fast_and_never_touches_mountain_pass(client, login_as, monkeypatch, app_module):
    """The elevation chart must render without any Overpass lookups at
    all — cols are a separate, slower endpoint."""
    login_as(client, "user-1")
    points = _mountain_profile_points()
    encoded = polyline_lib.encode(points, precision=5)
    conn = db_module.connect()
    db_module.upsert_ride(conn, "user-1", make_ride("r1", "2024-06-01T08:00:00Z", detailed_polyline=encoded))
    conn.commit()
    conn.close()

    monkeypatch.setattr(app_module.elevation, "get_elevations", lambda conn_, pts: [100.0] * len(pts))

    def fail_if_called(*a, **kw):
        raise AssertionError("mountain_pass.get_pass_name must not be called from /elevation")

    monkeypatch.setattr(app_module.mountain_pass, "get_pass_name", fail_if_called)

    resp = client.get("/api/rides/r1/elevation")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["profile"]) > 0
    assert data["elevation_gain"] == 0  # flat fake elevations


def test_cols_endpoint_persists_names_and_they_become_searchable(client, login_as, monkeypatch, app_module):
    login_as(client, "user-1")
    points = _mountain_profile_points()
    encoded = polyline_lib.encode(points, precision=5)
    conn = db_module.connect()
    db_module.upsert_ride(conn, "user-1", make_ride("r1", "2024-06-01T08:00:00Z", name="Ride to the peak", detailed_polyline=encoded))
    conn.commit()
    conn.close()

    # Elevations shaped like the points: climbs then descends, comfortably
    # over the default 100m prominence threshold.
    def fake_get_elevations(conn_, pts):
        return [100.0 + i * 20 if i < len(pts) / 2 else 100.0 + (len(pts) - i) * 20 for i, pt in enumerate(pts)]

    monkeypatch.setattr(app_module.elevation, "get_elevations", fake_get_elevations)
    monkeypatch.setattr(app_module.mountain_pass, "get_pass_name", lambda conn_, lat, lon, hint_elevation=None: "Col du Test")

    resp = client.get("/api/rides/r1/cols")
    assert resp.status_code == 200
    cols = resp.json()["cols"]
    assert len(cols) >= 1
    assert all(c["name"] == "Col du Test" for c in cols)

    # Persisted...
    conn = db_module.connect()
    rows = conn.execute("SELECT name FROM ride_cols WHERE ride_id = 'r1'").fetchall()
    conn.close()
    assert [r["name"] for r in rows] == [c["name"] for c in cols]

    # ...and surfaced on the rides list for client-side search.
    listed = client.get("/api/rides").json()
    ride = next(r for r in listed if r["id"] == "r1")
    assert ride["col_names"] == [c["name"] for c in cols]


def test_cols_endpoint_reruns_replace_previous_names(client, login_as, monkeypatch, app_module):
    """A ride's cols must be replaced, not accumulated, on a second lookup
    (e.g. after an algorithm change) — not append duplicates forever."""
    login_as(client, "user-1")
    points = _mountain_profile_points()
    encoded = polyline_lib.encode(points, precision=5)
    conn = db_module.connect()
    db_module.upsert_ride(conn, "user-1", make_ride("r1", "2024-06-01T08:00:00Z", detailed_polyline=encoded))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        app_module.elevation, "get_elevations",
        lambda conn_, pts: [100.0 + i * 20 if i < len(pts) / 2 else 100.0 + (len(pts) - i) * 20 for i, pt in enumerate(pts)],
    )
    monkeypatch.setattr(app_module.mountain_pass, "get_pass_name", lambda conn_, lat, lon, hint_elevation=None: "Col du Test")

    first_cols = client.get("/api/rides/r1/cols").json()["cols"]
    second_cols = client.get("/api/rides/r1/cols").json()["cols"]
    assert len(first_cols) == len(second_cols)

    conn = db_module.connect()
    n = conn.execute("SELECT COUNT(*) AS n FROM ride_cols WHERE ride_id = 'r1'").fetchone()["n"]
    conn.close()
    assert n == len(second_cols)
