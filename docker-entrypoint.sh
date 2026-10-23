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
  python manage.py ensure_jwt_key
  exec "$@"
fi

# Envoy listens on $PORT and passes requests on to gunicorn, which listens
# only on loopback, so nothing outside the container reaches it directly (issue
# #178). It rejects requests without a valid JWT from Django, except a few
# that need no login (issue #179; envoy/envoy.yaml). Envoy can't read
# environment variables, and its config needs Django's settings and URLconf,
# so `manage.py envoy_config` writes it from the template, after checking the
# settings below. It also creates the JWT signing key and the JWKS Envoy reads
# if they're missing, as ensure_jwt_key does when there are arguments.
gunicorn_port="${LIFETRACKER_GUNICORN_PORT:-8001}"
envoy_config="${TMPDIR:-/tmp}/lifetracker-envoy.yaml"
python manage.py envoy_config envoy/envoy.yaml "$envoy_config" \
  --listen-host "${LIFETRACKER_HOST:-0.0.0.0}" \
  --listen-port "${PORT:-8000}" \
  --upstream-port "$gunicorn_port"

# Run Envoy in the background, restarting it a second after it exits. This
# process becomes gunicorn below, the container's main process, so gunicorn
# exiting (e.g. the idle watchdog stopping the Machine) ends the container,
# Envoy included. Envoy starts first, but retries connecting to gunicorn for a
# while (see envoy/envoy.yaml), so requests that arrive meanwhile wait.
# One worker thread (--concurrency) is plenty on a shared-cpu-1x Machine.
# Hot restart is off, as nothing uses it and it needs shared memory.
#
# If Envoy exits within a minute of starting 5 times in a row (e.g. a bad
# config), restarting it won't help, and nothing can reach gunicorn, so give
# up: create the file below and stop gunicorn, whose on_exit hook
# (lifetracker/gunicorn_conf.py) then makes it exit 1 instead of 0. Fly's
# on-failure restart policy then restarts the Machine (up to 10 times), and
# the failure shows in the Machine's exit events, unlike an idle stop.
export LIFETRACKER_ENVOY_FAILED_FILE="${TMPDIR:-/tmp}/lifetracker-envoy-failed"
rm -f "$LIFETRACKER_ENVOY_FAILED_FILE"
(
  failures=0
  while :; do
    started="$(date +%s)"
    envoy \
      --config-path "$envoy_config" \
      --concurrency 1 \
      --disable-hot-restart \
      --log-level warn &&
      status=0 || status=$?
    echo "docker-entrypoint.sh: envoy exited with status $status" >&2
    if [ "$(($(date +%s) - started))" -lt 60 ]; then
      failures=$((failures + 1))
    else
      failures=1
    fi
    if [ "$failures" -ge 5 ]; then
      echo "docker-entrypoint.sh: envoy exited within a minute of starting" \
        "5 times in a row; stopping gunicorn" >&2
      : >"$LIFETRACKER_ENVOY_FAILED_FILE"
      kill -TERM "$$"
      exit 1
    fi
    sleep 1
  done
) &

# lifetracker.gunicorn_conf starts the idle watchdog (see
# lifetracker/core/idle_watchdog.py) if LIFETRACKER_IDLE_STOP turns it on.
# gthread, not the default single sync worker: browsers open spare connections
# and leave them idle, and a sync worker that accepts one blocks on it, leaving
# real requests queued until it gets a request or times out (issue #172).
# gthread also holds a thread for up to 5 seconds on a new connection before
# parking it, so there are enough threads for a few idle ones at once.
exec gunicorn \
  --config python:lifetracker.gunicorn_conf \
  --worker-class gthread \
  --threads "${LIFETRACKER_GUNICORN_THREADS:-8}" \
  --access-logfile - \
  --error-logfile - \
  --timeout "${LIFETRACKER_GUNICORN_TIMEOUT:-120}" \
  --bind \
  "127.0.0.1:$gunicorn_port" \
  lifetracker.wsgi
