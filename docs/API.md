# API reference

All endpoints are served by `app.py` under `/api`. Bodies are JSON.
Every endpoint below except `## Auth` requires a valid session — an httpOnly
`session_id` cookie set by `POST /api/auth/login` — and returns **401** if
it's missing or expired. Every result is scoped to that session's account;
there is no endpoint that returns another user's data (ride/roadtrip/tag ids
belonging to someone else resolve as **404**, not 403 — see
`docs/ARCHITECTURE.md`).

## Auth

### `POST /api/auth/login`
Body: `{ "email": "...", "password": "..." }`. The only way to authenticate.
Exchanges the password with Firebase, fetches the Liberty Rider account
(`currentUser.id`/`firstName`), creates the account on first login, sets the
session cookie. Returns `{ "ok": true, "first_name": "..." }`. **401** with
Firebase's own error message (e.g. `INVALID_PASSWORD`) on bad credentials.
**500** if the server has no `LIBERTY_RIDER_FIREBASE_API_KEY` configured.

### `POST /api/auth/logout`
Deletes the current session (server-side) and clears the cookie. Always
`{ "ok": true }`, even if there was no session.

### `GET /api/auth/status`
No session required (never 401s). Returns `{ "logged_in": false }` or
`{ "logged_in": true, "first_name": "..." }`. Used by the frontend on load
to decide whether to show the login screen or the app.

### `GET /api/auth/profile`
Live-fetches `first_name` and `manual_ride_count` from Liberty Rider itself
(refreshing the stored token first if needed — see `_live_client_for` in
`docs/ARCHITECTURE.md`). Note: `manual_ride_count` (Liberty Rider's own
`currentUser.manualRideCount` field) doesn't necessarily match this app's
own `total_rides` from `/api/sync` — its exact definition on Liberty Rider's
side isn't confirmed, only its name.

## Sync

### `POST /api/sync`
Pulls rides from the Liberty Rider API into the local database, for the
logged-in account. Body: `{ "full": false }`. The Liberty Rider token itself
is never passed by the client — it's resolved (and silently refreshed if
expired) server-side from the session's stored credentials.

`full: true` re-walks the entire ride history instead of just what's new
since the last sync (see `docs/ARCHITECTURE.md` for the pagination
semantics). Returns `{ "fetched": int, "upserted": int, "total_rides": int }`.
**401** if the stored token is invalid and couldn't be refreshed (log in
again); **502** on any other Liberty Rider API error.

## Rides

### `GET /api/rides?grouped={true|false}`
Lists rides. Omit `grouped` for everything; `true` for rides already in a
roadtrip; `false` for ungrouped rides only. Each ride includes `tags`,
`merge_ride_ids`, `notes`, `col_names` (names discovered so far via
`GET /api/rides/{id}/cols` — empty until that ride's detail has been
opened at least once, see `docs/ARCHITECTURE.md`), and the
grouping-suggestion fields (`suggested_link_prev`/`_next`,
`merge_candidate_prev_id`/`_next_id`). Absorbed merge members are never
included; only merge representatives are.

### `GET /api/rides/{ride_id}`
Full detail for one ride: same fields as the list, plus `polyline` (decoded
`[lat, lon]` pairs), `pauses`, and `timeline` — a list of
`{type: "ride"|"pause", start, end}` segments spanning the whole ride (see
`docs/ARCHITECTURE.md`).

### `GET /api/rides/{ride_id}/elevation`
Estimated elevation profile, resampled to ~60 points by distance:
`{ profile: [{distance_km, elevation}, ...], pauses: [{distance_km,
elevation, automatic, lat, lon}, ...], elevation_gain, elevation_loss }`.
`elevation`/`elevation_gain`/`elevation_loss` can be `null` if
open-elevation was unavailable. Does not include col names — see below.

### `GET /api/rides/{ride_id}/cols`
Named cols/summits detected along the ride:
`{ cols: [{distance_km, elevation, name, lat, lon}, ...] }` — only named
detections are included (an unnamed peak is dropped). Deliberately a
separate, slower endpoint from `/elevation` (one Overpass network call per
candidate peak) — see `docs/ARCHITECTURE.md`. Also persists found names to
the `ride_cols` table as a side effect, making them searchable via
`GET /api/rides`'s `col_names`.

### `PATCH /api/rides/{ride_id}/notes`
Body: `{ "notes": "..." }`. Sets (or clears, if blank) a free-text note on
the ride. Returns `{ "notes": "..." }` (trimmed; empty string if cleared).

### `POST /api/rides/merge`
Body: `{ "ride_ids": ["id1", "id2", ...] }` (2+). Merges them into one
logical ride — see `docs/ARCHITECTURE.md`. Returns
`{ "id": "<representative ride id>" }`. 400 if any ride is already merged,
tagged, or the set spans more than one roadtrip.

### `DELETE /api/rides/{ride_id}/merge`
Un-merges every ride absorbed into `ride_id`. Idempotent (no-op if `ride_id`
has no members).

### `POST /api/rides/{ride_id}/tags`
Body: `{ "name": "Paris" }`. Get-or-creates the tag by name and attaches it.
Returns `{ "tags": [{"id", "name"}, ...] }` (the ride's current tags).

### `DELETE /api/rides/{ride_id}/tags/{tag_id}`
Detaches one tag from one ride. Returns the ride's remaining tags, same
shape as above.

### `GET /api/rides/{ride_id}/export.gpx`
Downloads the ride (its full merge group, if any) as a GPX file.

### `DELETE /api/rides/{ride_id}/roadtrip`
Detaches a ride from its roadtrip (the ride itself is untouched, just
ungrouped again).

## Roadtrips

### `GET /api/roadtrips`
Lists roadtrips with aggregate stats: `ride_count`, `day_count` (calendar
span, inclusive — not just days with a ride), `start_date`/`end_date`,
`total_distance`, `total_duration`, `total_pause_count`.

### `GET /api/roadtrips/{trip_id}`
Same aggregate fields, plus `rides` (full ride dicts), `polylines`
(`{ride_id: [[lat, lon], ...]}`), `pauses` (`{ride_id: [pause, ...]}`), and
`days` — a day-by-day breakdown (`date`, `ride_ids`, per-day totals).

### `POST /api/roadtrips`
Body: `{ "name": "...", "ride_ids": [...] }` (`ride_ids` optional). Creates
a roadtrip and assigns the given rides to it. Returns `{ "id": int }`.

### `PATCH /api/roadtrips/{trip_id}`
Body: `{ "name": "..." }`. Renames.

### `DELETE /api/roadtrips/{trip_id}`
Deletes the roadtrip; its rides become ungrouped (not deleted).

### `POST /api/roadtrips/{trip_id}/rides`
Body: `{ "ride_ids": [...] }`. Assigns rides to this roadtrip.

### `GET /api/roadtrips/{trip_id}/export.gpx`
Downloads every ride in the roadtrip as one multi-track GPX file.

## Tags

### `GET /api/tags`
Lists tags with the same aggregate-stats shape as roadtrips, except
`day_count` here means the number of *distinct calendar days* with a tagged
ride (not a date span — a tag can legitimately span years).

### `GET /api/tags/{tag_id}`
Same detail shape as a roadtrip (`rides`, `polylines`, `pauses`, `days`).

### `PATCH /api/tags/{tag_id}`
Body: `{ "name": "..." }`. Renames.

### `DELETE /api/tags/{tag_id}`
Deletes the tag and all its ride associations. Rides themselves are
untouched.

### `GET /api/tags/{tag_id}/export.gpx`
Downloads every ride under the tag as one multi-track GPX file.
