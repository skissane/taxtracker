"""Gunicorn hooks, loaded by docker-entrypoint.sh with
``--config python:lifetracker.gunicorn_conf``. Its other settings are on that
command line."""

from lifetracker.core import idle_watchdog


def when_ready(server):
    # Runs once, in the master, after it starts listening; workers never run
    # it, so they never start a watchdog of their own.
    idle_watchdog.spawn(server.pid)
