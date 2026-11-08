import sys
from unittest import mock

from django.test import SimpleTestCase

from lifetracker.core import auto_backup, idle_watchdog


class FakeBackupProcess:
    """A ``Popen`` for a backup that finishes with ``exit_status`` after
    ``polls`` polls."""

    def __init__(self, polls: int = 0, exit_status: int = 0) -> None:
        self.polls = polls
        self.exit_status = exit_status
        self.killed = False

    def poll(self) -> int | None:
        if self.killed or self.polls <= 0:
            return self.exit_status
        self.polls -= 1
        return None

    def kill(self) -> None:
        self.killed = True
        self.exit_status = -9

    def wait(self) -> int:
        return self.exit_status


class AutoBackupTests(SimpleTestCase):
    START = 1_800_000_000

    def _backup(
        self, *processes: FakeBackupProcess | Exception, environ: dict[str, str] | None = None
    ) -> auto_backup.AutoBackup:
        self.messages: list[str] = []
        self.popen = mock.Mock(side_effect=list(processes))
        return auto_backup.AutoBackup(
            3600,
            self.START,
            log=self.messages.append,
            environ=environ or {},
            popen=self.popen,
        )

    def test_backs_up_at_startup_then_every_interval(self) -> None:
        backup = self._backup(FakeBackupProcess(), FakeBackupProcess())

        backup.tick(self.START)
        self.assertTrue(backup.running)
        backup.tick(self.START + 1)
        self.assertFalse(backup.running)
        backup.tick(self.START + 3599)
        self.assertEqual(self.popen.call_count, 1)
        backup.tick(self.START + 3600)
        self.assertEqual(self.popen.call_count, 2)
        self.assertEqual(
            self.messages,
            [
                "starting automatic backup (at startup)",
                "automatic backup (at startup) finished",
                "starting automatic backup (scheduled)",
            ],
        )

    def test_runs_cloud_backup_if_changed_with_settings_module(self) -> None:
        backup = self._backup(FakeBackupProcess(), environ={"PATH": "/bin"})

        backup.tick(self.START)

        self.popen.assert_called_once_with(
            [sys.executable, "-m", "django", "cloud_backup", "--if-changed"],
            env={"DJANGO_SETTINGS_MODULE": "lifetracker.settings", "PATH": "/bin"},
        )

    def test_one_at_a_time(self) -> None:
        backup = self._backup(FakeBackupProcess(polls=10))

        backup.tick(self.START)
        backup.start(self.START + 1, "before stopping")

        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(backup.reason, "at startup")
        self.assertEqual(backup.wake_time(self.START + 1), self.START + 6)

    def test_failure_is_logged(self) -> None:
        backup = self._backup(FakeBackupProcess(exit_status=1))

        backup.tick(self.START)
        backup.tick(self.START + 5)

        self.assertEqual(backup.last_exit_status, 1)
        self.assertIn("automatic backup (at startup) failed with exit status 1", self.messages)
        # Not retried until the next one's due.
        self.assertEqual(backup.wake_time(self.START + 5), self.START + 3600)

    def test_hung_backup_is_killed(self) -> None:
        process = FakeBackupProcess(polls=10**6)
        backup = self._backup(process)

        backup.tick(self.START)
        backup.tick(self.START + auto_backup.TIMEOUT_SECONDS - 1)
        self.assertTrue(backup.running)
        backup.tick(self.START + auto_backup.TIMEOUT_SECONDS)

        self.assertTrue(process.killed)
        self.assertFalse(backup.running)
        self.assertEqual(backup.last_exit_status, -9)
        self.assertIn(
            "automatic backup (at startup) still running after 30 minutes, killing it",
            self.messages,
        )

    def test_start_failure_is_logged(self) -> None:
        backup = self._backup(OSError("no such file"))

        backup.tick(self.START)

        self.assertFalse(backup.running)
        self.assertIsNone(backup.last_exit_status)
        self.assertIn("couldn't start automatic backup: no such file", self.messages)

    def test_status(self) -> None:
        backup = self._backup(FakeBackupProcess(polls=1))
        backup.tick(self.START)

        self.assertEqual(
            backup.status(idle_watchdog._iso),
            {
                "interval_seconds": 3600,
                "running": True,
                "reason": "at startup",
                "last_started_at": "2027-01-15T08:00:00+00:00",
                "last_finished_at": None,
                "last_exit_status": None,
                "next_run_at": "2027-01-15T09:00:00+00:00",
            },
        )
