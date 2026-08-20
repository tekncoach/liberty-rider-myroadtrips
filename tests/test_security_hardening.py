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
