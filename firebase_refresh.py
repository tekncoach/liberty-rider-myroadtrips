"""Exchange a long-lived Firebase refresh token for a fresh ID (Bearer) token.

Avoids re-logging in via a browser every ~1h: the refresh token is captured
once from an already-authenticated browser session (see README note at the
bottom of this file) and reused here indefinitely, until revoked.
"""
from __future__ import annotations

import requests

SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"


def refresh_id_token(refresh_token: str, api_key: str) -> dict:
    """Returns {"id_token": ..., "refresh_token": ..., "expires_in": ...}.

    Firebase may rotate the refresh token on use — always persist the
    returned `refresh_token`, not just the id_token.
    """
    resp = requests.post(
        SECURE_TOKEN_URL,
        params={"key": api_key},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "id_token": data["id_token"],
        "refresh_token": data["refresh_token"],
        "expires_in": data["expires_in"],
    }


# --- One-time capture of the refresh token (done from an authenticated browser) ---
#
# Firebase Auth (web SDK) persists its session in IndexedDB, under a database
# named "firebaseLocalStorageDb", store "firebaseLocalStorage". From a page
# already logged into liberty-rider.com, running this JS in the page context
# returns the current refresh token + the project's API key:
#
#   const dbs = await indexedDB.databases();
#   const req = indexedDB.open("firebaseLocalStorageDb");
#   req.onsuccess = () => {
#     const db = req.result;
#     const tx = db.transaction("firebaseLocalStorage", "readonly");
#     tx.objectStore("firebaseLocalStorage").getAll().onsuccess = (e) => {
#       console.log(JSON.stringify(e.target.result));
#     };
#   };
#
# The API key is also visible as the `key` query param on any request to
# `securetoken.googleapis.com` or `identitytoolkit.googleapis.com` made during
# login/token-refresh (visible in the Network tab).
