# Architecture

A small, dependency-light stack: no ORM, no build step, no framework on the
frontend.

```
liberty_client.py  →  sync.py  →  db.py (SQLite or Postgres)  →  app.py (FastAPI)  →  static/ (vanilla JS + Leaflet)
```

- **`liberty_client.py`** — minimal GraphQL client for Liberty Rider's
  (undocumented) API. Queries for the current user (`CurrentUser`) and their
  ride history (`MyRides`), plus a GPX-download helper.
- **`firebase_refresh.py`** — direct Firebase Auth REST calls:
  `sign_in_with_password` (the only login path in the app) and
  `refresh_id_token` (used server-side to silently renew an expired session's
  Liberty Rider token without asking the user to log in again).
- **`sync.py`** — walks the API's pagination to pull all-or-new rides and
  upserts them into the database, scoped to one user. Also answers
  "anything new to import?" (`pending_status`) — see **Sync freshness**
  below.
- **`db.py`** — schema + connection helper + `upsert_ride`. No ORM; picks
  SQLite (default, single file, no setup) or Postgres (via `psycopg`, when
  `DATABASE_URL` is set — the shape hosted deployments use) at import time.
  Application code writes one set of `?`-placeholder SQL against either
  backend — see the module's docstring for how that translation works and
  why the two schemas aren't byte-identical (Postgres has no
  `AUTOINCREMENT`, and `REAL` means single-precision there, unlike SQLite's
  always-8-byte `REAL` — silently wrong lat/lon lookups was a real bug this
  caught).
- **`app.py`** — FastAPI app: auth (login/logout/session), the HTTP API (see
  `API.md`), and serving `static/`.
- **`static/index.html` + `static/app.js`** — single-page app, no build step.
  A single global `state` object is the source of truth; each tab
  (Mes traces / Roadtrips / Tags) has its own render function that rebuilds
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
- The single exception, and it is scoped just as tightly by something else:
  a ride the owner has opted into a **public share link** is readable with
  no session, keyed by an unguessable token rather than by `user_id`. See
  **Public share links** below for why that path cannot be walked into
  anybody else's data.

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
`/api/sync`, `/api/sync/status` and `/api/auth/profile` so an expired token
never surfaces to the user as an error during normal use.

## Public share links

One ride at a time can be opted into a URL that works with no account —
`/t/{token}`. This is the only part of the app that answers to a visitor
with no session, so it is built around two invariants rather than around
convenience.

**1. The public read path resolves by token, and by token only.**
`_get_shared_ride(conn, token)` in app.py is the deliberate counterpart of
`_get_owned_ride(conn, ride_id, user_id)`: it takes no ride id and no user
id. A ride id passed where a token belongs cannot match, so there is no way
to walk the URL space into anybody's data — `GET /api/public/rides/{ride_id}`
is a 404 even for a ride that *is* shared, and a test pins that down.

**2. The public payload is an allow-list, not a filtered private one.**
`_public_ride_dict` enumerates every field it emits instead of deleting
fields from `_merged_ride_dict`. A field added to the private ride dict
later is therefore invisible publicly by default rather than leaking by
default. `tests/test_public_share.py` asserts the exact key set, so widening
it has to be a decision someone makes, not a side effect.

- **`ride_shares`** — `token` (PK), `ride_id`, `user_id`, `created_at`,
  `revoked_at`, `expires_at`. One row per token **ever issued**, never
  deleted. Revoking sets `revoked_at`; regenerating revokes the active row
  and inserts a new one. Keeping revoked rows is what makes a regenerated
  link genuinely un-resurrectable — the primary key spans active and revoked
  tokens alike, so a fresh draw can never reuse something already sent out —
  and it leaves an auditable trail of what was once public. A partial unique
  index (`WHERE revoked_at IS NULL`) caps it at one *active* link per ride,
  so neither the API nor the UI ever reasons about a list of live links.
  `expires_at` is never written by the current app but is already honoured
  by the lookup query, so time-limited links need no schema change.
- **The token** — `secrets.token_urlsafe(16)`: 128 bits from the OS CSPRNG,
  22 URL-safe characters. Same primitive as `create_session`. Never the ride
  id (which is the Liberty Rider id, traceable back to the account), never a
  counter, never a hash of either.
- **Unknown, revoked and expired are the same 404**, with the same body. A
  distinct status (410, say) would confirm that a token once existed. The
  *page* at `/t/{token}` still answers 200 with a sentence in French, since
  it says the same thing for all three.
- **Track truncation** — `SHARE_TRUNCATION_M` (250 m) is trimmed off both
  ends of the published track, and any pause inside either radius is
  dropped, before the payload is built. Server-side, so those points are
  absent rather than merely unrendered. A contiguous run is trimmed from
  each end rather than every point within the radius being filtered out:
  filtering would punch a hole mid-track on a loop ride and draw a straight
  line across it, which reads as a glitch and points straight at the hole.
  The honest limit: a loop passing back near its own start mid-ride still
  shows that passage. Stats are **not** recomputed from the trimmed track —
  the distance shown publicly stays the one the owner sees.
- **Headers** — `Referrer-Policy: no-referrer` on the page. Without it the
  token travels to the OpenStreetMap tile servers in the `Referer` of every
  tile the map loads, handing a third party the URL the owner chose who to
  send to. Plus `X-Robots-Tag: noindex, nofollow` on the page and the JSON,
  a `<meta name="robots">` in the HTML, and `Disallow: /t/` in `robots.txt`.
- **Merging revokes** — a ride absorbed into a merge keeps existing as a
  row, so its own link would keep resolving to a fragment of what the owner
  now thinks of as one ride. `api_merge_rides` cuts those links; the merged
  ride can be re-shared. `purge_user_data` deletes share rows with the data
  they point at.
- **The page** — `static/share.html` + `static/share.js`, standalone, no
  session, no `state`. Formatting and polyline decoding come from
  `static/shared.js`, which the logged-in app loads too (two plain `<script>`
  tags, no modules, no build step — the same way Leaflet's `L` is picked
  up). `/t/{token}` serves the file with this ride's `<title>` and Open
  Graph tags substituted into a placeholder, so a link pasted into a chat
  previews as the ride; a `str.replace`, not a template engine.

## Data model (`db.py`)

- **`rides`** — one row per Liberty Rider "stopped ride", upserted verbatim
  from the API (`raw_json` keeps the full original payload), scoped to
  `user_id`. A few more columns encode *our* organization on top of that
  raw data, all preserved across a re-sync (see `upsert_ride`'s pre-SELECT
  and its deliberately-excluded columns in the `ON CONFLICT` clause):
  - `roadtrip_id` — nullable FK to `roadtrips`. Set by the user grouping
    rides into a named, multi-day trip.
  - `merged_into` — nullable, self-referential FK to `rides.id`. See
    **Merge** below.
  - `notes` — a free-text note the user can attach to a ride
    (`PATCH /api/rides/{id}/notes`), shown inline and in search.
- **`roadtrips`** — `id`, `user_id`, `name`. All aggregation (distance,
  duration, day span, day-by-day breakdown) is computed on read from its
  member rides, never stored.
- **`pauses`** / **`resumes`** — one row per recorded pause/resume on a
  ride (`ride_id`, `estimated_time`, `automatic`, `lat`/`lon`). Replaced
  wholesale on every sync of that ride (delete + reinsert), since Liberty
  Rider doesn't give them stable ids. The GraphQL query has always
  requested `resumes`, but the app didn't store or use it until the ride
  timeline feature (below) — `db._backfill_resumes_from_raw_json` recovers
  it for already-synced rides from `raw_json` (no resync needed) the first
  time `init_db()` runs against an existing database.
- **`tags`** / **`ride_tags`** — a plain many-to-many, scoped by
  `tags.user_id` with `UNIQUE(user_id, name)` (two different accounts can
  each have their own "Paris" tag; the same account can't have it twice).
  Tags are get-or-create by name (`api_attach_tag`) and carry no date
  semantics — a tag can span rides from any year, which is the point (e.g. a
  "Paris" tag collecting every ride that passed through the city, 2022 and
  2026 alike).
- **`ride_shares`** — public share links, one row per token ever issued.
  See **Public share links** above.
- **`sync_state`** — `(user_id, key)` → `value`; currently one key per user
  (`last_sync_max_start_time`) tracking the newest `startTime` synced so
  far, for incremental syncs. It records what the *last sync run* saw, which
  is not the same as what Liberty Rider holds — see **Sync freshness**.
- **`elevation_cache`** / **`mountain_pass_cache`** / **`ride_cols`** — see
  **Elevation profile & named cols** below.

### Migrations (Postgres)

Plain `.sql` files in `migrations/`, applied in filename order by
`db._run_postgres_migrations()` and tracked in a `schema_migrations`
table (one row per applied filename) so each one only ever runs once per
database. To change the schema later, add a new `NNNN_description.sql`
file — don't edit an already-applied one, since it won't be re-run
against databases that already applied it. RLS is enabled (no policies —
deny-all) on every table as it's created, including `schema_migrations`
itself, since Supabase's `public` schema is reachable through its Data
API by default (see the Supabase `supabase` skill's security checklist);
the app connects as the table owner, which bypasses RLS.

### Migrations (SQLite)

This section is SQLite-only — unrelated to the Postgres mechanism above,
and only matters if you're running against a local/self-hosted SQLite
file.

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

## Ride timeline (trajet/pause chronology)

`GET /api/rides/{id}` includes `timeline`: a list of `{type: "ride"|"pause",
start, end}` segments built from Liberty Rider's `pauses`/`resumes` events,
rendered as a single horizontal bar (`renderRideTimeline` in `static/app.js`)
sized by each segment's *real duration* — deliberately not a bar-per-pause
histogram, since a histogram indexed by pause number can't show that a long
pause was followed by a short leg.

- **`_ride_own_timeline(start_time, duration, pauses, resumes)`** — a single
  raw ride's own state machine. State always starts `"ride"` at the ride's
  own `start_time` (matches how Liberty Rider itself computes that ride's
  `duration_without_pauses`/`total_pauses_duration`). `pauses` and `resumes`
  are **not positionally paired** — seen on real data, a ride can carry a
  `resume` event *before* its "matching" `pause` (likely closing a brief
  pause-at-start Liberty Rider never logs as a pause). So instead of zipping
  the two arrays by index, every event is merge-sorted chronologically and
  walked as a state machine: a `pause` while already paused, or a `resume`
  while already riding, is a no-op rather than a fabricated segment. A
  trailing unresolved pause (no `resume` before the ride ends) is capped at
  that ride's own end, not left open.
- **`_merged_ride_timeline(conn, row)`** — concatenates each merge-group
  member's own independently-built timeline across merge boundaries. An
  inter-member tracking gap is counted as `"pause"` — consistent with how
  the merged ride's `total_pauses_duration` is itself computed (merged span
  minus each member's own riding time, so the gap already counts as pause
  time there too) — and contiguous same-type segments across the boundary
  merge into one. Building each member's timeline **independently** (rather
  than merge-sorting every event across the whole group at once) matters: on
  real data, a member's own dangling pause otherwise gets "closed" by an
  unrelated `resume` belonging to the *next* member's later pause, ballooning
  one segment to cover everything in between.

## Elevation profile & named cols

Liberty Rider has no altitude-per-point data anywhere (checked via GraphQL
introspection and its own GPX export — only a single `maximumAltitude`
scalar per ride exists). Elevation is instead **estimated** via the free
[open-elevation](https://www.open-elevation.com/) API and aggressively
cached (`elevation.py`, `elevation_cache` table — every coordinate is
looked up at most once, ever).

- **`GET /api/rides/{id}/elevation`** resamples the ride's decoded polyline
  to ~60 points evenly spaced *by distance*, not by raw GPS index
  (`_resample_indices` — points cluster tighter at low speed or during a
  pause, so index-based sampling would over-represent those stretches).
  Returns the profile, each pause projected onto its nearest track point,
  and D+/D- summed over that resampled profile — an estimate of an
  estimate (open-elevation isn't a measurement either), fine for a fun
  stat, not a precise instrument reading.
- **`GET /api/rides/{id}/cols`** is a **separate endpoint**, deliberately:
  naming a col costs one Overpass network call per candidate peak and can
  be slow, and the elevation chart itself must never wait on it — the
  frontend fetches `/elevation` first (chart renders instantly) and
  `/cols` afterward, appending markers once (if) it resolves.
  - **`_detect_peaks(profile, min_prominence_m=100)`** — local elevation
    maxima with topographic-prominence filtering computed over the ride's
    own profile. A col is defined by *shape* — it climbs, then descends —
    not by absolute altitude, so a 300m pass and a 2000m pass are detected
    the same way.
  - **`_refine_peak_point`** — the coarse 60-point profile isn't precise
    enough to disambiguate a col from a nearby named summit a few hundred
    meters away (real example: Col des Tempêtes vs. Mont Ventoux, ~500m
    apart, both within Overpass's search radius of either). Refines the
    peak's location by densely sampling raw GPS points around it, plus any
    pause recorded along that same stretch (a rider stopping at a summit
    is often a very precise fix on exactly where the true high point was),
    and keeps whichever candidate has the highest estimated elevation.
  - **`mountain_pass.get_pass_name(conn, lat, lon, hint_elevation=...)`**
    queries OpenStreetMap's Overpass API for both through-route cols
    (`mountain_pass=yes`) and dead-end summits (`natural=peak` — e.g. Mont
    Ventoux, which was never going to be tagged as a pass since the road
    climbs it and comes back down the same side) within 1km. When several
    candidates are found, picks by closest match between `hint_elevation`
    and each candidate's OSM `ele` tag rather than by raw distance — a col
    and a summit can sit closer to each other than to the query point. A
    successful "nothing found nearby" is cached forever
    (`mountain_pass_cache`); a **request failure is deliberately NOT
    cached** — the public Overpass instance is occasionally rate-limited
    or flaky, and caching that indistinguishably from a genuine "nothing
    here" would poison the result forever over one transient hiccup.
  - Found names are persisted to **`ride_cols`** (`ride_id`, `name`) so
    they become searchable client-side (see `_attach_col_names_bulk`,
    surfaced as `col_names` on `GET /api/rides`) — **progressively**, only
    for a ride whose detail has actually been opened at least once, never
    backfilled for the whole history at once (which would mean hundreds of
    Overpass calls against a free, rate-limited public API in one go).

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

## Sync freshness (`sync.py`, `/api/sync/status`)

Whether there is anything left to import is a question about Liberty Rider,
so it is asked of Liberty Rider. `sync.pending_status()` fetches the newest
remote ride through `LibertyRiderClient.get_latest_ride()` — a deliberately
tiny query (`id` + `startTime`, `first: 1`, no `detailedPolyline`) — and
compares its `startTime` to the newest ride on file locally: the
`last_sync_max_start_time` cursor, falling back to `MAX(rides.start_time)`
when the cursor is missing (claimed orphan data, an interrupted first sync).
Both sides are ISO-8601 UTC strings straight from the same API, so they
compare lexicographically, exactly as `sync()` does while paging.

Cost: two small GraphQL calls per check (the `_live_client_for` token probe
plus the ride probe) against an undocumented, rate-limited API — so the
frontend calls it only on app open and right after a sync, never on a timer.
Everything the local database can answer on its own (ride counts, the "N
trajets synchronisés" label) stays local; only the freshness claim costs a
round-trip. An error there means "unknown", and the UI degrades to a plain
local count rather than asserting "à jour" — which is the failure mode this
whole path exists to prevent (see `docs/BUGFIX-sync-status.md`).

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
