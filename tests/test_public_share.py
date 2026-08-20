"""Public share links: opting one ride into a URL that works with no account.

Two invariants carry the whole feature, and most of what follows exists to
pin them down: a shared ride resolves BY TOKEN ONLY (never by id, never
scoped by user), and the public payload is an allow-list rather than a
filtered private one. See docs/PLAN-public-share.md.
"""
import polyline as polyline_lib
import pytest
from conftest import make_event, make_ride

import db as db_module
import elevation as elevation_module
import mountain_pass as mountain_pass_module

# Exactly what a visitor with no account is allowed to receive. Written out
# here rather than imported from app.py on purpose: adding a field to the
# public payload has to break this test and be argued for, not sail through
# because both sides moved together.
PUBLIC_KEYS = {
    "name",
    "start_time",
    "distance",
    "duration",
    "duration_without_pauses",
    "total_pauses_duration",
    "pause_count",
    "maximum_altitude",
    "polyline",
    "pauses",
    "timeline",
    "track_truncated",
}

# A 4 km straight track sampled every ~100 m (0.0009° of latitude). Long
# enough that trimming SHARE_TRUNCATION_M off each end leaves a real middle,
# and fine-grained enough that the trim lands mid-track instead of
# swallowing whole legs.
TRACK_POINTS = [(48.80 + i * 0.0009, 2.30) for i in range(41)]
LONG_TRACK = polyline_lib.encode(TRACK_POINTS)
TRUNCATION_M = 250


def _metres(a, b):
    from utils import haversine_km
    return haversine_km(a[0], a[1], b[0], b[1]) * 1000


def _seed_ride(user_id, ride_id, start_time, **kw):
    conn = db_module.connect()
    try:
        db_module.upsert_ride(conn, user_id, make_ride(ride_id, start_time, **kw))
        conn.commit()
    finally:
        conn.close()


def _share(client, ride_id, regenerate=False):
    resp = client.post(f"/api/rides/{ride_id}/share", json={"regenerate": regenerate})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the basic loop -----------------------------------------------------

def test_a_ride_is_not_shared_until_asked(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    assert client.get("/api/rides/r1").json()["share"] is None
    assert client.get("/api/rides").json()[0]["shared"] is False


def test_sharing_returns_a_token_and_an_absolute_url(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    share = _share(client, "r1")

    assert len(share["token"]) >= 20
    assert share["url"].endswith(f"/t/{share['token']}")
    assert client.get("/api/rides/r1").json()["share"]["token"] == share["token"]
    assert client.get("/api/rides").json()[0]["shared"] is True


def test_sharing_twice_hands_back_the_same_link(client, login_as):
    """Re-sharing must not quietly invalidate the URL already sent out."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    assert _share(client, "r1")["token"] == _share(client, "r1")["token"]


def test_a_visitor_with_no_session_can_read_a_shared_ride(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", name="Col du Galibier", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]

    visitor = make_client()  # its own cookie jar, never logged in
    resp = visitor.get(f"/api/public/rides/{token}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Col du Galibier"


# --- revocation and regeneration ----------------------------------------

def test_a_revoked_token_is_404(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]

    assert client.delete("/api/rides/r1/share").json() == {"revoked": True}
    assert make_client().get(f"/api/public/rides/{token}").status_code == 404


def test_an_unknown_token_is_404(make_client):
    assert make_client().get("/api/public/rides/nope-not-a-token").status_code == 404


def test_revoked_and_unknown_tokens_are_indistinguishable(client, make_client, login_as):
    """A distinct status or body for a revoked token would confirm that this
    token once existed — an oracle worth nothing to the owner."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]
    client.delete("/api/rides/r1/share")

    visitor = make_client()
    revoked = visitor.get(f"/api/public/rides/{token}")
    unknown = visitor.get("/api/public/rides/PZWfKvSdQqLmXbNrTgHjAw")

    assert revoked.status_code == unknown.status_code == 404
    assert revoked.json() == unknown.json()


def test_revoking_is_idempotent(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _share(client, "r1")

    assert client.delete("/api/rides/r1/share").json() == {"revoked": True}
    assert client.delete("/api/rides/r1/share").json() == {"revoked": False}


def test_regenerating_issues_a_different_token(client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")

    first = _share(client, "r1")["token"]
    second = _share(client, "r1", regenerate=True)["token"]

    assert first != second


def test_regenerating_kills_the_old_link_and_serves_the_new_one(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    old = _share(client, "r1")["token"]

    new = _share(client, "r1", regenerate=True)["token"]

    visitor = make_client()
    assert visitor.get(f"/api/public/rides/{old}").status_code == 404
    assert visitor.get(f"/api/public/rides/{new}").status_code == 200


def test_revoking_then_sharing_again_also_yields_a_new_token(client, make_client, login_as):
    """The other order of operations — cut the link now, share again later."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    old = _share(client, "r1")["token"]
    client.delete("/api/rides/r1/share")

    new = _share(client, "r1")["token"]

    assert new != old
    assert make_client().get(f"/api/public/rides/{old}").status_code == 404


def test_every_token_ever_issued_stays_on_file(client, login_as):
    """Revoked rows are kept, which is what makes a regenerated token
    genuinely unable to collide with one already sent out."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _share(client, "r1")
    _share(client, "r1", regenerate=True)
    _share(client, "r1", regenerate=True)

    conn = db_module.connect()
    try:
        rows = conn.execute("SELECT revoked_at FROM ride_shares WHERE ride_id = 'r1'").fetchall()
    finally:
        conn.close()

    assert len(rows) == 3
    assert len([r for r in rows if r["revoked_at"] is None]) == 1


# --- what the visitor is allowed to see ---------------------------------

def test_the_public_payload_is_exactly_the_allow_list(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]

    body = make_client().get(f"/api/public/rides/{token}").json()

    assert set(body) == PUBLIC_KEYS


def test_the_public_payload_carries_no_ids_notes_or_tags(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    client.patch("/api/rides/r1/notes", json={"notes": "code du portail 1234"})
    client.post("/api/rides/r1/tags", json={"name": "Maison"})
    token = _share(client, "r1")["token"]

    raw = make_client().get(f"/api/public/rides/{token}").text

    assert "1234" not in raw
    assert "Maison" not in raw
    assert "r1" not in raw


def test_the_public_payload_carries_no_start_or_stop_coordinates(client, make_client, login_as):
    """These two pairs are the home address, spelled out."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", start_lat=48.8566, start_lon=2.3522)
    token = _share(client, "r1")["token"]

    body = make_client().get(f"/api/public/rides/{token}").json()

    assert "start_lat" not in body and "stop_lat" not in body
    assert "48.8566" not in make_client().get(f"/api/public/rides/{token}").text


def test_the_public_payload_says_nothing_about_the_bike(client, make_client, login_as):
    """The owner's call: a bike is recognisable, and which one you ride is
    not part of "here is where I went"."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]

    raw = make_client().get(f"/api/public/rides/{token}").text

    # conftest's make_ride puts a Honda CB500 on every seeded ride.
    assert "Honda" not in raw
    assert "CB500" not in raw


def test_the_public_payload_never_identifies_the_owner(client, make_client, login_as):
    login_as(client, "user-1", first_name="Alexandra", email="alexandra@example.com")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]

    raw = make_client().get(f"/api/public/rides/{token}").text

    assert "Alexandra" not in raw
    assert "alexandra@example.com" not in raw
    assert "user-1" not in raw


def test_the_track_is_trimmed_at_both_ends(client, make_client, login_as):
    """The first and last stretch of a ride is, for most riders, their own
    street — and it is trimmed server-side, so those points never reach the
    visitor's browser at all rather than merely going unrendered."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]

    body = make_client().get(f"/api/public/rides/{token}").json()
    published = polyline_lib.decode(body["polyline"][0])

    assert body["track_truncated"] is True
    assert len(published) < len(TRACK_POINTS)
    assert _metres(published[0], TRACK_POINTS[0]) >= TRUNCATION_M
    assert _metres(published[-1], TRACK_POINTS[-1]) >= TRUNCATION_M
    # Everything in between survives — this hides the ends, not the ride.
    assert len(published) > len(TRACK_POINTS) / 2


def test_an_untrimmed_track_says_so(client, make_client, login_as):
    """`track_truncated` drives the notice shown to the visitor, so it must
    not claim a trim that didn't happen."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]

    assert make_client().get(f"/api/public/rides/{token}").json()["track_truncated"] is False


def test_a_ride_shorter_than_its_own_trim_publishes_no_track(client, make_client, login_as):
    """A 200 m ride is all endpoint. Publish nothing rather than a token
    gesture at a trim."""
    login_as(client, "user-1")
    short = polyline_lib.encode([(48.80 + i * 0.0009, 2.30) for i in range(3)])
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=short)
    token = _share(client, "r1")["token"]

    body = make_client().get(f"/api/public/rides/{token}").json()

    assert body["polyline"] == []
    assert body["track_truncated"] is True


def test_a_pause_taken_near_home_is_dropped(client, make_client, login_as):
    """A pause logged in the driveway is the single most revealing marker on
    the map — a pin, not a line that merely passes through."""
    login_as(client, "user-1")
    _seed_ride(
        "user-1", "r1", "2024-01-01T10:00:00Z",
        detailed_polyline=LONG_TRACK,
        pauses=[
            make_event("2024-01-01T10:01:00Z", lat=48.8001, lon=2.30),   # ~11 m from the start
            make_event("2024-01-01T10:30:00Z", lat=48.818, lon=2.30),    # mid-ride
        ],
    )
    token = _share(client, "r1")["token"]

    pauses = make_client().get(f"/api/public/rides/{token}").json()["pauses"]

    assert len(pauses) == 1
    assert round(pauses[0]["lat"], 3) == 48.818


# --- the elevation profile ----------------------------------------------

@pytest.fixture
def fake_elevations(monkeypatch):
    """open-elevation is a real external API; the suite never calls it.
    Altitude climbs with latitude here, so a profile is always non-flat."""
    def _fake(conn, points):
        return [round((lat - 48.0) * 10000) for lat, _lon in points]

    monkeypatch.setattr(elevation_module, "get_elevations", _fake)


def test_a_visitor_gets_the_same_elevation_profile_as_the_owner(
    client, make_client, login_as, fake_elevations
):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]

    body = make_client().get(f"/api/public/rides/{token}/elevation").json()

    assert len(body["profile"]) > 2
    assert body["elevation_gain"] is not None
    assert set(body) == {"profile", "pauses", "elevation_gain", "elevation_loss"}


def test_the_public_profile_is_built_from_the_truncated_track(
    client, make_client, login_as, fake_elevations
):
    """Otherwise the chart hands back the endpoint metres the map withholds:
    the altitude at the very first track point is the altitude at the
    rider's door."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]

    private = client.get("/api/rides/r1/elevation").json()
    public = make_client().get(f"/api/public/rides/{token}/elevation").json()

    # The public profile starts higher up the hill and ends lower down it —
    # both ends of the private one are missing from it.
    assert public["profile"][0]["elevation"] > private["profile"][0]["elevation"]
    assert public["profile"][-1]["elevation"] < private["profile"][-1]["elevation"]
    assert public["profile"][-1]["distance_km"] < private["profile"][-1]["distance_km"]


def test_a_pause_near_home_is_absent_from_the_public_profile(
    client, make_client, login_as, fake_elevations
):
    login_as(client, "user-1")
    _seed_ride(
        "user-1", "r1", "2024-01-01T10:00:00Z",
        detailed_polyline=LONG_TRACK,
        pauses=[
            make_event("2024-01-01T10:01:00Z", lat=48.8001, lon=2.30),
            make_event("2024-01-01T10:30:00Z", lat=48.818, lon=2.30),
        ],
    )
    token = _share(client, "r1")["token"]

    public = make_client().get(f"/api/public/rides/{token}/elevation").json()

    assert len(public["pauses"]) == 1
    assert len(client.get("/api/rides/r1/elevation").json()["pauses"]) == 2


def test_the_elevation_profile_needs_a_live_token_too(client, make_client, login_as, fake_elevations):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]
    client.delete("/api/rides/r1/share")

    visitor = make_client()
    assert visitor.get(f"/api/public/rides/{token}/elevation").status_code == 404
    assert visitor.get("/api/public/rides/PZWfKvSdQqLmXbNrTgHjAw/elevation").status_code == 404
    assert visitor.get("/api/public/rides/r1/elevation").status_code == 404


# --- named cols ---------------------------------------------------------

def _seed_col(ride_id, name, distance_km, elevation=1200.0, lat=48.81, lon=2.30):
    """A col as api_ride_cols persists it once the owner has opened the ride."""
    conn = db_module.connect()
    try:
        conn.execute(
            "INSERT INTO ride_cols (ride_id, name, distance_km, elevation, lat, lon) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ride_id, name, distance_km, elevation, lat, lon),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_visitor_sees_the_cols_the_app_already_knows(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    _seed_col("r1", "Col du Galibier", 2.0)
    token = _share(client, "r1")["token"]

    cols = make_client().get(f"/api/public/rides/{token}/cols").json()["cols"]

    assert [c["name"] for c in cols] == ["Col du Galibier"]
    assert cols[0]["elevation"] == 1200.0


def test_public_cols_never_compute_anything(client, make_client, login_as, monkeypatch):
    """Reading a col is a SELECT; *finding* one is an Overpass call per
    candidate peak plus a database write. Only the first belongs on an
    unauthenticated URL — so this endpoint must not so much as look up a
    name, even for a ride whose cols were never worked out."""
    def _boom(*args, **kwargs):
        raise AssertionError("the public endpoint must not look up a pass name")

    monkeypatch.setattr(mountain_pass_module, "get_pass_name", _boom)
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    token = _share(client, "r1")["token"]

    resp = make_client().get(f"/api/public/rides/{token}/cols")

    assert resp.status_code == 200
    assert resp.json() == {"cols": []}
    conn = db_module.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM ride_cols").fetchone()["n"] == 0
    finally:
        conn.close()


def test_public_cols_sit_on_the_truncated_axis(client, make_client, login_as):
    """A col's stored distance is measured along the full track, and the
    public chart's axis starts at the trimmed head — an unshifted marker
    would land a couple of hundred metres off."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    _seed_col("r1", "Col du Galibier", 2.0)
    token = _share(client, "r1")["token"]

    private = client.get("/api/rides/r1").json()  # ride exists, cols come from the table
    public = make_client().get(f"/api/public/rides/{token}/cols").json()["cols"]

    assert private["id"] == "r1"
    assert 0 < public[0]["distance_km"] < 2.0  # shifted back by the trimmed head


def test_a_col_inside_a_trimmed_end_is_dropped(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    _seed_col("r1", "Col de la Maison", 0.05)  # 50 m in — inside the trimmed head

    token = _share(client, "r1")["token"]

    assert make_client().get(f"/api/public/rides/{token}/cols").json()["cols"] == []


def test_cols_written_before_positions_existed_are_skipped(client, make_client, login_as):
    """Old rows carry a name and nothing else; drawing them at 0 km would
    put a col on the start line. They come back when that ride is reopened."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    conn = db_module.connect()
    try:
        conn.execute("INSERT INTO ride_cols (ride_id, name) VALUES (?, ?)", ("r1", "Col ancien"))
        conn.commit()
    finally:
        conn.close()
    token = _share(client, "r1")["token"]

    assert make_client().get(f"/api/public/rides/{token}/cols").json()["cols"] == []


def test_public_cols_need_a_live_token_too(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", detailed_polyline=LONG_TRACK)
    _seed_col("r1", "Col du Galibier", 2.0)
    token = _share(client, "r1")["token"]
    client.delete("/api/rides/r1/share")

    visitor = make_client()
    assert visitor.get(f"/api/public/rides/{token}/cols").status_code == 404
    assert visitor.get("/api/public/rides/r1/cols").status_code == 404


# --- ownership ----------------------------------------------------------

def test_a_ride_id_is_not_a_token(client, make_client, login_as):
    """The public path resolves by token only. Even for a ride that IS
    shared, its own id opens nothing — which is why no amount of guessing
    ride ids reaches anybody's data."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _share(client, "r1")

    assert make_client().get("/api/public/rides/r1").status_code == 404


def test_cannot_share_another_users_ride(client, make_client, login_as):
    login_as(client, "user-1")
    other = make_client()
    login_as(other, "user-2")
    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")

    assert client.post("/api/rides/r2/share", json={}).status_code == 404


def test_cannot_revoke_another_users_share(client, make_client, login_as):
    login_as(client, "user-1")
    other = make_client()
    login_as(other, "user-2")
    _seed_ride("user-2", "r2", "2024-01-02T10:00:00Z")
    token = _share(other, "r2")["token"]

    assert client.delete("/api/rides/r2/share").status_code == 404
    assert make_client().get(f"/api/public/rides/{token}").status_code == 200


def test_sharing_requires_a_session(client):
    assert client.post("/api/rides/r1/share", json={}).status_code == 401
    assert client.delete("/api/rides/r1/share").status_code == 401


# --- interaction with the rest of the app -------------------------------

def test_merging_a_shared_ride_cuts_the_absorbed_rides_link(client, make_client, login_as):
    """An absorbed ride's link would keep resolving to a fragment of what
    the owner now thinks of as one ride."""
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    _seed_ride("user-1", "r2", "2024-01-01T11:00:00Z")
    kept = _share(client, "r1")["token"]
    absorbed = _share(client, "r2")["token"]

    assert client.post("/api/rides/merge", json={"ride_ids": ["r1", "r2"]}).status_code == 200

    visitor = make_client()
    assert visitor.get(f"/api/public/rides/{absorbed}").status_code == 404
    assert visitor.get(f"/api/public/rides/{kept}").status_code == 200


def test_purging_an_account_takes_its_links_down(client, make_client, login_as, monkeypatch, app_module):
    login_as(client, "user-1")
    monkeypatch.setattr(app_module, "ADMIN_USER_IDS", {"user-1"})
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]

    assert client.delete("/api/account/data").status_code == 200

    assert make_client().get(f"/api/public/rides/{token}").status_code == 404


# --- the page itself ----------------------------------------------------

def test_the_share_page_is_served_without_a_session(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z", name="Col du Galibier")
    token = _share(client, "r1")["token"]

    resp = make_client().get(f"/t/{token}")

    assert resp.status_code == 200
    assert 'id="rideName"' in resp.text
    assert "Col du Galibier" in resp.text  # injected as the preview title


def test_the_share_page_answers_a_dead_link_with_a_page_not_an_error(make_client):
    """Whoever follows an old link from a chat thread gets a sentence in
    French. It says the same thing for a revoked and an unknown token."""
    resp = make_client().get("/t/PZWfKvSdQqLmXbNrTgHjAw")

    assert resp.status_code == 200
    assert "<!--HEAD_META-->" not in resp.text
    assert "Lien introuvable" in resp.text


def test_the_share_page_is_noindex_and_sends_no_referrer(make_client):
    """Without no-referrer the token travels to the OpenStreetMap tile
    servers in the Referer header of every tile the page loads."""
    resp = make_client().get("/t/PZWfKvSdQqLmXbNrTgHjAw")

    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in resp.headers["x-robots-tag"]
    assert 'name="robots"' in resp.text


def test_the_public_json_is_noindex(client, make_client, login_as):
    login_as(client, "user-1")
    _seed_ride("user-1", "r1", "2024-01-01T10:00:00Z")
    token = _share(client, "r1")["token"]

    resp = make_client().get(f"/api/public/rides/{token}")

    assert "noindex" in resp.headers["x-robots-tag"]


def test_robots_txt_keeps_share_links_out_of_search_engines(make_client):
    resp = make_client().get("/robots.txt")

    assert resp.status_code == 200
    assert "Disallow: /t/" in resp.text
