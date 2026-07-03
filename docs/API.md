# API reference

All endpoints are served by `app.py` under `/api`. Bodies are JSON; nothing
requires authentication (this app is meant to run locally, for one user, as
a private server — see the security note in the README).

## Sync

### `POST /api/sync`
Pulls rides from the Liberty Rider API into the local database.

Body: `{ "token": "<Liberty Rider Bearer token>", "full": false }`

`full: true` re-walks the entire ride history instead of just what's new
since the last sync (see `docs/ARCHITECTURE.md` for the pagination
semantics). Returns `{ "fetched": int, "upserted": int, "total_rides": int }`.

## Rides

### `GET /api/rides?grouped={true|false}`
Lists rides. Omit `grouped` for everything; `true` for rides already in a
roadtrip; `false` for ungrouped rides only. Each ride includes `tags`,
`merge_ride_ids`, and the grouping-suggestion fields
(`suggested_link_prev`/`_next`, `merge_candidate_prev_id`/`_next_id`) —
see `docs/ARCHITECTURE.md` for what drives them. Absorbed merge members are
never included; only merge representatives are.

### `GET /api/rides/{ride_id}`
Full detail for one ride: same fields as the list, plus `polyline` (decoded
`[lat, lon]` pairs) and `pauses`.

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
