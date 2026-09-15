"""GET /healthz endpoint for liveness and database reachability probes."""
import pytest

import db as db_module


def test_healthz_returns_200_on_healthy_app(client):
    """GET /healthz returns 200 and {'status': 'ok', 'database': 'ok'} when the app is healthy."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_healthz_requires_no_auth(client):
    """GET /healthz requires no session cookie or auth header."""
    # Remove any cookies that might be set
    client.cookies.clear()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_healthz_returns_503_when_database_unreachable(client, app_module, monkeypatch):
    """GET /healthz returns 503 and {'status': 'degraded', 'database': 'unreachable'} 
    when database is unreachable, without raising an exception."""
    # Mock db.connect to raise an exception simulating database unavailability
    original_connect = db_module.connect
    
    def failing_connect():
        raise RuntimeError("Database connection failed")
    
    monkeypatch.setattr(db_module, "connect", failing_connect)
    
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "unreachable"}
    
    # Restore the original function for cleanup
    monkeypatch.setattr(db_module, "connect", original_connect)
