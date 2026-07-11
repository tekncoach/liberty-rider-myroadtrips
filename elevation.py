"""Elevation lookup via the open-elevation public API (open-elevation.com),
backed by the `elevation_cache` table (see db.py) so the same coordinate is
never looked up twice. open-elevation is free/public but slow and
rate-limited — this is the one piece of the app with a real external cost,
so it's a candidate for a premium-only gate later.
"""
from __future__ import annotations

import requests

OPEN_ELEVATION_URL = "https://api.open-elevation.com/api/v1/lookup"
CACHE_PRECISION = 4  # ~11m grid — plenty for a visual profile, maximizes cache hits
BATCH_SIZE = 100


def _cache_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, CACHE_PRECISION), round(lon, CACHE_PRECISION))


def get_elevations(conn, points: list[tuple[float, float]]) -> list[float | None]:
    """Returns one elevation (meters) per input (lat, lon), in the same
    order — None for any point open-elevation couldn't resolve (a request
    failure never raises; the chart just renders with gaps)."""
    keys = [_cache_key(lat, lon) for lat, lon in points]
    unique_keys = list(dict.fromkeys(keys))

    cached: dict[tuple[float, float], float] = {}
    for lat, lon in unique_keys:
        row = conn.execute(
            "SELECT elevation FROM elevation_cache WHERE lat = ? AND lon = ?", (lat, lon)
        ).fetchone()
        if row is not None:
            cached[(lat, lon)] = row["elevation"]

    missing = [k for k in unique_keys if k not in cached]
    for i in range(0, len(missing), BATCH_SIZE):
        batch = missing[i:i + BATCH_SIZE]
        try:
            resp = requests.post(
                OPEN_ELEVATION_URL,
                json={"locations": [{"latitude": lat, "longitude": lon} for lat, lon in batch]},
                timeout=20,
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except (requests.RequestException, ValueError):
            continue  # leave this batch unresolved rather than fail the whole chart
        for (lat, lon), result in zip(batch, results):
            elevation = result.get("elevation")
            if elevation is None:
                continue
            cached[(lat, lon)] = elevation
            conn.execute(
                "INSERT INTO elevation_cache (lat, lon, elevation) VALUES (?, ?, ?) "
                "ON CONFLICT(lat, lon) DO UPDATE SET elevation = excluded.elevation",
                (lat, lon, elevation),
            )
    conn.commit()

    return [cached.get(k) for k in keys]
