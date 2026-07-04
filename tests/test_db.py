import sqlite3

import pytest

import db as db_module
from conftest import make_ride


@pytest.fixture
def conn(temp_db):
    db_module.init_db()
    connection = db_module.connect()
    yield connection
    connection.close()


# --- init_db --------------------------------------------------------------


def test_init_db_creates_expected_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for expected in ("users", "sessions", "rides", "roadtrips", "pauses", "sync_state", "tags", "ride_tags"):
        assert expected in tables


def test_init_db_is_idempotent(temp_db):
    db_module.init_db()
    db_module.init_db()  # must not raise on a second run
    conn = db_module.connect()
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "rides" in tables
    finally:
        conn.close()


# --- upsert_ride ------------------------------------------------------------


def test_upsert_ride_inserts_new_ride(conn):
    ride = make_ride("r1", "2024-01-01T10:00:00Z", name="Morning loop", distance=42.0)
    db_module.upsert_ride(conn, "u1", ride)
    conn.commit()

    row = conn.execute("SELECT * FROM rides WHERE id = 'r1'").fetchone()
    assert row is not None
    assert row["user_id"] == "u1"
    assert row["name"] == "Morning loop"
    assert row["distance"] == 42.0
    assert row["roadtrip_id"] is None
    assert row["merged_into"] is None


def test_upsert_ride_update_preserves_roadtrip_id_and_merged_into(conn):
    ride = make_ride("r1", "2024-01-01T10:00:00Z", name="Original name", distance=10.0)
    db_module.upsert_ride(conn, "u1", ride)
    conn.commit()

    # Simulate this ride having since been grouped into a roadtrip and merged
    # into another ride (representative "r2") — state that a later sync must
    # never silently wipe out.
    conn.execute(
        "INSERT INTO rides (id, user_id, start_time) VALUES ('r2', 'u1', '2024-01-01T09:00:00Z')"
    )
    conn.execute("UPDATE rides SET roadtrip_id = ?, merged_into = ? WHERE id = 'r1'", (5, "r2"))
    conn.commit()

    updated_ride = make_ride("r1", "2024-01-01T10:00:00Z", name="Updated name", distance=99.0)
    db_module.upsert_ride(conn, "u1", updated_ride)
    conn.commit()

    row = conn.execute("SELECT * FROM rides WHERE id = 'r1'").fetchone()
    assert row["name"] == "Updated name"
    assert row["distance"] == 99.0
    assert row["roadtrip_id"] == 5
    assert row["merged_into"] == "r2"


def test_upsert_ride_replaces_pauses(conn):
    ride = make_ride(
        "r1",
        "2024-01-01T10:00:00Z",
        pauses=[{"estimatedTime": "2024-01-01T10:30:00Z", "automatic": True, "lastLocation": {"latitude": 1.0, "longitude": 2.0}}],
    )
    db_module.upsert_ride(conn, "u1", ride)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM pauses WHERE ride_id = 'r1'").fetchone()["n"] == 1

    ride_no_pauses = make_ride("r1", "2024-01-01T10:00:00Z", pauses=[])
    db_module.upsert_ride(conn, "u1", ride_no_pauses)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM pauses WHERE ride_id = 'r1'").fetchone()["n"] == 0


def test_upsert_ride_replaces_resumes(conn):
    ride = make_ride(
        "r1",
        "2024-01-01T10:00:00Z",
        resumes=[{"estimatedTime": "2024-01-01T10:35:00Z", "automatic": True, "lastLocation": {"latitude": 1.0, "longitude": 2.0}}],
    )
    db_module.upsert_ride(conn, "u1", ride)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM resumes WHERE ride_id = 'r1'").fetchone()["n"] == 1

    ride_no_resumes = make_ride("r1", "2024-01-01T10:00:00Z", resumes=[])
    db_module.upsert_ride(conn, "u1", ride_no_resumes)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM resumes WHERE ride_id = 'r1'").fetchone()["n"] == 0


# --- _backfill_resumes_from_raw_json ----------------------------------------


def test_backfill_resumes_restores_from_raw_json(conn):
    # The GraphQL query has always requested `resumes`, so a ride synced
    # before the `resumes` table existed still has it recorded in raw_json —
    # the backfill's whole point is to recover it from there with no resync.
    ride = make_ride(
        "r1",
        "2024-01-01T10:00:00Z",
        pauses=[{"estimatedTime": "2024-01-01T10:30:00Z", "automatic": True, "lastLocation": {"latitude": 1.0, "longitude": 2.0}}],
        resumes=[{"estimatedTime": "2024-01-01T10:35:00Z", "automatic": True, "lastLocation": {"latitude": 1.0, "longitude": 2.0}}],
    )
    db_module.upsert_ride(conn, "u1", ride)
    conn.commit()

    # Simulate the pre-migration state: raw_json already has `resumes`, but
    # the (new) resumes table is still empty.
    conn.execute("DELETE FROM resumes")
    conn.commit()

    db_module._backfill_resumes_from_raw_json(conn)

    row = conn.execute("SELECT * FROM resumes WHERE ride_id = 'r1'").fetchone()
    assert row is not None
    assert row["estimated_time"] == "2024-01-01T10:35:00Z"
    assert bool(row["automatic"]) is True
    assert row["lat"] == 1.0 and row["lon"] == 2.0


def test_backfill_resumes_is_a_noop_once_table_has_rows(conn):
    ride1 = make_ride("r1", "2024-01-01T10:00:00Z", resumes=[{"estimatedTime": "2024-01-01T10:35:00Z", "automatic": True}])
    ride2 = make_ride("r2", "2024-01-02T10:00:00Z", resumes=[{"estimatedTime": "2024-01-02T10:35:00Z", "automatic": True}])
    db_module.upsert_ride(conn, "u1", ride1)
    db_module.upsert_ride(conn, "u1", ride2)
    conn.commit()

    # Simulate r2's resume row having been lost some other way — the table
    # is non-empty (r1's row survives), so the backfill's guard must skip
    # entirely rather than selectively restoring r2's, even though r2's
    # resume is still sitting right there in raw_json.
    conn.execute("DELETE FROM resumes WHERE ride_id = 'r2'")
    conn.commit()

    db_module._backfill_resumes_from_raw_json(conn)

    assert conn.execute("SELECT COUNT(*) AS n FROM resumes WHERE ride_id = 'r2'").fetchone()["n"] == 0


# --- claim_orphaned_data ----------------------------------------------------


def _insert_orphan_ride(conn, ride_id="r1"):
    conn.execute(
        "INSERT INTO rides (id, user_id, start_time) VALUES (?, NULL, '2024-01-01T10:00:00Z')",
        (ride_id,),
    )
    conn.commit()


def test_claim_orphaned_data_claims_when_single_user(conn):
    db_module.upsert_user(conn, "u1", "Alex", "alex@example.com")
    _insert_orphan_ride(conn, "r1")
    conn.execute("INSERT INTO roadtrips (id, user_id, name) VALUES (1, NULL, 'Trip')")
    conn.execute("INSERT INTO tags (id, user_id, name) VALUES (1, NULL, 'scenic')")
    conn.commit()

    db_module.claim_orphaned_data(conn, "u1")

    assert conn.execute("SELECT user_id FROM rides WHERE id = 'r1'").fetchone()["user_id"] == "u1"
    assert conn.execute("SELECT user_id FROM roadtrips WHERE id = 1").fetchone()["user_id"] == "u1"
    assert conn.execute("SELECT user_id FROM tags WHERE id = 1").fetchone()["user_id"] == "u1"


def test_claim_orphaned_data_does_not_claim_for_second_user(conn):
    db_module.upsert_user(conn, "u1", "Alex", "alex@example.com")
    db_module.upsert_user(conn, "u2", "Sam", "sam@example.com")
    _insert_orphan_ride(conn, "r1")
    conn.commit()

    # u1 already owns the account that pre-existed before multi-tenancy;
    # u2 is a brand-new second user and must NEVER get u1's (or anyone's)
    # pre-existing ownerless data.
    db_module.claim_orphaned_data(conn, "u2")

    row = conn.execute("SELECT user_id FROM rides WHERE id = 'r1'").fetchone()
    assert row["user_id"] is None


def test_claim_orphaned_data_noop_when_no_users(conn):
    _insert_orphan_ride(conn, "r1")
    conn.commit()
    # Should not raise even though 0 users exist (defensive — shouldn't
    # normally happen since claim_orphaned_data is only called right after
    # upsert_user in app.py, but the guard is `user_count != 1`, covering 0 too).
    db_module.claim_orphaned_data(conn, "whoever")
    row = conn.execute("SELECT user_id FROM rides WHERE id = 'r1'").fetchone()
    assert row["user_id"] is None


# --- tags UNIQUE(user_id, name) --------------------------------------------


def test_tags_same_name_allowed_for_different_users(conn):
    db_module.upsert_user(conn, "u1", "Alex", "alex@example.com")
    db_module.upsert_user(conn, "u2", "Sam", "sam@example.com")
    conn.execute("INSERT INTO tags (user_id, name) VALUES ('u1', 'scenic')")
    conn.execute("INSERT INTO tags (user_id, name) VALUES ('u2', 'scenic')")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) AS n FROM tags WHERE name = 'scenic'").fetchone()["n"]
    assert count == 2


def test_tags_same_name_rejected_twice_for_same_user(conn):
    db_module.upsert_user(conn, "u1", "Alex", "alex@example.com")
    conn.execute("INSERT INTO tags (user_id, name) VALUES ('u1', 'scenic')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tags (user_id, name) VALUES ('u1', 'scenic')")
