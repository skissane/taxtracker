import dataclasses
import datetime
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import typing
from pathlib import Path
from unittest import mock

import gunicorn.config
import gunicorn.glogging
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from lifetracker import gunicorn_conf
from lifetracker.core import client_ips, idle_watchdog, uptime_log
from lifetracker.core.admin_views import _format_duration, idle_watchdog_page_context
from tests.core.test_auto_backup import FakeBackupProcess

# /proc/net/tcp and tcp6 excerpts: a listener on 8000 (state 0A), established
# connections to local ports 8000 (0x1F40) and 22 (0x0016), and an outgoing one
# from ephemeral port 0xA2C4 to a remote port 22, which mustn't count.
_PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000 10001
   1: 0100007F:1F40 0100007F:D3B2 01 00000000:00000000 00:00000000 00000000 10001
   2: 0100007F:A2C4 0200007F:0016 01 00000000:00000000 00:00000000 00000000 10001
"""
_PROC_NET_TCP6 = """\
  sl  local_address                         remote_address                        st
   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A
   1: 00AA00FD000000000000000000000002:0016 00AA00FD000000000000000000000001:C350 01
   2: 00AA00FD000000000000000000000002:0016 00AA00FD000000000000000000000001:C351 01
"""


class IdleWatchdogConfigTests(SimpleTestCase):
    def test_defaults(self) -> None:
        config = idle_watchdog.config_from_env({})

        self.assertEqual(
            config,
            idle_watchdog.Config(
                mode="off",
                idle_seconds=600,
                interval_seconds=30,
                ports=(8000, 22),
                status_file=Path("/tmp/lifetracker-idle-watchdog.json"),
                request_file=Path("/tmp/lifetracker-last-request"),
            ),
        )

    def test_blank_values_use_defaults(self) -> None:
        config = idle_watchdog.config_from_env(
            {
                "LIFETRACKER_IDLE_STOP": " ",
                "LIFETRACKER_IDLE_STOP_MINUTES": "",
                "LIFETRACKER_IDLE_STOP_PORTS": "",
                "PORT": "",
            }
        )

        self.assertEqual(config, idle_watchdog.config_from_env({}))

    def test_auto_backup(self) -> None:
        for env, expected in [
            ({}, None),
            ({"LIFETRACKER_AUTO_BACKUP": " "}, None),
            ({"LIFETRACKER_AUTO_BACKUP": "off"}, None),
            ({"LIFETRACKER_AUTO_BACKUP": "On"}, 6 * 3600),
            (
                {
                    "LIFETRACKER_AUTO_BACKUP": "on",
                    "LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS": "0.5",
                },
                1800,
            ),
        ]:
            with self.subTest(env=env):
                config = idle_watchdog.config_from_env(env)
                self.assertEqual(config.auto_backup_interval_seconds, expected)

    def test_invalid_auto_backup_settings(self) -> None:
        for env, message in [
            ({"LIFETRACKER_AUTO_BACKUP": "yes"}, "LIFETRACKER_AUTO_BACKUP must be"),
            (
                {"LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS": "0"},
                "LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS must be a positive number",
            ),
        ]:
            with self.subTest(env=env):
                with self.assertRaisesMessage(ValueError, message):
                    idle_watchdog.config_from_env(env)

    def test_default_ports_follow_gunicorn_port(self) -> None:
        config = idle_watchdog.config_from_env({"PORT": "9000"})

        self.assertEqual(config.ports, (9000, 22))

    def test_settings_from_env(self) -> None:
        config = idle_watchdog.config_from_env(
            {
                "LIFETRACKER_IDLE_STOP": "Stop",
                "LIFETRACKER_IDLE_STOP_MINUTES": "2.5",
                "LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS": "5",
                "LIFETRACKER_IDLE_STOP_PORTS": "8080, 2222,8080",
                "LIFETRACKER_IDLE_STOP_STATUS_FILE": "/run/status.json",
                "LIFETRACKER_IDLE_STOP_REQUEST_FILE": "/run/last-request",
            }
        )

        self.assertEqual(
            config,
            idle_watchdog.Config(
                mode="stop",
                idle_seconds=150,
                interval_seconds=5,
                ports=(8080, 2222),
                status_file=Path("/run/status.json"),
                request_file=Path("/run/last-request"),
            ),
        )

    def test_uptime_dir(self) -> None:
        for env, expected in [
            ({}, None),
            ({"LIFETRACKER_DATA_DIR": "/data"}, Path("/data/uptime")),
            ({"LIFETRACKER_DATA_DIR": " "}, None),
            (
                {"LIFETRACKER_DATA_DIR": "/data", "LIFETRACKER_UPTIME_DIR": "/up"},
                Path("/up"),
            ),
            (
                {"LIFETRACKER_DATA_DIR": "/data", "LIFETRACKER_UPTIME_DIR": "Off"},
                None,
            ),
        ]:
            with self.subTest(env=env):
                self.assertEqual(idle_watchdog.config_from_env(env).uptime_dir, expected)

    def test_invalid_values_raise(self) -> None:
        for name, value, message in [
            ("LIFETRACKER_IDLE_STOP", "on", "must be one of off, monitor, stop"),
            ("LIFETRACKER_IDLE_STOP_MINUTES", "0", "must be a positive number"),
            ("LIFETRACKER_IDLE_STOP_MINUTES", "ten", "must be a positive number"),
            ("LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS", "-1", "positive number"),
            ("LIFETRACKER_IDLE_STOP_MINUTES", "nan", "must be a positive number"),
            ("LIFETRACKER_IDLE_STOP_MINUTES", "inf", "must be a positive number"),
            ("LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS", "-inf", "positive number"),
            ("LIFETRACKER_IDLE_STOP_PORTS", "80,ssh", "comma-separated port"),
            ("LIFETRACKER_IDLE_STOP_PORTS", "0", "comma-separated port"),
            ("LIFETRACKER_IDLE_STOP_PORTS", "65536", "comma-separated port"),
        ]:
            with self.subTest(name=name, value=value):
                with self.assertRaisesMessage(ValueError, message):
                    idle_watchdog.config_from_env({name: value})


class IdleWatchdogConnectionTests(SimpleTestCase):
    def test_established_local_ports(self) -> None:
        self.assertEqual(
            list(idle_watchdog.established_local_ports(_PROC_NET_TCP)),
            [8000, 0xA2C4],
        )
        self.assertEqual(list(idle_watchdog.established_local_ports(_PROC_NET_TCP6)), [22, 22])

    def test_count_connections_reads_ipv4_and_ipv6(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            tcp = Path(tempdir) / "tcp"
            tcp6 = Path(tempdir) / "tcp6"
            tcp.write_text(_PROC_NET_TCP)
            tcp6.write_text(_PROC_NET_TCP6)

            counts = idle_watchdog.count_connections([8000, 22, 443], paths=[tcp, tcp6])

        self.assertEqual(counts, {8000: 1, 22: 2, 443: 0})

    def test_count_connections_skips_missing_table(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            tcp = Path(tempdir) / "tcp"
            tcp.write_text(_PROC_NET_TCP)

            counts = idle_watchdog.count_connections([8000, 22], paths=[tcp, Path(tempdir) / "tcp6"])

        self.assertEqual(counts, {8000: 1, 22: 0})


def _watchdog_config(mode: str = "stop", status_file: Path = Path("/nonexistent/status.json")) -> idle_watchdog.Config:
    return idle_watchdog.Config(
        mode=mode,
        idle_seconds=60,
        interval_seconds=10,
        ports=(8000, 22),
        status_file=status_file,
        request_file=Path("/nonexistent/last-request"),
    )


class IdleWatchdogStateTests(SimpleTestCase):
    START = 1_800_000_000  # 2027-01-15T08:00:00Z

    def test_idle_time_counts_from_startup(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)

        self.assertFalse(watchdog.check(self.START + 59, {8000: 0, 22: 0}, None))
        self.assertTrue(watchdog.check(self.START + 60, {8000: 0, 22: 0}, None))

    def test_connection_resets_idle_time(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)

        self.assertFalse(watchdog.check(self.START + 50, {8000: 0, 22: 1}, None))
        self.assertFalse(watchdog.check(self.START + 100, {8000: 0, 22: 0}, None))
        self.assertTrue(watchdog.check(self.START + 110, {8000: 0, 22: 0}, None))

    def test_request_resets_idle_time(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)
        idle = {8000: 0, 22: 0}

        # A request between checks, whose connection no check saw open.
        self.assertFalse(watchdog.check(self.START + 50, idle, self.START + 45))
        self.assertFalse(watchdog.check(self.START + 100, idle, self.START + 45))
        self.assertTrue(watchdog.check(self.START + 105, idle, self.START + 45))
        self.assertEqual(watchdog.status()["last_request_at"], "2027-01-15T08:00:45+00:00")

    def test_request_before_startup_is_ignored(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)

        self.assertTrue(watchdog.check(self.START + 60, {8000: 0, 22: 0}, self.START - 3600))

    def test_request_time_in_future_counts_as_now(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)
        idle = {8000: 0, 22: 0}

        watchdog.check(self.START + 10, idle, self.START + 3600)

        self.assertEqual(watchdog.idle_since, self.START + 10)

    def test_status(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1234, self.START)
        watchdog.check(self.START + 10, {8000: 2, 22: 0}, None)
        watchdog.check(self.START + 20, {8000: 0, 22: 0}, None)

        self.assertEqual(
            watchdog.status(),
            {
                "mode": "stop",
                "state": "idle",
                "pid": os.getpid(),
                "master_pid": 1234,
                "started_at": "2027-01-15T08:00:00+00:00",
                "checked_at": "2027-01-15T08:00:20+00:00",
                "check_interval_seconds": 10,
                "idle_timeout_seconds": 60,
                "last_request_at": None,
                "idle_since": "2027-01-15T08:00:10+00:00",
                "stop_due_at": "2027-01-15T08:01:10+00:00",
                "ports": [
                    {
                        "port": 8000,
                        "connections": 0,
                        "last_connection_at": "2027-01-15T08:00:10+00:00",
                    },
                    {"port": 22, "connections": 0, "last_connection_at": None},
                ],
                "auto_backup": None,
            },
        )

    def test_status_state(self) -> None:
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)
        self.assertEqual(watchdog.status()["checked_at"], None)

        watchdog.check(self.START + 1, {8000: 0, 22: 1}, None)
        self.assertEqual(watchdog.status()["state"], "active")

        watchdog.backing_up = True
        self.assertEqual(watchdog.status()["state"], "backing up")

        watchdog.stopping = True
        self.assertEqual(watchdog.status()["state"], "stopping")

    def test_write_status_replaces_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "status.json"
            path.write_text("old")

            idle_watchdog.write_status(path, {"state": "idle"})

            self.assertEqual(json.loads(path.read_text()), {"state": "idle"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(os.listdir(tempdir), ["status.json"])

    def test_write_status_removes_temp_file_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "status.json"

            with self.assertRaises(TypeError):
                idle_watchdog.write_status(path, {"unserialisable": object()})

            self.assertEqual(os.listdir(tempdir), [])


class IdleWatchdogRunTests(SimpleTestCase):
    MASTER_PID = 4321

    def _run(
        self,
        mode: str,
        counts: typing.Iterable[dict[int, int]],
        *,
        getppid: typing.Callable[[], int] | None = None,
        status_file: Path | None = None,
    ) -> tuple[int, mock.Mock, str, typing.Any]:
        """Run the loop with a fake clock that advances 10s per sleep, and
        fake connection counts taken from ``counts`` in turn."""
        now = [1_800_000_000.0]
        counts = iter(counts)
        kill = mock.Mock()
        with tempfile.TemporaryDirectory() as tempdir:
            config = _watchdog_config(mode, status_file or Path(tempdir) / "status.json")
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                result = idle_watchdog.run(
                    config,
                    self.MASTER_PID,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    count=lambda ports: next(counts),
                    request_time=lambda path: None,
                    getppid=getppid or (lambda: self.MASTER_PID),
                    kill=kill,
                )
            status = json.loads(config.status_file.read_text()) if config.status_file.exists() else None
        return result, kill, stderr.getvalue(), status

    def test_stop_mode_sends_sigterm_once_idle(self) -> None:
        idle = {8000: 0, 22: 0}
        busy = {8000: 0, 22: 1}

        result, kill, stderr, status = self._run("stop", [busy, idle, idle, idle, idle, idle, idle, idle])

        self.assertEqual(result, 0)
        kill.assert_called_once_with(self.MASTER_PID, signal.SIGTERM)
        self.assertIn("sending SIGTERM to gunicorn master 4321", stderr)
        self.assertEqual(status["state"], "stopping")
        self.assertEqual(status["checked_at"], "2027-01-15T08:01:00+00:00")

    def test_monitor_mode_reports_but_never_stops(self) -> None:
        idle = {8000: 0, 22: 0}
        busy = {8000: 1, 22: 0}
        ppids = iter([self.MASTER_PID] * 16 + [1])

        result, kill, stderr, status = self._run(
            "monitor", [idle] * 7 + [busy] + [idle] * 8, getppid=lambda: next(ppids)
        )

        self.assertEqual(result, 0)
        kill.assert_not_called()
        # Reported once per idle stretch: after startup, and after the busy check.
        self.assertEqual(stderr.count("monitor mode: not stopping"), 2)
        self.assertIn("gunicorn master 4321 has exited, exiting", stderr)
        self.assertEqual(status["state"], "idle")

    def test_status_write_failure_is_logged_and_ignored(self) -> None:
        result, kill, stderr, status = self._run(
            "stop",
            [{8000: 0, 22: 0}] * 7,
            status_file=Path("/nonexistent/status.json"),
        )

        self.assertEqual(result, 0)
        kill.assert_called_once()
        self.assertIn("couldn't write /nonexistent/status.json", stderr)
        self.assertIsNone(status)


class IdleWatchdogAutoBackupRunTests(SimpleTestCase):
    MASTER_PID = 4321

    def _run(
        self, mode: str, counts: typing.Iterable[dict[int, int]], processes: list[FakeBackupProcess]
    ) -> tuple[int, mock.Mock, mock.Mock, str, list[typing.Any]]:
        """Run the loop with backups every hour, a fake clock that each sleep
        advances by what was asked, fake connection counts from ``counts`` (one
        per check, every 10 seconds), and fake backup processes."""
        now = [1_800_000_000.0]
        counts = iter(counts)
        kill = mock.Mock()
        popen = mock.Mock(side_effect=list(processes))
        statuses = []
        with tempfile.TemporaryDirectory() as tempdir:
            config = dataclasses.replace(
                _watchdog_config(mode, Path(tempdir) / "status.json"),
                auto_backup_interval_seconds=3600,
            )
            real_write_status = idle_watchdog.write_status

            def write_status(path: Path, status: typing.Any) -> None:
                statuses.append(status)
                real_write_status(path, status)

            with (
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
                mock.patch.object(idle_watchdog, "write_status", write_status),
            ):
                result = idle_watchdog.run(
                    config,
                    self.MASTER_PID,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    count=lambda ports: next(counts),
                    request_time=lambda path: None,
                    getppid=lambda: self.MASTER_PID,
                    kill=kill,
                    popen=popen,
                )
        return result, kill, popen, stderr.getvalue(), statuses

    def test_backs_up_before_stopping(self) -> None:
        idle = {8000: 0, 22: 0}
        # The pre-stop backup takes three polls (5 seconds apart).
        result, kill, popen, stderr, statuses = self._run(
            "stop", [idle] * 9, [FakeBackupProcess(), FakeBackupProcess(polls=3)]
        )

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 2)
        kill.assert_called_once_with(self.MASTER_PID, signal.SIGTERM)
        lines = stderr.splitlines()
        self.assertIn(
            "idle-watchdog: automatic backups at startup, every 1 hours and before stopping",
            lines,
        )
        backup_line = lines.index("idle-watchdog: starting automatic backup (before stopping)")
        self.assertLess(
            backup_line,
            lines.index("idle-watchdog: automatic backup (before stopping) finished"),
        )
        self.assertIn("sending SIGTERM", lines[-1])
        # Idle at 60s; the backup finished by the check at 80s.
        self.assertEqual(
            [(status["checked_at"][11:19], status["state"]) for status in statuses],
            [
                ("08:00:00", "idle"),
                ("08:00:10", "idle"),
                ("08:00:20", "idle"),
                ("08:00:30", "idle"),
                ("08:00:40", "idle"),
                ("08:00:50", "idle"),
                ("08:01:00", "backing up"),
                ("08:01:10", "backing up"),
                ("08:01:20", "stopping"),
            ],
        )
        self.assertEqual(statuses[-1]["auto_backup"]["reason"], "before stopping")

    def test_waits_for_running_backup_then_backs_up_again(self) -> None:
        idle = {8000: 0, 22: 0}
        # The startup backup is still running when the idle timeout is reached.
        result, kill, popen, stderr, statuses = self._run(
            "stop", [idle] * 10, [FakeBackupProcess(polls=14), FakeBackupProcess()]
        )

        self.assertEqual(popen.call_count, 2)
        kill.assert_called_once()
        self.assertIn("starting automatic backup (before stopping)", stderr)

    def test_failed_backup_still_stops(self) -> None:
        idle = {8000: 0, 22: 0}
        result, kill, popen, stderr, statuses = self._run(
            "stop",
            [idle] * 8,
            [FakeBackupProcess(), FakeBackupProcess(exit_status=1)],
        )

        kill.assert_called_once()
        self.assertIn("automatic backup (before stopping) failed", stderr)

    def test_activity_calls_off_the_stop(self) -> None:
        idle = {8000: 0, 22: 0}
        busy = {8000: 1, 22: 0}
        # Activity while the pre-stop backup runs; idle again for another
        # minute before the next one (both backups in one idle stretch).
        counts = [idle] * 7 + [busy] + [idle] * 7
        result, kill, popen, stderr, statuses = self._run(
            "stop",
            counts,
            [FakeBackupProcess(), FakeBackupProcess(polls=4), FakeBackupProcess()],
        )

        self.assertEqual(popen.call_count, 3)
        self.assertEqual(stderr.count("starting automatic backup (before stopping)"), 2)
        self.assertEqual(statuses[7]["state"], "active")
        kill.assert_called_once()

    def test_monitor_mode_never_backs_up_before_stopping(self) -> None:
        idle = {8000: 0, 22: 0}
        ppids = iter([self.MASTER_PID] * 30 + [1])
        now = [1_800_000_000.0]
        popen = mock.Mock(return_value=FakeBackupProcess())
        config = dataclasses.replace(_watchdog_config("monitor"), auto_backup_interval_seconds=3600)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            idle_watchdog.run(
                config,
                self.MASTER_PID,
                clock=lambda: now[0],
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                count=lambda ports: idle,
                request_time=lambda path: None,
                getppid=lambda: next(ppids),
                kill=mock.Mock(),
                popen=popen,
            )

        popen.assert_called_once()
        self.assertNotIn("before stopping", stderr.getvalue())

    def test_backups_alone(self) -> None:
        # Neither idle checks nor uptime: it wakes for the backups alone.
        ppids = iter([self.MASTER_PID] * 4 + [1])
        now = [1_800_000_000.0]
        sleeps = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        popen = mock.Mock(side_effect=[FakeBackupProcess(polls=1)] * 2)
        config = dataclasses.replace(_watchdog_config("off"), auto_backup_interval_seconds=3600)
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            idle_watchdog.run(
                config,
                self.MASTER_PID,
                clock=lambda: now[0],
                sleep=sleep,
                getppid=lambda: next(ppids),
                popen=popen,
            )

        self.assertEqual(sleeps, [5, 5, 3590, 5])
        self.assertEqual(popen.call_count, 2)


class IdleWatchdogSpawnTests(SimpleTestCase):
    def test_off_by_default(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            self.assertIsNone(idle_watchdog.spawn(99, {}))

        popen.assert_not_called()

    def test_invalid_config_is_logged_not_raised(self) -> None:
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertIsNone(idle_watchdog.spawn(99, {"LIFETRACKER_IDLE_STOP": "yes"}))

        popen.assert_not_called()
        self.assertIn("idle-watchdog: not starting: LIFETRACKER_IDLE_STOP", stderr.getvalue())

    def test_not_started_without_proc_net(self) -> None:
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(idle_watchdog, "PROC_NET_TCP_FILES", (Path("/nonexistent/tcp"),)),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertIsNone(idle_watchdog.spawn(99, {"LIFETRACKER_IDLE_STOP": "monitor"}))

        popen.assert_not_called()
        self.assertIn("/nonexistent/tcp is missing", stderr.getvalue())

    def test_starts_for_uptime_alone(self) -> None:
        env = {"LIFETRACKER_DATA_DIR": "/data"}
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertIs(idle_watchdog.spawn(99, env), popen.return_value)

    def test_starts_for_auto_backup_alone(self) -> None:
        env = {"LIFETRACKER_AUTO_BACKUP": "on"}
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertIs(idle_watchdog.spawn(99, env), popen.return_value)

    def test_starts_module_with_master_pid(self) -> None:
        env = {"LIFETRACKER_IDLE_STOP": "stop"}
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertIs(idle_watchdog.spawn(99, env), popen.return_value)

        popen.assert_called_once_with([sys.executable, "-m", "lifetracker.core.idle_watchdog", "99"], env=env)

    def test_request_file_from_env(self) -> None:
        for env, expected in [
            ({}, None),
            ({"LIFETRACKER_IDLE_STOP": "bogus"}, None),
            (
                {"LIFETRACKER_IDLE_STOP": "monitor"},
                Path("/tmp/lifetracker-last-request"),
            ),
            (
                {
                    "LIFETRACKER_IDLE_STOP": "stop",
                    "LIFETRACKER_IDLE_STOP_REQUEST_FILE": "/run/last-request",
                },
                Path("/run/last-request"),
            ),
        ]:
            with self.subTest(env=env):
                self.assertEqual(idle_watchdog.request_file_from_env(env), expected)

    def test_record_request_touches_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "last-request"
            self.assertIsNone(idle_watchdog.last_request_time(path))

            idle_watchdog.record_request(path)
            first = idle_watchdog.last_request_time(path)
            assert first is not None
            os.utime(path, (first - 100, first - 100))
            idle_watchdog.record_request(path)

            later = idle_watchdog.last_request_time(path)
            assert later is not None
            self.assertGreaterEqual(later, first)

    def test_record_request_never_raises(self) -> None:
        idle_watchdog.record_request(None)
        idle_watchdog.record_request(Path("/nonexistent/last-request"))

    def test_gunicorn_post_request_records_request(self) -> None:
        path = Path("/run/last-request")
        with (
            mock.patch.object(gunicorn_conf, "_REQUEST_FILE", path),
            mock.patch.object(idle_watchdog, "record_request") as record_request,
        ):
            gunicorn_conf.post_request(mock.Mock(), mock.Mock(), {}, mock.Mock())

        record_request.assert_called_once_with(path)

    def test_gunicorn_access_log_shows_fly_client_ip(self) -> None:
        # Gunicorn takes any module-level name in its config module that's a
        # setting's name as that setting.
        cfg = gunicorn.config.Config()
        cfg.set("access_log_format", gunicorn_conf.access_log_format)
        cfg.set("accesslog", "-")
        logger = gunicorn.glogging.Logger(cfg)
        environ = {
            "REMOTE_ADDR": "172.16.1.2",
            "REQUEST_METHOD": "GET",
            "RAW_URI": "/admin/",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        resp = mock.Mock(status="200 OK", sent=42, headers=[])

        def logged(headers: list[tuple[str, str]]) -> str:
            req = mock.Mock(headers=headers)
            with mock.patch.object(logger.access_log, "info") as info:
                logger.access(resp, req, environ, datetime.timedelta(0))
            fmt, atoms = info.call_args.args
            return str(fmt % atoms)

        line = logged([("FLY-CLIENT-IP", "203.0.113.7")])
        self.assertRegex(
            line,
            r'^203\.0\.113\.7 172\.16\.1\.2 - - \[.*\] "GET /admin/ HTTP/1\.1" 200',
        )
        self.assertRegex(logged([]), r"^- 172\.16\.1\.2 ")

    def test_gunicorn_on_exit_exits_1_if_envoy_failed(self) -> None:
        server = mock.Mock()
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "envoy-failed"
            env = {"LIFETRACKER_ENVOY_FAILED_FILE": str(path)}
            with mock.patch.dict(os.environ, env):
                gunicorn_conf.on_exit(server)
                path.touch()
                with self.assertRaises(SystemExit) as cm:
                    gunicorn_conf.on_exit(server)

        self.assertEqual(cm.exception.code, 1)
        server.log.error.assert_called_once_with("Exiting with status 1, as Envoy kept failing")

    def test_gunicorn_on_exit_without_envoy_failed_file_setting(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("LIFETRACKER_ENVOY_FAILED_FILE", None)
            gunicorn_conf.on_exit(mock.Mock())

    def test_gunicorn_when_ready_spawns_for_master(self) -> None:
        server = mock.Mock(pid=1234)
        with (
            mock.patch.object(idle_watchdog, "spawn") as spawn,
            mock.patch.object(client_ips, "spawn") as spawn_collector,
        ):
            gunicorn_conf.when_ready(server)

        spawn.assert_called_once_with(1234)
        spawn_collector.assert_called_once_with(1234)

    def test_main_usage(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(idle_watchdog.main([]), 2)

        self.assertIn("usage:", stderr.getvalue())

    def test_main_invalid_config(self) -> None:
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP_MINUTES": "x"}),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            mock.patch("signal.signal"),
        ):
            self.assertEqual(idle_watchdog.main(["99"]), 2)

        self.assertIn("LIFETRACKER_IDLE_STOP_MINUTES", stderr.getvalue())

    def test_main_runs_watchdog_for_master_pid(self) -> None:
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP": "monitor"}),
            mock.patch("signal.signal") as set_handler,
            mock.patch.object(idle_watchdog, "run", return_value=0) as run,
        ):
            self.assertEqual(idle_watchdog.main(["99"]), 0)

        run.assert_called_once_with(idle_watchdog.config_from_env({"LIFETRACKER_IDLE_STOP": "monitor"}), 99)
        set_handler.assert_has_calls(
            [
                mock.call(signal.SIGINT, idle_watchdog._exit_quietly),
                mock.call(signal.SIGTERM, idle_watchdog._exit_quietly),
            ]
        )

    def test_exit_quietly_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            idle_watchdog._exit_quietly(signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 0)

    def test_real_process_stops_its_parent(self) -> None:
        """End to end: a shell stands in for gunicorn's master, starts the
        watchdog as its child, and is sent SIGTERM once it's been idle."""
        with tempfile.TemporaryDirectory() as tempdir:
            status_file = Path(tempdir) / "status.json"
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    f'"{sys.executable}" -m lifetracker.core.idle_watchdog $$ & wait',
                ],
                env={
                    **os.environ,
                    "LIFETRACKER_IDLE_STOP": "stop",
                    "LIFETRACKER_IDLE_STOP_MINUTES": "0.001",
                    "LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS": "0.05",
                    # Port 1 (tcpmux): nothing will be connected to it.
                    "LIFETRACKER_IDLE_STOP_PORTS": "1",
                    "LIFETRACKER_IDLE_STOP_STATUS_FILE": str(status_file),
                    "LIFETRACKER_UPTIME_DIR": str(Path(tempdir) / "uptime"),
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            status = json.loads(status_file.read_text())
            uptime_files = list((Path(tempdir) / "uptime").glob("*.bin"))
            up = uptime_log.active_seconds(uptime_files[0].read_bytes())

        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertGreaterEqual(up, 1)
        self.assertIn("sending SIGTERM", result.stderr)
        self.assertEqual(status["state"], "stopping")
        self.assertEqual(status["ports"], [{"port": 1, "connections": 0, "last_connection_at": None}])


class IdleWatchdogUptimeRunTests(SimpleTestCase):
    MASTER_PID = 4321
    START = 1_800_000_000.25  # 2027-01-15T08:00:00.25Z

    def _run(
        self,
        mode: str,
        *,
        ticks: int,
        uptime_dir: Path,
        booted: float | None = None,
        clock_step: typing.Callable[[int], float] | None = None,
    ) -> tuple[int, list[float], str]:
        """Run the loop for ``ticks`` wakes (then the master "exits"), with a
        fake clock that each sleep advances by what was asked plus
        ``clock_step`` (a function of the wake number), if given."""
        now = [self.START]
        ppids = iter([self.MASTER_PID] * ticks + [1])
        sleeps = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds + (clock_step(len(sleeps)) if clock_step else 0)

        config = dataclasses.replace(_watchdog_config(mode), uptime_dir=uptime_dir)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = idle_watchdog.run(
                config,
                self.MASTER_PID,
                clock=lambda: now[0],
                sleep=sleep,
                count=lambda ports: {8000: 1, 22: 0},
                request_time=lambda path: None,
                getppid=lambda: next(ppids),
                kill=mock.Mock(),
                uptime=lambda: booted,
            )
        return result, sleeps, stderr.getvalue()

    def _up_seconds(self, uptime_dir: Path) -> list[int]:
        data = uptime_log.read_day(uptime_dir, uptime_log.day_of(self.START))
        assert data is not None
        bits = int.from_bytes(data, "big")
        return [
            second
            for second in range(uptime_log.SECONDS_PER_DAY)
            if bits >> (uptime_log.SECONDS_PER_DAY - 1 - second) & 1
        ]

    def test_records_every_second(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            result, sleeps, stderr = self._run("off", ticks=5, uptime_dir=Path(tempdir))
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(result, 0)
        # First to the next whole second, then a second at a time.
        self.assertEqual(sleeps, [0.75, 1, 1, 1, 1])
        self.assertEqual(up, [28800, 28801, 28802, 28803, 28804])
        self.assertIn(f"recording uptime in {tempdir}", stderr)
        self.assertNotIn("started in", stderr)

    def test_nothing_to_do_exits_at_once(self) -> None:
        # e.g. run by hand with both off: without the early return the loop
        # would have nothing to wake for, and time.sleep(inf) raises.
        sleep = mock.Mock()

        result = idle_watchdog.run(_watchdog_config("off"), self.MASTER_PID, sleep=sleep)

        self.assertEqual(result, 0)
        sleep.assert_not_called()

    def test_checks_still_every_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            status_file = Path(tempdir) / "status.json"
            config = dataclasses.replace(
                _watchdog_config("monitor", status_file),
                uptime_dir=Path(tempdir) / "uptime",
            )
            now = [self.START]
            ppids = iter([self.MASTER_PID] * 25 + [1])
            count = mock.Mock(return_value={8000: 0, 22: 1})
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                idle_watchdog.run(
                    config,
                    self.MASTER_PID,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    count=count,
                    request_time=lambda path: None,
                    getppid=lambda: next(ppids),
                    kill=mock.Mock(),
                    uptime=lambda: None,
                )
            status = json.loads(status_file.read_text())

        # Checks at 0, 10 and 20 seconds, of 25 wakes.
        self.assertEqual(count.call_count, 3)
        self.assertEqual(status["checked_at"], "2027-01-15T08:00:20+00:00")

    def test_uptime_alone_still_syncs_every_interval(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tempdir,
            mock.patch.object(uptime_log.UptimeLog, "sync") as sync,
        ):
            self._run("off", ticks=25, uptime_dir=Path(tempdir))

        # Once on taking up the first day (when there's nothing to sync), then
        # every 10s interval: the wakes 11 and 21 seconds in, of 25.
        self.assertEqual(sync.call_count, 3)

    def test_short_gaps_are_filled_long_ones_not(self) -> None:
        # The 2nd sleep oversleeps by 2s, the 4th by 10s.
        with tempfile.TemporaryDirectory() as tempdir:
            self._run(
                "off",
                ticks=6,
                uptime_dir=Path(tempdir),
                clock_step=lambda n: {2: 2, 4: 10}.get(n, 0),
            )
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(up, [28800, 28801, 28802, 28803, 28804, 28805, 28816, 28817])

    def test_fresh_boot_is_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            self._run("off", ticks=1, uptime_dir=Path(tempdir), booted=3.5)
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(up, [28796, 28797, 28798, 28799, 28800])

    def test_long_uptime_is_not_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            self._run("off", ticks=1, uptime_dir=Path(tempdir), booted=3600)
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(up, [28800])

    def test_write_failure_is_logged_once_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            blocker = Path(tempdir) / "uptime"
            blocker.write_text("not a directory")

            def unblock(n: int) -> int:
                if n == 3:
                    blocker.unlink()
                return 0

            result, sleeps, stderr = self._run("off", ticks=5, uptime_dir=blocker, clock_step=unblock)
            up = self._up_seconds(blocker)

        self.assertEqual(result, 0)
        self.assertEqual(stderr.count("couldn't record uptime"), 1)
        self.assertIn("recording uptime again", stderr)
        # The seconds that failed are recorded once it works again.
        self.assertEqual(up, [28800, 28801, 28802, 28803, 28804])


def _watchdog_status(now: datetime.datetime, **overrides: typing.Any) -> dict[str, typing.Any]:
    """A status file's contents, as ``Watchdog.status`` writes them, with times
    relative to ``now`` (a UTC datetime)."""

    def iso(seconds_ago: float) -> str:
        return (now - datetime.timedelta(seconds=seconds_ago)).isoformat()

    status = {
        "mode": "monitor",
        "state": "active",
        "pid": 42,
        "master_pid": 41,
        "started_at": iso(3600),
        "checked_at": iso(20),
        "check_interval_seconds": 30,
        "idle_timeout_seconds": 600,
        "last_request_at": iso(300),
        "idle_since": iso(20),
        "stop_due_at": iso(20 - 600),
        "ports": [
            {"port": 8000, "connections": 0, "last_connection_at": None},
            {"port": 22, "connections": 1, "last_connection_at": iso(20)},
        ],
    }
    status.update(overrides)
    return status


class IdleWatchdogReadTests(SimpleTestCase):
    def test_read_status(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "status.json"
            self.assertIsNone(idle_watchdog.read_status(path))

            idle_watchdog.write_status(path, {"state": "idle"})
            self.assertEqual(idle_watchdog.read_status(path), {"state": "idle"})

            path.write_text("[1, 2]")
            with self.assertRaisesMessage(ValueError, "doesn't hold a JSON object"):
                idle_watchdog.read_status(path)

            path.write_text("{")
            with self.assertRaises(ValueError):
                idle_watchdog.read_status(path)

    def test_system_uptime(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "uptime"
            self.assertIsNone(idle_watchdog.system_uptime(path))

            path.write_text("12345.67 54321.00\n")
            self.assertEqual(idle_watchdog.system_uptime(path), 12345.67)

            path.write_text("")
            self.assertIsNone(idle_watchdog.system_uptime(path))


class IdleWatchdogPageContextTests(SimpleTestCase):
    NOW = datetime.datetime(2027, 1, 15, 8, 0, tzinfo=datetime.UTC)

    def _context(
        self,
        status: dict[str, typing.Any] | None = None,
        env: dict[str, str] | None = None,
        raw: str | None = None,
        last_request: float | None = None,
    ) -> dict[str, typing.Any]:
        with tempfile.TemporaryDirectory() as tempdir:
            status_file = Path(tempdir) / "status.json"
            request_file = Path(tempdir) / "last-request"
            if last_request is not None:
                request_file.touch()
                os.utime(request_file, (last_request, last_request))
            if status is not None:
                idle_watchdog.write_status(status_file, status)
            if raw is not None:
                status_file.write_text(raw)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LIFETRACKER_IDLE_STOP": "monitor",
                        "LIFETRACKER_IDLE_STOP_STATUS_FILE": str(status_file),
                        "LIFETRACKER_IDLE_STOP_REQUEST_FILE": str(request_file),
                        **(env or {}),
                    },
                ),
                mock.patch.object(idle_watchdog, "system_uptime", return_value=90),
            ):
                return idle_watchdog_page_context(now=self.NOW)

    def test_format_duration(self) -> None:
        for seconds, expected in [
            (-5, "0 seconds"),
            (1, "1 second"),
            (59.9, "59 seconds"),
            (60, "1\xa0minute"),
            (3 * 3600 + 5 * 60 + 7, "3\xa0hours, 5\xa0minutes"),
        ]:
            with self.subTest(seconds=seconds):
                self.assertEqual(_format_duration(seconds), expected)

    def test_status(self) -> None:
        # The request file is newer than the status file's copy of its time
        # (as just after a request, before the next check), and wins.
        context = self._context(
            _watchdog_status(self.NOW),
            last_request=(self.NOW - datetime.timedelta(seconds=7)).timestamp(),
        )

        self.assertIsNone(context["config_error"])
        self.assertIsNone(context["status_error"])
        self.assertEqual(context["config"].mode, "monitor")
        self.assertEqual(context["system_uptime"], "1\xa0minute")
        status = context["status"]
        self.assertEqual(status["checked_ago"], "20 seconds")
        self.assertEqual(status["check_interval"], "30 seconds")
        self.assertEqual(status["idle_timeout"], "10\xa0minutes")
        self.assertEqual(status["uptime"], "1\xa0hour")
        self.assertEqual(status["last_request_at"], self.NOW - datetime.timedelta(seconds=7))
        self.assertEqual(status["last_request_at_ago"], "7 seconds")
        self.assertEqual(status["idle_since_ago"], "20 seconds")
        self.assertFalse(status["stale"])
        # Not the status file's stop_due_at: this page's request restarts the
        # idle timer.
        self.assertEqual(status["stop_at"], self.NOW + datetime.timedelta(minutes=10))
        self.assertEqual(
            [(port["port"], port["connections"], port["last_connection_ago"]) for port in status["ports"]],
            [(8000, 0, None), (22, 1, "20 seconds")],
        )

    def test_auto_backup(self) -> None:
        context = self._context(
            _watchdog_status(
                self.NOW,
                auto_backup={
                    "interval_seconds": 21600,
                    "running": False,
                    "reason": "at startup",
                    "last_started_at": (self.NOW - datetime.timedelta(seconds=90)).isoformat(),
                    "last_finished_at": (self.NOW - datetime.timedelta(seconds=80)).isoformat(),
                    "last_exit_status": 0,
                    "next_run_at": (self.NOW + datetime.timedelta(hours=6, seconds=-90)).isoformat(),
                },
            ),
            env={"LIFETRACKER_AUTO_BACKUP": "on"},
        )

        self.assertIsNone(context["status_error"])
        self.assertEqual(context["auto_backup_interval"], "6\xa0hours")
        backup = context["status"]["auto_backup"]
        self.assertEqual(backup["last_started_ago"], "1\xa0minute")
        self.assertEqual(backup["last_exit_status"], 0)
        self.assertEqual(backup["next_run_at"], self.NOW + datetime.timedelta(hours=6, seconds=-90))

    def test_auto_backup_off(self) -> None:
        context = self._context(_watchdog_status(self.NOW))

        self.assertIsNone(context["auto_backup_interval"])
        self.assertIsNone(context["status"]["auto_backup"])

    def test_no_request_file(self) -> None:
        # The status file's copy of the time is ignored, even without a file.
        status = self._context(_watchdog_status(self.NOW))["status"]
        self.assertIsNone(status["last_request_at"])
        self.assertIsNone(status["last_request_at_ago"])

    def test_stale(self) -> None:
        context = self._context(
            _watchdog_status(
                self.NOW,
                checked_at=(self.NOW - datetime.timedelta(minutes=30)).isoformat(),
            )
        )

        self.assertTrue(context["status"]["stale"])

    def test_before_first_check_judged_by_start_time(self) -> None:
        for started_seconds_ago, stale in [(10, False), (600, True)]:
            with self.subTest(started_seconds_ago=started_seconds_ago):
                started_at = self.NOW - datetime.timedelta(seconds=started_seconds_ago)
                context = self._context(_watchdog_status(self.NOW, checked_at=None, started_at=started_at.isoformat()))

                self.assertIsNone(context["status"]["checked_ago"])
                self.assertEqual(context["status"]["stale"], stale)

    def test_no_status_file(self) -> None:
        context = self._context()

        self.assertIsNone(context["status"])
        self.assertIsNone(context["status_error"])

    def test_invalid_config(self) -> None:
        context = self._context(env={"LIFETRACKER_IDLE_STOP": "sometimes"})

        self.assertIn("LIFETRACKER_IDLE_STOP must be one of", context["config_error"])
        self.assertIsNone(context["config"])

    def test_unreadable_status(self) -> None:
        context = self._context(raw="not json")

        self.assertIn("Couldn't read", context["status_error"])
        self.assertIsNone(context["status"])

    def test_unexpected_status_contents(self) -> None:
        status = _watchdog_status(self.NOW)
        del status["ports"]

        context = self._context(status)

        self.assertIn("Unexpected contents", context["status_error"])
        self.assertIsNone(context["status"])

    def test_status_with_times_without_time_zone(self) -> None:
        for field in ("started_at", "checked_at"):
            with self.subTest(field=field):
                naive = (self.NOW - datetime.timedelta(seconds=10)).replace(tzinfo=None).isoformat()
                context = self._context(_watchdog_status(self.NOW, **{field: naive}))

                self.assertIn("Unexpected contents", context["status_error"])
                self.assertIsNone(context["status"])

    def test_status_without_start_time(self) -> None:
        status = _watchdog_status(self.NOW, started_at=None)

        context = self._context(status)

        self.assertIn("Unexpected contents", context["status_error"])
        self.assertIn("no start time", context["status_error"])
        self.assertIsNone(context["status"])


class IdleWatchdogAdminViewTests(TestCase):
    URL = "/admin/idle-watchdog/"

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.status_file = Path(tempdir.name) / "status.json"
        env = mock.patch.dict(
            os.environ,
            {
                "LIFETRACKER_IDLE_STOP": "stop",
                "LIFETRACKER_IDLE_STOP_STATUS_FILE": str(self.status_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_url(self) -> None:
        self.assertEqual(reverse("admin:idle_watchdog"), self.URL)

    def test_top_bar_links_to_page_for_superusers_only(self) -> None:
        link = f'<a href="{self.URL}">Idle Watchdog</a>'
        self.assertContains(self.client.get(reverse("admin:index")), link, html=True)

        staff = User.objects.create_user("staff", "s@example.com", "pass")
        staff.is_staff = True
        staff.save()
        client = Client()
        client.login(username="staff", password="pass")
        self.assertNotContains(client.get(reverse("admin:index")), self.URL)
        self.assertEqual(client.get(self.URL).status_code, 403)

    def test_page_requires_login(self) -> None:
        response = Client().get(self.URL)

        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.URL}",
            fetch_redirect_response=False,
        )

    def test_page_shows_status(self) -> None:
        idle_watchdog.write_status(
            self.status_file,
            _watchdog_status(timezone.now(), mode="stop", state="idle"),
        )

        response = self.client.get(self.URL)

        self.assertContains(response, "<code>stop</code>")
        self.assertContains(response, "Idle (no connections open)")
        self.assertContains(response, "Stops At")
        self.assertContains(response, "if nothing else happens (10\xa0minutes after")
        self.assertNotContains(response, "monitor mode")
        self.assertContains(response, "gunicorn master PID 41")
        self.assertContains(response, "<code>22</code>")
        self.assertNotContains(response, "has probably stopped running")

    def test_page_stop_time_by_state_and_mode(self) -> None:
        for mode, state, label, expected in [
            ("stop", "active", "Stops At", "Not while connections are open"),
            ("stop", "stopping", "Stops At", "Now: the idle timeout was reached"),
            ("stop", "backing up", "Stops At", "Once the backup finishes"),
            ("monitor", "idle", "Would Stop At", "(monitor mode: never stops)"),
        ]:
            with self.subTest(mode=mode, state=state):
                idle_watchdog.write_status(
                    self.status_file,
                    _watchdog_status(timezone.now(), mode=mode, state=state),
                )

                response = self.client.get(self.URL)

                self.assertContains(response, label)
                self.assertContains(response, expected)

    def test_page_shows_auto_backups(self) -> None:
        now = timezone.now()
        idle_watchdog.write_status(
            self.status_file,
            _watchdog_status(
                now,
                mode="stop",
                state="backing up",
                auto_backup={
                    "interval_seconds": 21600,
                    "running": True,
                    "reason": "before stopping",
                    "last_started_at": now.isoformat(),
                    "last_finished_at": None,
                    "last_exit_status": None,
                    "next_run_at": (now + datetime.timedelta(hours=6)).isoformat(),
                },
            ),
        )

        with mock.patch.dict(os.environ, {"LIFETRACKER_AUTO_BACKUP": "on"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "Backing up before stopping")
        self.assertContains(response, "At startup, every 6\xa0hours while up and before stopping")
        self.assertContains(response, "Before stopping, started 0")
        self.assertContains(response, "Once this one finishes, then")

    def test_page_with_auto_backups_off(self) -> None:
        idle_watchdog.write_status(self.status_file, _watchdog_status(timezone.now()))

        response = self.client.get(self.URL)

        self.assertContains(response, "<code>LIFETRACKER_AUTO_BACKUP</code>")
        self.assertNotContains(response, "Last Automatic Backup")

    def test_page_warns_when_stale(self) -> None:
        stale = timezone.now() - datetime.timedelta(hours=1)
        idle_watchdog.write_status(
            self.status_file,
            _watchdog_status(stale, state="stopping"),
        )

        response = self.client.get(self.URL)

        self.assertContains(
            response,
            "hasn't checked for longer than 3 check intervals, so it has probably stopped running",
        )
        self.assertContains(response, "Stopping gunicorn")
        self.assertContains(response, "Unknown: the watchdog doesn")
        self.assertNotContains(response, "Now: the idle timeout was reached")

    def test_page_without_status(self) -> None:
        response = self.client.get(self.URL)

        self.assertContains(response, "No status yet: the watchdog writes it")

    def test_page_when_off(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP": "off"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "No status yet: the watchdog is off.")

    def test_page_with_invalid_config(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP_MINUTES": "x"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "settings are invalid")

    def test_page_with_unreadable_status(self) -> None:
        self.status_file.write_text("{")

        response = self.client.get(self.URL)

        self.assertContains(response, "Couldn&#x27;t read")
