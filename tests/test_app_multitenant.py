"""The core multi-tenant guarantee: users only ever see their own data."""
from conftest import make_ride

import db as db_module


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def _seed_roadtrip(user_id, name):
    conn = db_module.connect()
    try:
        cur = conn.execute("INSERT INTO roadtrips (user_id, name) VALUES (?, ?)", (user_id, name))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _seed_tag(user_id, name):
    conn = db_module.connect()
    try:
        cur = conn.execute("INSERT INTO tags (user_id, name) VALUES (?, ?)", (user_id, name))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_rides_list_is_scoped_to_own_user(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")

    rides_u1 = client.get("/api/rides").json()
    rides_u2 = other_client.get("/api/rides").json()

    assert [r["id"] for r in rides_u1] == ["r1"]
    assert [r["id"] for r in rides_u2] == ["r2"]


def test_ride_detail_cross_user_is_404(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")

    resp = client.get("/api/rides/r2")
    assert resp.status_code == 404

    resp = other_client.get("/api/rides/r2")
    assert resp.status_code == 200


def test_roadtrips_list_and_detail_are_scoped(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    trip_id = _seed_roadtrip("user-2", "Vexin loop")

    assert client.get("/api/roadtrips").json() == []
    assert [t["id"] for t in other_client.get("/api/roadtrips").json()] == [trip_id]

    assert client.get(f"/api/roadtrips/{trip_id}").status_code == 404
    assert other_client.get(f"/api/roadtrips/{trip_id}").status_code == 200


def test_tags_list_and_detail_are_scoped(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    tag_id = _seed_tag("user-2", "scenic")

    assert client.get("/api/tags").json() == []
    assert [t["id"] for t in other_client.get("/api/tags").json()] == [tag_id]

    assert client.get(f"/api/tags/{tag_id}").status_code == 404
    assert other_client.get(f"/api/tags/{tag_id}").status_code == 200


def test_cannot_attach_tag_to_other_users_ride(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")

    resp = client.post("/api/rides/r2/tags", json={"name": "scenic"})
    assert resp.status_code == 404


def test_cannot_detach_other_users_ride_from_roadtrip(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")

    resp = client.delete("/api/rides/r2/roadtrip")
    assert resp.status_code == 404


def test_cannot_add_other_users_rides_to_own_roadtrip(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")
    trip_id = _seed_roadtrip("user-1", "My trip")

    resp = client.post(f"/api/roadtrips/{trip_id}/rides", json={"ride_ids": ["r2"]})
    assert resp.status_code == 200  # the roadtrip is owned, so the call succeeds...

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT roadtrip_id FROM rides WHERE id = 'r2'").fetchone()
    finally:
        conn.close()
    # ...but the UPDATE is scoped by user_id, so the other user's ride is untouched.
    assert row["roadtrip_id"] is None
