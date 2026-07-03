"""Sync personal Liberty Rider rides ("Mes trajets") into local SQLite.

Pagination note — verified empirically against the real API (2026-07-03),
the docs' assumption was wrong:
  - `after` is silently ignored by the server (confirmed: identical results
    with/without it).
  - Without `before`, stoppedRides returns the MOST RECENT `first` rides
    (capped at 50 server-side regardless of the requested `first`), ascending
    within that page.
  - With `before` set, it returns the OLDEST `first` rides with
    startTime < before (also ascending), starting from the very beginning of
    history — not from wherever `before` falls chronologically.
So to walk the full history we start with an unbounded call (grabs the most
recent page), then repeatedly set `before` to the oldest startTime seen so
far to walk further back, until a page comes back empty or short.
"""
from __future__ import annotations

import db
from liberty_client import LibertyRiderClient

PAGE_SIZE = 50
LAST_SYNC_KEY = "last_sync_max_start_time"


def sync(token: str, full: bool = False, page_size: int = PAGE_SIZE) -> dict:
    client = LibertyRiderClient(token)
    conn = db.connect()
    try:
        last_sync_max = None if full else db.get_sync_state(conn, LAST_SYNC_KEY)

        before = None
        newest_seen = None
        fetched = 0
        upserted = 0

        while True:
            result = client.get_stopped_rides(first=page_size, before=before)
            rides = result["stoppedRides"]
            if not rides:
                break
            if newest_seen is None:
                newest_seen = rides[-1]["startTime"]

            stop_paging = False
            # Each page is ascending; walk it oldest-first so an incremental
            # sync can stop the instant it reaches already-synced data.
            for ride in rides:
                fetched += 1
                if last_sync_max and ride["startTime"] <= last_sync_max:
                    continue
                db.upsert_ride(conn, ride)
                upserted += 1
            conn.commit()

            oldest_in_page = rides[0]["startTime"]
            if last_sync_max and oldest_in_page <= last_sync_max:
                stop_paging = True

            if stop_paging or len(rides) < page_size:
                break
            before = oldest_in_page

        if newest_seen and (last_sync_max is None or newest_seen > last_sync_max):
            db.set_sync_state(conn, LAST_SYNC_KEY, newest_seen)
            conn.commit()

        total_rides = conn.execute("SELECT COUNT(*) AS n FROM rides").fetchone()["n"]
        return {"fetched": fetched, "upserted": upserted, "total_rides": total_rides}
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Liberty Rider personal rides")
    parser.add_argument("--token", required=True, help="Firebase Bearer token")
    parser.add_argument("--full", action="store_true", help="Full re-sync of all rides")
    args = parser.parse_args()

    db.init_db()
    summary = sync(args.token, full=args.full)
    print(summary)
