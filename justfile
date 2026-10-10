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

# Run the test suite in parallel with pytest-xdist
test:
    uv run --group dev pytest

# Run the test suite in parallel, print a coverage report, and write coverage.xml
coverage:
    uv run --group dev pytest --cov=src/lifetracker --cov-report=term --cov-report=xml

# Build the container image
docker-build:
    docker build -t lifetracker .

# Host config directory (secret key, initial admin password) that `just
# docker-run` mounts read-only, the same one ./run.sh uses by default. Made
# absolute (a relative path is taken from the repo root), since docker treats a
# relative -v source as a named volume.
#
# docker-run first imports settings on the host (`manage.py check`), which
# creates this directory and the secret key as the current user if missing.
# Otherwise the Docker daemon would create a missing directory as root on
# Linux, and the container couldn't generate the key in a read-only mount.
config_dir := absolute_path(env("LIFETRACKER_CONFIG_DIR", env("HOME") / ".config/lifetracker"))

# Run the app container locally (DEBUG on, loopback only, host config read-only)
docker-run:
    LIFETRACKER_CONFIG_DIR="{{ config_dir }}" uv run python manage.py check
    docker run --rm --name=lifetracker -p 127.0.0.1:8000:8000 -e LIFETRACKER_DEBUG=1 -v lifetracker-data:/data -v "{{ config_dir }}:/config:ro" -e LIFETRACKER_CONFIG_DIR=/config lifetracker

# Run every ruff/djlint lint and format-check step (no fixes, no tests)
check: lint fmt-check lint-templates fmt-check-templates

# Autofix lint issues and reformat code/templates
fix: lint-fix fmt fmt-templates

# Run everything the CI workflow runs
ci: lock-check sync check coverage docker-build
