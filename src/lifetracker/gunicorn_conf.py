"""Gunicorn hooks, loaded by docker-entrypoint.sh with
``--config python:lifetracker.gunicorn_conf``. Its other settings are on that
command line."""

import os
import sys

from lifetracker.core import client_ips, idle_watchdog

# Gunicorn's default access log format, with the client's IP first. The
# default's %(h)s is the connection's peer, which on Fly is always Fly Proxy;
# gunicorn never takes it from X-Forwarded-For. Fly Proxy sets Fly-Client-IP
# to the address the client connected from, which, unlike X-Forwarded-For,
# needs no parsing; without the proxy (e.g. locally) it's usually "-". Only
# for the log: nothing trusts it.
access_log_format = (
    '%({fly-client-ip}i)s %(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
)

# Read once here, in the master; workers inherit it.
_REQUEST_FILE = idle_watchdog.request_file_from_env()


def when_ready(server):
    # Runs once, in the master, after it starts listening; workers never run
    # it, so they never start a watchdog of their own.
    idle_watchdog.spawn(server.pid)
    client_ips.spawn(server.pid)


def post_request(worker, req, environ, resp):
    # Runs in the workers, after every request (even a failed one), so short
    # requests count as activity although the watchdog's checks rarely catch
    # their connections open.
    idle_watchdog.record_request(_REQUEST_FILE)


def on_exit(server):
    # Runs in the master as gunicorn shuts down. docker-entrypoint.sh creates
    # this file before stopping gunicorn when Envoy keeps failing, so the
    # container exits 1, a failure Fly restarts it after, rather than 0, which
    # Fly takes as a clean stop (as when the idle watchdog stops it).
    path = os.environ.get("LIFETRACKER_ENVOY_FAILED_FILE")
    if path and os.path.exists(path):
        server.log.error("Exiting with status 1, as Envoy kept failing")
        sys.exit(1)
