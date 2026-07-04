"""Direct Firebase Auth REST calls: password sign-in and token refresh.

Used by app.py's login/sync endpoints — this is the only auth path in the
app (email + password, exchanged server-side for a Firebase session), so
these two calls are all it needs.
"""
from __future__ import annotations

import requests

SECURE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"
SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"


def sign_in_with_password(email: str, password: str, api_key: str) -> dict:
    """Exchanges an email/password directly with Firebase's own sign-in
    endpoint (the same one the Liberty Rider web app calls). Returns
    {"id_token": ..., "refresh_token": ...}. Raises requests.HTTPError with
    Firebase's error message in the response body on failure (bad
    credentials, wrong api_key, etc).
    """
    resp = requests.post(
        SIGN_IN_URL,
        params={"key": api_key},
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"id_token": data["idToken"], "refresh_token": data["refreshToken"]}


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
