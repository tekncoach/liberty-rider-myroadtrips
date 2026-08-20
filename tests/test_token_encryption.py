"""Encryption at rest for the Liberty Rider credentials (finding APP-01).

The module reads its key at import time, so each test reloads `crypto`, `db`
and `app` under the environment it wants — that's what the `with_key` helper
below does.
"""
import importlib

import pytest
from conftest import FakeLRClient
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import crypto as crypto_module
import db as db_module


def _mock_outbound(app_mod, monkeypatch):
    """Nothing in this file may touch the network."""
    monkeypatch.setattr(app_mod, "LibertyRiderClient", FakeLRClient)
    monkeypatch.setattr(
        app_mod,
        "sign_in_with_password",
        lambda email, password, key: {"id_token": "tok-user-1", "refresh_token": "refresh-user-1"},
    )


def _login(client, user_id="user-1"):
    FakeLRClient.current_user = {"id": user_id, "firstName": "Alex", "manualRideCount": 0}
    FakeLRClient.latest_ride = None
    resp = client.post("/api/auth/login", json={"email": f"{user_id}@example.com", "password": "pw"})
    assert resp.status_code == 200, resp.text


@pytest.fixture
def with_key(temp_db, monkeypatch):
    """Reload the stack with a fresh encryption key, and return the modules.

    Order matters: `importlib.reload` restores the module's real attributes,
    so the Liberty Rider client and the Firebase sign-in have to be re-mocked
    *after* every reload. Getting this wrong doesn't fail loudly — it sends
    real requests to Liberty Rider's API from the test suite.
    """
    def _reload(key=None):
        if key is None:
            key = Fernet.generate_key().decode()
        monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)
        importlib.reload(crypto_module)
        importlib.reload(db_module)
        monkeypatch.setattr(db_module, "DB_PATH", temp_db)
        import app as app_mod

        importlib.reload(app_mod)
        _mock_outbound(app_mod, monkeypatch)
        return app_mod, key

    yield _reload
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    importlib.reload(crypto_module)
    importlib.reload(db_module)
    import app as app_mod

    importlib.reload(app_mod)


def _stored(column="refresh_token", user_id="user-1"):
    conn = db_module.connect()
    try:
        row = conn.execute(
            "SELECT bearer_token, refresh_token, firebase_api_key FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return row[column]
    finally:
        conn.close()


def test_tokens_are_unreadable_in_the_database(with_key):
    app_mod, _ = with_key()
    client = TestClient(app_mod.app)
    _login(client)

    raw = _stored()
    assert raw.startswith(crypto_module.PREFIX)
    assert "refresh-user-1" not in raw, "the plaintext token must not survive in the row"


def test_the_app_still_works_with_encryption_on(with_key):
    app_mod, _ = with_key()
    client = TestClient(app_mod.app)
    _login(client)
    # Reading a ride goes through get_session_user, i.e. through decryption.
    assert client.get("/api/rides").status_code == 200


def test_decryption_round_trips_through_get_session_user(with_key):
    app_mod, _ = with_key()
    client = TestClient(app_mod.app)
    _login(client)

    conn = db_module.connect()
    try:
        session_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]
        user = db_module.get_session_user(conn, session_id)
    finally:
        conn.close()
    assert user["refresh_token"] == "refresh-user-1"
    assert user["bearer_token"] == "tok-user-1"


def test_without_a_key_tokens_stay_in_clear(client, login_as):
    """The documented fallback: no key configured means no encryption, so a
    missing env var can't turn into a login outage."""
    login_as(client, "user-1")
    assert _stored() == "refresh-user-1"
    assert not crypto_module.is_enabled()


def test_plaintext_rows_are_migrated_in_place(with_key, login_as, client):
    """A row written before the key existed must keep working, and be
    encrypted on the next boot."""
    login_as(client, "user-1")           # written in clear
    assert _stored() == "refresh-user-1"

    app_mod, _ = with_key()              # key configured, restart
    app_mod._encrypt_tokens_at_startup()

    assert _stored().startswith(crypto_module.PREFIX)
    conn = db_module.connect()
    try:
        session_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]
        assert db_module.get_session_user(conn, session_id)["refresh_token"] == "refresh-user-1"
    finally:
        conn.close()


def test_migration_is_idempotent(with_key):
    app_mod, _ = with_key()
    client = TestClient(app_mod.app)
    _login(client)

    before = _stored()
    conn = db_module.connect()
    try:
        assert db_module.encrypt_plaintext_tokens(conn) == 0, "already-encrypted rows are skipped"
    finally:
        conn.close()
    assert _stored() == before, "re-encrypting would make the value unreadable"


def test_a_rotated_key_asks_the_user_to_log_in_again_rather_than_crashing(with_key):
    app_mod, _ = with_key()
    client = TestClient(app_mod.app)
    _login(client)

    # Same database, different key — as if the key were lost or rotated.
    app_mod, _ = with_key(Fernet.generate_key().decode())

    conn = db_module.connect()
    try:
        session_id = conn.execute("SELECT id FROM sessions").fetchone()["id"]
        user = db_module.get_session_user(conn, session_id)
    finally:
        conn.close()
    assert user["refresh_token"] is None
    assert user["id"] == "user-1", "the account itself is still readable"


def test_a_malformed_key_refuses_to_start_instead_of_storing_in_clear(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(RuntimeError, match="valid Fernet key"):
        importlib.reload(crypto_module)
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    importlib.reload(crypto_module)
