"""POST /api/rides/merge validation rules and lossless unmerge."""
from conftest import insert_returning_id, make_ride

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
        return insert_returning_id(conn, "INSERT INTO roadtrips (user_id, name) VALUES (?, ?)", (user_id, name))
    finally:
        conn.close()


def _set_roadtrip(ride_id, trip_id):
    conn = db_module.connect()
    try:
        conn.execute("UPDATE rides SET roadtrip_id = ? WHERE id = ?", (trip_id, ride_id))
        conn.commit()
    finally:
        conn.close()


def test_merge_requires_at_least_two_ids(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1"]})
    assert resp.status_code == 400


def test_merge_rejects_ride_already_part_of_a_merge(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")
    _seed_ride("user-1", "r3", "2024-01-01T12:00:00Z")

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200

    # r2 is now merged into r1 (merged_into = r1) — trying to merge r2 again
    # (into r3) must be rejected.
    resp = client.post("/api/rides/merge", json={"ride_ids": ["r2", "r3"]})
    assert resp.status_code == 400


def test_merge_rejects_ride_that_already_has_members(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")
    _seed_ride("user-1", "r3", "2024-01-01T12:00:00Z")

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200

    # r1 is now a merge representative (has r2 merged into it) — merging it
    # again (with r3) must be rejected too.
    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r3"]})
    assert resp.status_code == 400


def test_merge_rejects_tagged_rides(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")

    resp = client.post("/api/rides/r1/tags", json={"name": "scenic"})
    assert resp.status_code == 200

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 400


def test_merge_rejects_rides_split_across_two_roadtrips(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")
    trip_a = _seed_roadtrip("user-1", "Trip A")
    trip_b = _seed_roadtrip("user-1", "Trip B")
    _set_roadtrip("r1", trip_a)
    _set_roadtrip("r2", trip_b)

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 400


def test_merge_allows_same_roadtrip(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")
    trip_a = _seed_roadtrip("user-1", "Trip A")
    _set_roadtrip("r1", trip_a)
    _set_roadtrip("r2", trip_a)

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200
    assert resp.json()["id"] == "r1"


def test_merge_allows_one_side_null_roadtrip(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")
    trip_a = _seed_roadtrip("user-1", "Trip A")
    _set_roadtrip("r1", trip_a)
    # r2 has no roadtrip at all.

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200

    conn = db_module.connect()
    try:
        # the representative (earliest start_time = r1) picks up the common roadtrip.
        row = conn.execute("SELECT roadtrip_id FROM rides WHERE id = 'r1'").fetchone()
    finally:
        conn.close()
    assert row["roadtrip_id"] == trip_a


def test_unmerge_restores_original_data_losslessly(client, login_as):
    login_as(client, "user-1")
    ride1_kwargs = dict(
        name="First leg", distance=20.0, duration=1200.0, duration_without_pauses=1000.0,
        total_pauses_duration=200.0, pause_count=1, maximum_altitude=150.0,
        start_lat=48.80, start_lon=2.30, stop_lat=48.85, stop_lon=2.35,
    )
    ride2_kwargs = dict(
        name="Second leg", distance=30.0, duration=1800.0, duration_without_pauses=1600.0,
        total_pauses_duration=200.0, pause_count=2, maximum_altitude=250.0,
        start_lat=48.85, start_lon=2.35, stop_lat=48.90, stop_lon=2.40,
    )
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", **ride1_kwargs)
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z", **ride2_kwargs)

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200
    rep_id = resp.json()["id"]
    assert rep_id == "r1"

    merged = client.get(f"/api/rides/{rep_id}").json()
    assert merged["distance"] == ride1_kwargs["distance"] + ride2_kwargs["distance"]
    assert merged["duration_without_pauses"] == (
        ride1_kwargs["duration_without_pauses"] + ride2_kwargs["duration_without_pauses"]
    )
    assert merged["maximum_altitude"] == 250.0
    assert merged["merge_ride_ids"] == ["r1", "r2"]
    # merged ride ("r2") no longer appears standalone in the rides list.
    listed_ids = [r["id"] for r in client.get("/api/rides").json()]
    assert listed_ids == ["r1"]

    resp = client.delete(f"/api/rides/{rep_id}/merge")
    assert resp.status_code == 200

    listed_ids = sorted(r["id"] for r in client.get("/api/rides").json())
    assert listed_ids == ["r1", "r2"]

    r1 = client.get("/api/rides/r1").json()
    r2 = client.get("/api/rides/r2").json()
    assert r1["name"] == "First leg"
    assert r1["distance"] == 20.0
    assert r1["duration_without_pauses"] == 1000.0
    assert r1["maximum_altitude"] == 150.0
    assert r1["merge_ride_ids"] == ["r1"]

    assert r2["name"] == "Second leg"
    assert r2["distance"] == 30.0
    assert r2["duration_without_pauses"] == 1600.0
    assert r2["maximum_altitude"] == 250.0
    assert r2["merge_ride_ids"] == ["r2"]
