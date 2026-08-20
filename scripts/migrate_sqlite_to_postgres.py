"""One-off: copy one account's data from a local SQLite file into the
Postgres backend (Supabase or any DATABASE_URL target).

Usage:
    DATABASE_URL=postgres://... .venv/bin/python3 scripts/migrate_sqlite_to_postgres.py \
        --sqlite-path data/rides.db --user-id <liberty-rider-user-id>

Safe to re-run: refuses to proceed if the target already has rides for
that user_id, unless --force is passed (which wipes that user's rows in
the target first via db.purge_user_data before copying).

Not migrated: `sessions` (ephemeral, meaningless across a DB swap) and the
`schema_migrations` bookkeeping table (target manages its own).
"""
# ruff: noqa: S608 — every statement below interpolates column *names* read
# from the source schema plus a count of `?` placeholders; values are always
# bound. Same rule as the app (see CONTRIBUTING.md).
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import db


def _rows(conn: sqlite3.Connection, query: str, params=()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def migrate(sqlite_path: str, user_id: str, force: bool) -> None:
    if not db.IS_POSTGRES:
        raise SystemExit("DATABASE_URL must be set — that's the migration target.")

    src = sqlite3.connect(sqlite_path)
    dst = db.PGConnection(db.DATABASE_URL)
    db._run_postgres_migrations(dst)

    existing = dst.execute("SELECT COUNT(*) AS n FROM rides WHERE user_id = ?", (user_id,)).fetchone()["n"]
    if existing:
        if not force:
            raise SystemExit(
                f"Target already has {existing} ride(s) for user_id={user_id!r}. "
                "Pass --force to wipe and re-migrate."
            )
        print(f"--force: purging {existing} existing ride(s) for this user in the target first…")
        db.purge_user_data(dst, user_id)

    user_rows = _rows(src, "SELECT * FROM users WHERE id = ?", (user_id,))
    if not user_rows:
        raise SystemExit(f"No user_id={user_id!r} found in {sqlite_path}.")
    u = user_rows[0]
    dst.execute(
        """
        INSERT INTO users (id, first_name, email, bearer_token, refresh_token, firebase_api_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            first_name=excluded.first_name, email=excluded.email, created_at=excluded.created_at
        """,
        (u["id"], u["first_name"], u["email"], u["bearer_token"], u["refresh_token"], u["firebase_api_key"], u["created_at"]),
    )
    print("users: 1")

    roadtrips = _rows(src, "SELECT * FROM roadtrips WHERE user_id = ?", (user_id,))
    for r in roadtrips:
        dst.execute(
            "INSERT INTO roadtrips (id, user_id, name) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (r["id"], r["user_id"], r["name"]),
        )
    if roadtrips:
        dst.execute("SELECT setval('roadtrips_id_seq', (SELECT MAX(id) FROM roadtrips))")
    print(f"roadtrips: {len(roadtrips)}")

    tags = _rows(src, "SELECT * FROM tags WHERE user_id = ?", (user_id,))
    for t in tags:
        dst.execute(
            "INSERT INTO tags (id, user_id, name) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (t["id"], t["user_id"], t["name"]),
        )
    if tags:
        dst.execute("SELECT setval('tags_id_seq', (SELECT MAX(id) FROM tags))")
    print(f"tags: {len(tags)}")

    rides = _rows(src, "SELECT * FROM rides WHERE user_id = ?", (user_id,))
    ride_cols = [
        "id", "user_id", "name", "start_time", "distance", "duration", "duration_without_pauses",
        "total_pauses_duration", "pause_count", "maximum_altitude", "is_favorite", "hidden", "state",
        "detailed_polyline", "preview_picture_url", "gpx_export_url", "gpx_local_path",
        "start_lat", "start_lon", "stop_lat", "stop_lon",
        "vehicle_brand", "vehicle_model", "vehicle_type", "vehicle_cc",
        "created_roadbook_id", "notes", "raw_json",
    ]
    placeholders = ", ".join("?" * len(ride_cols))
    for r in rides:
        # roadtrip_id/merged_into deliberately left NULL here — both can
        # reference a ride not yet inserted in this same batch (merged_into
        # is self-referential; a ride can belong to a roadtrip inserted
        # just above, but simplest to always defer both to a second pass
        # once every ride row exists).
        dst.execute(
            f"""
            INSERT INTO rides ({', '.join(ride_cols)}) VALUES ({placeholders})
            ON CONFLICT(id) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in ride_cols if c != 'id')}
            """,
            tuple(r[c] for c in ride_cols),
        )
    for r in rides:
        if r["roadtrip_id"] is not None or r["merged_into"] is not None:
            dst.execute(
                "UPDATE rides SET roadtrip_id = ?, merged_into = ? WHERE id = ?",
                (r["roadtrip_id"], r["merged_into"], r["id"]),
            )
    print(f"rides: {len(rides)}")

    def _copy_by_ride(table: str, cols: list[str]) -> int:
        rows = _rows(
            src,
            f"SELECT {table}.* FROM {table} JOIN rides ON rides.id = {table}.ride_id WHERE rides.user_id = ?",
            (user_id,),
        )
        ph = ", ".join("?" * len(cols))
        for row in rows:
            dst.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})", tuple(row[c] for c in cols))
        return len(rows)

    n = _copy_by_ride("pauses", ["ride_id", "estimated_time", "automatic", "lat", "lon"])
    print(f"pauses: {n}")
    n = _copy_by_ride("resumes", ["ride_id", "estimated_time", "automatic", "lat", "lon"])
    print(f"resumes: {n}")
    n = _copy_by_ride("ride_cols", ["ride_id", "name"])
    print(f"ride_cols: {n}")

    ride_tags = _rows(
        src,
        "SELECT ride_tags.* FROM ride_tags JOIN rides ON rides.id = ride_tags.ride_id WHERE rides.user_id = ?",
        (user_id,),
    )
    for rt in ride_tags:
        dst.execute(
            "INSERT INTO ride_tags (ride_id, tag_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (rt["ride_id"], rt["tag_id"]),
        )
    print(f"ride_tags: {len(ride_tags)}")

    sync_state = _rows(src, "SELECT * FROM sync_state WHERE user_id = ?", (user_id,))
    for s in sync_state:
        dst.execute(
            "INSERT INTO sync_state (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (s["user_id"], s["key"], s["value"]),
        )
    print(f"sync_state: {len(sync_state)}")

    elevation = _rows(src, "SELECT * FROM elevation_cache")
    for e in elevation:
        dst.execute(
            "INSERT INTO elevation_cache (lat, lon, elevation) VALUES (?, ?, ?) "
            "ON CONFLICT(lat, lon) DO NOTHING",
            (e["lat"], e["lon"], e["elevation"]),
        )
    print(f"elevation_cache: {len(elevation)}")

    mountain_pass = _rows(src, "SELECT * FROM mountain_pass_cache")
    for m in mountain_pass:
        dst.execute(
            "INSERT INTO mountain_pass_cache (lat, lon, name) VALUES (?, ?, ?) "
            "ON CONFLICT(lat, lon) DO NOTHING",
            (m["lat"], m["lon"], m["name"]),
        )
    print(f"mountain_pass_cache: {len(mountain_pass)}")

    dst.commit()
    src.close()
    dst.close()
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", default="data/rides.db")
    parser.add_argument("--user-id", required=True, help="Liberty Rider currentUser.id to migrate")
    parser.add_argument("--force", action="store_true", help="wipe existing target data for this user first")
    args = parser.parse_args()
    migrate(args.sqlite_path, args.user_id, args.force)
