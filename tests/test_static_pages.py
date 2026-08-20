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


def test_landing_carries_the_share_and_seo_metadata(client):
    """A missing OG tag is invisible until the link is shared and unfurls as
    a bare URL — nothing in the app breaks, so only a test catches it."""
    html = client.get("/").text
    assert '<meta name="description"' in html
    assert '<link rel="canonical"' in html
    assert 'property="og:image"' in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    # The card image has to be an absolute URL: unfurlers resolve it from
    # wherever the link was pasted, not from the page's own origin.
    assert 'property="og:image" content="https://' in html
    assert 'rel="icon"' in html


def test_app_page_is_not_indexable(client):
    assert 'name="robots" content="noindex' in client.get("/app").text


def test_robots_txt_is_served_from_the_root(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Disallow: /api/" in resp.text


def test_favicon_is_served(client):
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
