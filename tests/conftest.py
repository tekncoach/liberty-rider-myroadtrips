"""Shared fixtures for the test suite.

Isolation strategy: every test gets its own throwaway SQLite file (via the
`temp_db` fixture, which monkeypatches `db.DB_PATH`), and every test that
needs the FastAPI app gets a freshly-`importlib.reload`ed `app` module so
that its module-level `db.init_db()` call runs against that fresh file
instead of whatever the previous test left behind.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

import db as db_module


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db.DB_PATH at a throwaway file for the duration of this test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def app_module(temp_db):
    """The `app` module, freshly reloaded so its module-level db.init_db()
    runs against this test's temp_db."""
    import app as app_mod

    importlib.reload(app_mod)
    return app_mod


@pytest.fixture
def make_client(app_module):
    """Factory for fresh TestClients (i.e. fresh cookie jars) against the
    same app/db — needed when a test logs in more than one user at once."""
    def _make():
        return TestClient(app_module.app)

    return _make


@pytest.fixture
def client(make_client):
    return make_client()


class FakeLRClient:
    """Stand-in for LibertyRiderClient. `current_user` is swapped per-login
    by the `login_as` fixture; `stopped_rides_pages` can be set by a test
    before calling /api/sync to control what get_stopped_rides returns, and
    `latest_ride` likewise for /api/sync/status."""

    current_user = {"id": "user-1", "firstName": "Alex", "manualRideCount": 0}
    stopped_rides_pages: list[list[dict]] = [[]]
    # Newest remote ride (`{id, startTime}` or None), as returned to
    # /api/sync/status — set by a test to simulate Liberty Rider having
    # something this account hasn't imported yet.
    latest_ride: dict | None = None

    def __init__(self, token):
        self.token = token
        self._page_index = 0

    def get_current_user(self):
        return type(self).current_user

    def get_latest_ride(self):
        return type(self).latest_ride

    def get_stopped_rides(self, first=50, before=None, after=None, only_favorites=False):
        pages = type(self).stopped_rides_pages
        page = pages[self._page_index] if self._page_index < len(pages) else []
        self._page_index += 1
        return {"stoppedRides": page}


@pytest.fixture
def login_as(app_module, monkeypatch):
    """login_as(test_client, user_id, first_name=..., email=...) logs
    `test_client` in as a fake Liberty Rider user with that id, mocking out
    both Firebase sign-in and the Liberty Rider GraphQL call."""
    monkeypatch.setattr(app_module, "LibertyRiderClient", FakeLRClient)

    def _login(test_client, user_id, first_name="Alex", email=None, password="pw"):
        email = email or f"{user_id}@example.com"
        monkeypatch.setattr(
            app_module,
            "sign_in_with_password",
            lambda e, p, k: {"id_token": f"tok-{user_id}", "refresh_token": f"refresh-{user_id}"},
        )
        FakeLRClient.current_user = {"id": user_id, "firstName": first_name, "manualRideCount": 0}
        FakeLRClient.latest_ride = None
        resp = test_client.post("/api/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, resp.text
        return resp

    return _login


def make_ride(
    ride_id,
    start_time,
    *,
    name="Test ride",
    distance=10.0,
    duration=1000.0,
    duration_without_pauses=900.0,
    total_pauses_duration=100.0,
    pause_count=1,
    maximum_altitude=100.0,
    is_favorite=False,
    hidden=False,
    state="FINISHED",
    detailed_polyline=None,
    start_lat=48.8,
    start_lon=2.3,
    stop_lat=48.9,
    stop_lon=2.4,
    pauses=None,
    resumes=None,
) -> dict:
    """Builds a ride dict shaped like the GraphQL `stoppedRides` payload,
    suitable for db.upsert_ride()."""
    return {
        "id": ride_id,
        "name": name,
        "startTime": start_time,
        "distance": distance,
        "duration": duration,
        "durationWithoutPauses": duration_without_pauses,
        "totalPausesDuration": total_pauses_duration,
        "pauseCount": pause_count,
        "maximumAltitude": maximum_altitude,
        "isFavorite": is_favorite,
        "hidden": hidden,
        "state": state,
        "detailedPolyline": detailed_polyline,
        "previewPictureUrl": None,
        "gpxExportUrl": None,
        "startLocation": {"latitude": start_lat, "longitude": start_lon},
        "stopLocation": {"latitude": stop_lat, "longitude": stop_lon},
        "vehicle": {
            "brandName": "Honda",
            "modelName": "CB500",
            "vehicleType": "MOTORCYCLE",
            "engineDisplacementCc": 500,
        },
        "createdRoadbook": {},
        "pauses": pauses or [],
        "resumes": resumes or [],
    }


def make_event(estimated_time, *, automatic=True, lat=48.8, lon=2.3) -> dict:
    """A single pause/resume entry, shaped like the GraphQL payload."""
    return {"estimatedTime": estimated_time, "automatic": automatic, "lastLocation": {"latitude": lat, "longitude": lon}}


@pytest.fixture
def ride_factory():
    return make_ride
