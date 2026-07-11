"""Minimal client for the Liberty Rider GraphQL API — personal rides ("Mes trajets").

Mirrors the style of ../scripts/liberty_rider/client.py (public roadbook search),
but targets `currentUser.stoppedRides` instead.
"""
from __future__ import annotations

import time

import requests

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
            print(f"[liberty_client] partial GraphQL errors: {data['errors']}")
        return data["data"]["currentUser"]

    def get_current_user(self) -> dict:
        for query in (CURRENT_USER_QUERY, CURRENT_USER_QUERY_MINIMAL):
            payload = {"operationName": "CurrentUser", "query": query}
            resp = _post_with_retry(self.session, API_URL, payload, timeout=15)
            data = resp.json()
            if data.get("data") is not None:
                return data["data"]["currentUser"]
        raise RuntimeError(f"GraphQL error: {data.get('errors')}")

    def download_gpx(self, gpx_export_url: str) -> bytes:
        resp = self.session.get(gpx_export_url, timeout=30)
        resp.raise_for_status()
        return resp.content
