# List available recipes
default:
    @just --list

# Check that uv.lock is up to date with pyproject.toml
lock-check:
    uv lock --check

# Sync dependencies (including dev group)
sync:
    uv sync --group dev

# Check that dependency minimums in pyproject.toml match uv.lock
requirements-check:
    uv run --no-project python scripts/requirements_min_versions.py check

# Rewrite dependency minimums in pyproject.toml to the versions in uv.lock
requirements-fix:
    uv run --no-project python scripts/requirements_min_versions.py fix

# Check that uv.lock only has wheels for supported OS/CPU combinations
lock-os-arch-check:
    uv run --no-project python scripts/validate_uv_lock_os_arch.py

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

# Run the test suite in parallel, print a coverage report, and write coverage.xml and junit.xml
coverage:
    uv run --group dev pytest --cov=src/lifetracker --cov=scripts --cov-report=term --cov-report=xml --junitxml=junit.xml -o junit_family=legacy

# The screenshot tests live outside `testpaths`, so plain `pytest` (and
# `just test`/`just coverage`) never collects them; `-n 0` because two tests
# don't justify a worker per core. `playwright install` is a quick no-op once
# its Chromium is downloaded.

# Screenshot admin screens into screenshots/ (needs a Playwright Chromium)
screenshots:
    uv run --group screenshots playwright install chromium
    uv run --group dev --group screenshots pytest screenshot_tests -n 0

# screenshots-post's arguments are a PR number and, optionally, --commit and
# --run-url for the comment's "Taken for" line. It posts as whoever gh is
# logged in as (GH_TOKEN in CI), and needs a gh with `--attach` (2.102.0 has
# it). It deletes that user's previous screenshot comment on the PR, found by a
# hidden marker, once the new one is up.

# Post screenshots/ to a PR as a comment, replacing the previous one
[positional-arguments]
screenshots-post *args:
    uv run --no-project python scripts/post_screenshots.py "$@"

# Build the container image
docker-build:
    docker build --build-arg LIFETRACKER_COMMIT_ID="$(git rev-parse HEAD)" -t lifetracker .

# Host config directory (secret key, initial admin password) that `just
# docker-run` mounts read-only, the same one ./run.sh uses by default. Made
# absolute (a relative path is taken from the repo root), since docker treats a
# relative -v source as a named volume.
#
# docker-run first imports settings on the host (`manage.py check`), which
# creates this directory and the secret key as the current user if missing.
# Otherwise the Docker daemon would create a missing directory as root on
# Linux, and the container couldn't generate the key in a read-only mount.
#
# The container runs as your host UID/GID (LIFETRACKER_UID/LIFETRACKER_GID),
# since the secret key is only readable by its owner (mode 0600).
config_dir := absolute_path(env("LIFETRACKER_CONFIG_DIR", env("HOME") / ".config/lifetracker"))

# docker-run's arguments replace the Gunicorn server with a command, run after
# migrations, e.g. `just docker-run python manage.py shell`. -t only with a
# terminal on stdin, so it still works piped or in CI. The host port is
# LIFETRACKER_DOCKER_PORT (default 8000). It can't run alongside a running
# server container (same container name); use docker-shell for that.
# Like run.sh, it passes ./.env to the container (--env-file) if present, e.g.
# for the LIFETRACKER_BACKUP_S3_* settings; the recipe's own -e flags win over
# the file. It sets LIFETRACKER_BACKUP_HOSTNAME to docker.<host's hostname>, since
# the container's own hostname is a meaningless ID and its database is separate
# from local dev's, so their cloud backups need distinct prefixes.

# Run the app container locally (DEBUG on, loopback only, host config read-only)
[positional-arguments]
docker-run *args:
    LIFETRACKER_CONFIG_DIR="{{ config_dir }}" uv run python manage.py check
    docker run -i $([ -t 0 ] && echo -t) --rm --name=lifetracker -p 127.0.0.1:${LIFETRACKER_DOCKER_PORT:-8000}:8000 $([ -f .env ] && echo --env-file .env) -e LIFETRACKER_DEBUG=1 -e LIFETRACKER_BACKUP_HOSTNAME="docker.$(hostname)" -v lifetracker-data:/data -v "{{ config_dir }}:/config:ro" -e LIFETRACKER_CONFIG_DIR=/config -e LIFETRACKER_UID="$(id -u)" -e LIFETRACKER_GID="$(id -g)" lifetracker "$@"

# docker-shell's arguments replace bash with a command run in the running
# container, e.g. `just docker-shell python manage.py shell`. -t only with a
# terminal on stdin, as in docker-run. The default and the exec share one line
# since just runs each recipe line in its own shell.

# Open a bash shell (or run a command) in the running container from docker-run
[positional-arguments]
docker-shell *args:
    [ $# -gt 0 ] || set -- /bin/bash; docker exec -i $([ -t 0 ] && echo -t) -u lifetracker lifetracker "$@"

# Create whatever's missing of the Fly app, volume and secrets from .env
[positional-arguments]
fly-setup *args:
    ./fly-setup.sh "$@"

# Run every ruff/djlint lint and format-check step (no fixes, no tests)
check: lint fmt-check lint-templates fmt-check-templates

# Autofix lint issues and reformat code/templates
fix: lint-fix fmt fmt-templates

# Run everything the CI workflow runs
ci: lock-check lock-os-arch-check sync requirements-check check coverage docker-build
