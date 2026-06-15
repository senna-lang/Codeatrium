VENV := .venv/bin

# ruff: prefer the project venv, fall back to `uvx ruff` when no venv is present
RUFF := $(shell [ -x .venv/bin/ruff ] && echo .venv/bin/ruff || echo "uvx ruff")

.PHONY: test lint fmt typecheck check hooks

test:
	$(VENV)/pytest tests/ -v

lint:
	$(RUFF) check src/ tests/

fmt:
	$(RUFF) format src/ tests/

typecheck:
	$(VENV)/pyright src/

check: lint typecheck test

hooks:
	@echo '#!/bin/sh\nmake check' > .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "pre-commit hook installed: runs make check before every commit"
