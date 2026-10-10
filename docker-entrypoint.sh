#!/usr/bin/env sh
set -eu

cd "${LIFETRACKER_APP_DIR:-/app}"

python manage.py upgrade_legacy_db
python manage.py migrate
python manage.py ensure_superuser

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec gunicorn \
  --access-logfile - \
  --error-logfile - \
  --timeout "${LIFETRACKER_GUNICORN_TIMEOUT:-120}" \
  --bind \
  "${LIFETRACKER_HOST:-0.0.0.0}:${PORT:-8000}" \
  lifetracker.wsgi
