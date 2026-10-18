"""Stop gunicorn, and so the container, once nothing has connected for a while.

On Fly.io the Machine stops when its main process exits cleanly, and Fly Proxy
starts it again on the next request, but the proxy can't see SSH sessions (they
bypass it), so its own auto-stop can stop the Machine mid-session (issue #135).
This watchdog replaces it: gunicorn's ``when_ready`` hook (``gunicorn_conf``)
starts it as a separate process via ``spawn``, and every check it counts the
established TCP connections to the monitored local ports (the app's and SSH's)
in ``/proc/net/tcp`` and ``/proc/net/tcp6``. A check only sees the connections
open at that instant, which catches SSH sessions but misses most HTTP requests
(gunicorn's sync workers close the connection after each one), so gunicorn's
``post_request`` hook also touches a file (``record_request``), and a newer
modification time than the last activity counts as activity too. Once there's
been no activity for the idle timeout, it sends gunicorn's master SIGTERM,
which shuts gunicorn down gracefully and exits 0.

It's a process rather than a thread in the master because the master forks
replacement workers after startup, and forking a multi-threaded process can
deadlock the child. It's started with ``subprocess`` rather than ``os.fork()``
so it inherits neither gunicorn's signal handlers nor its listening sockets.
It doesn't import Django.

Configured by environment variables (see ``config_from_env``); off unless
``LIFETRACKER_IDLE_STOP`` is ``monitor`` (check and report, never stop) or
``stop``. Linux only, as it reads ``/proc/net``; the Docker image is Linux
whatever the host. Every check rewrites a JSON status file (``Watchdog.status``)
for an admin page to show.
"""

import dataclasses
import datetime
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

MODES = ("off", "monitor", "stop")
DEFAULT_IDLE_MINUTES = 10
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_STATUS_FILE = "/tmp/lifetracker-idle-watchdog.json"
DEFAULT_REQUEST_FILE = "/tmp/lifetracker-last-request"
SSH_PORT = 22
PROC_NET_TCP_FILES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
# The `st` column of /proc/net/tcp{,6}: 01 is TCP_ESTABLISHED (0A is LISTEN).
TCP_ESTABLISHED = "01"


@dataclasses.dataclass(frozen=True)
class Config:
    mode: str
    idle_seconds: float
    interval_seconds: float
    ports: tuple[int, ...]
    status_file: Path
    request_file: Path


def _positive_number(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = 0
    if not value > 0:
        raise ValueError(f"{name} must be a positive number, not {raw!r}")
    return value


def config_from_env(environ: Mapping[str, str] = os.environ) -> Config:
    """Read the watchdog's settings, raising ValueError if any is invalid.

    - ``LIFETRACKER_IDLE_STOP``: ``off`` (the default, also when blank),
      ``monitor`` or ``stop``.
    - ``LIFETRACKER_IDLE_STOP_MINUTES``: idle timeout (default 10).
    - ``LIFETRACKER_IDLE_STOP_PORTS``: comma-separated local ports to watch
      (default gunicorn's ``$PORT``, else 8000, and 22 for SSH).
    - ``LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS``: time between checks
      (default 30).
    - ``LIFETRACKER_IDLE_STOP_STATUS_FILE``: where to write the status JSON
      (default ``/tmp/lifetracker-idle-watchdog.json``).
    - ``LIFETRACKER_IDLE_STOP_REQUEST_FILE``: the file gunicorn's workers touch
      after each request (default ``/tmp/lifetracker-last-request``).
    """
    mode = environ.get("LIFETRACKER_IDLE_STOP", "").strip().lower() or "off"
    if mode not in MODES:
        raise ValueError(
            f"LIFETRACKER_IDLE_STOP must be one of {', '.join(MODES)}, not {mode!r}"
        )
    idle_minutes = _positive_number(
        environ, "LIFETRACKER_IDLE_STOP_MINUTES", DEFAULT_IDLE_MINUTES
    )
    interval_seconds = _positive_number(
        environ, "LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS
    )
    raw_ports = environ.get("LIFETRACKER_IDLE_STOP_PORTS", "").strip()
    if not raw_ports:
        raw_ports = f"{environ.get('PORT', '').strip() or 8000},{SSH_PORT}"
    try:
        ports = tuple(dict.fromkeys(int(port) for port in raw_ports.split(",")))
    except ValueError:
        ports = ()
    if not ports or not all(0 < port < 65536 for port in ports):
        raise ValueError(
            "LIFETRACKER_IDLE_STOP_PORTS must be comma-separated port numbers,"
            f" not {raw_ports!r}"
        )
    status_file = Path(
        environ.get("LIFETRACKER_IDLE_STOP_STATUS_FILE", "").strip()
        or DEFAULT_STATUS_FILE
    )
    request_file = Path(
        environ.get("LIFETRACKER_IDLE_STOP_REQUEST_FILE", "").strip()
        or DEFAULT_REQUEST_FILE
    )
    return Config(
        mode=mode,
        idle_seconds=idle_minutes * 60,
        interval_seconds=interval_seconds,
        ports=ports,
        status_file=status_file,
        request_file=request_file,
    )


def request_file_from_env(environ: Mapping[str, str] = os.environ) -> Path | None:
    """The file for ``record_request`` to touch, or None if the watchdog is
    off or misconfigured (``spawn`` logs that)."""
    try:
        config = config_from_env(environ)
    except ValueError:
        return None
    return None if config.mode == "off" else config.request_file


def record_request(path: Path | None) -> None:
    """Mark that a request has just finished, for the watchdog to see. Called
    by every gunicorn worker after every request, so it's one cheap system call
    and never raises."""
    if path is None:
        return
    try:
        path.touch()
    except OSError:
        pass


def last_request_time(path: Path) -> float | None:
    """When ``record_request`` last touched ``path``, or None if never."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def established_local_ports(text: str) -> Iterable[int]:
    """Yield the local port of each established connection in a /proc/net/tcp
    (or tcp6) table, e.g. ``0: 0100007F:1F40 0100007F:A2C4 01 ...`` gives 8000.
    """
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 4 and fields[3] == TCP_ESTABLISHED:
            yield int(fields[1].rsplit(":", 1)[1], 16)


def count_connections(
    ports: Iterable[int], paths: Iterable[Path] = PROC_NET_TCP_FILES
) -> dict[int, int]:
    """Count established connections to each of ``ports``, over IPv4 and IPv6.

    A missing table (e.g. no IPv6) counts as no connections.
    """
    counts = dict.fromkeys(ports, 0)
    for path in paths:
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        for port in established_local_ports(text):
            if port in counts:
                counts[port] += 1
    return counts


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(timestamp, datetime.UTC).isoformat(
        timespec="seconds"
    )


class Watchdog:
    """Idle tracking, kept apart from the clock and /proc so it's testable."""

    def __init__(self, config: Config, master_pid: int, started_at: float):
        self.config = config
        self.master_pid = master_pid
        self.started_at = started_at
        # Idle time counts from startup until the first connection is seen.
        self.idle_since = started_at
        self.checked_at: float | None = None
        self.counts = dict.fromkeys(config.ports, 0)
        self.last_seen: dict[int, float | None] = dict.fromkeys(config.ports)
        self.last_request: float | None = None
        self.stopping = False

    def check(
        self, now: float, counts: Mapping[int, int], last_request: float | None
    ) -> bool:
        """Record one check's connection counts and the request file's
        modification time; return whether it's time to stop (whatever the
        mode)."""
        self.checked_at = now
        self.counts = dict(counts)
        for port, count in counts.items():
            if count:
                self.last_seen[port] = now
                self.idle_since = now
        self.last_request = last_request
        # Only a request newer than the current idle start counts, so one left
        # from before this watchdog started (e.g. a previous run's, as /tmp
        # outlives it outside Fly) is ignored.
        if last_request is not None and last_request > self.idle_since:
            self.idle_since = min(last_request, now)
        return not any(counts.values()) and self.idle_due(now)

    def idle_due(self, now: float) -> bool:
        return now - self.idle_since >= self.config.idle_seconds

    def status(self) -> dict:
        """The status file's contents. Times are ISO 8601 UTC; ``stop_due_at``
        is when the idle timeout runs out if nothing connects (in ``monitor``
        mode nothing happens then)."""
        if self.stopping:
            state = "stopping"
        elif any(self.counts.values()):
            state = "active"
        else:
            state = "idle"
        return {
            "mode": self.config.mode,
            "state": state,
            "pid": os.getpid(),
            "master_pid": self.master_pid,
            "started_at": _iso(self.started_at),
            "checked_at": _iso(self.checked_at),
            "check_interval_seconds": self.config.interval_seconds,
            "idle_timeout_seconds": self.config.idle_seconds,
            "last_request_at": _iso(self.last_request),
            "idle_since": _iso(self.idle_since),
            "stop_due_at": _iso(self.idle_since + self.config.idle_seconds),
            "ports": [
                {
                    "port": port,
                    "connections": self.counts.get(port, 0),
                    "last_connection_at": _iso(self.last_seen[port]),
                }
                for port in self.config.ports
            ],
        }


def write_status(path: Path, status: dict) -> None:
    """Replace ``path`` with ``status`` as JSON, atomically (via a temporary
    file in the same directory and a rename), so a reader never sees a
    partial file."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as temp_file:
            json.dump(status, temp_file, indent=2)
            temp_file.write("\n")
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def log(message: str) -> None:
    print(f"idle-watchdog: {message}", file=sys.stderr, flush=True)


def run(
    config: Config,
    master_pid: int,
    *,
    clock=time.time,
    sleep=time.sleep,
    count=count_connections,
    request_time=last_request_time,
    getppid=os.getppid,
    kill=os.kill,
) -> int:
    """Check every ``config.interval_seconds`` until it's time to stop (in
    ``stop`` mode) or gunicorn's master has gone; returns the exit status."""
    watchdog = Watchdog(config, master_pid, started_at=clock())
    log(
        f"started in {config.mode} mode, watching ports"
        f" {', '.join(map(str, config.ports))}, idle timeout"
        f" {config.idle_seconds / 60:g} minutes, status in {config.status_file}"
    )
    reported_due = False
    while True:
        # Exiting with the master (when it's replaced, the watchdog is
        # reparented) keeps a watchdog from outliving its gunicorn.
        if getppid() != master_pid:
            log(f"gunicorn master {master_pid} has exited, exiting")
            return 0
        due = watchdog.check(
            clock(), count(config.ports), request_time(config.request_file)
        )
        if due and config.mode == "stop":
            watchdog.stopping = True
        try:
            write_status(config.status_file, watchdog.status())
        except OSError as exc:
            log(f"couldn't write {config.status_file}: {exc}")
        idle_minutes = config.idle_seconds / 60
        if watchdog.stopping:
            log(
                f"no activity for {idle_minutes:g} minutes, sending SIGTERM to"
                f" gunicorn master {master_pid}"
            )
            kill(master_pid, signal.SIGTERM)
            return 0
        if due and not reported_due:
            log(
                f"no activity for {idle_minutes:g} minutes (monitor mode: not stopping)"
            )
        reported_due = due
        sleep(config.interval_seconds)


def spawn(master_pid: int, environ: Mapping[str, str] = os.environ):
    """Start the watchdog for gunicorn master ``master_pid`` unless it's off.

    Returns the ``Popen``, or None if it's off, misconfigured (logged; better
    to keep serving than fail to start over this) or ``/proc/net/tcp`` is
    missing (not Linux).
    """
    try:
        config = config_from_env(environ)
    except ValueError as exc:
        log(f"not starting: {exc}")
        return None
    if config.mode == "off":
        return None
    if not PROC_NET_TCP_FILES[0].exists():
        log(f"not starting: {PROC_NET_TCP_FILES[0]} is missing (Linux only)")
        return None
    return subprocess.Popen(
        [sys.executable, "-m", __name__, str(master_pid)], env=dict(environ)
    )


def _exit_quietly(signum, frame):
    sys.exit(0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].isdigit():
        log("usage: python -m lifetracker.core.idle_watchdog <gunicorn master PID>")
        return 2
    # e.g. Ctrl-C in a foreground `docker run` reaches the whole process group.
    signal.signal(signal.SIGINT, _exit_quietly)
    signal.signal(signal.SIGTERM, _exit_quietly)
    try:
        config = config_from_env()
    except ValueError as exc:
        log(str(exc))
        return 2
    return run(config, int(argv[0]))


if __name__ == "__main__":
    sys.exit(main())
