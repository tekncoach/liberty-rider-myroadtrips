"""Capture Liberty Rider auth tokens by watching a real login, automatically.

Opens a real (visible) browser window on liberty-rider.com. You log in
exactly as you normally would — your email and password go straight to
Liberty Rider's own login page and are never seen by this script. Once
you're logged in, this script watches network traffic for the resulting
Bearer token and the underlying Firebase refresh token, and writes them to
the same files the rest of the app already reads:

  .liberty_rider_token              (fresh ~1h Bearer token)
  .liberty_rider_refresh_token      (long-lived, used by `make refresh-token`)
  .liberty_rider_firebase_api_key   (Firebase project key, public but needed
                                      alongside the refresh token)

Requires Playwright with a Chromium build installed once:
  pip install playwright
  playwright install chromium

Usage:
  python3 login_playwright.py
"""
from __future__ import annotations

import pathlib
import re
import time

ROOT = pathlib.Path(__file__).parent
TOKEN_FILE = ROOT / ".liberty_rider_token"
REFRESH_TOKEN_FILE = ROOT / ".liberty_rider_refresh_token"
API_KEY_FILE = ROOT / ".liberty_rider_firebase_api_key"

LOGIN_TIMEOUT_SECONDS = 300

# See firebase_refresh.py for what this reads and why.
INDEXEDDB_JS = """
() => new Promise((resolve) => {
  const req = indexedDB.open("firebaseLocalStorageDb");
  req.onsuccess = () => {
    const db = req.result;
    if (!db.objectStoreNames.contains("firebaseLocalStorage")) { resolve(null); return; }
    const tx = db.transaction("firebaseLocalStorage", "readonly");
    tx.objectStore("firebaseLocalStorage").getAll().onsuccess = (e) => resolve(e.target.result);
  };
  req.onerror = () => resolve(null);
})
"""


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright isn't installed. Run:\n"
            "  pip install playwright && playwright install chromium"
        )

    captured: dict = {}

    def on_request(request) -> None:
        url = request.url
        if "api.liberty-rider.com/graphql" in url and "token" not in captured:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                captured["token"] = auth[len("Bearer "):]
        if "api_key" not in captured and (
            "securetoken.googleapis.com" in url or "identitytoolkit.googleapis.com" in url
        ):
            match = re.search(r"[?&]key=([^&]+)", url)
            if match:
                captured["api_key"] = match.group(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        page.on("request", on_request)
        page.goto("https://liberty-rider.com")

        print("Log in via the site's login button — this script never sees your password.")
        print(f"Waiting up to {LOGIN_TIMEOUT_SECONDS}s for a token to appear...")

        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while "token" not in captured:
            if page.is_closed():
                raise SystemExit("Browser closed before a token was captured.")
            if time.monotonic() > deadline:
                browser.close()
                raise SystemExit("Timed out waiting for login. Try again?")
            time.sleep(1)

        TOKEN_FILE.write_text(captured["token"])
        print(f"Bearer token captured -> {TOKEN_FILE}")

        refresh_token = None
        for entry in page.evaluate(INDEXEDDB_JS) or []:
            refresh_token = (entry.get("value") or {}).get("stsTokenManager", {}).get("refreshToken")
            if refresh_token:
                break
        if refresh_token:
            REFRESH_TOKEN_FILE.write_text(refresh_token)
            print(f"Refresh token captured -> {REFRESH_TOKEN_FILE}")
        if "api_key" in captured:
            API_KEY_FILE.write_text(captured["api_key"])
            print(f"Firebase API key captured -> {API_KEY_FILE}")

        browser.close()

    if refresh_token and "api_key" in captured:
        print("\nAll set — `make refresh-token` will now mint fresh tokens automatically.")
    else:
        print("\nBearer token captured, but the refresh token wasn't found — you'll need to")
        print("re-run this script (or the manual DevTools steps in the README) once it expires.")


if __name__ == "__main__":
    main()
