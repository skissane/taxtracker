"""Gunicorn hooks, loaded by docker-entrypoint.sh with
``--config python:lifetracker.gunicorn_conf``. Its other settings are on that
command line."""

from lifetracker.core import idle_watchdog

# Read once here, in the master; workers inherit it.
_REQUEST_FILE = idle_watchdog.request_file_from_env()


def when_ready(server):
    # Runs once, in the master, after it starts listening; workers never run
    # it, so they never start a watchdog of their own.
    idle_watchdog.spawn(server.pid)


def post_request(worker, req, environ, resp):
    # Runs in the workers, after every request (even a failed one), so short
    # requests count as activity although the watchdog's checks rarely catch
    # their connections open.
    idle_watchdog.record_request(_REQUEST_FILE)
