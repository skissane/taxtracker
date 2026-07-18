# List available recipes
default:
    @just --list

# Check that uv.lock is up to date with pyproject.toml
lock-check:
    uv lock --check

# Sync dependencies (including dev group)
sync:
    uv sync --group dev

# Lint Python code with ruff
lint:
    uv run --group dev ruff check .

# Autofix Python lint issues with ruff
lint-fix:
    uv run --group dev ruff check --fix .

# Check Python formatting with ruff
fmt-check:
    uv run --group dev ruff format --check .

# Format Python code with ruff
fmt:
    uv run --group dev ruff format .

# Lint Django templates with djlint
lint-templates:
    uv run --group dev djlint --lint .

# Check Django template formatting with djlint
fmt-check-templates:
    uv run --group dev djlint --check .

# Format Django templates with djlint
fmt-templates:
    uv run --group dev djlint --reformat .

# Run the Django test suite
test:
    uv run python manage.py test tests.core tests.taxtracker tests.cli --verbosity=2

# Run the test suite under coverage, print a report, and write coverage.xml
coverage:
    uv run --group dev coverage run manage.py test tests.core tests.taxtracker tests.cli --verbosity=2
    uv run --group dev coverage combine
    uv run --group dev coverage report
    uv run --group dev coverage xml

# Run every ruff/djlint lint and format-check step (no fixes, no tests)
check: lint fmt-check lint-templates fmt-check-templates

# Autofix lint issues and reformat code/templates
fix: lint-fix fmt fmt-templates

# Run everything the CI workflow runs
ci: lock-check sync check coverage
