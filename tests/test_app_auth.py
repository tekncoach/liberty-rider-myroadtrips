"""Login/session/401 behavior of app.py."""
import db as db_module


def test_login_sets_session_cookie_and_creates_user_row(client, app_module, login_as):
    login_as(client, "user-1", first_name="Alex", email="alex@example.com")

    assert "session_id" in client.cookies

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = 'user-1'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["first_name"] == "Alex"
    assert row["email"] == "alex@example.com"


def test_login_wrong_password_returns_401(client, app_module, monkeypatch):
    import requests

    class FakeResponse:
        status_code = 401

        def json(self):
            return {"error": {"message": "INVALID_PASSWORD"}}

    def fake_sign_in(email, password, api_key):
        err = requests.HTTPError("bad creds")
        err.response = FakeResponse()
        raise err

    monkeypatch.setattr(app_module, "sign_in_with_password", fake_sign_in)

    resp = client.post("/api/auth/login", json={"email": "x@example.com", "password": "wrong"})
    assert resp.status_code == 401
    assert "session_id" not in client.cookies


def test_auth_status_reflects_logged_in_state(client, login_as):
    resp = client.get("/api/auth/status")
    assert resp.json() == {"logged_in": False}

    login_as(client, "user-1", first_name="Alex")

    resp = client.get("/api/auth/status")
    assert resp.json() == {"logged_in": True, "first_name": "Alex"}


def test_data_endpoints_require_session(client):
    for path in ("/api/rides", "/api/roadtrips", "/api/tags"):
        resp = client.get(path)
        assert resp.status_code == 401, path


def test_logout_clears_session(client, login_as):
    login_as(client, "user-1")
    assert client.get("/api/rides").status_code == 200

    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200

    resp = client.get("/api/rides")
    assert resp.status_code == 401


def test_bogus_session_cookie_is_401(client):
    client.cookies.set("session_id", "does-not-exist")
    resp = client.get("/api/rides")
    assert resp.status_code == 401
