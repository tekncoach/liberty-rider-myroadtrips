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
- `db.py` — schema + queries, no ORM. SQLite locally by default; Postgres
  (via `psycopg`) when `DATABASE_URL` is set — see the module docstring.
- `migrations/*.sql` — Postgres-only schema migrations, applied in
  filename order (see `docs/ARCHITECTURE.md`). Add a new numbered file for
  a schema change; never edit one that's already been applied anywhere.
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
- SQL is written by hand — no ORM. Keep new queries consistent with that
  (parameterized with `?`, no string-built SQL — `db.py` translates `?` to
  Postgres's `%s` under the hood, so stick to `?` everywhere).
- The frontend is intentionally framework-free vanilla JS/HTML/CSS. Don't
  introduce a build step or a frontend framework for a small feature.
- Favor small, focused modules over adding more responsibilities to
  `app.py`.

## Cutting a release

Releases are cheap here and exist so a bug report can say *which* version
it is about.

1. Move the `[Unreleased]` entries in `CHANGELOG.md` under a new
   `## [X.Y.Z] — YYYY-MM-DD` heading, and open a fresh empty
   `[Unreleased]` above it.
2. Tag the commit: `git tag -a vX.Y.Z -m "…"` and push the tag.
3. Create the GitHub release from that tag, pasting the changelog section
   as its notes.

Version numbers follow semver, read for an app rather than a library: the
major/minor/patch decision hangs on whether an operator has something to
do before deploying (a migration to run, an environment variable to set),
not on a function signature.

## Submitting a change

1. Fork the repo and create a branch off `main`.
2. Make your change, keeping the PR focused on one thing — separate
   unrelated fixes/features into separate PRs.
3. Make sure `make test` passes.
4. Open a PR describing what changed and why.

## Dependencies

`requirements.txt` is the file you edit: one line per direct dependency,
pinned to an exact version. `requirements.lock` is generated from it and
pins the *transitive* tree with hashes, so a build installs the same bytes
today and in six months. CI audits the lock, and fails if a pin in
requirements.txt is missing from it.

After changing requirements.txt — including when merging a Dependabot PR:

```bash
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt
```
