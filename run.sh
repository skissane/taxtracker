#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"

# Load ./.env (dotenv syntax, parsed by uv, not executed by the shell) into
# every `uv run` below, unless UV_ENV_FILE already points somewhere else.
# Variables already set in the environment take precedence over the file.
if [ -f .env ] && [ -z "${UV_ENV_FILE:-}" ]; then
    export UV_ENV_FILE=.env
fi

export LIFETRACKER_COMMIT_ID="$(git rev-parse HEAD)"

uv sync
uv run python manage.py upgrade_legacy_db
uv run python manage.py migrate
uv run python manage.py ensure_superuser
uv run python manage.py runserver
