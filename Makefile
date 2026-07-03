.PHONY: install run refresh-token sync sync-full

VENV := .venv/bin

install:
	python3 -m venv .venv
	$(VENV)/pip install -q -r requirements.txt

run:
	$(VENV)/uvicorn app:app --reload --port 8420

# Mints a fresh .liberty_rider_token from a saved Firebase refresh token
# (see firebase_refresh.py for the one-time capture procedure).
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
