.PHONY: install run test hooks

VENV := .venv/bin

install:
	python3 -m venv .venv
	$(VENV)/pip install -q -r requirements.txt

run:
	$(VENV)/uvicorn app:app --reload --port 8420

test:
	$(VENV)/pip install -q -r requirements-dev.txt
	$(VENV)/pytest

# Copies the pre-commit hook into the shared git hooks directory. Not a
# symlink: a worktree on a branch without hooks/ would break the link.
hooks:
	@cp hooks/pre-commit "$$(git rev-parse --git-common-dir)/hooks/pre-commit"
	@chmod +x "$$(git rev-parse --git-common-dir)/hooks/pre-commit"
	@echo "pre-commit hook installed"
