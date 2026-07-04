.PHONY: install run test

VENV := .venv/bin

install:
	python3 -m venv .venv
	$(VENV)/pip install -q -r requirements.txt

run:
	$(VENV)/uvicorn app:app --reload --port 8420

test:
	$(VENV)/pip install -q -r requirements-dev.txt
	$(VENV)/pytest
