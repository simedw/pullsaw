.PHONY: lint format check test all

# Run all checks (ruff, mypy, tests)
all: lint test

# Lint and type check
lint:
	uv run ruff check .
	uv run mypy pullsaw/

# Format code
format:
	uv run ruff format .

# Check formatting without modifying
check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy pullsaw/

# Run tests
test:
	uv run pytest tests/

# Fix auto-fixable lint issues and format
fix:
	uv run ruff check --fix .
	uv run ruff format .
