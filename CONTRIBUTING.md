# Contributing

This is a small, single-maintainer personal project — contributions are
welcome, but keep things simple and in proportion to the codebase.

## Dev setup

```bash
make install   # creates .venv, installs requirements.txt
make run        # uvicorn app:app --reload --port 8420
```

Then open http://127.0.0.1:8420 and log in with your own Liberty Rider
email/password (there's no test account — see README.md for how auth
works).

For running tests, install the dev dependencies on top of the regular ones
and run the suite via `make test`:

```bash
.venv/bin/pip install -r requirements-dev.txt
make test
```

Copy `.env.example` to `.env` if you need to override `COOKIE_SECURE` or
`LIBERTY_RIDER_FIREBASE_API_KEY` — neither is required for local dev.

## Where things live

- `app.py` — FastAPI routes and session/auth handling.
- `db.py` — SQLite schema + queries, plain `sqlite3`, no ORM.
- `sync.py`, `liberty_client.py`, `firebase_refresh.py` — talking to the
  Liberty Rider GraphQL API and Firebase auth.
- `elevation.py`, `mountain_pass.py` — the two external, free/keyless APIs
  (open-elevation, OpenStreetMap Overpass) behind the elevation chart and
  named cols/summits — see `docs/ARCHITECTURE.md`.
- `static/` — the whole frontend: plain HTML/CSS/JS, no build step, no
  framework. Ride search and chart rendering live here and currently have
  no automated test coverage (no JS test framework in the repo) — verify
  UI changes by hand (or a throwaway repro page fed real data) rather than
  assuming a passing `make test` covers them.
- `docs/ARCHITECTURE.md` and `docs/API.md` — how the pieces fit together
  and what the Liberty Rider GraphQL API looks like. Read these before
  touching sync/auth code — they document behavior that isn't obvious from
  the code alone.

## Code style

- Docstrings are for the non-obvious: module-level docstrings explain
  multi-tenancy/auth invariants that matter across the file; individual
  functions mostly don't have one unless the "why" isn't clear from the
  name and body (see the migration helpers in `db.py` for examples of
  when a docstring earns its place).
- SQL is raw `sqlite3`, written by hand — no ORM. Keep new queries
  consistent with that (parameterized, no string-built SQL).
- The frontend is intentionally framework-free vanilla JS/HTML/CSS. Don't
  introduce a build step or a frontend framework for a small feature.
- Favor small, focused modules over adding more responsibilities to
  `app.py`.

## Submitting a change

1. Fork the repo and create a branch off `main`.
2. Make your change, keeping the PR focused on one thing — separate
   unrelated fixes/features into separate PRs.
3. Make sure `make test` passes.
4. Open a PR describing what changed and why.
