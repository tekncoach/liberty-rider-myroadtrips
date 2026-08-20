"""GET /api/sync/status — "is there anything new on Liberty Rider?".

Regression cover for the bug where the userbar claimed "à jour" from local
state alone (a `manualRideCount` vs local-count diff) and so never noticed
rides waiting on Liberty Rider.
"""
import db as db_module
import sync as sync_module

from conftest import FakeLRClient, make_ride


def _sync_rides(client, rides):
    """Run one /api/sync against a single fake page of `rides`."""
    FakeLRClient.stopped_rides_pages = [rides]
    resp = client.post("/api/sync", json={"full": False})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_status_requires_a_session(client):
    assert client.get("/api/sync/status").status_code == 401


def test_pending_when_nothing_has_been_synced_yet(client, login_as):
    login_as(client, "user-1")
    FakeLRClient.latest_ride = {"id": "r1", "startTime": "2026-07-05T10:00:00.000Z"}

    body = client.get("/api/sync/status").json()
    assert body["pending"] is True
    assert body["local_rides"] == 0
    assert body["last_sync_start_time"] is None


def test_not_pending_when_liberty_rider_has_no_rides(client, login_as):
    login_as(client, "user-1")
    FakeLRClient.latest_ride = None

    body = client.get("/api/sync/status").json()
    assert body["pending"] is False
    assert body["remote_latest_start_time"] is None


def test_not_pending_once_the_newest_remote_ride_is_imported(client, login_as):
    login_as(client, "user-1")
    _sync_rides(client, [make_ride("r1", "2026-07-05T10:00:00.000Z")])
    FakeLRClient.latest_ride = {"id": "r1", "startTime": "2026-07-05T10:00:00.000Z"}

    body = client.get("/api/sync/status").json()
    assert body["pending"] is False
    assert body["local_rides"] == 1
    assert body["last_sync_start_time"] == "2026-07-05T10:00:00.000Z"


def test_pending_when_a_newer_ride_appeared_since_the_last_sync(client, login_as):
    login_as(client, "user-1")
    _sync_rides(client, [make_ride("r1", "2026-07-05T10:00:00.000Z")])
    # Rode again this morning — nothing local changed, so only Liberty
    # Rider itself can reveal it.
    FakeLRClient.latest_ride = {"id": "r2", "startTime": "2026-07-06T08:30:00.000Z"}

    body = client.get("/api/sync/status").json()
    assert body["pending"] is True
    assert body["remote_latest_start_time"] == "2026-07-06T08:30:00.000Z"


def test_syncing_clears_the_pending_flag(client, login_as):
    login_as(client, "user-1")
    _sync_rides(client, [make_ride("r1", "2026-07-05T10:00:00.000Z")])
    FakeLRClient.latest_ride = {"id": "r2", "startTime": "2026-07-06T08:30:00.000Z"}
    assert client.get("/api/sync/status").json()["pending"] is True

    _sync_rides(client, [make_ride("r2", "2026-07-06T08:30:00.000Z")])

    assert client.get("/api/sync/status").json()["pending"] is False


def test_falls_back_to_the_newest_local_ride_when_the_cursor_is_missing(client, login_as):
    """Data can exist without a cursor (claimed orphan data, an interrupted
    first sync) — that must not read as "never synced, everything pending"."""
    login_as(client, "user-1")
    _sync_rides(client, [make_ride("r1", "2026-07-05T10:00:00.000Z")])
    conn = db_module.connect()
    try:
        conn.execute(
            "DELETE FROM sync_state WHERE user_id = ? AND key = ?",
            ("user-1", sync_module.LAST_SYNC_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    FakeLRClient.latest_ride = {"id": "r1", "startTime": "2026-07-05T10:00:00.000Z"}

    body = client.get("/api/sync/status").json()
    assert body["pending"] is False
    assert body["last_sync_start_time"] == "2026-07-05T10:00:00.000Z"


def test_status_is_scoped_to_the_session_account(make_client, login_as):
    alice, bob = make_client(), make_client()
    login_as(alice, "user-1", first_name="Alice")
    _sync_rides(alice, [make_ride("r1", "2026-07-05T10:00:00.000Z")])
    login_as(bob, "user-2", first_name="Bob")
    FakeLRClient.latest_ride = {"id": "r1", "startTime": "2026-07-05T10:00:00.000Z"}

    # Same remote ride: already imported for Alice, still to import for Bob.
    assert alice.get("/api/sync/status").json()["pending"] is False
    bob_body = bob.get("/api/sync/status").json()
    assert bob_body["pending"] is True
    assert bob_body["local_rides"] == 0
