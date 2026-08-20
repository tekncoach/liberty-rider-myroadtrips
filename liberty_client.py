"""Minimal client for the Liberty Rider GraphQL API — personal rides ("Mes trajets").

Mirrors the style of ../scripts/liberty_rider/client.py (public roadbook search),
but targets `currentUser.stoppedRides` instead.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger("carnet.liberty")

API_URL = "https://api.liberty-rider.com/graphql"

# Liberty Rider's API occasionally 502s on heavy queries (a page of many
# rides with full detailedPolyline) — retried a few times with backoff
# before giving up, since a retry a second later routinely succeeds.
RETRYABLE_STATUSES = {502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_S = 2


def _post_with_retry(session: requests.Session, url: str, json: dict, timeout: int):
    for attempt in range(MAX_RETRIES + 1):
        resp = session.post(url, json=json, timeout=timeout)
        if resp.status_code not in RETRYABLE_STATUSES or attempt == MAX_RETRIES:
            resp.raise_for_status()
            return resp
        time.sleep(RETRY_BACKOFF_S * (2 ** attempt))

STOPPED_RIDES_QUERY = """
query MyRides($first: Int, $after: DateTime, $before: DateTime, $onlyFavorites: Boolean) {
  currentUser {
    id
    manualRideCount
    stoppedRides(first: $first, after: $after, before: $before, onlyFavorites: $onlyFavorites) {
      id
      name
      startTime
      distance
      duration
      durationWithoutPauses
      totalPausesDuration
      pauseCount
      maximumAltitude
      isFavorite
      hidden
      state
      detailedPolyline
      previewPictureUrl(width: 320, height: 180)
      gpxExportUrl
      startLocation { latitude longitude accuracy }
      stopLocation { latitude longitude accuracy }
      vehicle { id brandName modelName vehicleType engineDisplacementCc }
      createdRoadbook { id path }
      pauses { estimatedTime automatic lastLocation { latitude longitude } }
      resumes { estimatedTime automatic lastLocation { latitude longitude } }
    }
  }
}
""".strip()

# `firstName` is confirmed to exist on the roadbook-author variant of this
# type (see docs/graphql-api.md) — assumed to exist on `currentUser` too.
# get_current_user() falls back to the minimal query below if not.
CURRENT_USER_QUERY = """
query CurrentUser {
  currentUser {
    id
    firstName
    manualRideCount
  }
}
""".strip()

CURRENT_USER_QUERY_MINIMAL = """
query CurrentUser {
  currentUser {
    id
    manualRideCount
  }
}
""".strip()

# Deliberately tiny — this one is used to answer "is there anything new to
# import?" on every app open, so it asks for the newest ride's `startTime`
# and nothing else (no detailedPolyline, no pauses). Same unbounded-call
# semantics as sync.py's first page: without `before`, the server returns
# the MOST RECENT rides, ascending.
LATEST_RIDE_QUERY = """
query LatestRide($first: Int) {
  currentUser {
    id
    stoppedRides(first: $first) {
      id
      startTime
    }
  }
}
""".strip()


class LibertyRiderClient:
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "liberty-client-name": "roadbook-react",
            "liberty-client-platform": "USER_WEBAPP",
        })

    def get_stopped_rides(
        self,
        first: int = 50,
        after: str | None = None,
        before: str | None = None,
        only_favorites: bool = False,
    ) -> dict:
        """Fetch a page of the current user's stopped rides.

        `after`/`before` are DateTime bounds on startTime, not cursors — to
        paginate through full history, repeatedly push `before` back to the
        startTime of the oldest ride in the previous batch.
        """
        payload = {
            "operationName": "MyRides",
            "variables": {
                "first": first,
                "after": after,
                "before": before,
                "onlyFavorites": only_favorites,
            },
            "query": STOPPED_RIDES_QUERY,
        }
        resp = _post_with_retry(self.session, API_URL, payload, timeout=30)
        data = resp.json()
        if data.get("data") is None:
            raise RuntimeError(f"GraphQL error: {data.get('errors')}")
        if "errors" in data:
            # Partial failure (e.g. a transient 502 re-deriving one field on one
            # ride) — the rest of the page is still usable, so just warn.
            # Third-party payload going into a log line: flattened so a
            # newline in an error message can't forge journal entries.
            flattened = str(data["errors"]).replace("\r", "").replace("\n", " ")
            logger.warning("partial GraphQL errors: %s", flattened[:500])
        return data["data"]["currentUser"]

    def get_current_user(self) -> dict:
        for query in (CURRENT_USER_QUERY, CURRENT_USER_QUERY_MINIMAL):
            payload = {"operationName": "CurrentUser", "query": query}
            resp = _post_with_retry(self.session, API_URL, payload, timeout=15)
            data = resp.json()
            if data.get("data") is not None:
                return data["data"]["currentUser"]
        raise RuntimeError(f"GraphQL error: {data.get('errors')}")

    def get_latest_ride(self) -> dict | None:
        """The single most recent stopped ride (`{id, startTime}`), or None
        if the account has none — the cheap probe behind /api/sync/status."""
        payload = {
            "operationName": "LatestRide",
            "variables": {"first": 1},
            "query": LATEST_RIDE_QUERY,
        }
        resp = _post_with_retry(self.session, API_URL, payload, timeout=15)
        data = resp.json()
        if data.get("data") is None:
            raise RuntimeError(f"GraphQL error: {data.get('errors')}")
        rides = data["data"]["currentUser"]["stoppedRides"] or []
        if not rides:
            return None
        # The server has been seen to ignore the requested `first` and hand
        # back a whole page (see sync.py's docstring), so pick the newest of
        # whatever came back instead of assuming a single-element list.
        return max(rides, key=lambda ride: ride["startTime"] or "")

    # download_gpx() used to live here: it GET'd a URL handed to us by the
    # Liberty Rider API with this session's `Authorization: Bearer <user
    # token>` header still attached, so a hostile or compromised URL in that
    # payload would have exfiltrated the user's token (or reached an
    # internal address). Nothing called it — the app builds GPX itself from
    # stored polylines — so it is gone rather than guarded. If it ever comes
    # back: a separate unauthenticated session, and an allowlist on the
    # URL's host.
