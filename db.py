"""SQLite storage for personal Liberty Rider rides + manual roadtrip grouping."""
from __future__ import annotations

import json
import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "data" / "rides.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rides (
  id TEXT PRIMARY KEY,
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
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pauses (
  ride_id TEXT REFERENCES rides(id),
  estimated_time TEXT,
  automatic INTEGER,
  lat REAL, lon REAL
);

CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
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
"""

# Indexes on columns added via migration (below) — created after the ALTER
# TABLE runs, since the column may not exist yet on a pre-existing database.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_rides_merged_into ON rides(merged_into);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(rides)")}
        if "preview_picture_url" not in existing_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN preview_picture_url TEXT")
        if "merged_into" not in existing_cols:
            conn.execute("ALTER TABLE rides ADD COLUMN merged_into TEXT REFERENCES rides(id)")
        conn.executescript(POST_MIGRATION_INDEXES)
        conn.commit()
    finally:
        conn.close()


def upsert_ride(conn: sqlite3.Connection, ride: dict) -> None:
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
            id, name, start_time, distance, duration, duration_without_pauses,
            total_pauses_duration, pause_count, maximum_altitude, is_favorite,
            hidden, state, detailed_polyline, preview_picture_url, gpx_export_url,
            start_lat, start_lon, stop_lat, stop_lon,
            vehicle_brand, vehicle_model, vehicle_type, vehicle_cc,
            created_roadbook_id, roadtrip_id, merged_into, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ride["id"], ride.get("name"), ride["startTime"], ride.get("distance"),
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


def get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
