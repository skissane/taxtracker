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
whatever the host. Every check rewrites a JSON status file (``Watchdog.status``),
which the admin's Idle Watchdog page shows (``read_status``).

The same process also records which seconds the app was up, one bit per second
in a file per UTC day (``uptime_log``), if there's an uptime directory
(``uptime_log.uptime_dir_from_env``), even with ``LIFETRACKER_IDLE_STOP`` off.
So it wakes every second, and checks for idleness every check interval.

It also runs automatic cloud backups (``auto_backup``) if
``LIFETRACKER_AUTO_BACKUP`` is ``on``: at startup, every few hours, and in
``stop`` mode before stopping gunicorn, which waits for that backup to finish.
"""

import dataclasses
import datetime
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from lifetracker.core import auto_backup, uptime_log

MODES = ("off", "monitor", "stop")
DEFAULT_IDLE_MINUTES = 10
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_STATUS_FILE = "/tmp/lifetracker-idle-watchdog.json"
DEFAULT_REQUEST_FILE = "/tmp/lifetracker-last-request"
AUTO_BACKUP_MODES = ("off", "on")
DEFAULT_AUTO_BACKUP_INTERVAL_HOURS = 6
SSH_PORT = 22
PROC_NET_TCP_FILES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
PROC_UPTIME_FILE = Path("/proc/uptime")
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
    # Where to record uptime (see ``uptime_log``), or None not to.
    uptime_dir: Path | None = None
    # Time between automatic backups (see ``auto_backup``), or None for none.
    auto_backup_interval_seconds: float | None = None

    @property
    def has_work(self) -> bool:
        return (
            self.mode != "off"
            or self.uptime_dir is not None
            or self.auto_backup_interval_seconds is not None
        )


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
    - ``LIFETRACKER_UPTIME_DIR``: see ``uptime_log.uptime_dir_from_env``.
    - ``LIFETRACKER_AUTO_BACKUP``: ``off`` (the default, also when blank) or
      ``on``, for automatic cloud backups (see ``auto_backup``).
    - ``LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS``: time between automatic
      backups while the app is up (default 6).
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
    auto_backup_mode = (
        environ.get("LIFETRACKER_AUTO_BACKUP", "").strip().lower() or "off"
    )
    if auto_backup_mode not in AUTO_BACKUP_MODES:
        raise ValueError(
            "LIFETRACKER_AUTO_BACKUP must be one of"
            f" {', '.join(AUTO_BACKUP_MODES)}, not {auto_backup_mode!r}"
        )
    auto_backup_hours = _positive_number(
        environ,
        "LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS",
        DEFAULT_AUTO_BACKUP_INTERVAL_HOURS,
    )
    return Config(
        mode=mode,
        idle_seconds=idle_minutes * 60,
        interval_seconds=interval_seconds,
        ports=ports,
        status_file=status_file,
        request_file=request_file,
        uptime_dir=uptime_log.uptime_dir_from_env(environ),
        auto_backup_interval_seconds=(
            auto_backup_hours * 3600 if auto_backup_mode == "on" else None
        ),
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
        # Waiting for the backup before stopping to finish.
        self.backing_up = False
        self.auto_backup: auto_backup.AutoBackup | None = None

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
        elif self.backing_up:
            state = "backing up"
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
            "auto_backup": (
                self.auto_backup.status(_iso) if self.auto_backup else None
            ),
        }


def read_status(path: Path) -> dict | None:
    """The status ``write_status`` last wrote to ``path``, or None if there's
    no such file. Raises ValueError if it isn't a JSON object, and OSError if it
    can't be read."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    status = json.loads(text)
    if not isinstance(status, dict):
        raise ValueError(f"{path} doesn't hold a JSON object")
    return status


def system_uptime(path: Path = PROC_UPTIME_FILE) -> float | None:
    """Seconds since the (virtual) machine booted, from /proc/uptime, or None
    where that's missing or unreadable (not Linux)."""
    try:
        return float(path.read_text().split()[0])
    except OSError, ValueError, IndexError:
        return None


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
    uptime=system_uptime,
    popen=subprocess.Popen,
) -> int:
    """Record uptime every second (if there's an uptime directory), check
    for idleness every ``config.interval_seconds`` (unless the mode is
    ``off``) and run automatic backups (if on), until it's time to stop (in
    ``stop`` mode) or gunicorn's master has gone; returns the exit status."""
    if not config.has_work:
        return 0
    started_at = clock()
    watchdog = Watchdog(config, master_pid, started_at=started_at)
    checking = config.mode != "off"
    if checking:
        log(
            f"started in {config.mode} mode, watching ports"
            f" {', '.join(map(str, config.ports))}, idle timeout"
            f" {config.idle_seconds / 60:g} minutes, status in {config.status_file}"
        )
    uptime_recorder = None
    since = None
    if config.uptime_dir is not None:
        uptime_recorder = uptime_log.UptimeLog(config.uptime_dir)
        log(f"recording uptime in {config.uptime_dir}")
        # Count the seconds since a fresh boot, before gunicorn was ready.
        booted = uptime()
        if booted is not None and booted <= uptime_log.MAX_BOOT_BACKFILL_SECONDS:
            since = started_at - booted
    backup = None
    if config.auto_backup_interval_seconds is not None:
        backup = auto_backup.AutoBackup(
            config.auto_backup_interval_seconds, started_at, log=log, popen=popen
        )
        watchdog.auto_backup = backup
        log(
            "automatic backups at startup, every"
            f" {config.auto_backup_interval_seconds / 3600:g} hours"
            + (" and before stopping" if config.mode == "stop" else "")
        )
    # Whether the backup before stopping has started, for this idle stretch.
    pre_stop_backup = False
    uptime_failing = False
    next_check = started_at
    # Uptime is fsynced every check interval, even when not checking.
    next_sync = started_at + config.interval_seconds
    reported_due = False
    while True:
        # Exiting with the master (when it's replaced, the watchdog is
        # reparented) keeps a watchdog from outliving its gunicorn, and from
        # recording uptime after it.
        if getppid() != master_pid:
            log(f"gunicorn master {master_pid} has exited, exiting")
            return 0
        now = clock()
        check_due = checking and now >= next_check
        if uptime_recorder is not None:
            try:
                uptime_recorder.record(now, since=since)
                if now >= next_sync:
                    uptime_recorder.sync()
                    next_sync = max(next_sync + config.interval_seconds, now)
            except OSError as exc:
                # Logged once per failing stretch, not every second.
                if not uptime_failing:
                    log(f"couldn't record uptime: {exc}")
                uptime_failing = True
            else:
                if uptime_failing:
                    log("recording uptime again")
                uptime_failing = False
                since = None
        if backup is not None:
            backup.tick(now)
        if check_due:
            due = watchdog.check(
                now, count(config.ports), request_time(config.request_file)
            )
            stop = due and config.mode == "stop"
            backing_up = False
            if not due:
                pre_stop_backup = False
            if stop and backup is not None:
                # Back up first, waiting for any backup already running, and
                # then for this one; stop anyway if it fails (the next startup
                # backs up). Activity meanwhile calls off the stop.
                if not pre_stop_backup and not backup.running:
                    backup.start(now, "before stopping")
                    pre_stop_backup = True
                stop = pre_stop_backup and not backup.running
                backing_up = not stop
            watchdog.stopping = stop
            watchdog.backing_up = backing_up
            try:
                write_status(config.status_file, watchdog.status())
            except OSError as exc:
                log(f"couldn't write {config.status_file}: {exc}")
            idle_minutes = config.idle_seconds / 60
            if watchdog.stopping:
                log(
                    f"no activity for {idle_minutes:g} minutes, sending SIGTERM"
                    f" to gunicorn master {master_pid}"
                )
                kill(master_pid, signal.SIGTERM)
                return 0
            if due and config.mode == "monitor" and not reported_due:
                log(
                    f"no activity for {idle_minutes:g} minutes"
                    " (monitor mode: not stopping)"
                )
            reported_due = due
            # From the schedule rather than now, so checks don't drift; but
            # never into the past, so a long stall doesn't bunch them up.
            next_check = max(next_check + config.interval_seconds, now)
        # Wake at the next whole second, or the next check if that's sooner.
        wake = math.floor(now) + 1 if uptime_recorder is not None else math.inf
        if checking:
            wake = min(wake, next_check)
        if backup is not None:
            wake = min(wake, backup.wake_time(now))
        sleep(max(wake - clock(), 0))


def spawn(master_pid: int, environ: Mapping[str, str] = os.environ):
    """Start the watchdog for gunicorn master ``master_pid`` unless it has
    nothing to do: it's off, there's no uptime directory and automatic backups
    are off.

    Returns the ``Popen``, or None if it has nothing to do, is misconfigured
    (logged; better to keep serving than fail to start over this) or
    ``/proc/net/tcp`` is missing (not Linux).
    """
    try:
        config = config_from_env(environ)
    except ValueError as exc:
        log(f"not starting: {exc}")
        return None
    if not config.has_work:
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
