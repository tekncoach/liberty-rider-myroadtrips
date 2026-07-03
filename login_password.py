"""Alternative to login_playwright.py: exchange your email/password directly
with Firebase's REST auth API.

⚠️  Security note: unlike login_playwright.py (which never sees your
password — you type it into Liberty Rider's own login page), this script
DOES read your real Liberty Rider password, in memory, once. It is never
written to disk, logged, or sent anywhere except Firebase's official
`identitytoolkit.googleapis.com` sign-in endpoint (the same one the Liberty
Rider web app itself calls). Prefer login_playwright.py unless you have a
specific reason not to use a browser here (e.g. a headless server). Read
this file — it's short — before trusting it with your credentials.

Requires the Firebase project's public web API key, either passed with
--api-key, or already captured into .liberty_rider_firebase_api_key by a
previous run of login_playwright.py. To find it by hand: log into
liberty-rider.com in a browser, open DevTools → Network, find any request
to identitytoolkit.googleapis.com or securetoken.googleapis.com, and copy
its `key=` query parameter.

Usage:
  python3 login_password.py --email you@example.com
"""
from __future__ import annotations

import argparse
import getpass
import pathlib

import requests

ROOT = pathlib.Path(__file__).parent
TOKEN_FILE = ROOT / ".liberty_rider_token"
REFRESH_TOKEN_FILE = ROOT / ".liberty_rider_refresh_token"
API_KEY_FILE = ROOT / ".liberty_rider_firebase_api_key"

SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--api-key", help="Firebase web API key (see file docstring for how to find it)")
    args = parser.parse_args()

    api_key = args.api_key or (API_KEY_FILE.read_text().strip() if API_KEY_FILE.exists() else None)
    if not api_key:
        raise SystemExit(
            "Missing Firebase API key. Pass --api-key, or run login_playwright.py once "
            "to capture it automatically into .liberty_rider_firebase_api_key."
        )

    password = getpass.getpass("Liberty Rider password (not echoed, not stored): ")

    resp = requests.post(
        SIGN_IN_URL,
        params={"key": api_key},
        json={"email": args.email, "password": password, "returnSecureToken": True},
        timeout=15,
    )
    del password  # out of scope for the rest of the process regardless, but be explicit
    if not resp.ok:
        detail = resp.json().get("error", {}).get("message", resp.text)
        raise SystemExit(f"Login failed: {detail}")
    data = resp.json()

    TOKEN_FILE.write_text(data["idToken"])
    REFRESH_TOKEN_FILE.write_text(data["refreshToken"])
    API_KEY_FILE.write_text(api_key)
    print(f"Bearer token -> {TOKEN_FILE}")
    print(f"Refresh token -> {REFRESH_TOKEN_FILE}")
    print("`make refresh-token` will now mint fresh tokens automatically.")


if __name__ == "__main__":
    main()
