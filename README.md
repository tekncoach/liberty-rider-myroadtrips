# Liberty Rider — Mes trajets

A self-hosted, multi-user tool for organizing your personal ride history
from the [Liberty Rider](https://liberty-rider.com/) motorcycle app. Liberty
Rider's own web app only shows "roadbooks" you've explicitly published — it
has no view of your raw ride history ("Mes trajets" / stopped rides). This
tool syncs that history into a database and gives you a map-based UI to
group rides into multi-day roadtrips, tag them by geographic zone, and
clean up tracking artifacts like a single ride getting split in two.

Each account is scoped to its own data — logging in with your Liberty Rider
email/password only ever shows *your* rides, never anyone else's, whether
you're running this for yourself or hosting it for a few people.

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
make run
```

Then open **http://127.0.0.1:8420** and log in with your Liberty Rider
email and password — that's it. There's no separate token to fetch or paste;
the app exchanges your credentials directly with Firebase (the same
identity provider Liberty Rider's own web app uses) and keeps a session for
you from then on. Your password is never stored — only the resulting
session tokens are, encrypted-at-rest storage of which is on the roadmap
before this is used for anyone but yourself (see Disclaimer).

Once logged in, click **Synchroniser** to pull your ride history in, then
**Full sync** any time you want to re-walk the entire history instead of
just what's new.

### Running this for more than one person

Every account is fully isolated (its own rides/roadtrips/tags, scoped by
your Liberty Rider user id) — logging in just works for anyone with a
Liberty Rider account, no per-user setup needed. If you deploy this
somewhere reachable over the network rather than on `127.0.0.1`, set:

```bash
export COOKIE_SECURE=1   # only send the session cookie over HTTPS
```

and put a real HTTPS reverse proxy in front of it — the app itself doesn't
terminate TLS.

## How it works

- **Backend**: FastAPI + raw SQLite (no ORM) — see `app.py`, `db.py`,
  `sync.py`. Ride data is fetched via a small GraphQL client
  (`liberty_client.py`) and upserted into `data/rides.db`, which is
  gitignored.
- **Frontend**: a single-page vanilla-JS app (`static/index.html`,
  `static/app.js`) using Leaflet for mapping, with no build step.
- **Auth**: one method — email + password, exchanged directly with
  Firebase (`firebase_refresh.py`). On success the app looks up your
  Liberty Rider user id (`currentUser.id` — the same id Liberty Rider
  itself uses) and creates a `users` row keyed by it, plus a session
  cookie. Every ride/roadtrip/tag row is scoped to that id, and every
  request needs a valid session — see `docs/ARCHITECTURE.md`.

## Disclaimer

This is an independent, unofficial, community-built project. It is **not
affiliated with, endorsed by, sponsored by, or in any way officially
connected to Liberty Rider**, or any of its subsidiaries or affiliates. The
name "Liberty Rider" and any related names, marks, emblems, and images are
registered trademarks of their respective owners. The official Liberty
Rider app and website are at **[liberty-rider.com](https://liberty-rider.com/)**.

This project talks to an **undocumented, reverse-engineered** Liberty Rider
API that was not designed or published for third-party use. It may break at
any time if Liberty Rider changes their API, without notice. Use it at your
own risk, against your own account, in accordance with Liberty Rider's own
Terms of Service.

## License

MIT License — see [LICENSE](LICENSE).

## A note on how this was built

This project was built collaboratively with [Claude](https://www.anthropic.com/claude), Anthropic's AI assistant.
