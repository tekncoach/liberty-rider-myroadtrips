"""PATCH /api/rides/{id}/notes and notes' preservation across a resync —
previously had zero test coverage."""
from conftest import make_ride

import db as db_module


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def test_set_notes(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    resp = client.patch("/api/rides/r1/notes", json={"notes": "  Belle route, col sympa  "})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "Belle route, col sympa"  # trimmed

    ride = client.get("/api/rides/r1").json()
    assert ride["notes"] == "Belle route, col sympa"


def test_clear_notes(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    client.patch("/api/rides/r1/notes", json={"notes": "something"})

    resp = client.patch("/api/rides/r1/notes", json={"notes": "   "})
    assert resp.status_code == 200
    assert resp.json()["notes"] == ""
    assert client.get("/api/rides/r1").json()["notes"] is None


def test_notes_survive_a_resync(client, login_as):
    """A later sync of the same ride (e.g. Liberty Rider edits its name)
    must not wipe out a note the user attached locally."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", name="Original name")
    client.patch("/api/rides/r1/notes", json={"notes": "Remember this spot"})

    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", name="Renamed by Liberty Rider")

    ride = client.get("/api/rides/r1").json()
    assert ride["name"] == "Renamed by Liberty Rider"
    assert ride["notes"] == "Remember this spot"


def test_notes_are_scoped_to_the_owning_account(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    other = make_client()
    login_as(other, "user-2")
    resp = other.patch("/api/rides/r1/notes", json={"notes": "sneaky"})
    assert resp.status_code == 404
