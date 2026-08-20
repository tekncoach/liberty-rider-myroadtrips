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

PAGE_SIZE = 50
LAST_SYNC_KEY = "last_sync_max_start_time"


def pending_status(client, user_id: str) -> dict:
    """Is there anything left to import from Liberty Rider for this account?

    Answering that needs Liberty Rider itself: the local cursor
    (`last_sync_max_start_time`) only ever says "nothing new *as of the last
    sync I ran*", which is not the same claim. So this asks the API for its
    single newest ride (one tiny query — id + startTime only) and compares
    that startTime to the newest ride we have on file. Both sides are
    ISO-8601 UTC strings straight from the same API, so they compare
    lexicographically, exactly like `sync()` compares them while paging.
    """
    conn = db.connect()
    try:
        cursor = db.get_sync_state(conn, user_id, LAST_SYNC_KEY)
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(start_time) AS newest FROM rides WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        local_rides, local_newest = row["n"], row["newest"]
    finally:
        conn.close()

    # `start_time` is stored verbatim from the API, so the newest ride on
    # file is directly comparable — and it keeps the answer right even when
    # the cursor is missing (data claimed by `claim_orphaned_data`, a
    # half-finished first sync…), where the cursor alone would say "never
    # synced" and cry wolf forever.
    local_latest = max((x for x in (cursor, local_newest) if x), default=None)

    latest = client.get_latest_ride()
    remote_latest = latest["startTime"] if latest else None

    if remote_latest is None:
        pending = False  # nothing on the remote side at all
    elif local_latest is None:
        pending = True  # never synced, and there is something to fetch
    else:
        pending = remote_latest > local_latest

    return {
        "pending": pending,
        "remote_latest_start_time": remote_latest,
        "last_sync_start_time": local_latest,
        "local_rides": local_rides,
    }


def sync(client, user_id: str, full: bool = False, page_size: int = PAGE_SIZE) -> dict:
    """`client` is an already-authenticated LibertyRiderClient (app.py
    resolves/refreshes the token before calling this); `user_id` scopes
    what gets upserted and where the sync cursor is stored."""
    conn = db.connect()
    try:
        last_sync_max = None if full else db.get_sync_state(conn, user_id, LAST_SYNC_KEY)

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
                db.upsert_ride(conn, user_id, ride)
                upserted += 1
            conn.commit()

            oldest_in_page = rides[0]["startTime"]
            if last_sync_max and oldest_in_page <= last_sync_max:
                stop_paging = True

            if stop_paging or len(rides) < page_size:
                break
            before = oldest_in_page

        if newest_seen and (last_sync_max is None or newest_seen > last_sync_max):
            db.set_sync_state(conn, user_id, LAST_SYNC_KEY, newest_seen)
            conn.commit()

        total_rides = conn.execute(
            "SELECT COUNT(*) AS n FROM rides WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
        return {"fetched": fetched, "upserted": upserted, "total_rides": total_rides}
    finally:
        conn.close()
