# Changelog

Notable changes to Carnet de Route. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
— with one caveat: this is a self-hosted app, not a library, so *breaking*
means "an operator has something to do before deploying this" (a migration
to run, an environment variable to set), not an API signature change.

Dates are ISO 8601. Unreleased work sits at the top; see
[CONTRIBUTING.md](CONTRIBUTING.md#cutting-a-release) for how a release is
cut.

## [Unreleased]

### Added

- A share card, favicon and `robots.txt` on the public pages: the landing
  page now unfurls with a screenshot instead of a bare URL when the link
  is pasted somewhere.
- A "Security & your data" section in the README, and badges for CI,
  CodeQL, the supported Python versions and the licence.
- `SECURITY.md` — how to report a vulnerability, what is in scope, and
  what the app stores.
- CodeQL and Dependabot workflows, a Postgres job in CI, and a Python
  version matrix (3.11 / 3.12 / 3.13).
- `pyproject.toml` with a `ruff` configuration (including the
  flake8-bandit rules), run in CI and by a shared pre-commit hook.

### Changed

- The modals (ride detail, admin, guided tour) are now real dialogs for
  assistive technology and keyboard users: labelled, closable with
  Escape, and focus stays inside them. Emoji-only buttons carry a name.
- `/docs` and `/openapi.json` no longer publish the whole endpoint
  surface on a hosted deployment — they follow `COOKIE_SECURE`, and
  `EXPOSE_API_DOCS=1` opts back in.

### Security

- Hostile ride/tag/note text is escaped everywhere it reaches the DOM
  (three sinks were interpolating it raw).
- Security headers and a CSP on every response; Leaflet pinned with an
  SRI hash.
- Failed logins are throttled per IP and per account, and answered with a
  single non-discriminating error.
- Sessions expire server-side and are purged; the Liberty Rider tokens go
  with the last session of an account.
- An explicit `Origin` check on mutating requests, length caps on user
  input, and defensive `user_id` scoping on every query.

The full write-up, including what was deliberately left open, is in
[docs/SECURITY-REMEDIATION.md](docs/SECURITY-REMEDIATION.md).

## Before this file

No release has been tagged yet, so everything above is also everything
this file covers. The history before it is in `git log`, in four rough
eras:

- **A personal SQLite tool** — one account, one file, a token pasted by
  hand. The last commit of that era is tagged `pre-postgres`.
- **Multi-tenant rewrite** — email/password login against Firebase, every
  row scoped to a Liberty Rider user id, several accounts on one
  instance.
- **Hosted deployment** — a Postgres backend with pooled connections and
  numbered migrations, plus the N+1 and payload-size work that made the
  hosted version usable.
- **A product rather than a script** — the landing page at `/`, the
  onboarding tour, ride chronology, estimated elevation profiles with
  named cols, search, and GPX export.
