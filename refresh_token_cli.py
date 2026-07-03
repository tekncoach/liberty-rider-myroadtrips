"""Refresh the Liberty Rider Bearer token from a saved Firebase refresh token.

Reads (at repo root):
  .liberty_rider_refresh_token   (Firebase refresh token, captured once from a logged-in browser)
  .liberty_rider_firebase_api_key (Firebase web API key, public but kept alongside for convenience)
Writes:
  .liberty_rider_token           (fresh ~1h Bearer token, same file the sync scripts already read)
Also rewrites the refresh token file if Firebase rotated it.
"""
from __future__ import annotations

import pathlib

from firebase_refresh import refresh_id_token

ROOT = pathlib.Path(__file__).parent
REFRESH_TOKEN_FILE = ROOT / ".liberty_rider_refresh_token"
API_KEY_FILE = ROOT / ".liberty_rider_firebase_api_key"
TOKEN_FILE = ROOT / ".liberty_rider_token"


def main() -> str:
    if not REFRESH_TOKEN_FILE.exists() or not API_KEY_FILE.exists():
        raise SystemExit(
            f"Missing {REFRESH_TOKEN_FILE.name} or {API_KEY_FILE.name} at repo root. "
            "Capture them once from an authenticated browser session (see firebase_refresh.py)."
        )
    refresh_token = REFRESH_TOKEN_FILE.read_text().strip()
    api_key = API_KEY_FILE.read_text().strip()

    result = refresh_id_token(refresh_token, api_key)

    TOKEN_FILE.write_text(result["id_token"])
    REFRESH_TOKEN_FILE.write_text(result["refresh_token"])
    return result["id_token"]


if __name__ == "__main__":
    token = main()
    print(f"Fresh token written to {TOKEN_FILE} (expires in ~1h).")
