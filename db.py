"""SQLite storage for personal Liberty Rider rides + manual roadtrip grouping.

Multi-tenant: every ride/roadtrip/tag belongs to a `users` row, keyed
directly by the user's own Liberty Rider id (`currentUser.id` — no separate
internal id is minted). Every request is scoped to the logged-in session's
user — see the `get_session_user` dependency in app.py.
"""
from __future__ import annotations

import json
import pathlib
import secrets
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "data" / "rides.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  first_name TEXT,
  email TEXT,
  bearer_token TEXT,
  refresh_token TEXT,
  firebase_api_key TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rides (
  id TEXT PRIMARY KEY,
  user_id TEXT REFERENCES users(id),
  name TEXT,
  start_time TEXT NOT NULL,
  distance REAL,
  duration REAL,
  duration_without_pauses REAL,
  total_pauses_duration REAL,
  pause_count INTEGER,
  maximum_altitude REAL,
  is_favorite INTEGER,
  hidden INTEGER,
  state TEXT,
  detailed_polyline TEXT,
  preview_picture_url TEXT,
  gpx_export_url TEXT,
  gpx_local_path TEXT,
  start_lat REAL, start_lon REAL,
  stop_lat REAL, stop_lon REAL,
  vehicle_brand TEXT, vehicle_model TEXT, vehicle_type TEXT, vehicle_cc REAL,
  created_roadbook_id TEXT,
  roadtrip_id INTEGER REFERENCES roadtrips(id),
  merged_into TEXT REFERENCES rides(id),
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS roadtrips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT REFERENCES users(id),
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pauses (
  ride_id TEXT REFERENCES rides(id),
  estimated_time TEXT,
  automatic INTEGER,
  lat REAL, lon REAL
);

CREATE TABLE IF NOT EXISTS sync_state (
  user_id TEXT REFERENCES users(id),
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS ride_tags (
  ride_id TEXT REFERENCES rides(id),
  tag_id INTEGER REFERENCES tags(id),
  PRIMARY KEY (ride_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_rides_start_time ON rides(start_time);
CREATE INDEX IF NOT EXISTS idx_rides_roadtrip ON rides(roadtrip_id);
CREATE INDEX IF NOT EXISTS idx_pauses_ride ON pauses(ride_id);
CREATE INDEX IF NOT EXISTS idx_ride_tags_tag ON ride_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# Indexes on columns added via migration (below) — created after the ALTER
# TABLE runs, since the column may not exist yet on a pre-existing database.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_rides_merged_into ON rides(merged_into);
CREATE INDEX IF NOT EXISTS idx_rides_user ON rides(user_id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Foreign keys are documentation-only here (no cascades relied upon) —
    # and enforcement doesn't play well with the RENAME-based table
    # migrations below (SQLite silently rewrites other tables' REFERENCES
    # clauses to point at the renamed-away table, e.g. `users_old`).
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)

        _migrate_users_table(conn)

        rides_cols = {row["name"] for row in conn.execute("PRAGMA table_info(rides)")}
        if "preview_picture_url" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN preview_picture_url TEXT")
        if "merged_into" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN merged_into TEXT REFERENCES rides(id)")
        if "user_id" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN user_id TEXT REFERENCES users(id)")

        roadtrips_cols = {row["name"] for row in conn.execute("PRAGMA table_info(roadtrips)")}
        if "user_id" not in roadtrips_cols:
            conn.execute("ALTER TABLE roadtrips ADD COLUMN user_id TEXT REFERENCES users(id)")

        _migrate_tags_table(conn)
        _migrate_sync_state_table(conn)

        conn.executescript(POST_MIGRATION_INDEXES)
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_users_table(conn: sqlite3.Connection) -> None:
    """An early draft of this table used a surrogate autoincrement `id` with
    a separate `liberty_rider_id` column; the current design uses the
    Liberty Rider id directly as the primary key, plus `first_name`. Rebuild
    if the old shape is detected — safe since no real session/login existed
    yet on that shape (nothing references it except this same migration).

    Commits right after finishing (and is safe to re-run: it recovers a
    `users_old` left behind by a previous run that got interrupted before
    dropping it, instead of silently stranding its data — this bit us once
    when an unrelated later step in the same init_db() call raised before
    the single end-of-function commit was reached)."""
    if _table_exists(conn, "users_old"):
        _finish_users_migration(conn)
        conn.commit()
        return
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "first_name" in cols:
        return
    conn.execute("ALTER TABLE users RENAME TO users_old")
    _finish_users_migration(conn)
    conn.commit()


def _finish_users_migration(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY,
          first_name TEXT,
          email TEXT,
          bearer_token TEXT,
          refresh_token TEXT,
          firebase_api_key TEXT,
          created_at TEXT NOT NULL
        )
    """)
    old_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users_old)")}
    if "liberty_rider_id" in old_cols:
        conn.execute("""
            INSERT OR IGNORE INTO users (id, email, bearer_token, refresh_token, firebase_api_key, created_at)
            SELECT liberty_rider_id, email, bearer_token, refresh_token, firebase_api_key, created_at
            FROM users_old WHERE liberty_rider_id IS NOT NULL
        """)
    conn.execute("DROP TABLE users_old")


def _migrate_tags_table(conn: sqlite3.Connection) -> None:
    """tags used to be a single global namespace (UNIQUE(name)); multi-tenancy
    needs UNIQUE(user_id, name) instead, which SQLite can't ALTER onto an
    existing table — rebuild it, preserving ids (ride_tags references them).

    Commits right after finishing, and recovers a leftover `tags_old` from an
    interrupted previous run (see _migrate_users_table's docstring)."""
    if _table_exists(conn, "tags_old"):
        _finish_tags_migration(conn)
        conn.commit()
        return
    if not _table_exists(conn, "tags"):
        conn.execute("""
            CREATE TABLE tags (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT REFERENCES users(id),
              name TEXT NOT NULL,
              UNIQUE(user_id, name)
            )
        """)
        conn.commit()
        return
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tags)")}
    if "user_id" in cols:
        return
    conn.execute("ALTER TABLE tags RENAME TO tags_old")
    _finish_tags_migration(conn)
    conn.commit()


def _finish_tags_migration(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tags (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT REFERENCES users(id),
          name TEXT NOT NULL,
          UNIQUE(user_id, name)
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO tags (id, user_id, name) SELECT id, NULL, name FROM tags_old"
    )
    conn.execute("DROP TABLE tags_old")


def _migrate_sync_state_table(conn: sqlite3.Connection) -> None:
    """sync_state used to be a single global key/value row; multi-tenancy
    needs it keyed per user."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_state'"
    ).fetchone()
    if not exists:
        return
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(sync_state)")}
    if "user_id" in cols:
        return
    conn.execute("ALTER TABLE sync_state RENAME TO sync_state_old")
    conn.execute("""
        CREATE TABLE sync_state (
          user_id TEXT REFERENCES users(id),
          key TEXT NOT NULL,
          value TEXT,
          PRIMARY KEY (user_id, key)
        )
    """)
    # Old rows have no user — leave them out; claim_orphaned_data backfills
    # everything else, but a stale global sync cursor isn't worth keeping.
    conn.execute("DROP TABLE sync_state_old")
    conn.commit()


def claim_orphaned_data(conn: sqlite3.Connection, user_id: str) -> None:
    """One-time migration helper: if this is the very first account ever
    created on this database (the pre-multi-tenant → multi-tenant
    transition), attribute all not-yet-owned rows to it. Never runs once a
    second account exists, so it can't accidentally hand one user's data to
    another."""
    user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if user_count != 1:
        return
    conn.execute("UPDATE rides SET user_id = ? WHERE user_id IS NULL", (user_id,))
    conn.execute("UPDATE roadtrips SET user_id = ? WHERE user_id IS NULL", (user_id,))
    conn.execute("UPDATE tags SET user_id = ? WHERE user_id IS NULL", (user_id,))
    conn.commit()


def upsert_user(conn: sqlite3.Connection, liberty_rider_id: str, first_name: str | None, email: str | None) -> None:
    conn.execute(
        """
        INSERT INTO users (id, first_name, email, created_at) VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            first_name = COALESCE(excluded.first_name, users.first_name),
            email = COALESCE(excluded.email, users.email)
        """,
        (liberty_rider_id, first_name, email),
    )
    conn.commit()


def save_user_tokens(conn: sqlite3.Connection, user_id: str, bearer_token: str, refresh_token: str | None, api_key: str | None) -> None:
    conn.execute(
        "UPDATE users SET bearer_token = ?, refresh_token = COALESCE(?, refresh_token), "
        "firebase_api_key = COALESCE(?, firebase_api_key) WHERE id = ?",
        (bearer_token, refresh_token, api_key, user_id),
    )
    conn.commit()


def create_session(conn: sqlite3.Connection, user_id: str) -> str:
    session_id = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at) VALUES (?, ?, datetime('now'))",
        (session_id, user_id),
    )
    conn.commit()
    return session_id


def get_session_user(conn: sqlite3.Connection, session_id: str):
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def upsert_ride(conn: sqlite3.Connection, user_id: str, ride: dict) -> None:
    start = ride.get("startLocation") or {}
    stop = ride.get("stopLocation") or {}
    vehicle = ride.get("vehicle") or {}
    roadbook = ride.get("createdRoadbook") or {}

    existing = conn.execute(
        "SELECT roadtrip_id, merged_into FROM rides WHERE id = ?", (ride["id"],)
    ).fetchone()
    keep_roadtrip_id = existing["roadtrip_id"] if existing else None
    keep_merged_into = existing["merged_into"] if existing else None

    conn.execute(
        """
        INSERT INTO rides (
            id, user_id, name, start_time, distance, duration, duration_without_pauses,
            total_pauses_duration, pause_count, maximum_altitude, is_favorite,
            hidden, state, detailed_polyline, preview_picture_url, gpx_export_url,
            start_lat, start_lon, stop_lat, stop_lon,
            vehicle_brand, vehicle_model, vehicle_type, vehicle_cc,
            created_roadbook_id, roadtrip_id, merged_into, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, start_time=excluded.start_time, distance=excluded.distance,
            duration=excluded.duration, duration_without_pauses=excluded.duration_without_pauses,
            total_pauses_duration=excluded.total_pauses_duration, pause_count=excluded.pause_count,
            maximum_altitude=excluded.maximum_altitude, is_favorite=excluded.is_favorite,
            hidden=excluded.hidden, state=excluded.state, detailed_polyline=excluded.detailed_polyline,
            preview_picture_url=excluded.preview_picture_url,
            gpx_export_url=excluded.gpx_export_url,
            start_lat=excluded.start_lat, start_lon=excluded.start_lon,
            stop_lat=excluded.stop_lat, stop_lon=excluded.stop_lon,
            vehicle_brand=excluded.vehicle_brand, vehicle_model=excluded.vehicle_model,
            vehicle_type=excluded.vehicle_type, vehicle_cc=excluded.vehicle_cc,
            created_roadbook_id=excluded.created_roadbook_id, raw_json=excluded.raw_json
        """,
        (
            ride["id"], user_id, ride.get("name"), ride["startTime"], ride.get("distance"),
            ride.get("duration"), ride.get("durationWithoutPauses"),
            ride.get("totalPausesDuration"), ride.get("pauseCount"),
            ride.get("maximumAltitude"), int(bool(ride.get("isFavorite"))),
            int(bool(ride.get("hidden"))), ride.get("state"),
            ride.get("detailedPolyline"), ride.get("previewPictureUrl"), ride.get("gpxExportUrl"),
            start.get("latitude"), start.get("longitude"),
            stop.get("latitude"), stop.get("longitude"),
            vehicle.get("brandName"), vehicle.get("modelName"),
            vehicle.get("vehicleType"), vehicle.get("engineDisplacementCc"),
            roadbook.get("id"), keep_roadtrip_id, keep_merged_into,
            json.dumps(ride, ensure_ascii=False),
        ),
    )

    conn.execute("DELETE FROM pauses WHERE ride_id = ?", (ride["id"],))
    for p in ride.get("pauses") or []:
        loc = p.get("lastLocation") or {}
        conn.execute(
            "INSERT INTO pauses (ride_id, estimated_time, automatic, lat, lon) VALUES (?, ?, ?, ?, ?)",
            (ride["id"], p.get("estimatedTime"), int(bool(p.get("automatic"))),
             loc.get("latitude"), loc.get("longitude")),
        )


def get_sync_state(conn: sqlite3.Connection, user_id: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()
    return row["value"] if row else None


def set_sync_state(conn: sqlite3.Connection, user_id: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
        (user_id, key, value),
    )
