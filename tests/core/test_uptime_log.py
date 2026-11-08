import io
import os
import re
import tempfile
import typing
from pathlib import Path
from typing import override
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from lifetracker.core import uptime_log

if typing.TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


class UptimeLogTests(SimpleTestCase):
    DAY = 20_748  # 2026-10-22
    MIDNIGHT = DAY * uptime_log.SECONDS_PER_DAY

    def test_file_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT + 0.9)
            log.record(self.MIDNIGHT + 9)
            log.record(self.MIDNIGHT + 86_399)
            path = Path(tempdir) / "2026-10-22.bin"
            data = path.read_bytes()

        self.assertEqual(len(data), 10_800)
        # Most significant bit first: second 0 is 0x80 of byte 0.
        self.assertEqual(data[:2], b"\x80\x40")
        self.assertEqual(data[-1], 0x01)
        self.assertEqual(uptime_log.active_seconds(data), 3)

    def test_leaving_a_day_syncs_it(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT - 1)
            with mock.patch("os.fsync") as fsync:
                log.record(self.MIDNIGHT)

        fsync.assert_called_once()

    def test_reopening_keeps_earlier_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT + 1)
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT + 2)
            data = uptime_log.read_day(Path(tempdir), self.DAY)

        assert data is not None
        self.assertEqual(data[0], 0x60)

    def test_range_across_midnight(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT + 1, since=self.MIDNIGHT - 2)
            log.sync()
            before = uptime_log.read_day(Path(tempdir), self.DAY - 1)
            after = uptime_log.read_day(Path(tempdir), self.DAY)
            first = uptime_log.first_recorded_day(Path(tempdir))

        assert before is not None
        assert after is not None
        self.assertEqual(before[-1], 0x03)
        self.assertEqual(after[0], 0xC0)
        self.assertEqual(first, self.DAY - 1)

    def test_missing_and_short_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            self.assertIsNone(uptime_log.read_day(Path(tempdir), self.DAY))
            self.assertIsNone(uptime_log.first_recorded_day(Path(tempdir)))
            (Path(tempdir) / "2026-10-22.bin").write_bytes(b"\xff")
            (Path(tempdir) / "notes.bin").write_bytes(b"")
            data = uptime_log.read_day(Path(tempdir), self.DAY)
            first = uptime_log.first_recorded_day(Path(tempdir))

        assert data is not None
        self.assertEqual(len(data), 10_800)
        self.assertEqual(uptime_log.active_seconds(data), 8)
        self.assertEqual(first, self.DAY)

    def test_start_count(self) -> None:
        data = bytearray(10_800)
        data[0] = 0b1100_1000  # up at midnight, then a start at second 4
        data[1] = 0b0000_0001  # ... and at second 15
        data[2] = 0b1000_0000  # still up at 16

        self.assertEqual(uptime_log.start_count(bytes(data)), 3)
        self.assertEqual(uptime_log.start_count(bytes(10_800)), 0)

    def test_up_periods(self) -> None:
        data = bytearray(10_800)
        data[0] = 0b1100_1000  # up at midnight, then a start at second 4
        data[1] = 0b0000_0001  # ... and at second 15
        data[2] = 0b1000_0000  # still up at 16
        data[-1] = 0b0000_0011  # and up for the day's last two seconds

        periods = uptime_log.up_periods(bytes(data))

        self.assertEqual(
            [(p.start, p.end, p.seconds) for p in periods],
            [(0, 1, 2), (4, 4, 1), (15, 16, 2), (86_398, 86_399, 2)],
        )
        self.assertEqual(len(periods), uptime_log.start_count(bytes(data)))
        self.assertEqual(sum(p.seconds for p in periods), uptime_log.active_seconds(bytes(data)))
        self.assertEqual(uptime_log.up_periods(bytes(10_800)), [])
        self.assertEqual(
            [(p.start, p.end) for p in uptime_log.up_periods(b"\xff" * 10_800)],
            [(0, 86_399)],
        )

    def test_day_periods_across_midnight(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            # Up from 23:00 the day before until 01:00, then 02:00 to the end
            # of the day, which the next day continues.
            log.record(self.MIDNIGHT + 3599, since=self.MIDNIGHT - 3600)
            log.record(self.MIDNIGHT + 86_400, since=self.MIDNIGHT + 7200)
            now = self.MIDNIGHT + 2 * 86_400
            result = uptime_log.day_periods(Path(tempdir), self.DAY, now)
            before = uptime_log.day_periods(Path(tempdir), self.DAY - 1, now)
            empty_after = uptime_log.day_periods(Path(tempdir), self.DAY + 2, now)

        self.assertEqual(result.date, uptime_log.day_date(self.DAY))
        assert result.periods is not None
        self.assertEqual(
            [(p.start, p.end) for p in result.periods],
            [(0, 3599), (7200, 86_399)],
        )
        self.assertTrue(result.from_previous_day)
        self.assertTrue(result.into_next_day)
        self.assertFalse(result.ongoing)
        self.assertFalse(result.today)
        # The day before ran to midnight, but didn't begin the day before it.
        self.assertFalse(before.from_previous_day)
        self.assertTrue(before.into_next_day)
        self.assertEqual(empty_after.periods, [])
        self.assertTrue(empty_after.today)

    def test_day_periods_midnight_without_neighbours(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT + 86_399, since=self.MIDNIGHT)
            result = uptime_log.day_periods(Path(tempdir), self.DAY, self.MIDNIGHT + 2 * 86_400)

        assert result.periods is not None
        self.assertEqual([(p.start, p.end) for p in result.periods], [(0, 86_399)])
        self.assertFalse(result.from_previous_day)
        self.assertFalse(result.into_next_day)

    def test_day_periods_ongoing_today(self) -> None:
        now = self.MIDNIGHT + 3600.5
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT + 100, since=self.MIDNIGHT + 10)
            log.record(now - 5, since=self.MIDNIGHT + 600)
            ongoing = uptime_log.day_periods(Path(tempdir), self.DAY, now)
            stopped = uptime_log.day_periods(Path(tempdir), self.DAY, now + 10)

        assert ongoing.periods is not None
        self.assertEqual(len(ongoing.periods), 2)
        self.assertTrue(ongoing.today)
        self.assertTrue(ongoing.ongoing)
        self.assertFalse(stopped.ongoing)

    def test_day_periods_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            empty = uptime_log.day_periods(Path(tempdir), self.DAY, self.MIDNIGHT)
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT)
            before = uptime_log.day_periods(Path(tempdir), self.DAY - 1, self.MIDNIGHT)

        self.assertIsNone(empty.periods)
        self.assertIsNone(before.periods)
        self.assertFalse(before.from_previous_day or before.into_next_day)


class UptimeReportCommandTests(SimpleTestCase):
    NOW = 20_748 * 86_400 + 43_199.5  # 2026-10-22T11:59:59.5Z

    def _report(self, env: dict[str, str], days: int | None = None) -> str:
        out = io.StringIO()
        kwargs = {} if days is None else {"days": days}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("time.time", return_value=self.NOW),
        ):
            call_command("uptime_report", stdout=out, **kwargs)
        return out.getvalue()

    def test_report(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            # Half of 2026-10-20, none of the 21st, a quarter of today so far.
            log.record(20_746 * 86_400 + 43_199, since=20_746 * 86_400)
            log.record(self.NOW, since=self.NOW - 10_799.5)
            report = self._report({"LIFETRACKER_UPTIME_DIR": tempdir}, days=4)

        self.assertEqual(
            report.splitlines(),
            [
                "2026-10-19  no data",
                "2026-10-20   50.00%  up 43200 of 86400 s, 1 starts",
                "2026-10-21    0.00%  up 0 of 86400 s, 0 starts",
                "2026-10-22   25.00%  up 10800 of 43200 s, 1 starts  (so far)",
            ],
        )

    def test_errors(self) -> None:
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": "off"}),
            self.assertRaisesMessage(CommandError, "Uptime isn't recorded"),
        ):
            call_command("uptime_report")
        with self.assertRaisesMessage(CommandError, "--days must be at least 1"):
            self._report({"LIFETRACKER_UPTIME_DIR": "/nonexistent"}, days=0)


def _table_rows(response: TestResponse) -> list[list[str]]:
    """The cells of each row of the page's table body."""
    return [
        [cell.strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        for row in re.findall(
            r"<tr>(.*?)</tr>",
            response.content.decode().split("<tbody>")[1],
            re.S,
        )
    ]


class UptimeReportAdminViewTests(TestCase):
    URL = "/admin/uptime/"
    NOW = UptimeReportCommandTests.NOW

    @override
    def setUp(self) -> None:
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.directory = Path(tempdir.name)
        for patcher in (
            mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": tempdir.name}),
            mock.patch("time.time", return_value=self.NOW),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rows(self, response: TestResponse) -> list[list[str]]:
        return _table_rows(response)

    def test_url(self) -> None:
        self.assertEqual(reverse("admin:uptime_report"), self.URL)

    def test_top_bar_links_to_page_for_superusers_only(self) -> None:
        link = f'<a href="{self.URL}">Uptime</a>'
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

    def test_page_shows_report(self) -> None:
        log = uptime_log.UptimeLog(self.directory)
        # As in UptimeReportCommandTests.test_report.
        log.record(20_746 * 86_400 + 43_199, since=20_746 * 86_400)
        log.record(self.NOW, since=self.NOW - 10_799.5)

        response = self.client.get(self.URL, {"days": "4"})

        self.assertContains(response, f"<code>{self.directory}</code>")
        self.assertContains(response, 'value="4"')
        rows = self._rows(response)
        # Newest first.
        self.assertEqual(
            [(row[0].split()[0], row[1]) for row in rows],
            [
                ("2026-10-22", "25.00%"),
                ("2026-10-21", "0.00%"),
                ("2026-10-20", "50.00%"),
                ("2026-10-19", "No data"),
            ],
        )
        self.assertIn("(so far)", rows[0][0])
        self.assertEqual(rows[0][3], "3\xa0hours of 12\xa0hours")
        self.assertEqual(rows[0][4], '<a href="/admin/uptime/2026-10-22/?days=4">1</a>')
        self.assertEqual(rows[3][4], "")
        self.assertEqual(rows[2][3], "12\xa0hours of 1\xa0day")
        self.assertIn('<meter min="0" max="100" value="50.00">', rows[2][2])

    def test_default_days(self) -> None:
        response = self.client.get(self.URL)

        self.assertEqual(len(self._rows(response)), 7)
        self.assertNotContains(response, "errornote")

    def test_bad_days(self) -> None:
        for days in ("0", "367", "x"):
            with self.subTest(days=days):
                response = self.client.get(self.URL, {"days": days})

                self.assertContains(response, "Days must be a whole number from 1 to 366")
                self.assertEqual(len(self._rows(response)), 7)

    def test_not_recorded(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": "off"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "Uptime isn't recorded")
        self.assertNotContains(response, 'lt-uptime"')

    def test_today_start_in_local_time(self) -> None:
        response = self.client.get(self.URL)

        self.assertContains(response, "/static/core/local_times.js")
        self.assertContains(
            response,
            "today's UTC day began at <time data-lt-local datetime=\""
            '2026-10-22T00:00:00Z">2026-10-22T00:00:00Z</time>.',
        )


class UptimeDayAdminViewTests(TestCase):
    NOW = UptimeReportCommandTests.NOW  # 2026-10-22T11:59:59.5Z
    URL = "/admin/uptime/2026-10-20/"

    @override
    def setUp(self) -> None:
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.directory = Path(tempdir.name)
        for patcher in (
            mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": tempdir.name}),
            mock.patch("time.time", return_value=self.NOW),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rows(self, response: TestResponse) -> list[list[str]]:
        """The table's cells, as for the uptime page, but as text: the local
        times' ``<time>`` elements hold the UTC time until ``local_times.js``
        replaces it."""
        return [
            [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip() for cell in row] for row in _table_rows(response)
        ]

    def _record(self) -> None:
        log = uptime_log.UptimeLog(self.directory)
        midnight = 20_746 * 86_400  # 2026-10-20
        # From 23:30 the day before to 00:30, 09:05:03 for 2 hours, and the
        # last second of the day, which the next day doesn't continue.
        log.record(midnight + 1799, since=midnight - 1800)
        log.record(midnight + 32_703 + 7199, since=midnight + 32_703)
        log.record(midnight + 86_399)
        log.record(self.NOW, since=self.NOW - 59.5)

    def test_url(self) -> None:
        self.assertEqual(reverse("admin:uptime_day", args=["2026-10-20"]), self.URL)

    def test_superusers_only(self) -> None:
        staff = User.objects.create_user("staff", "s@example.com", "pass")
        staff.is_staff = True
        staff.save()
        client = Client()
        client.login(username="staff", password="pass")
        self.assertEqual(client.get(self.URL).status_code, 403)
        self.assertRedirects(
            Client().get(self.URL),
            f"{reverse('admin:login')}?next={self.URL}",
            fetch_redirect_response=False,
        )

    def test_report_links_starts(self) -> None:
        self._record()

        response = self.client.get("/admin/uptime/", {"days": "5"})

        self.assertContains(response, '<a href="/admin/uptime/2026-10-20/?days=5">3</a>', html=True)
        self.assertContains(response, '<a href="/admin/uptime/2026-10-21/?days=5">0</a>', html=True)
        # No data, so no link.
        self.assertContains(response, "No data")
        self.assertNotContains(response, "/admin/uptime/2026-10-18/")

    def test_page_lists_periods(self) -> None:
        self._record()

        response = self.client.get(self.URL, {"days": "4"})

        self.assertEqual(
            self._rows(response),
            [
                [
                    "00:00:00 (2026-10-20T00:00:00Z local)",
                    "00:29:59 (2026-10-20T00:29:59Z local)",
                    "00:30:00",
                    "Began the day before.",
                ],
                [
                    "09:05:03 (2026-10-20T09:05:03Z local)",
                    "11:05:02 (2026-10-20T11:05:02Z local)",
                    "02:00:00",
                    "",
                ],
                [
                    "23:59:59 (2026-10-20T23:59:59Z local)",
                    "23:59:59 (2026-10-20T23:59:59Z local)",
                    "00:00:01",
                    "",
                ],
            ],
        )
        self.assertContains(response, "3 periods, 02:30:01 in all")
        self.assertContains(response, "<title>Uptime on 2026-10-20")
        # Links back to the report, and to the days either side, keep its days.
        self.assertContains(response, 'href="/admin/uptime/?days=4"')
        self.assertContains(response, 'href="/admin/uptime/2026-10-19/?days=4"')
        self.assertContains(response, 'href="/admin/uptime/2026-10-21/?days=4"')

    def test_today(self) -> None:
        self._record()

        response = self.client.get("/admin/uptime/2026-10-22/")

        self.assertEqual(
            self._rows(response),
            [
                [
                    "11:59:00 (2026-10-22T11:59:00Z local)",
                    "11:59:59 (2026-10-22T11:59:59Z local)",
                    "00:01:00",
                    "Still up.",
                ]
            ],
        )
        self.assertContains(response, "so far")
        # No next day yet, and no days parameter to keep.
        self.assertNotContains(response, "2026-10-23")
        self.assertContains(response, 'href="/admin/uptime/"')

    def test_continues_into_next_day(self) -> None:
        uptime_log.UptimeLog(self.directory).record(20_747 * 86_400 + 10, since=20_747 * 86_400 - 10)

        response = self.client.get("/admin/uptime/2026-10-20/")

        self.assertEqual(
            self._rows(response),
            [
                [
                    "23:59:50 (2026-10-20T23:59:50Z local)",
                    "23:59:59 (2026-10-20T23:59:59Z local)",
                    "00:00:10",
                    "Continued into the next day.",
                ]
            ],
        )

    def test_local_times(self) -> None:
        self._record()

        response = self.client.get(self.URL)

        # local_times.js shows them in the browser's time zone, and unhides
        # them and the day's local range, hidden without it.
        self.assertContains(response, "/static/core/local_times.js")
        self.assertContains(
            response,
            '<span data-lt-local-only hidden>(<time data-lt-local="weekday"'
            ' datetime="2026-10-20T09:05:03Z">2026-10-20T09:05:03Z</time> local)'
            "</span>",
            html=True,
        )
        self.assertContains(response, "data-lt-local-only hidden", count=7)
        self.assertContains(
            response,
            '<time data-lt-local datetime="2026-10-20T00:00:00Z">'
            '2026-10-20T00:00:00Z</time> to <time data-lt-local datetime="'
            '2026-10-20T23:59:59Z">2026-10-20T23:59:59Z</time>',
        )

    def test_first_possible_date_local_range(self) -> None:
        uptime_log.UptimeLog(self.directory).record(self.NOW)

        response = self.client.get("/admin/uptime/0001-01-01/")

        self.assertEqual(response.context["day_start"], "0001-01-01T00:00:00Z")
        self.assertEqual(response.context["day_end"], "0001-01-01T23:59:59Z")

    def test_no_periods_and_no_data(self) -> None:
        uptime_log.UptimeLog(self.directory).record(20_746 * 86_400)

        empty = self.client.get("/admin/uptime/2026-10-21/")
        before = self.client.get("/admin/uptime/2026-10-19/")

        self.assertContains(empty, "The app wasn't up on 2026-10-21.")
        self.assertContains(before, "No data: 2026-10-19 is before the first file")
        self.assertNotContains(before, "<tbody>")

    def test_bad_and_future_dates(self) -> None:
        for date in ("2026-10-23", "2026-13-01", "yesterday"):
            with self.subTest(date=date):
                self.assertEqual(self.client.get(f"/admin/uptime/{date}/").status_code, 404)

    def test_first_possible_date(self) -> None:
        response = self.client.get("/admin/uptime/0001-01-01/")

        self.assertContains(response, "No data: 0001-01-01")
        self.assertContains(response, 'href="/admin/uptime/0001-01-02/"')
        self.assertNotContains(response, "←")

    def test_bad_days_dropped(self) -> None:
        response = self.client.get(self.URL, {"days": "0"})

        self.assertContains(response, 'href="/admin/uptime/"')
        self.assertNotContains(response, "days=")

    def test_not_recorded(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": "off"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "Uptime isn't recorded")
