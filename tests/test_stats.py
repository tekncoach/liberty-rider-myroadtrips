"""GET /api/stats — account totals, records and per-year breakdown."""
from conftest import make_ride

import db as db_module


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def test_stats_totals_arithmetic(client, login_as):
    login_as(client, "user-1")
    _seed_ride(
        "user-1", "r1", "2024-03-01T10:00:00Z",
        distance=100.0, duration=3600.0, duration_without_pauses=3000.0, pause_count=2,
        maximum_altitude=500.0,
    )
    _seed_ride(
        "user-1", "r2", "2024-03-02T10:00:00Z",
        distance=50.0, duration=1800.0, duration_without_pauses=1500.0, pause_count=1,
        maximum_altitude=800.0,
    )

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()

    assert body["totals"] == {
        "ride_count": 2,
        "distance": 150.0,
        "duration": 5400.0,
        "duration_without_pauses": 4500.0,
        "pause_count": 3,
    }
    assert body["records"]["longest_ride"] == {"id": "r1", "name": "Test ride", "distance": 100.0}
    assert body["records"]["highest_point"] == {"id": "r2", "name": "Test ride", "maximum_altitude": 800.0}
    assert body["records"]["longest_day"] == {"date": "2024-03-01", "distance": 100.0, "ride_count": 1}
    assert body["by_year"] == [{"year": 2024, "ride_count": 2, "distance": 150.0, "duration": 5400.0}]


def test_stats_is_scoped_to_own_user(client, make_client, login_as):
    login_as(client, "user-1")
    other_client = make_client()
    login_as(other_client, "user-2")

    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", distance=10.0)
    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z", distance=999.0)

    stats_u1 = client.get("/api/stats").json()
    stats_u2 = other_client.get("/api/stats").json()

    assert stats_u1["totals"]["ride_count"] == 1
    assert stats_u1["totals"]["distance"] == 10.0
    assert stats_u1["records"]["longest_ride"]["id"] == "r1"

    assert stats_u2["totals"]["ride_count"] == 1
    assert stats_u2["totals"]["distance"] == 999.0
    assert stats_u2["records"]["longest_ride"]["id"] == "r2"


def test_stats_counts_merged_ride_once(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", distance=100.0)
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z", distance=50.0)

    resp = client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]})
    assert resp.status_code == 200

    stats = client.get("/api/stats").json()
    # Only the merge representative is counted — its distance already
    # includes r2's, so ride_count is 1, not 2, and distance is not doubled.
    assert stats["totals"]["ride_count"] == 1
    assert stats["totals"]["distance"] == 150.0


def test_stats_excludes_hidden_rides(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", distance=100.0, hidden=False)
    _seed_ride("user-1", "r2", "2024-01-02T10:00:00Z", distance=999.0, hidden=True)

    stats = client.get("/api/stats").json()
    assert stats["totals"]["ride_count"] == 1
    assert stats["totals"]["distance"] == 100.0
    assert stats["records"]["longest_ride"]["id"] == "r1"


def test_stats_empty_account_is_zeroed_not_500(client, login_as):
    login_as(client, "user-1")

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()

    assert body["totals"] == {
        "ride_count": 0,
        "distance": 0,
        "duration": 0,
        "duration_without_pauses": 0,
        "pause_count": 0,
    }
    assert body["records"] == {"longest_ride": None, "highest_point": None, "longest_day": None}
    assert body["by_year"] == []


def test_stats_by_year_sorted_newest_first(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2023-05-01T10:00:00Z", distance=10.0, duration=100.0)
    _seed_ride("user-1", "r2", "2024-05-01T10:00:00Z", distance=20.0, duration=200.0)
    _seed_ride("user-1", "r3", "2022-05-01T10:00:00Z", distance=30.0, duration=300.0)

    body = client.get("/api/stats").json()
    assert [y["year"] for y in body["by_year"]] == [2024, 2023, 2022]
