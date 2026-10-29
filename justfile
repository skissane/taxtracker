set minimum-version := '1.58.0'
# Dummy assignment, so every recipe run sets up the repo's local Git config
# (see scripts/git-config-setup.sh). `eager` makes just evaluate it even though
# nothing uses it, whether or not `set lazy` is on (casey/just#3759).
eager _ := shell('./scripts/git-config-setup.sh')

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

# Re-lock a Dependabot branch's uv.lock with this uv, from the base ref's uv.lock
dependabot-relock base:
    uv run --no-project python scripts/dependabot_relock.py {{base}}

# Check that uv.lock only has wheels for supported OS/CPU combinations
lock-os-arch-check:
    uv run --no-project python scripts/validate_uv_lock_os_arch.py

# Check Git-tracked text files for trailing whitespace and bad newlines
whitespace-check:
    uv run --no-project python scripts/check_whitespace.py

# Check that screenshots/ has nothing tracked but its unchanged .gitignore
screenshots-dir-check:
    uv run --no-project python scripts/check_screenshots_dir.py

# Check .git-blame-ignore-revs lists real commits that are ancestors of target
blame-ignore-revs-check target="HEAD":
    uv run --no-project python scripts/check_blame_ignore_revs.py {{quote(target)}}

# Lint Python code with ruff
lint:
    uv run --group dev ruff check .

# Autofix Python lint issues with ruff
lint-fix:
    uv run --group dev ruff check --fix --exit-zero .

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

# djlint --reformat exits 1 when it changes a file, so then check the result
# instead, which fails only if something is still wrong.

# Format Django templates with djlint
fmt-templates:
    uv run --group dev djlint --reformat . || uv run --group dev djlint --check .

# Run the test suite in parallel with pytest-xdist
test:
    uv run --group dev pytest

# Run the test suite in parallel, print a coverage report, and write coverage.xml and junit.xml
coverage:
    uv run --group dev pytest --cov=src/lifetracker --cov=scripts --cov-report=term --cov-report=xml --junitxml=junit.xml -o junit_family=legacy

# patch-coverage's first argument is the branch to compare with (default:
# origin/$GITHUB_BASE_REF on a PR, else origin/master); any more go to
# diff-cover, e.g. `--format markdown:patch-coverage.md`, as CI passes to post
# the report as a PR comment.

# Show patch coverage from coverage.xml against a base branch (or PR base)
[positional-arguments]
patch-coverage compare_branch="" *args:
    compare_branch="${1:-${GITHUB_BASE_REF:+origin/$GITHUB_BASE_REF}}"; shift $(( $# > 0 )); uv run --group dev diff-cover coverage.xml --compare-branch="${compare_branch:-origin/master}" "$@"

# The screenshot tests live outside `testpaths`, so plain `pytest` (and
# `just test`/`just coverage`) never collects them; `-n 0` because two tests
# don't justify a worker per core. `playwright install` is a quick no-op once
# its Chromium is downloaded. The admin header shows LIFETRACKER_COMMIT_ID, so
# it's set to HEAD unless already set; CI sets it to a PR's head commit, because
# on a PR actions/checkout checks out a merge commit instead.

# Screenshot admin screens into screenshots/ (needs a Playwright Chromium)
screenshots:
    uv run --group screenshots playwright install chromium
    LIFETRACKER_COMMIT_ID="${LIFETRACKER_COMMIT_ID:-$(git rev-parse HEAD)}" uv run --group dev --group screenshots pytest screenshot_tests -n 0

# screenshots-post's arguments are a PR number and, optionally, --commit and
# --run-url for the comment's "Taken for" line (see --help for the rest); with
# --commit, it posts nothing unless that's still the PR's head commit. It
# uploads the images with $SCREENSHOTS_TOKEN (a user token) and comments with
# $GITHUB_TOKEN, each defaulting to `gh auth token`, so locally it posts as
# you. It edits the previous screenshot comment by the same author, found by a
# hidden marker, rather than posting another.

# Post screenshots/ to a PR as a comment, or update the existing one
[positional-arguments]
screenshots-post *args:
    uv run --no-project python -m scripts.post_screenshots "$@"

# patch-coverage-post's arguments are a PR number and, optionally, --report
# (default patch-coverage.md, from `just patch-coverage '' --format
# markdown:patch-coverage.md`), --commit and --run-url (see --help for the
# rest); like screenshots-post, it comments with $GITHUB_TOKEN (default `gh auth
# token`), posts nothing unless --commit is still the PR's head, and edits its
# previous comment rather than posting another.

# Post a patch coverage report to a PR as a comment, or update the existing one
[positional-arguments]
patch-coverage-post *args:
    uv run --no-project python -m scripts.post_patch_coverage "$@"

# Build the container image
docker-build:
    docker build --build-arg LIFETRACKER_COMMIT_ID="$(git rev-parse HEAD)" -t lifetracker .

# docker-smoke runs the image as production does (DEBUG off), with a throwaway
# data volume, waits up to a minute for the login page through the published
# port, checks that /healthz/ answers without a login and that Envoy, not
# Django (which would 404), redirects a request for any other page without a
# JWT to the login page, then checks from inside the container that Envoy's
# port answers on the container's own address and gunicorn's (8001) doesn't,
# and waits up to 30 seconds for the client IP collector to record the
# requests (without downloading the DB-IP databases, to spare db-ip.com a
# download every CI run).
# It prints the container's log if anything fails.

# Start the image from docker-build and check requests reach Django through Envoy
docker-smoke:
    #!/usr/bin/env sh
    set -eu
    name=lifetracker-smoke
    docker run -d --rm --name "$name" -p 127.0.0.1::8000 -e LIFETRACKER_ALLOWED_HOSTS=127.0.0.1 -e LIFETRACKER_INITIAL_ADMIN_PASSWORD=smoke-test -e LIFETRACKER_GEOIP_DOWNLOAD=0 lifetracker >/dev/null
    trap 'status=$?; [ "$status" -eq 0 ] || docker logs "$name"; docker rm -f "$name" >/dev/null' EXIT
    base="http://$(docker port "$name" 8000/tcp | head -n 1)"
    url="$base/admin/login/"
    i=0
    until curl -fs -o /dev/null "$url"; do
      i=$((i + 1))
      if [ "$i" -ge 60 ]; then
        echo "docker-smoke: no response from $url" >&2
        exit 1
      fi
      sleep 1
    done
    curl -fs -o /dev/null "$base/healthz/"
    redirect="$(curl -s -o /dev/null -w '%{http_code} %header{location}' "$base/no-such-page/?a=b")"
    if [ "$redirect" != "302 /admin/login/?next=/no-such-page/%3Fa%3Db" ]; then
      echo "docker-smoke: expected Envoy's login redirect, got: $redirect" >&2
      exit 1
    fi
    docker exec "$name" python -c 'import socket; ip = socket.gethostbyname(socket.gethostname()); socket.create_connection((ip, 8000)).close(); s = socket.socket(); assert s.connect_ex((ip, 8001)) != 0, "gunicorn is reachable from outside the container"'
    i=0
    until docker exec -u lifetracker "$name" python -c 'import sqlite3, sys; sys.exit(not sqlite3.connect("file:/data/client_ips.sqlite3?mode=ro", uri=True).execute("SELECT count(*) FROM client_ip").fetchone()[0])' 2>/dev/null; do
      i=$((i + 1))
      if [ "$i" -ge 30 ]; then
        echo "docker-smoke: no client IPs recorded" >&2
        exit 1
      fi
      sleep 1
    done
    echo "docker-smoke: OK"

# Host config directory (secret key, initial admin password) that `just
# docker-run` mounts read-only, the same one ./run.sh uses by default. Made
# absolute (a relative path is taken from the repo root), since docker treats a
# relative -v source as a named volume.
#
# docker-run first imports settings on the host (`manage.py check`), which
# creates this directory and the secret key as the current user if missing,
# then runs `manage.py ensure_jwt_key` there, which creates the JWT key pair.
# Otherwise the Docker daemon would create a missing directory as root on
# Linux, and the container couldn't generate the keys in a read-only mount.
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
    LIFETRACKER_CONFIG_DIR="{{ config_dir }}" uv run python manage.py ensure_jwt_key
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

# Lint the GitHub Actions workflows with jactionlint
lint-workflows:
    uv run --group dev jactionlint --profile correctness

# Run every whitespace, screenshots/, blame-ignore-revs, ruff, djlint and jactionlint check (no fixes, no tests)
check: whitespace-check screenshots-dir-check blame-ignore-revs-check lint fmt-check lint-templates fmt-check-templates lint-workflows

# Autofix lint issues and reformat code/templates
fix: lint-fix fmt fmt-templates

# Run everything the CI workflow runs
ci: lock-check lock-os-arch-check sync requirements-check check coverage docker-build docker-smoke
