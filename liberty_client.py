"""Minimal client for the Liberty Rider GraphQL API — personal rides ("Mes trajets").

Mirrors the style of ../scripts/liberty_rider/client.py (public roadbook search),
but targets `currentUser.stoppedRides` instead.
"""
from __future__ import annotations

import requests

API_URL = "https://api.liberty-rider.com/graphql"

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
        resp = self.session.post(API_URL, json=payload, timeout=30)
        resp.raise_for_status()
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
            resp = self.session.post(API_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data") is not None:
                return data["data"]["currentUser"]
        raise RuntimeError(f"GraphQL error: {data.get('errors')}")

    def download_gpx(self, gpx_export_url: str) -> bytes:
        resp = self.session.get(gpx_export_url, timeout=30)
        resp.raise_for_status()
        return resp.content
