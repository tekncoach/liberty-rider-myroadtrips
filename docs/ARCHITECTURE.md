# Architecture

A small, dependency-light stack: no ORM, no build step, no framework on the
frontend.

```
liberty_client.py  →  sync.py  →  db.py (SQLite)  →  app.py (FastAPI)  →  static/ (vanilla JS + Leaflet)
```

- **`liberty_client.py`** — minimal GraphQL client for Liberty Rider's
  (undocumented) API. Queries for the current user (`CurrentUser`) and their
  ride history (`MyRides`), plus a GPX-download helper.
- **`firebase_refresh.py`** — direct Firebase Auth REST calls:
  `sign_in_with_password` (the only login path in the app) and
  `refresh_id_token` (used server-side to silently renew an expired session's
  Liberty Rider token without asking the user to log in again).
- **`sync.py`** — walks the API's pagination to pull all-or-new rides and
  upserts them into SQLite, scoped to one user.
- **`db.py`** — schema + connection helper + `upsert_ride`. Raw `sqlite3`,
  no ORM.
- **`app.py`** — FastAPI app: auth (login/logout/session), the HTTP API (see
  `API.md`), and serving `static/`.
- **`static/index.html` + `static/app.js`** — single-page app, no build step.
  A single global `state` object is the source of truth; each tab
  (Roadtrips / Tags / Mes traces) has its own render function that rebuilds
  its DOM subtree from `state` on every change. Leaflet renders the map.
  `#authScreen` (login form) and `#app` (the real UI) are mutually
  exclusive — the app never fetches or renders any ride data until
  `GET /api/auth/status` confirms a session exists.

## Multi-tenancy and auth

The app is multi-tenant: every account is scoped to its own Liberty Rider
user id, and every row of ride/roadtrip/tag data belongs to exactly one
account. There is exactly one way to authenticate — email + password,
exchanged directly with Firebase (`POST /api/auth/login`) — no token pasting,
no browser automation, no other login path.

- **`users`** — one row per account, keyed by `id` = the user's own Liberty
  Rider id (`currentUser.id` from the GraphQL API — *not* a separate
  internally-minted id, so it's directly traceable back to the same account
  on Liberty Rider itself). Also holds `first_name` (fetched at login),
  `email` (as typed at login), and the current `bearer_token` /
  `refresh_token` / `firebase_api_key`, used to make Liberty Rider API calls
  on that user's behalf and to silently refresh the token when it expires.
- **`sessions`** — `id` (an opaque, random session token — see
  `db.create_session`) → `user_id`. The session id is set as an httpOnly
  cookie (`SESSION_COOKIE` in app.py); every protected endpoint depends on
  `get_session_user`, which looks up this table and 401s if the cookie is
  missing or the session doesn't exist.
- Every other table's rows are scoped by a `user_id` column (`rides`,
  `roadtrips`, `tags`) or transitively through one (`pauses` via `ride_id`,
  `ride_tags` via both, `sync_state` via `user_id` directly). Every query in
  `app.py` filters by the current session's `user_id` — there is no
  endpoint that returns unscoped data.

**Login flow** (`POST /api/auth/login` in app.py): exchange email/password
with Firebase → fetch `currentUser` from Liberty Rider's API using the
resulting token → `db.upsert_user` (creates the account on first login, by
Liberty Rider id) → `db.save_user_tokens` → `db.claim_orphaned_data` (see
below) → `db.create_session` → set the session cookie.

**`claim_orphaned_data(conn, user_id)`**: a one-time migration helper, not
a normal-operation code path. If this is the very first account ever
created on this database (i.e. exactly one row in `users`), it attributes
any pre-existing `user_id IS NULL` rows to that account. This exists purely
to carry forward data from this app's pre-multi-tenant single-user version;
it's a no-op (and permanently inert) as soon as a second account exists, so
it can never hand one user's data to another.

**Token refresh**: `_live_client_for(conn, user)` in app.py makes one cheap
API call with the user's stored `bearer_token`; on a 401/403 it exchanges
the stored `refresh_token` via `firebase_refresh.refresh_id_token` and
persists the new tokens, transparently, before retrying. Used by
`/api/sync` and `/api/auth/profile` so an expired token never surfaces to
the user as an error during normal use.

## Data model (`db.py`)

- **`rides`** — one row per Liberty Rider "stopped ride", upserted verbatim
  from the API (`raw_json` keeps the full original payload), scoped to
  `user_id`. Two more columns encode *our* organization on top of that raw
  data:
  - `roadtrip_id` — nullable FK to `roadtrips`. Set by the user grouping
    rides into a named, multi-day trip.
  - `merged_into` — nullable, self-referential FK to `rides.id`. See
    **Merge** below.
- **`roadtrips`** — `id`, `user_id`, `name`. All aggregation (distance,
  duration, day span, day-by-day breakdown) is computed on read from its
  member rides, never stored.
- **`pauses`** — one row per recorded pause on a ride (`ride_id`,
  `estimated_time`, `automatic`, `lat`/`lon`). Replaced wholesale on every
  sync of that ride (delete + reinsert), since Liberty Rider doesn't give
  pauses stable ids.
- **`tags`** / **`ride_tags`** — a plain many-to-many, scoped by
  `tags.user_id` with `UNIQUE(user_id, name)` (two different accounts can
  each have their own "Paris" tag; the same account can't have it twice).
  Tags are get-or-create by name (`api_attach_tag`) and carry no date
  semantics — a tag can span rides from any year, which is the point (e.g. a
  "Paris" tag collecting every ride that passed through the city, 2022 and
  2026 alike).
- **`sync_state`** — `(user_id, key)` → `value`; currently one key per user
  (`last_sync_max_start_time`) tracking the newest `startTime` synced so
  far, for incremental syncs.

### Migrations

`db.init_db()` runs a handful of one-shot migration functions
(`_migrate_users_table`, `_migrate_tags_table`, `_migrate_sync_state_table`)
that rebuild older table shapes into the current multi-tenant one —
relevant if you're running this against a database created before
multi-tenancy was added; a no-op otherwise. Each rebuild follows the same
pattern: rename the old table, create the new one, copy rows across, drop
the old one, `commit()` immediately (rather than waiting for one final
commit at the end of `init_db()` — an earlier version did that and a later
unrelated failure in the same call left a half-renamed table stranded).
Each function also self-heals if it's re-run after being interrupted before
its own `DROP TABLE ..._old` (checks for a leftover `..._old` table first
and recovers it instead of silently ignoring it).

Foreign keys are enforced with `PRAGMA foreign_keys = OFF` — deliberately.
SQLite's `ALTER TABLE ... RENAME TO` automatically rewrites *other* tables'
stored `REFERENCES` clauses to point at the renamed-away name (e.g. renaming
`users` to `users_old` mid-migration silently changed `rides`' stored schema
to say `REFERENCES users_old(id)`). Nothing here relies on FK cascades, so
enforcement is off to avoid that failure mode entirely rather than working
around it table-by-table.

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
allowed — the representative inherits that common roadtrip. All of this
(and every other ride/roadtrip/tag lookup in app.py) is scoped to the
current session's `user_id` — merging, tagging, or grouping someone else's
ride isn't just rejected by these rules, the row simply doesn't resolve at
all for a different account (404, not 403 — see `_get_owned_ride` /
`_get_owned_roadtrip` / `_get_owned_tag`).

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
