# Architecture

A small, dependency-light stack: no ORM, no build step, no framework on the
frontend.

```
liberty_client.py  →  sync.py  →  db.py (SQLite)  →  app.py (FastAPI)  →  static/ (vanilla JS + Leaflet)
```

- **`liberty_client.py`** — minimal GraphQL client for Liberty Rider's
  (undocumented) API. One query (`MyRides`) plus a GPX-download helper.
- **`sync.py`** — walks the API's pagination to pull all-or-new rides and
  upserts them into SQLite.
- **`db.py`** — schema + connection helper + `upsert_ride`. Raw `sqlite3`,
  no ORM.
- **`app.py`** — FastAPI app exposing the HTTP API (see `API.md`) and serving
  `static/`.
- **`static/index.html` + `static/app.js`** — single-page app, no build step.
  A single global `state` object is the source of truth; each tab
  (Roadtrips / Tags / Mes traces) has its own render function that rebuilds
  its DOM subtree from `state` on every change. Leaflet renders the map.

## Data model (`db.py`)

- **`rides`** — one row per Liberty Rider "stopped ride", upserted verbatim
  from the API (`raw_json` keeps the full original payload). Two columns
  encode *our* organization on top of that raw data:
  - `roadtrip_id` — nullable FK to `roadtrips`. Set by the user grouping
    rides into a named, multi-day trip.
  - `merged_into` — nullable, self-referential FK to `rides.id`. See
    **Merge** below.
- **`roadtrips`** — just `id` + `name`. All aggregation (distance, duration,
  day span, day-by-day breakdown) is computed on read from its member rides,
  never stored.
- **`pauses`** — one row per recorded pause on a ride (`ride_id`,
  `estimated_time`, `automatic`, `lat`/`lon`). Replaced wholesale on every
  sync of that ride (delete + reinsert), since Liberty Rider doesn't give
  pauses stable ids.
- **`tags`** / **`ride_tags`** — a plain many-to-many. Tags are get-or-create
  by name (`api_attach_tag`) and carry no date semantics — a tag can span
  rides from any year, which is the point (e.g. a "Paris" tag collecting
  every ride that passed through the city, 2022 and 2026 alike).
- **`sync_state`** — a single key (`last_sync_max_start_time`) tracking the
  newest `startTime` synced so far, for incremental syncs.

## Merge: the non-destructive part

Liberty Rider sometimes stops tracking mid-ride and starts a new one a few
minutes later at the same spot (a phone sleep/wake glitch, usually). The
merge feature lets the user glue two (or more) such rides back into one
*without ever mutating the underlying raw rows*.

Mechanically: given rides A (earlier) and B (later), merging sets
`B.merged_into = A.id`. A becomes the **representative**; B becomes an
**absorbed member**. That's the entire on-disk effect — no other column
changes, and `raw_json` is never touched.

Every read path re-derives the merged view on the fly:

- `_merge_group(conn, row)` — given a row, returns `[row]` if it isn't
  merged with anything, or the full chronological group (representative +
  members) otherwise.
- `_merged_ride_dict(conn, row)` — the user-facing dict for a ride. For a
  multi-row group it recomputes: `name` (`"first → last"`), `start_time`
  (the earliest), `distance`/`pause_count` (summed), `maximum_altitude`
  (max), start/stop location (first's start, last's stop), and critically
  **`duration`** — computed as the full wall-clock span from the first
  ride's start to the last ride's calculated end, *not* the sum of the
  individual `duration` fields. That span minus total riding time
  (`duration_without_pauses`, summed) becomes `total_pauses_duration` — so
  the gap between the two Liberty-Rider-tracked segments is correctly
  counted as a pause, not silently dropped. `pause_count` adds one implicit
  pause per group boundary for the same reason.
- `_merged_polyline_and_pauses(conn, row)` — concatenates each member's
  decoded polyline in chronological order, and unions their recorded pauses,
  for map rendering and GPX export.

Listing endpoints (`GET /api/rides`, roadtrip/tag detail) only ever return
representative rows — absorbed members are filtered out at the SQL level
(`WHERE merged_into IS NULL`) so they never appear as duplicates.

**Un-merging** (`DELETE /api/rides/{id}/merge`) just clears `merged_into` on
every member of that representative's group — lossless, since nothing else
was ever changed.

**Merge validation** (`POST /api/rides/merge`) rejects: rides already part of
another merge (in either direction), tagged rides (ambiguous which ride's
tags should "win"), and rides belonging to two *different* roadtrips. Rides
belonging to the *same* roadtrip (or where only one side has one) are
allowed — the representative inherits that common roadtrip.

## Grouping heuristics

Two distinct adjacency checks, both computed from the gap between one ride's
calculated end and the next ride's start, plus the distance between the
first's stop location and the second's start location:

- **`_is_adjacent`** (loose) — same calendar day, OR less than 24h apart and
  under 5 km — used to flag "these two could belong in the same roadtrip"
  (`suggested_link_prev`/`suggested_link_next` in the UI).
- **`_is_merge_candidate`** (tight) — under 30 minutes apart AND under
  1.5 km — used to flag "this is probably one ride Liberty Rider split in
  two" and surface a one-click merge button in the UI.

Both are pure hints computed on every `GET /api/rides` response; nothing is
grouped or merged automatically.

## Pagination quirk (`sync.py`)

Liberty Rider's `stoppedRides(first, after, before)` API does not behave
like a typical cursor-paginated API — this was reverse-engineered
empirically (see the docstring at the top of `sync.py`):

- `after` is silently ignored by the server.
- Without `before`: returns the most recent `first` rides (server-capped at
  50 regardless of the requested value), ascending within that page.
- With `before` set: returns the *oldest* `first` rides with
  `startTime < before` — starting from the very beginning of history, not
  from wherever `before` falls chronologically.

So a full sync starts with one unbounded call (grabs the most recent page),
then repeatedly sets `before` to the oldest `startTime` seen so far to walk
backwards through history, until a page comes back empty or short. An
incremental sync does the same walk but stops as soon as it reaches a ride
older than or equal to the last synced `startTime`.
