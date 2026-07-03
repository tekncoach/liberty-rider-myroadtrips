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

The Liberty Rider API isn't public, so there's no API key to request —
instead, grab the same token your browser uses:

1. Log into [liberty-rider.com](https://liberty-rider.com/) in a browser.
2. Open DevTools → Network tab.
3. Find any request to `api.liberty-rider.com/graphql`.
4. Copy the `authorization` request header value, then strip the leading
   `Bearer ` prefix — you just need the raw JWT (`eyJ...`).

The token is a short-lived Firebase ID token (~1 hour). You'll need a fresh
one each time it expires — either repeat the steps above, or set up the
optional saved-refresh-token flow (see `firebase_refresh.py` and
`refresh_token_cli.py`) to mint new tokens with `make refresh-token` without
going back to a browser.

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
- **Auth**: no login flow is implemented for Liberty Rider itself — you
  supply your own Firebase Bearer token, either pasted by hand or refreshed
  via the optional saved-refresh-token helper scripts.

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
