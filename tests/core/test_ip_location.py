"""Tests for the IP location lookups (core/ip_location.py): the DB-IP Lite
databases, the iCloud Private Relay ranges, and update_geoip."""

import datetime
import gzip
import io
import sqlite3
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn
from unittest import mock

import maxminddb
import requests
from django.test import SimpleTestCase

from lifetracker.core import ip_location
from tests.core.mmdb_writer import write_geoip, write_mmdb

if TYPE_CHECKING:
    pass
from tests.core.client_ip_helpers import RELAY_LINES, TempDirMixin, write_relay_ranges

# tests/conftest.py replaces it in every test, so it never downloads anything.
download_relay_ranges = ip_location._download_relay_ranges


def relay_lookup(geoip_dir: Path | None, ip: str) -> ip_location.Relay:
    relay = ip_location.RelayRanges(geoip_dir)
    try:
        return relay.lookup(ip)
    finally:
        relay.close()


class GeoIPTests(TempDirMixin, SimpleTestCase):
    def test_lookup(self) -> None:
        write_geoip(self.temp)
        geoip = ip_location.GeoIP(self.temp)
        self.addCleanup(geoip.close)
        self.assertEqual(
            geoip.lookup("203.0.113.7"),
            ip_location.Location("AU", "Australia", 64500, "Example ISP"),
        )
        self.assertEqual(geoip.lookup("2001:db8::1"), ip_location.Location("NZ", "NZ", None, None))
        self.assertFalse(geoip.lookup("198.51.100.1"))

    def test_no_databases(self) -> None:
        self.assertFalse(ip_location.GeoIP(self.temp).lookup("203.0.113.7"))
        self.assertFalse(ip_location.GeoIP(None).lookup("203.0.113.7"))

    def test_uses_newest_and_reopens_for_a_newer_one(self) -> None:
        write_geoip(self.temp, "2026-09")
        geoip = ip_location.GeoIP(self.temp)
        self.addCleanup(geoip.close)
        self.assertEqual(geoip.lookup("203.0.113.7").asn, 64500)
        write_mmdb(
            self.temp / "dbip-asn-lite-2026-10.mmdb",
            {
                "203.0.113.0/24": {
                    "autonomous_system_number": 64501,
                    "autonomous_system_organization": "New ISP",
                }
            },
        )
        self.assertEqual(geoip.lookup("203.0.113.7").asn, 64501)

    def test_unreadable_database_is_skipped(self) -> None:
        (self.temp / "dbip-asn-lite-2026-10.mmdb").write_bytes(b"junk")
        self.assertFalse(ip_location.GeoIP(self.temp).lookup("203.0.113.7"))


class UpdateGeoIPTests(TempDirMixin, SimpleTestCase):
    # 2026-01-03 00:00 UTC.
    NOW = datetime.datetime(2026, 1, 3, tzinfo=datetime.UTC).timestamp()

    def _fake_download(
        self, available: set[str], fail: Collection[str] = ()
    ) -> tuple[Callable[[str, Path], bool], list[str]]:
        calls: list[str] = []

        def download(url: str, destination: Path) -> bool:
            calls.append(url)
            if url in fail:
                raise requests.ConnectionError("offline")
            if url not in available:
                return False
            destination.write_bytes(b"db")
            return True

        return download, calls

    def _url(self, name: str, month: str) -> str:
        return ip_location.GEOIP_URL.format(name=name, month=month)

    def _update(self, download: Callable[[str, Path], bool], now: float = NOW) -> dict[str, dict[str, Any]]:
        return ip_location.update_geoip(self.temp, now=now, download=download)

    def _files(self) -> list[str]:
        return sorted(path.name for path in self.temp.glob("*.mmdb"))

    def test_downloads_this_month(self) -> None:
        available = {self._url(name, "2026-01") for name in ip_location.GEOIP_DATABASES}
        download, calls = self._fake_download(available)
        (self.temp / "dbip-asn-lite-2025-11.mmdb").write_bytes(b"old")
        statuses = self._update(download)
        self.assertEqual(
            statuses["dbip-country-lite"],
            {
                "attempted": self.NOW,
                "outcome": ip_location.DOWNLOADED,
                "fell_back": False,
                "url": self._url("dbip-country-lite", "2026-01"),
                "file": "dbip-country-lite-2026-01.mmdb",
                "succeeded": self.NOW,
                "error": None,
                "traceback": None,
            },
        )
        self.assertEqual(statuses["dbip-asn-lite"]["file"], "dbip-asn-lite-2026-01.mmdb")
        self.assertEqual(
            self._files(),
            ["dbip-asn-lite-2026-01.mmdb", "dbip-country-lite-2026-01.mmdb"],
        )
        self.assertEqual(ip_location.read_geoip_status(self.temp), statuses)
        # Already there: nothing to download.
        statuses = self._update(download, now=self.NOW + 60)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {status["outcome"] for status in statuses.values()},
            {ip_location.UP_TO_DATE},
        )
        self.assertEqual(statuses["dbip-asn-lite"]["url"], None)
        self.assertEqual(statuses["dbip-asn-lite"]["succeeded"], self.NOW + 60)

    def test_falls_back_to_last_month(self) -> None:
        available = {self._url(name, "2025-12") for name in ip_location.GEOIP_DATABASES}
        download, calls = self._fake_download(available)
        statuses = self._update(download)
        self.assertEqual(
            [statuses[name]["file"] for name in ip_location.GEOIP_DATABASES],
            ["dbip-country-lite-2025-12.mmdb", "dbip-asn-lite-2025-12.mmdb"],
        )
        self.assertEqual(statuses["dbip-asn-lite"]["outcome"], ip_location.DOWNLOADED)
        self.assertTrue(statuses["dbip-asn-lite"]["fell_back"])
        self.assertEqual(statuses["dbip-asn-lite"]["url"], self._url("dbip-asn-lite", "2025-12"))
        # Next time, this month's is tried again, but last month's is kept.
        statuses = self._update(download)
        self.assertEqual(len(calls), 6)
        self.assertEqual(
            calls[4:],
            [self._url(name, "2026-01") for name in ip_location.GEOIP_DATABASES],
        )
        self.assertEqual(statuses["dbip-asn-lite"]["outcome"], ip_location.UP_TO_DATE)
        self.assertTrue(statuses["dbip-asn-lite"]["fell_back"])
        self.assertEqual(statuses["dbip-asn-lite"]["url"], self._url("dbip-asn-lite", "2026-01"))

    def test_neither_month_available(self) -> None:
        download, _ = self._fake_download(set())
        statuses = self._update(download)
        status = statuses["dbip-country-lite"]
        self.assertEqual(status["outcome"], ip_location.FAILED)
        self.assertEqual(
            status["error"],
            "OSError(\"Neither 2026-01's nor 2025-12's dbip-country-lite is available\")",
        )
        self.assertIn("Traceback (most recent call last)", status["traceback"])
        self.assertEqual(status["url"], self._url("dbip-country-lite", "2025-12"))
        self.assertIsNone(status["file"])
        self.assertIsNone(status["succeeded"])

    def test_failure_keeps_last_success_and_tries_the_other(self) -> None:
        country = self._url("dbip-country-lite", "2026-01")
        asn = self._url("dbip-asn-lite", "2026-01")
        download, _ = self._fake_download({country, asn})
        self._update(download)
        # A month later: the country database's download fails, the ASN's works.
        later = self.NOW + 31 * 86400
        country = self._url("dbip-country-lite", "2026-02")
        asn = self._url("dbip-asn-lite", "2026-02")
        download, _ = self._fake_download({asn}, fail={country})
        statuses = self._update(download, now=later)
        status = statuses["dbip-country-lite"]
        self.assertEqual(status["outcome"], ip_location.FAILED)
        self.assertEqual(status["attempted"], later)
        self.assertEqual(status["succeeded"], self.NOW)
        self.assertEqual(status["url"], country)
        self.assertEqual(status["error"], "ConnectionError('offline')")
        self.assertIn("requests.exceptions.ConnectionError", status["traceback"])
        # The old one stays in use.
        self.assertEqual(status["file"], "dbip-country-lite-2026-01.mmdb")
        self.assertEqual(statuses["dbip-asn-lite"]["outcome"], ip_location.DOWNLOADED)
        self.assertEqual(
            self._files(),
            ["dbip-asn-lite-2026-02.mmdb", "dbip-country-lite-2026-01.mmdb"],
        )

    def test_unreadable_previous_status_is_ignored(self) -> None:
        (self.temp / ip_location.GEOIP_STATUS_NAME).write_text("not json")
        download, _ = self._fake_download(set(), fail={self._url("dbip-asn-lite", "2026-01")})
        statuses = self._update(download)
        self.assertIsNone(statuses["dbip-asn-lite"]["succeeded"])
        self.assertEqual(ip_location.read_geoip_status(self.temp), statuses)

    def test_read_geoip_status(self) -> None:
        self.assertIsNone(ip_location.read_geoip_status(self.temp))
        path = self.temp / ip_location.GEOIP_STATUS_NAME
        for contents in ("{}", '{"databases": []}', '{"databases": {"x": 1}}'):
            path.write_text(contents)
            with (
                self.subTest(contents),
                self.assertRaisesMessage(ValueError, "doesn't hold each database's status"),
            ):
                ip_location.read_geoip_status(self.temp)
        for contents in ("[]", "{"):
            path.write_text(contents)
            with self.subTest(contents), self.assertRaises(ValueError):
                ip_location.read_geoip_status(self.temp)


class DownloadTests(TempDirMixin, SimpleTestCase):
    def _response(self, status: int, body: bytes = b"") -> mock.MagicMock:
        response = mock.MagicMock(status_code=status, raw=io.BytesIO(body))
        response.__enter__.return_value = response
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status))
        return response

    def test_downloads_and_unzips(self) -> None:
        source = self.temp / "source.mmdb"
        write_mmdb(source, {"203.0.113.0/24": {"x": 1}})
        destination = self.temp / "dbip-asn-lite-2026-10.mmdb"
        with mock.patch.object(
            requests,
            "get",
            return_value=self._response(200, gzip.compress(source.read_bytes())),
        ) as get:
            self.assertTrue(ip_location._download("https://x/y.gz", destination))
        get.assert_called_once_with("https://x/y.gz", stream=True, timeout=ip_location.GEOIP_TIMEOUT_SECONDS)
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

    def test_not_found(self) -> None:
        destination = self.temp / "x.mmdb"
        with mock.patch.object(requests, "get", return_value=self._response(404)):
            self.assertFalse(ip_location._download("https://x/y.gz", destination))
        self.assertFalse(destination.exists())

    def test_error(self) -> None:
        with mock.patch.object(requests, "get", return_value=self._response(500)):
            with self.assertRaises(requests.HTTPError):
                ip_location._download("https://x/y.gz", self.temp / "x.mmdb")

    def test_not_a_database_is_left_out(self) -> None:
        destination = self.temp / "x.mmdb"
        with mock.patch.object(requests, "get", return_value=self._response(200, gzip.compress(b"junk"))):
            with self.assertRaises(maxminddb.InvalidDatabaseError):
                ip_location._download("https://x/y.gz", destination)
        self.assertEqual(list(self.temp.iterdir()), [])


class RelayRangesTests(TempDirMixin, SimpleTestCase):
    def test_lookup(self) -> None:
        count = write_relay_ranges(
            self.temp,
            [
                *RELAY_LINES,
                "",
                "# a comment",
                "not a prefix,XX,,,",
                # Host bits set, and nothing but the prefix.
                "198.51.100.9/30",
            ],
        )
        self.assertEqual(count, 4)
        relay = ip_location.RelayRanges(self.temp)
        self.addCleanup(relay.close)
        sydney = ip_location.Relay(True, "AU", "AU-NSW", "Sydney")
        not_relay = ip_location.Relay(False)
        for ip, expected in (
            ("203.0.113.0", sydney),
            ("203.0.113.31", sydney),
            ("203.0.113.32", not_relay),
            ("203.0.113.65", ip_location.Relay(True, "AU", "AU-VIC", "Melbourne")),
            ("203.0.113.66", not_relay),
            ("198.51.100.11", ip_location.Relay(True)),
            ("2001:db8:1::abcd", ip_location.Relay(True, "NZ", "NZ-AUK", "Auckland")),
            ("2001:db8:1:1::", not_relay),
            ("192.0.2.1", not_relay),
            ("::1", not_relay),
        ):
            with self.subTest(ip):
                self.assertEqual(relay.lookup(ip), expected)

    def test_no_ranges(self) -> None:
        self.assertEqual(relay_lookup(self.temp, "203.0.113.1"), ip_location.Relay())
        self.assertEqual(relay_lookup(None, "203.0.113.1"), ip_location.Relay())
        (self.temp / ip_location.RELAY_RANGES_FILE).write_bytes(b"junk" * 100)
        self.assertEqual(relay_lookup(self.temp, "203.0.113.1"), ip_location.Relay())

    def test_unreadable_once_open(self) -> None:
        write_relay_ranges(self.temp)
        relay = ip_location.RelayRanges(self.temp)
        self.addCleanup(relay.close)
        self.assertTrue(relay.lookup("203.0.113.1").private_relay)
        assert relay._connection is not None
        relay._connection.close()
        relay._connection = mock.Mock(execute=mock.Mock(side_effect=sqlite3.DatabaseError("disk I/O error")))
        self.assertEqual(relay.lookup("203.0.113.1"), ip_location.Relay())

    def test_reopens_when_replaced(self) -> None:
        write_relay_ranges(self.temp)
        geoip = ip_location.GeoIP(self.temp)
        self.addCleanup(geoip.close)
        self.assertTrue(geoip.relay("203.0.113.1").private_relay)
        write_relay_ranges(self.temp, ["192.0.2.0/24,US,US-CA,Los Angeles,"])
        self.assertFalse(geoip.relay("203.0.113.1").private_relay)
        self.assertEqual(geoip.relay("192.0.2.1").relay_city, "Los Angeles")
        (self.temp / ip_location.RELAY_RANGES_FILE).unlink()
        self.assertEqual(geoip.relay("192.0.2.1"), ip_location.Relay())

    def test_empty_list_keeps_the_old_ranges(self) -> None:
        write_relay_ranges(self.temp)
        with self.assertRaisesMessage(ValueError, "no ranges in the list"):
            write_relay_ranges(self.temp, ["", "junk"])
        self.assertEqual(
            sorted(path.name for path in self.temp.iterdir()),
            [ip_location.RELAY_RANGES_FILE],
        )
        self.assertTrue(relay_lookup(self.temp, "203.0.113.1").private_relay)

    def _response(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> mock.MagicMock:
        response = mock.MagicMock(status_code=status, headers=headers or {})
        response.iter_lines.return_value = iter(body.splitlines())
        response.__enter__.return_value = response
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status))
        return response

    def test_download(self) -> None:
        destination = self.temp / ip_location.RELAY_RANGES_FILE
        body = "\n".join(RELAY_LINES).encode()
        with mock.patch.object(
            requests,
            "get",
            return_value=self._response(200, body, {"ETag": '"v1"'}),
        ) as get:
            self.assertEqual(download_relay_ranges("https://x/y.csv", destination, None), '"v1"')
        get.assert_called_once_with(
            "https://x/y.csv",
            headers={},
            stream=True,
            timeout=ip_location.GEOIP_TIMEOUT_SECONDS,
        )
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
        self.assertTrue(relay_lookup(self.temp, "203.0.113.1").private_relay)
        # Without an ETag.
        with mock.patch.object(requests, "get", return_value=self._response(200, body)):
            self.assertEqual(download_relay_ranges("https://x/y.csv", destination, '"v1"'), "")

    def test_download_unchanged(self) -> None:
        with mock.patch.object(requests, "get", return_value=self._response(304)) as get:
            self.assertIsNone(download_relay_ranges("https://x/y.csv", self.temp / "r.sqlite3", '"v1"'))
        self.assertEqual(get.call_args.kwargs["headers"], {"If-None-Match": '"v1"'})
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_download_error(self) -> None:
        with mock.patch.object(requests, "get", return_value=self._response(500)):
            with self.assertRaises(requests.HTTPError):
                download_relay_ranges("https://x/y.csv", self.temp / "r.sqlite3", None)
        self.assertEqual(list(self.temp.iterdir()), [])


class UpdateRelayRangesTests(TempDirMixin, SimpleTestCase):
    NOW = 1_800_000_000.0

    def _update(self, download: Callable[[str, Path, str | None], str | None], now: float = NOW) -> dict[str, Any]:
        return ip_location.update_geoip(
            self.temp,
            now=now,
            download=lambda url, destination: False,
            download_relay_ranges=download,
        )[ip_location.RELAY_RANGES]

    def test_downloads_then_checks_etag(self) -> None:
        etags = []

        def download(url: str, destination: Path, etag: str | None) -> str | None:
            etags.append(etag)
            if etag == '"v1"':
                return None
            write_relay_ranges(destination.parent)
            return '"v1"'

        status = self._update(download)
        self.assertEqual(
            status,
            {
                "attempted": self.NOW,
                "url": ip_location.RELAY_RANGES_URL,
                "outcome": ip_location.DOWNLOADED,
                "fell_back": False,
                "file": ip_location.RELAY_RANGES_FILE,
                "succeeded": self.NOW,
                "etag": '"v1"',
                "error": None,
                "traceback": None,
            },
        )
        status = self._update(download, now=self.NOW + 60)
        self.assertEqual(etags, [None, '"v1"'])
        self.assertEqual(status["outcome"], ip_location.UP_TO_DATE)
        self.assertEqual(status["etag"], '"v1"')
        self.assertEqual(status["succeeded"], self.NOW + 60)
        statuses = ip_location.read_geoip_status(self.temp)
        assert statuses is not None
        self.assertEqual(statuses[ip_location.RELAY_RANGES], status)
        # Without the file, its ETag is no use.
        (self.temp / ip_location.RELAY_RANGES_FILE).unlink()
        status = self._update(download)
        self.assertEqual(etags[-1], None)
        self.assertEqual(status["outcome"], ip_location.DOWNLOADED)

    def test_failure_keeps_the_last_list(self) -> None:
        def download(url: str, destination: Path, etag: str | None) -> str:
            write_relay_ranges(destination.parent)
            return '"v1"'

        self._update(download)

        def fail(url: str, destination: Path, etag: str | None) -> NoReturn:
            raise requests.ConnectionError("offline")

        status = self._update(fail, now=self.NOW + 60)
        self.assertEqual(status["outcome"], ip_location.FAILED)
        self.assertEqual(status["error"], "ConnectionError('offline')")
        self.assertIn("requests.exceptions.ConnectionError", status["traceback"])
        self.assertEqual(status["succeeded"], self.NOW)
        self.assertEqual(status["file"], ip_location.RELAY_RANGES_FILE)
        self.assertEqual(status["etag"], '"v1"')

    def test_failure_without_a_list(self) -> None:
        def fail(url: str, destination: Path, etag: str | None) -> NoReturn:
            raise ValueError("no ranges in the list")

        status = self._update(fail)
        self.assertEqual(status["outcome"], ip_location.FAILED)
        self.assertIsNone(status["succeeded"])
        self.assertIsNone(status["file"])
        self.assertIsNone(status["etag"])
