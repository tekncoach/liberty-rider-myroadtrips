"""GET / (marketing landing page) vs GET /app (the actual SPA) — split so
logged-out visitors land on marketing content, not the bare login form."""


def test_root_serves_the_landing_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Carnet de Route" in resp.text
    assert 'href="/app"' in resp.text


def test_app_route_serves_the_spa(client):
    resp = client.get("/app")
    assert resp.status_code == 200
    assert 'id="authScreen"' in resp.text
    assert 'id="loginForm"' in resp.text
