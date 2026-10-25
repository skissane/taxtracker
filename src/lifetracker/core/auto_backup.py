"""Automatic cloud backups, run from the idle watchdog's process (issue #83).

The Machine is stopped whenever it's idle (see ``idle_watchdog``), and nothing
can change the database while it's stopped, so backups follow use rather than
the clock: the watchdog runs one at startup, every
``LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS`` while it's up, and (in ``stop``
mode) before stopping gunicorn. Each runs ``manage.py cloud_backup
--if-changed`` as a subprocess, which skips the upload when the database
hasn't changed since the last cloud backup (``backup.database_changed``), so
an unused Machine uploads nothing. The startup backup catches changes from
before a stop that skipped the backup before stopping, e.g. a crash, or a
deploy restarting a running Machine.

The command runs in a subprocess, as the watchdog doesn't import Django, and
without blocking the watchdog's loop, which has to keep recording uptime every
second.
"""

import os
import subprocess
import sys
from collections.abc import Callable, Mapping

# A backup still running after this long is killed, so a hung upload can't
# keep the Machine from stopping for ever.
TIMEOUT_SECONDS = 30 * 60
# How often the watchdog checks whether a running backup has finished.
POLL_SECONDS = 5


def command() -> list[str]:
    return [sys.executable, "-m", "django", "cloud_backup", "--if-changed"]


class AutoBackup:
    """Starts backup subprocesses and keeps track of the latest one, kept apart
    from the clock and ``subprocess`` so it's testable."""

    def __init__(
        self,
        interval_seconds: float,
        started_at: float,
        *,
        log: Callable[[str], None],
        environ: Mapping[str, str] = os.environ,
        popen=subprocess.Popen,
    ):
        self.interval_seconds = interval_seconds
        # The first backup is at startup.
        self.next_run = started_at
        self.log = log
        self.popen = popen
        # The watchdog is started from gunicorn's master, where wsgi.py, which
        # sets this, hasn't run.
        self.env = {"DJANGO_SETTINGS_MODULE": "lifetracker.settings", **environ}
        self.process = None
        self.runs = 0
        self.reason: str | None = None
        self.last_started_at: float | None = None
        self.last_finished_at: float | None = None
        self.last_exit_status: int | None = None

    @property
    def running(self) -> bool:
        return self.process is not None

    def start(self, now: float, reason: str) -> None:
        """Start a backup, unless one is running. ``reason`` is for the log."""
        if self.running:
            return
        self.runs += 1
        self.reason = reason
        self.last_started_at = now
        self.next_run = now + self.interval_seconds
        self.log(f"starting automatic backup ({reason})")
        try:
            self.process = self.popen(command(), env=self.env)
        except OSError as exc:
            self.log(f"couldn't start automatic backup: {exc}")
            self.last_finished_at = now
            self.last_exit_status = None

    def poll(self, now: float) -> None:
        """Note whether the running backup has finished, killing it if it's
        taken too long."""
        if self.process is None:
            return
        exit_status = self.process.poll()
        if exit_status is None:
            if now - self.last_started_at < TIMEOUT_SECONDS:
                return
            self.log(
                f"automatic backup ({self.reason}) still running after {TIMEOUT_SECONDS / 60:g} minutes, killing it"
            )
            self.process.kill()
            exit_status = self.process.wait()
        self.process = None
        self.last_finished_at = now
        self.last_exit_status = exit_status
        if exit_status == 0:
            self.log(f"automatic backup ({self.reason}) finished")
        else:
            self.log(f"automatic backup ({self.reason}) failed with exit status {exit_status}")

    def tick(self, now: float) -> None:
        """Called on every wake: note a finished backup, and start the next
        one when it's due."""
        self.poll(now)
        if not self.running and now >= self.next_run:
            self.start(now, "scheduled" if self.runs else "at startup")

    def wake_time(self, now: float) -> float:
        """When ``tick`` next has something to do."""
        return now + POLL_SECONDS if self.running else self.next_run

    def status(self, iso: Callable[[float | None], str | None]) -> dict:
        """For the watchdog's status file, with times formatted by ``iso``."""
        return {
            "interval_seconds": self.interval_seconds,
            "running": self.running,
            "reason": self.reason,
            "last_started_at": iso(self.last_started_at),
            "last_finished_at": iso(self.last_finished_at),
            "last_exit_status": self.last_exit_status,
            "next_run_at": iso(self.next_run),
        }
