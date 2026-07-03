# Liberty Rider — Mes trajets

A self-hosted tool for organizing your personal ride history from the
[Liberty Rider](https://liberty-rider.com/) motorcycle app. Liberty Rider's
own web app only shows "roadbooks" you've explicitly published — it has no
view of your raw ride history ("Mes trajets" / stopped rides). This tool
syncs that history into a local database and gives you a map-based UI to
group rides into multi-day roadtrips, tag them by geographic zone, and
clean up tracking artifacts like a single ride getting split in two.

It's built for one user at a time, running locally, against your own
Liberty Rider account.

## Features

- **Sync** — pulls your full ride history (or just what's new) from Liberty
  Rider's GraphQL API into a local SQLite database.
- **Roadtrips** — manually group rides into named, multi-day trips.
- **Tags** — many-to-many labels for geographic zones (e.g. "Paris"),
  independent of any date range, so a place you keep passing through can
  accumulate rides over time.
- **Merge** — non-destructively combines two rides that got tracking-split
  into one back into a single logical ride; raw rows are never deleted, so
  a merge can be undone at any time. Same-day, near-zero-gap pairs are
  automatically flagged as merge candidates.
- **Map view** — every ride, roadtrip, and tag rendered on a Leaflet /
  OpenStreetMap map, color-coded per day, with ride preview thumbnails
  pulled from Liberty Rider's own static tile server.
- **GPX export** — download any ride, roadtrip, or tag as a GPX file.

## Screenshots

Coming soon.

## Requirements

- Python 3.9+
- A Liberty Rider account with some ride history

## Setup

```bash
make install
```

### 1. Get a Bearer token

The Liberty Rider API isn't public, so there's no API key to request. Three
ways to get a token, from most to least convenient:

**Recommended — `make login`**: opens a real browser window on
liberty-rider.com. Log in exactly as you normally would — your password
goes straight to Liberty Rider's own login page and this script never sees
it. It watches network traffic for the resulting token and captures it
automatically, along with the underlying Firebase refresh token, so future
tokens can be renewed with `make refresh-token` without opening a browser
again.

```bash
make login-setup   # one-time: installs Playwright + a Chromium build
make login
```

**Alternative — `make login-password`**: skips the browser and exchanges
your email/password directly with Firebase's login endpoint. Only use this
if you have a specific reason to avoid a browser (e.g. a headless server) —
read the security note at the top of `login_password.py` first, since it
means typing your real password into a third-party script rather than
Liberty Rider's own login page.

```bash
make login-password EMAIL=you@example.com
```

**Manual fallback**: copy the token straight out of your browser's
DevTools, no scripts involved:

1. Log into [liberty-rider.com](https://liberty-rider.com/) in a browser.
2. Open DevTools → Network tab.
3. Find any request to `api.liberty-rider.com/graphql`.
4. Copy the `authorization` request header value, then strip the leading
   `Bearer ` prefix — you just need the raw JWT (`eyJ...`).

Either way, the token is a short-lived Firebase ID token (~1 hour) — once
you have the refresh token too (via either `make login*` option above),
`make refresh-token` mints new ones indefinitely without any further login.

### 2. Sync your rides

```bash
make sync TOKEN="eyJ..."
```

This does an incremental sync (only rides newer than the last sync). Use
`make sync-full TOKEN=...` to re-download the entire history. You can also
paste the token directly into the web UI's sync box instead of using the
Makefile.

### 3. Run the app

```bash
make run
```

Then open **http://127.0.0.1:8420**.

## How it works

- **Backend**: FastAPI + raw SQLite (no ORM) — see `app.py`, `db.py`,
  `sync.py`. Ride data is fetched via a small GraphQL client
  (`liberty_client.py`) and upserted into `data/rides.db`, which is
  gitignored.
- **Frontend**: a single-page vanilla-JS app (`static/index.html`,
  `static/app.js`) using Leaflet for mapping, with no build step.
- **Auth**: `login_playwright.py` / `login_password.py` / manual DevTools
  copy all end up writing the same three files at the repo root —
  `.liberty_rider_token`, `.liberty_rider_refresh_token`,
  `.liberty_rider_firebase_api_key` — all gitignored, never committed, and
  read by nothing except this app running on your own machine.

## Disclaimer

This project talks to an **undocumented, reverse-engineered** Liberty Rider
API. It is not officially supported, may break at any time if Liberty Rider
changes their API, and is **not affiliated with, endorsed by, or supported
by Liberty Rider** in any way. Use at your own risk, against your own
account only.

## License

MIT License — see [LICENSE](LICENSE).

## A note on how this was built

This project was built collaboratively with [Claude](https://www.anthropic.com/claude), Anthropic's AI assistant.
