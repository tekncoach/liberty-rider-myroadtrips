"""Remediation cover for SECURITY_AUDIT.md — APP-02/03/04/06/08/11/12.

Each test names the finding it closes so a future reader can tie the
assertion back to the audit rather than guessing at intent.
"""
import importlib

import db as db_module


def test_security_headers_on_every_response(client):
    """APP-04 — headers must be present on API and static responses alike."""
    for path in ("/", "/app", "/api/auth/status"):
        headers = client.get(path).headers
        assert headers["x-content-type-options"] == "nosniff", path
        assert headers["x-frame-options"] == "DENY", path
        assert "frame-ancestors 'none'" in headers["content-security-policy"], path
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin", path


def test_csp_allows_what_the_page_actually_loads(client):
    csp = client.get("/app").headers["content-security-policy"]
    assert "script-src 'self' https://unpkg.com" in csp
    assert "https://*.tile.openstreetmap.org" in csp
    assert "object-src 'none'" in csp


def test_cross_site_mutation_is_rejected(client, login_as):
    """APP-12 — SameSite=lax already keeps the cookie away, but the check is
    now ours and explicit."""
    login_as(client, "user-1")
    resp = client.post(
        "/api/roadtrips",
        json={"name": "Depuis un autre site"},
        headers={"origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    # Same-origin (what a browser sends on its own page) still works.
    resp = client.post(
        "/api/roadtrips",
        json={"name": "Depuis l'app"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 200


def test_login_failures_are_throttled(client, app_module, monkeypatch):
    """APP-02 — an unlimited login endpoint is a credential-stuffing relay."""
    import requests

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error": {"message": "INVALID_PASSWORD"}}

    def fake_sign_in(email, password, api_key):
        err = requests.HTTPError("bad creds")
        err.response = FakeResponse()
        raise err

    monkeypatch.setattr(app_module, "sign_in_with_password", fake_sign_in)

    body = {"email": "alex@example.com", "password": "wrong"}
    for _ in range(app_module.LOGIN_MAX_FAILURES):
        assert client.post("/api/auth/login", json=body).status_code == 401
    blocked = client.post("/api/auth/login", json=body)
    assert blocked.status_code == 429


def test_login_error_does_not_reveal_whether_the_account_exists(client, app_module, monkeypatch):
    """APP-02 — relaying Firebase's EMAIL_NOT_FOUND vs INVALID_PASSWORD made
    this an account-enumeration oracle."""
    import requests

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error": {"message": "EMAIL_NOT_FOUND"}}

    def fake_sign_in(email, password, api_key):
        err = requests.HTTPError("no such user")
        err.response = FakeResponse()
        raise err

    monkeypatch.setattr(app_module, "sign_in_with_password", fake_sign_in)
    resp = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Identifiants invalides"
    assert "EMAIL_NOT_FOUND" not in resp.text


def test_expired_session_is_refused(client, login_as):
    """APP-03 — sessions used to be valid forever server-side."""
    login_as(client, "user-1")
    assert client.get("/api/rides").status_code == 200

    conn = db_module.connect()
    try:
        conn.execute("UPDATE sessions SET expires_at = ?", ("2000-01-01 00:00:00",))
        conn.commit()
    finally:
        conn.close()

    assert client.get("/api/rides").status_code == 401


def test_sessions_predating_the_expiry_column_still_work(client, login_as):
    """A deploy must not log everyone out: NULL expires_at stays valid."""
    login_as(client, "user-1")
    conn = db_module.connect()
    try:
        conn.execute("UPDATE sessions SET expires_at = NULL")
        conn.commit()
    finally:
        conn.close()
    assert client.get("/api/rides").status_code == 200


def test_expired_sessions_can_be_purged(client, login_as):
    login_as(client, "user-1")
    conn = db_module.connect()
    try:
        conn.execute("UPDATE sessions SET expires_at = ?", ("2000-01-01 00:00:00",))
        conn.commit()
        assert db_module.delete_expired_sessions(conn) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
    finally:
        conn.close()


def test_logout_drops_the_stored_liberty_rider_tokens(client, login_as):
    """APP-01 — the refresh token never expires on its own; keeping it for a
    logged-out account is pure liability."""
    login_as(client, "user-1")
    conn = db_module.connect()
    try:
        assert conn.execute("SELECT refresh_token FROM users WHERE id = 'user-1'").fetchone()["refresh_token"]
    finally:
        conn.close()

    client.post("/api/auth/logout")

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT bearer_token, refresh_token FROM users WHERE id = 'user-1'").fetchone()
    finally:
        conn.close()
    assert row["refresh_token"] is None
    assert row["bearer_token"] is None


def test_adding_an_empty_ride_list_does_not_build_a_broken_query(client, login_as):
    """APP-06 — `IN ()` is a 200 on SQLite and a 500 on Postgres."""
    login_as(client, "user-1")
    trip_id = client.post("/api/roadtrips", json={"name": "Vide"}).json()["id"]
    assert client.post(f"/api/roadtrips/{trip_id}/rides", json={"ride_ids": []}).status_code == 200


def test_oversized_text_is_rejected(client, login_as):
    """APP-08 — a 200k-character roadtrip name used to be stored verbatim."""
    login_as(client, "user-1")
    resp = client.post("/api/roadtrips", json={"name": "X" * 200_000})
    assert resp.status_code == 422


def test_api_docs_are_not_published_in_production(temp_db, monkeypatch):
    """APP-07 — /docs and /openapi.json listed the whole endpoint surface,
    maintainer-only routes included, to anonymous visitors."""
    from fastapi.testclient import TestClient

    import app as app_mod

    monkeypatch.setenv("COOKIE_SECURE", "1")
    importlib.reload(app_mod)
    try:
        prod_client = TestClient(app_mod.app)
        assert prod_client.get("/docs").status_code == 404
        assert prod_client.get("/openapi.json").status_code == 404
    finally:
        monkeypatch.delenv("COOKIE_SECURE", raising=False)
        importlib.reload(app_mod)


# --- follow-ups from the adversarial re-review (R-2 … R-7) ---------------

def test_sessions_without_an_expiry_are_backfilled_not_left_immortal(client, login_as, app_module):
    """R-2 — a NULL expires_at was accepted forever by get_session_user AND
    skipped by delete_expired_sessions: an unpurgeable, immortal session."""
    login_as(client, "user-1")
    conn = db_module.connect()
    try:
        conn.execute("UPDATE sessions SET expires_at = NULL")
        conn.commit()
    finally:
        conn.close()

    db_module.init_db()  # what a deploy does

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT expires_at FROM sessions").fetchone()
    finally:
        conn.close()
    assert row["expires_at"] is not None


def test_login_purges_expired_sessions(client, login_as):
    """R-3 — nothing called the purge, so the table only ever grew."""
    login_as(client, "user-1")
    conn = db_module.connect()
    try:
        conn.execute("UPDATE sessions SET expires_at = ?", ("2000-01-01 00:00:00",))
        conn.commit()
    finally:
        conn.close()

    login_as(client, "user-1")  # a second login runs the purge

    conn = db_module.connect()
    try:
        rows = conn.execute("SELECT expires_at FROM sessions").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1  # only the fresh one survived


def test_logout_on_one_device_leaves_other_sessions_working(make_client, login_as):
    """R-5 — clearing the account's tokens on any logout broke every other
    logged-in device's sync."""
    phone, laptop = make_client(), make_client()
    login_as(phone, "user-1")
    login_as(laptop, "user-1")

    phone.post("/api/auth/logout")

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT refresh_token FROM users WHERE id = 'user-1'").fetchone()
    finally:
        conn.close()
    assert row["refresh_token"] is not None, "the laptop still needs it"
    assert laptop.get("/api/rides").status_code == 200


def test_logout_everywhere_drops_all_sessions_and_tokens(make_client, login_as):
    """R-4 — delete_user_sessions() existed but was wired to nothing."""
    phone, laptop = make_client(), make_client()
    login_as(phone, "user-1")
    login_as(laptop, "user-1")

    assert phone.post("/api/auth/logout-all").status_code == 200

    assert laptop.get("/api/rides").status_code == 401
    conn = db_module.connect()
    try:
        row = conn.execute("SELECT refresh_token FROM users WHERE id = 'user-1'").fetchone()
        assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
    finally:
        conn.close()
    assert row["refresh_token"] is None


def test_logout_everywhere_requires_a_session(client):
    assert client.post("/api/auth/logout-all").status_code == 401


def test_origin_check_ignores_the_scheme(client, login_as):
    """R-6 — comparing the full origin made every write depend on the proxy
    forwarding X-Forwarded-Proto: drop it and the app 403s everything."""
    login_as(client, "user-1")
    resp = client.post(
        "/api/roadtrips",
        json={"name": "Derrière un proxy TLS"},
        headers={"origin": "https://testserver"},  # app itself sees http
    )
    assert resp.status_code == 200


def test_allowed_origin_pins_the_full_origin_when_set(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    import app as app_mod

    monkeypatch.setenv("ALLOWED_ORIGIN", "https://carnet.example")
    importlib.reload(app_mod)
    try:
        pinned = TestClient(app_mod.app)
        resp = pinned.post("/api/roadtrips", json={"name": "x"}, headers={"origin": "http://testserver"})
        assert resp.status_code == 403
    finally:
        monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
        importlib.reload(app_mod)


def test_login_failure_tracking_does_not_grow_without_bound(client, app_module, monkeypatch):
    """R-7 — only the key being read was pruned, so a spray across many
    emails left an entry per email behind, forever."""
    import requests

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error": {"message": "INVALID_PASSWORD"}}

    def fake_sign_in(email, password, api_key):
        err = requests.HTTPError("bad creds")
        err.response = FakeResponse()
        raise err

    monkeypatch.setattr(app_module, "sign_in_with_password", fake_sign_in)
    for i in range(20):
        client.post("/api/auth/login", json={"email": f"spray{i}@example.com", "password": "x"})
    assert len(app_module._login_failures) > 1

    # Everything has aged out; the next check sweeps the whole dict, not
    # just the key it happens to look at.
    monkeypatch.setattr(app_module.time, "monotonic", lambda: 10_000_000.0)
    app_module._check_login_throttle(["email:someone@example.com"])
    assert app_module._login_failures == {}


def test_log_values_cannot_forge_journal_entries(app_module):
    """CodeQL alert #1 (py/log-injection) — a newline in an email address
    would otherwise let the caller write whole fake log lines."""
    forged = "victim@example.com\nAug 20 21:00 sudo: root granted"
    safe = app_module._log_safe(forged)
    assert "\n" not in safe
    assert "sudo" in safe.replace("_", " "), "the text is neutralised, not dropped"
    assert len(app_module._log_safe("x" * 500)) <= 120


def test_costly_endpoints_are_rate_limited(client, login_as, app_module):
    """APP-05 — /sync walks pages of ride history against a third-party API;
    an account must not be able to run it in a loop."""
    login_as(client, "user-1")
    limit, _ = app_module.COSTLY_LIMITS["sync"]
    for _ in range(limit):
        client.post("/api/sync", json={"full": False})
    assert client.post("/api/sync", json={"full": False}).status_code == 429


def test_rate_limit_is_per_account(make_client, login_as, app_module):
    """One account burning its allowance must not lock out another."""
    alice, bob = make_client(), make_client()
    login_as(alice, "user-1")
    login_as(bob, "user-2")
    limit, _ = app_module.COSTLY_LIMITS["sync"]
    for _ in range(limit + 1):
        alice.post("/api/sync", json={"full": False})
    assert alice.post("/api/sync", json={"full": False}).status_code == 429
    assert bob.post("/api/sync", json={"full": False}).status_code == 200
