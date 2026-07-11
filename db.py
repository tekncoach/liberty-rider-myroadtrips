"""Storage for personal Liberty Rider rides + manual roadtrip grouping.

Multi-tenant: every ride/roadtrip/tag belongs to a `users` row, keyed
directly by the user's own Liberty Rider id (`currentUser.id` — no separate
internal id is minted). Every request is scoped to the logged-in session's
user — see the `get_session_user` dependency in app.py.

Two backends, picked by the presence of a `DATABASE_URL` env var:
- Unset (local dev, tests): a single-file SQLite database at `DB_PATH`. Runs
  the historical ALTER-TABLE/RENAME migrations below, since this file may
  have been created by an earlier version of the schema.
- Set (hosted deployments): Postgres via `psycopg`. Schema comes from the
  plain-SQL files in `migrations/`, applied in filename order and tracked
  in a `schema_migrations` table — see `_run_postgres_migrations` below.
  Add a new `NNNN_description.sql` file for future schema changes; nothing
  here replays SQLite's own ALTER-TABLE history (see `docs/ARCHITECTURE.md`).

Application code (`app.py`, `sync.py`, `elevation.py`, `mountain_pass.py`)
is written once against both: SQL uses `?` placeholders and `conn.execute(
...).fetchone()/.fetchall()` throughout, and `PGConnection` below translates
that onto psycopg for the Postgres case. Anything backend-specific (PRAGMA,
sqlite_master, AUTOINCREMENT, the ALTER-TABLE migrations) stays confined to
this module.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import sqlite3
from datetime import datetime, timezone

DB_PATH = pathlib.Path(__file__).parent / "data" / "rides.db"
MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # Every request opened (and closed) its own connection — fine on
    # SQLite, expensive on Postgres behind a pooler (Supabase's Supavisor,
    # e.g.), where each fresh connection pays a full TCP+TLS+auth
    # handshake through an extra network hop. A small persistent pool
    # amortizes that cost across requests instead of paying it every time.
    _POOL = ConnectionPool(DATABASE_URL, kwargs={"row_factory": dict_row}, min_size=1, max_size=5)


def _now() -> str:
    """A portable stand-in for SQLite's `datetime('now')` — Postgres has no
    such function, so the timestamp is computed here and bound as a plain
    parameter instead of relying on server-side SQL."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

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
  notes TEXT,
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

CREATE TABLE IF NOT EXISTS resumes (
  ride_id TEXT REFERENCES rides(id),
  estimated_time TEXT,
  automatic INTEGER,
  lat REAL, lon REAL
);

-- Liberty Rider has no altitude-per-point data anywhere (verified via
-- GraphQL introspection and its own GPX export — only a single
-- maximumAltitude scalar per ride exists). Elevation is instead estimated
-- via the open-elevation API and cached here forever, keyed by lat/lon
-- rounded to ~11m — open-elevation is free but slow/rate-limited, and
-- this is the one feature in the app with a real (if currently free)
-- external cost, so it's a candidate to gate behind a premium tier later.
CREATE TABLE IF NOT EXISTS elevation_cache (
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  elevation REAL NOT NULL,
  PRIMARY KEY (lat, lon)
);

-- Named mountain passes near a detected elevation peak (see
-- mountain_pass.py), looked up via OpenStreetMap's Overpass API. `name` is
-- NULL when no named pass was found nearby — that's cached too, so an
-- unnamed peak isn't re-queried on every ride reopen.
CREATE TABLE IF NOT EXISTS mountain_pass_cache (
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  name TEXT,
  PRIMARY KEY (lat, lon)
);

-- Named cols/summits found along a given ride (see api_ride_cols), so they
-- become searchable — filled in progressively, the first time each ride's
-- detail is opened, not backfilled for the whole history at once (an
-- eager backfill would mean hundreds of Overpass calls against a free,
-- rate-limited public API).
CREATE TABLE IF NOT EXISTS ride_cols (
  ride_id TEXT REFERENCES rides(id),
  name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ride_cols_ride ON ride_cols(ride_id);

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
CREATE INDEX IF NOT EXISTS idx_resumes_ride ON resumes(ride_id);
CREATE INDEX IF NOT EXISTS idx_ride_tags_tag ON ride_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

# Indexes on columns added via migration (below) — created after the ALTER
# TABLE runs, since the column may not exist yet on a pre-existing database.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_rides_merged_into ON rides(merged_into);
CREATE INDEX IF NOT EXISTS idx_rides_user ON rides(user_id);
"""

# Postgres schema lives in migrations/*.sql, applied by _run_postgres_migrations().


class _PGCursor:
    """Wraps a psycopg cursor so call sites written for sqlite3 (`conn.execute(
    sql, params).fetchone()`, bare iteration, `?` placeholders) work unchanged."""

    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self):
        return self._cursor.rowcount


class PGConnection:
    """A `sqlite3.Connection`-shaped wrapper around a psycopg connection —
    see the module docstring for why this is the only Postgres-specific
    piece application code needs to know about.

    Pass `url` for a plain one-off connection (used by one-off scripts,
    e.g. `scripts/migrate_sqlite_to_postgres.py`) that `close()`s for
    real. `connect()` below instead pulls an already-open connection from
    `_POOL` and returns it there on `close()`."""

    def __init__(self, url: str | None = None, _pooled_conn=None):
        if _pooled_conn is not None:
            self._conn = _pooled_conn
            self._pooled = True
        else:
            self._conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
            self._pooled = False

    @staticmethod
    def _translate(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()) -> _PGCursor:
        cur = self._conn.cursor()
        cur.execute(self._translate(sql), params)
        return _PGCursor(cur)

    def executemany(self, sql: str, seq_of_params) -> None:
        cur = self._conn.cursor()
        cur.executemany(self._translate(sql), list(seq_of_params))

    def executescript(self, sql: str) -> None:
        # psycopg (unlike sqlite3) executes a semicolon-separated batch fine
        # via a plain .execute() — no separate "script" API needed.
        self._conn.cursor().execute(sql)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        if self._pooled:
            # Rolls back anything left uncommitted before returning the
            # connection to the pool, so a caller that forgot to commit
            # (or errored before committing) never leaks a dangling
            # transaction into the next request that reuses it.
            self._conn.rollback()
            _POOL.putconn(self._conn)
        else:
            self._conn.close()


# What `connect()` actually returns, for type hints on every function below —
# never `sqlite3.Connection` alone, since it's a `PGConnection` whenever
# `DATABASE_URL` is set (see the module docstring).
DBConnection = sqlite3.Connection | PGConnection


def connect() -> DBConnection:
    if IS_POSTGRES:
        return PGConnection(_pooled_conn=_POOL.getconn())
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
        if IS_POSTGRES:
            _run_postgres_migrations(conn)
            return

        conn.executescript(SCHEMA)

        _migrate_users_table(conn)

        rides_cols = {row["name"] for row in conn.execute("PRAGMA table_info(rides)")}
        if "preview_picture_url" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN preview_picture_url TEXT")
        if "merged_into" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN merged_into TEXT REFERENCES rides(id)")
        if "user_id" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN user_id TEXT REFERENCES users(id)")
        if "notes" not in rides_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN notes TEXT")

        roadtrips_cols = {row["name"] for row in conn.execute("PRAGMA table_info(roadtrips)")}
        if "user_id" not in roadtrips_cols:
            conn.execute("ALTER TABLE roadtrips ADD COLUMN user_id TEXT REFERENCES users(id)")

        _migrate_tags_table(conn)
        _migrate_sync_state_table(conn)

        conn.executescript(POST_MIGRATION_INDEXES)
        conn.commit()

        _backfill_resumes_from_raw_json(conn)
    finally:
        conn.close()


def _run_postgres_migrations(conn: DBConnection) -> None:
    """Applies every `migrations/*.sql` file not yet recorded in
    `schema_migrations`, in filename order (hence the `NNNN_` prefix — sort
    order is the apply order). Add a new numbered file for future schema
    changes; each one only ever runs once per database, tracked here."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
    """)
    # Not covered by any migrations/*.sql file (it's created here, before
    # any of them run) — deny-all by default, same as every other table.
    conn.execute("ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY")
    conn.commit()
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (path.name, _now()),
        )
        conn.commit()


def _table_exists(conn: DBConnection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _migrate_users_table(conn: DBConnection) -> None:
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


def _finish_users_migration(conn: DBConnection) -> None:
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


def _migrate_tags_table(conn: DBConnection) -> None:
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


def _finish_tags_migration(conn: DBConnection) -> None:
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


def _migrate_sync_state_table(conn: DBConnection) -> None:
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


def _backfill_resumes_from_raw_json(conn: DBConnection) -> None:
    """One-time migration: the `resumes` table is new, but every ride ever
    synced already carries its raw GraphQL response (including `resumes`,
    which the query has always requested but the app never stored) in
    `raw_json` — so this backfills from data already on disk, no resync
    needed. Guarded on an empty `resumes` table so it only runs once."""
    if conn.execute("SELECT 1 FROM resumes LIMIT 1").fetchone():
        return
    rows = conn.execute(
        "SELECT id, raw_json FROM rides WHERE raw_json IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            continue
        for p in raw.get("resumes") or []:
            loc = p.get("lastLocation") or {}
            conn.execute(
                "INSERT INTO resumes (ride_id, estimated_time, automatic, lat, lon) VALUES (?, ?, ?, ?, ?)",
                (row["id"], p.get("estimatedTime"), int(bool(p.get("automatic"))),
                 loc.get("latitude"), loc.get("longitude")),
            )
    conn.commit()


def claim_orphaned_data(conn: DBConnection, user_id: str) -> None:
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


def purge_user_data(conn: DBConnection, user_id: str) -> None:
    """Wipes everything synced/organized for this account (rides, roadtrips,
    tags, sync cursor) so the next sync starts from a genuinely empty state —
    lets one Liberty Rider account be used to re-test onboarding repeatedly.
    Leaves the `users` row and current session alone (still logged in), and
    leaves the global elevation/mountain-pass caches alone (not user data —
    just lat/lon lookups, harmless and expensive to redo)."""
    conn.execute(
        "DELETE FROM pauses WHERE ride_id IN (SELECT id FROM rides WHERE user_id = ?)", (user_id,)
    )
    conn.execute(
        "DELETE FROM resumes WHERE ride_id IN (SELECT id FROM rides WHERE user_id = ?)", (user_id,)
    )
    conn.execute(
        "DELETE FROM ride_cols WHERE ride_id IN (SELECT id FROM rides WHERE user_id = ?)", (user_id,)
    )
    conn.execute(
        "DELETE FROM ride_tags WHERE ride_id IN (SELECT id FROM rides WHERE user_id = ?)", (user_id,)
    )
    conn.execute("DELETE FROM rides WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM roadtrips WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM tags WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM sync_state WHERE user_id = ?", (user_id,))
    conn.commit()


def upsert_user(conn: DBConnection, liberty_rider_id: str, first_name: str | None, email: str | None) -> None:
    conn.execute(
        """
        INSERT INTO users (id, first_name, email, created_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            first_name = COALESCE(excluded.first_name, users.first_name),
            email = COALESCE(excluded.email, users.email)
        """,
        (liberty_rider_id, first_name, email, _now()),
    )
    conn.commit()


def save_user_tokens(conn: DBConnection, user_id: str, bearer_token: str, refresh_token: str | None, api_key: str | None) -> None:
    conn.execute(
        "UPDATE users SET bearer_token = ?, refresh_token = COALESCE(?, refresh_token), "
        "firebase_api_key = COALESCE(?, firebase_api_key) WHERE id = ?",
        (bearer_token, refresh_token, api_key, user_id),
    )
    conn.commit()


def create_session(conn: DBConnection, user_id: str) -> str:
    session_id = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (id, user_id, created_at) VALUES (?, ?, ?)",
        (session_id, user_id, _now()),
    )
    conn.commit()
    return session_id


def get_session_user(conn: DBConnection, session_id: str):
    return conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (session_id,),
    ).fetchone()


def delete_session(conn: DBConnection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def upsert_ride(conn: DBConnection, user_id: str, ride: dict) -> None:
    start = ride.get("startLocation") or {}
    stop = ride.get("stopLocation") or {}
    vehicle = ride.get("vehicle") or {}
    roadbook = ride.get("createdRoadbook") or {}

    existing = conn.execute(
        "SELECT roadtrip_id, merged_into, notes FROM rides WHERE id = ?", (ride["id"],)
    ).fetchone()
    keep_roadtrip_id = existing["roadtrip_id"] if existing else None
    keep_merged_into = existing["merged_into"] if existing else None
    keep_notes = existing["notes"] if existing else None

    conn.execute(
        """
        INSERT INTO rides (
            id, user_id, name, start_time, distance, duration, duration_without_pauses,
            total_pauses_duration, pause_count, maximum_altitude, is_favorite,
            hidden, state, detailed_polyline, preview_picture_url, gpx_export_url,
            start_lat, start_lon, stop_lat, stop_lon,
            vehicle_brand, vehicle_model, vehicle_type, vehicle_cc,
            created_roadbook_id, roadtrip_id, merged_into, notes, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            roadbook.get("id"), keep_roadtrip_id, keep_merged_into, keep_notes,
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

    conn.execute("DELETE FROM resumes WHERE ride_id = ?", (ride["id"],))
    for p in ride.get("resumes") or []:
        loc = p.get("lastLocation") or {}
        conn.execute(
            "INSERT INTO resumes (ride_id, estimated_time, automatic, lat, lon) VALUES (?, ?, ?, ?, ?)",
            (ride["id"], p.get("estimatedTime"), int(bool(p.get("automatic"))),
             loc.get("latitude"), loc.get("longitude")),
        )


def get_sync_state(conn: DBConnection, user_id: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE user_id = ? AND key = ?", (user_id, key)
    ).fetchone()
    return row["value"] if row else None


def set_sync_state(conn: DBConnection, user_id: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
        (user_id, key, value),
    )
