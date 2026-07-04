"""Named landmark lookup via OpenStreetMap's Overpass API, for peaks
detected on a ride's elevation profile (see app.py's `_detect_peaks`). A
peak is identified by shape (climbs then descends), not absolute altitude
— Overpass just supplies its name if OSM has tagged one nearby, whether
it's a through-route col (`mountain_pass=yes`) or a dead-end summit
(`natural=peak`, e.g. Mont Ventoux — the road climbs it and comes back
down the same side, so it was never going to be tagged as a pass). A
successful "nothing named nearby" is cached forever (`mountain_pass_cache`
table, see db.py) so an unnamed peak isn't re-queried on every ride reopen.
"""
from __future__ import annotations

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass rejects requests.post's default User-Agent with 406 Not
# Acceptable — needs something identifying the client.
REQUEST_HEADERS = {"User-Agent": "liberty-rider-myroadtrips/1.0 (personal project)"}
CACHE_PRECISION = 3  # ~110m grid — passes are sparse, a coarser cache is fine
SEARCH_RADIUS_M = 1000


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, CACHE_PRECISION), round(lon, CACHE_PRECISION))


def get_pass_name(conn, lat: float, lon: float, hint_elevation: float | None = None) -> str | None:
    """Name of the OSM-tagged mountain pass or peak within SEARCH_RADIUS_M
    of (lat, lon), or None if there isn't one — also None on a request
    failure, but a failure is NOT cached (the public Overpass instance is
    occasionally rate-limited/flaky; caching that indistinguishably from a
    genuine "nothing here" would poison the result forever over a
    transient hiccup — only a successful query gets cached).

    When `hint_elevation` (our own best estimate of the actual peak's
    altitude) is given and a candidate has an OSM `ele` tag, elevation
    match is used ahead of raw distance to pick among several candidates —
    a col and a named summit can both sit within radius (e.g. Col des
    Tempêtes ~1827m vs. Mont Ventoux ~1910m), and matching the measured
    altitude is a much stronger signal than which one happens to be a few
    dozen meters closer on the ground."""
    key_lat, key_lon = _cache_key(lat, lon)
    row = conn.execute(
        "SELECT name FROM mountain_pass_cache WHERE lat = ? AND lon = ?", (key_lat, key_lon)
    ).fetchone()
    if row is not None:
        return row["name"]

    try:
        query = f"""
        [out:json][timeout:10];
        (
          node(around:{SEARCH_RADIUS_M},{lat},{lon})[mountain_pass];
          node(around:{SEARCH_RADIUS_M},{lat},{lon})[natural=peak];
        );
        out body 10;
        """
        resp = requests.post(OVERPASS_URL, data={"data": query}, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        elements = resp.json().get("elements") or []
    except (requests.RequestException, ValueError):
        return None  # transient failure — retry next time, don't cache

    named = [e for e in elements if e.get("tags", {}).get("name")]

    def _score(e):
        dist = (e["lat"] - lat) ** 2 + (e["lon"] - lon) ** 2
        ele = e.get("tags", {}).get("ele")
        if hint_elevation is not None and ele is not None:
            try:
                return (abs(float(ele) - hint_elevation), dist)
            except ValueError:
                pass
        return (float("inf"), dist)  # no usable ele tag/hint — fall back to distance only

    name = min(named, key=_score)["tags"]["name"] if named else None
    conn.execute(
        "INSERT OR REPLACE INTO mountain_pass_cache (lat, lon, name) VALUES (?, ?, ?)",
        (key_lat, key_lon, name),
    )
    conn.commit()
    return name
