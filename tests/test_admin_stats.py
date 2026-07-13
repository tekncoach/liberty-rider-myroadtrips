"""GET /api/admin/stats — maintainer-only dashboard (gated on
ADMIN_USER_IDS): per-account activity plus overall totals."""
import db as db_module
from conftest import make_ride


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def test_non_admin_gets_403(client, login_as):
    login_as(client, "user-1")
    resp = client.get("/api/admin/stats")
    assert resp.status_code == 403


def test_admin_sees_totals_and_per_user_rows(client, make_client, app_module, monkeypatch, login_as):
    monkeypatch.setattr(app_module, "ADMIN_USER_IDS", {"admin-1"})
    login_as(client, "admin-1", first_name="Alex", email="alex@example.com")
    _seed_ride("admin-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("admin-1", "r2", "2024-01-02T10:00:00Z")
    client.post("/api/roadtrips", json={"name": "Trip A", "ride_ids": ["r1", "r2"]})

    # a second account with its own data — must appear as a separate row,
    # scoped to its own counts.
    other = make_client()
    login_as(other, "user-2", first_name="Sam", email="sam@example.com")
    _seed_ride("user-2", "r3", "2024-03-01T10:00:00Z")

    resp = client.get("/api/admin/stats")
    assert resp.status_code == 200
    data = resp.json()

    assert data["user_count"] == 2
    assert data["total_rides"] == 3
    assert data["total_roadtrips"] == 1

    by_id = {u["id"]: u for u in data["users"]}
    assert by_id["admin-1"]["rides"] == 2
    assert by_id["admin-1"]["roadtrips"] == 1
    assert by_id["admin-1"]["email"] == "alex@example.com"
    assert by_id["user-2"]["rides"] == 1
    assert by_id["user-2"]["roadtrips"] == 0
    assert by_id["user-2"]["email"] == "sam@example.com"
    # last_session is populated (each logged in at least once)
    assert by_id["admin-1"]["last_session"] is not None
