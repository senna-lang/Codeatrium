VENV := .venv/bin

.PHONY: test lint fmt check

test:
	$(VENV)/pytest tests/ -v

lint:
	$(VENV)/ruff check src/ tests/

fmt:
	$(VENV)/ruff format src/ tests/

check: lint test
