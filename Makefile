.PHONY: install login-setup login login-password run refresh-token sync sync-full

VENV := .venv/bin

install:
	python3 -m venv .venv
	$(VENV)/pip install -q -r requirements.txt

# One-time setup for `make login` (installs Playwright's browser binary too).
login-setup:
	$(VENV)/pip install -q playwright
	$(VENV)/playwright install chromium

# Recommended: opens a real browser, you log in normally, tokens are
# captured automatically. Never sees your password. Run login-setup first.
login:
	$(VENV)/python3 login_playwright.py

# Alternative: type your email/password directly into this script instead of
# a browser. Reads Firebase's own login endpoint. See login_password.py's
# docstring for the security tradeoff before using this.
login-password:
	$(VENV)/python3 login_password.py --email "$(EMAIL)"

run:
	$(VENV)/uvicorn app:app --reload --port 8420

# Mints a fresh .liberty_rider_token from a saved Firebase refresh token
# (captured by `make login` or `make login-password`).
refresh-token:
	$(VENV)/python3 refresh_token_cli.py

# TOKEN can be passed explicitly (make sync TOKEN=...); otherwise falls back
# to .liberty_rider_token, refreshed first if a refresh token is available.
sync:
ifdef TOKEN
	$(VENV)/python3 sync.py --token "$(TOKEN)"
else
	@test -f .liberty_rider_refresh_token && $(MAKE) refresh-token || true
	$(VENV)/python3 sync.py --token "$$(cat .liberty_rider_token)"
endif

sync-full:
ifdef TOKEN
	$(VENV)/python3 sync.py --token "$(TOKEN)" --full
else
	@test -f .liberty_rider_refresh_token && $(MAKE) refresh-token || true
	$(VENV)/python3 sync.py --token "$$(cat .liberty_rider_token)" --full
endif
