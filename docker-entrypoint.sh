#!/usr/bin/env sh
set -eu

# Started as root (the default): give the lifetracker user/group the IDs from
# LIFETRACKER_UID/LIFETRACKER_GID, make sure it owns the data directory, then
# re-run this script as that user. Started with `docker run --user`, there's
# nothing to change (or permission to change it), so carry on as that user.
if [ "$(id -u)" = 0 ]; then
  if [ "$(id -g lifetracker)" != "$LIFETRACKER_GID" ]; then
    # -o: the requested GID may already belong to another group in the image
    # (e.g. 100 is users on both Debian and typical Linux hosts).
    groupmod -o -g "$LIFETRACKER_GID" lifetracker
  fi
  if [ "$(id -u lifetracker)" != "$LIFETRACKER_UID" ]; then
    usermod -o -u "$LIFETRACKER_UID" lifetracker
  fi

  # Only touch what isn't already owned correctly: a volume from before the
  # image ran as lifetracker has root-owned files, and a changed UID/GID
  # leaves files owned by the old IDs (usermod re-owns the home directory, but
  # groupmod doesn't).
  data_dir="${LIFETRACKER_DATA_DIR:-/data}"
  mkdir -p "$data_dir"
  find "$data_dir" /home/lifetracker \( ! -user lifetracker -o ! -group lifetracker \) \
    -exec chown -h lifetracker:lifetracker {} +

  # setpriv leaves HOME as /root, which lifetracker can't write to (gunicorn
  # puts its control socket there). usermod -u above re-owns the home directory.
  HOME=/home/lifetracker exec setpriv \
    --reuid=lifetracker --regid=lifetracker --init-groups "$0" "$@"
fi

cd "${LIFETRACKER_APP_DIR:-/app}"

python manage.py upgrade_legacy_db
python manage.py migrate
python manage.py ensure_superuser

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# lifetracker.gunicorn_conf starts the idle watchdog (see
# lifetracker/core/idle_watchdog.py) if LIFETRACKER_IDLE_STOP turns it on.
exec gunicorn \
  --config python:lifetracker.gunicorn_conf \
  --access-logfile - \
  --error-logfile - \
  --timeout "${LIFETRACKER_GUNICORN_TIMEOUT:-120}" \
  --bind \
  "${LIFETRACKER_HOST:-0.0.0.0}:${PORT:-8000}" \
  lifetracker.wsgi
