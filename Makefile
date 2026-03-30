VENV := .venv/bin

.PHONY: test lint fmt typecheck check hooks

test:
	$(VENV)/pytest tests/ -v

lint:
	$(VENV)/ruff check src/ tests/

fmt:
	$(VENV)/ruff format src/ tests/

typecheck:
	$(VENV)/pyright src/

check: lint typecheck test

hooks:
	@echo '#!/bin/sh\nmake check' > .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "pre-commit hook installed: runs make check before every commit"
