#!/usr/bin/env sh
set -eu

cd "${LIFETRACKER_APP_DIR:-/app}"

python manage.py upgrade_legacy_db
python manage.py migrate

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec python manage.py runserver \
  "${LIFETRACKER_HOST:-0.0.0.0}:${PORT:-8000}"
