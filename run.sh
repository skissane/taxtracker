#!/usr/bin/env bash
set -eu

uv sync
uv run python manage.py upgrade_legacy_db
uv run python manage.py migrate
uv run python manage.py ensure_superuser
uv run python manage.py runserver
