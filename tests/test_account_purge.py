"""DELETE /api/account/data — maintainer-only tool (gated on ADMIN_USER_IDS)
to wipe one's own synced data and re-test onboarding from empty. Previously
had zero test coverage."""
from conftest import make_ride

import db as db_module


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def test_non_admin_gets_403(client, login_as):
    login_as(client, "user-1")
    resp = client.delete("/api/account/data")
    assert resp.status_code == 403


def test_admin_purges_own_data(client, app_module, monkeypatch, login_as):
    monkeypatch.setattr(app_module, "ADMIN_USER_IDS", {"user-1"})
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    client.post("/api/rides/r1/tags", json={"name": "Chevreuse"})
    client.post("/api/roadtrips", json={"name": "Trip A", "ride_ids": ["r1"]})

    assert len(client.get("/api/rides").json()) == 1
    assert len(client.get("/api/roadtrips").json()) == 1
    assert len(client.get("/api/tags").json()) == 1

    resp = client.delete("/api/account/data")
    assert resp.status_code == 200

    assert client.get("/api/rides").json() == []
    assert client.get("/api/roadtrips").json() == []
    assert client.get("/api/tags").json() == []

    # The account itself (session/login) survives the purge.
    assert client.get("/api/auth/status").json()["logged_in"] is True


def test_purge_does_not_touch_another_account(client, make_client, app_module, monkeypatch, login_as):
    monkeypatch.setattr(app_module, "ADMIN_USER_IDS", {"user-1"})
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    other = make_client()
    login_as(other, "user-2")
    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")

    client.delete("/api/account/data")

    assert client.get("/api/rides").json() == []
    assert len(other.get("/api/rides").json()) == 1
