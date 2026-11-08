"""Tests for the client IP record (core/client_ips.py), its admin page and the
update_geoip and client_ips_export commands (core/ip_location.py's own tests
are in test_ip_location.py)."""

import datetime
import gzip
import io
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, override
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from lifetracker.core import client_ips, ip_location, sqlite_helpers
from tests.core.client_ip_helpers import TempDirMixin, write_relay_ranges
from tests.core.mmdb_writer import write_geoip, write_mmdb

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse


def envoy_line(**fields: object) -> str:
    """A line of Envoy's request log, as envoy/envoy.yaml's json_format
    writes it (tests/core/test_envoy.py checks that against the real Envoy)."""
    return json.dumps(
        {
            "time": "1761200000.123",
            "fly_client_ip": "203.0.113.7",
            "peer": "172.16.0.2",
            "method": "GET",
            "path": "/admin/",
            "status": 302,
            "user_agent": "curl/8",
            "jwt_failure": None,
            "jwt_sub": None,
            **fields,
        }
    )


def request(ip: str = "203.0.113.7", time: float = 1000.0, **fields: Any) -> client_ips.Request:
    return client_ips.Request(
        **{
            "ip": ip,
            "time": time,
            "request_line": "GET /admin/",
            "status": 200,
            "user_agent": "UA",
            "rejected": False,
            "logged_in": False,
            **fields,
        }
    )


def _parse(line: str) -> client_ips.Request:
    parsed = client_ips.parse_line(line)
    assert parsed is not None
    return parsed


class ConfigTests(SimpleTestCase):
    def test_defaults_under_data_dir(self) -> None:
        config = client_ips.config_from_env({"LIFETRACKER_DATA_DIR": "/data", "TMPDIR": "/scratch"})
        self.assertEqual(config.db_path, Path("/data/client_ips.sqlite3"))
        self.assertEqual(config.geoip_dir, Path("/data/geoip"))
        self.assertEqual(config.request_log, Path("/scratch/lifetracker-envoy-requests.jsonl"))
        self.assertTrue(config.download)

    def test_off_without_data_dir(self) -> None:
        config = client_ips.config_from_env({})
        self.assertIsNone(config.db_path)
        self.assertIsNone(config.geoip_dir)
        self.assertEqual(config.request_log, Path("/tmp/lifetracker-envoy-requests.jsonl"))

    def test_off(self) -> None:
        config = client_ips.config_from_env({"LIFETRACKER_DATA_DIR": "/data", "LIFETRACKER_CLIENT_IP_DB": "Off"})
        self.assertIsNone(config.db_path)

    def test_explicit(self) -> None:
        config = client_ips.config_from_env(
            {
                "LIFETRACKER_CLIENT_IP_DB": "/x/ips.sqlite3",
                "LIFETRACKER_CLIENT_IP_REQUEST_LOG": "/y/log",
                "LIFETRACKER_GEOIP_DIR": "/z",
                "LIFETRACKER_GEOIP_DOWNLOAD": "0",
            }
        )
        self.assertEqual(config.db_path, Path("/x/ips.sqlite3"))
        self.assertEqual(config.request_log, Path("/y/log"))
        self.assertEqual(config.geoip_dir, Path("/z"))
        self.assertFalse(config.download)


class ParseLineTests(SimpleTestCase):
    def test_rejected(self) -> None:
        line = envoy_line(jwt_failure={"code": 401, "message": "Jwt is missing"})
        self.assertEqual(
            client_ips.parse_line(line),
            client_ips.Request(
                ip="203.0.113.7",
                time=1761200000.123,
                request_line="GET /admin/",
                status=302,
                user_agent="curl/8",
                rejected=True,
                logged_in=False,
            ),
        )

    def test_logged_in(self) -> None:
        parsed = _parse(envoy_line(jwt_sub="1", status=200))
        self.assertTrue(parsed.logged_in)
        self.assertFalse(parsed.rejected)
        self.assertEqual(parsed.status, 200)

    def test_peer_without_fly_client_ip(self) -> None:
        for value in (None, "-", "not an IP"):
            with self.subTest(value):
                parsed = _parse(envoy_line(fly_client_ip=value))
                self.assertEqual(parsed.ip, "172.16.0.2")

    def test_ipv6_is_normalised(self) -> None:
        parsed = _parse(envoy_line(fly_client_ip="2001:DB8:0::1"))
        self.assertEqual(parsed.ip, "2001:db8::1")

    def test_missing_fields(self) -> None:
        parsed = _parse(envoy_line(method=None, path=None, status=None, user_agent=None))
        self.assertEqual(parsed.request_line, "- -")
        self.assertIsNone(parsed.status)
        self.assertEqual(parsed.user_agent, "")

    def test_long_text_is_cut_off(self) -> None:
        parsed = _parse(envoy_line(path="/" + "x" * 1000, user_agent="y" * 1000))
        self.assertEqual(len(parsed.request_line), client_ips.MAX_TEXT_LENGTH)
        self.assertEqual(len(parsed.user_agent), client_ips.MAX_TEXT_LENGTH)

    def test_not_a_request(self) -> None:
        for line in (
            "",
            '{"time": "1',
            "[]",
            json.dumps({"fly_client_ip": "203.0.113.7"}),
            envoy_line(fly_client_ip=None, peer=None),
        ):
            with self.subTest(line):
                self.assertIsNone(client_ips.parse_line(line))


class RequestLogReaderTests(TempDirMixin, SimpleTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.path = self.temp / "requests.jsonl"

    def test_missing_file(self) -> None:
        self.assertEqual(client_ips.RequestLogReader(self.path).read(), [])

    def test_reads_only_new_complete_lines(self) -> None:
        reader = client_ips.RequestLogReader(self.path)
        self.path.write_text("one\ntwo\nthr")
        self.assertEqual(reader.read(), ["one", "two"])
        self.assertEqual(reader.read(), [])
        with self.path.open("a") as file:
            file.write("ee\nfour\n")
        self.assertEqual(reader.read(), ["three", "four"])

    def test_empties_file_once_read_and_big(self) -> None:
        reader = client_ips.RequestLogReader(self.path, truncate_bytes=10)
        self.path.write_text("a\nb\n")
        self.assertEqual(reader.read(), ["a", "b"])
        self.assertEqual(self.path.stat().st_size, 4)
        self.path.write_text("a\nb\nc23456\n")
        self.assertEqual(reader.read(), ["c23456"])
        self.assertEqual(self.path.stat().st_size, 0)
        with self.path.open("a") as file:
            file.write("d\n")
        self.assertEqual(reader.read(), ["d"])

    def test_not_emptied_mid_line(self) -> None:
        reader = client_ips.RequestLogReader(self.path, truncate_bytes=1)
        self.path.write_text("a\npart")
        self.assertEqual(reader.read(), ["a"])
        self.assertEqual(self.path.read_text(), "a\npart")

    def test_starts_again_when_replaced(self) -> None:
        reader = client_ips.RequestLogReader(self.path)
        self.path.write_text("first line\n")
        reader.read()
        self.path.write_text("new\n")
        self.assertEqual(reader.read(), ["new"])
        self.path.unlink()
        self.assertEqual(reader.read(), [])
        self.path.write_text("again\n")
        self.assertEqual(reader.read(), ["again"])


class RecordTests(TempDirMixin, SimpleTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.temp / "client_ips.sqlite3"
        self.connection = client_ips.connect(self.db_path)
        self.addCleanup(self.connection.close)
        self.geoip_dir = self.temp / "geoip"
        self.geoip = ip_location.GeoIP(self.geoip_dir)
        self.addCleanup(self.geoip.close)

    def _row(self, ip: str = "203.0.113.7") -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM client_ip WHERE ip = ?", (ip,)).fetchone()
        assert row is not None
        return dict(row)

    def test_adds_up_requests_per_ip(self) -> None:
        write_geoip(self.geoip_dir)
        updated = client_ips.record(
            self.connection,
            [
                request(time=1002, rejected=True, request_line="GET /x", status=302),
                request(time=1000, logged_in=True),
                request(ip="198.51.100.1", time=1001),
            ],
            self.geoip,
        )
        self.assertEqual(updated, 2)
        self.assertEqual(
            self._row(),
            {
                "ip": "203.0.113.7",
                "first_seen": 1000,
                "last_seen": 1002,
                "requests": 2,
                "rejected": 1,
                "logged_in": 1,
                "first_logged_in": 1000,
                "last_logged_in": 1000,
                "last_request": "GET /x",
                "last_status": 302,
                "last_user_agent": "UA",
                "country": "AU",
                "country_name": "Australia",
                "asn": 64500,
                "as_org": "Example ISP",
                # No relay ranges to check against.
                "private_relay": None,
                "relay_country": None,
                "relay_region": None,
                "relay_city": None,
            },
        )
        other = self._row("198.51.100.1")
        self.assertEqual((other["requests"], other["country"]), (1, None))

        client_ips.record(
            self.connection,
            [
                request(time=1005, logged_in=True, request_line="GET /y", user_agent="B"),
                # Older than what's recorded, e.g. out of order.
                request(time=900),
            ],
            self.geoip,
        )
        row = self._row()
        self.assertEqual(
            {
                k: row[k]
                for k in (
                    "first_seen",
                    "last_seen",
                    "requests",
                    "rejected",
                    "logged_in",
                    "first_logged_in",
                    "last_logged_in",
                    "last_request",
                    "last_user_agent",
                )
            },
            {
                "first_seen": 900,
                "last_seen": 1005,
                "requests": 4,
                "rejected": 1,
                "logged_in": 2,
                "first_logged_in": 1000,
                "last_logged_in": 1005,
                "last_request": "GET /y",
                "last_user_agent": "B",
            },
        )

    def test_latest_request_kept_when_batch_is_older(self) -> None:
        client_ips.record(self.connection, [request(time=1000)], self.geoip)
        client_ips.record(self.connection, [request(time=500, request_line="GET /old")], self.geoip)
        self.assertEqual(self._row()["last_request"], "GET /admin/")
        self.assertEqual(self._row()["last_seen"], 1000)

    def test_failed_batch_is_rolled_back(self) -> None:
        geoip = mock.Mock(
            lookup=mock.Mock(side_effect=[ip_location.Location(), ValueError]),
            relay=mock.Mock(return_value=ip_location.Relay()),
        )
        with self.assertRaises(ValueError):
            client_ips.record(self.connection, [request(), request(ip="198.51.100.1")], geoip)
        self.assertEqual(client_ips.ip_count(self.connection), 0)
        client_ips.record(self.connection, [request()], self.geoip)
        self.assertEqual(client_ips.ip_count(self.connection), 1)

    def test_nothing_to_record(self) -> None:
        self.assertEqual(client_ips.record(self.connection, [], self.geoip), 0)

    def test_location_kept_from_first_lookup(self) -> None:
        write_geoip(self.geoip_dir, "2026-09")
        client_ips.record(self.connection, [request()], self.geoip)
        write_mmdb(
            self.geoip_dir / "dbip-asn-lite-2026-10.mmdb",
            {"203.0.113.0/24": {"autonomous_system_number": 64501}},
        )
        client_ips.record(self.connection, [request(time=2000)], self.geoip)
        self.assertEqual(self._row()["asn"], 64500)

    def test_location_looked_up_once_available(self) -> None:
        client_ips.record(self.connection, [request()], self.geoip)
        self.assertIsNone(self._row()["asn"])
        write_geoip(self.geoip_dir)
        client_ips.record(self.connection, [request(time=2000)], self.geoip)
        self.assertEqual(self._row()["asn"], 64500)

    def test_fill_in_locations(self) -> None:
        client_ips.record(
            self.connection,
            [request(), request(ip="198.51.100.1")],
            self.geoip,
        )
        write_geoip(self.geoip_dir)
        self.assertEqual(client_ips.fill_in_locations(self.connection, self.geoip), 1)
        self.assertEqual(self._row()["country"], "AU")
        self.assertIsNone(self._row("198.51.100.1")["country"])

    def test_queries(self) -> None:
        write_geoip(self.geoip_dir)
        client_ips.record(
            self.connection,
            [
                request(time=1000, logged_in=True),
                request(ip="203.0.113.8", time=3000, logged_in=True),
                request(ip="198.51.100.1", time=2000, logged_in=True),
                request(ip="198.51.100.2", time=4000),
            ],
            self.geoip,
        )
        self.assertEqual(
            [row["ip"] for row in client_ips.recent_ips(self.connection, limit=3)],
            ["198.51.100.2", "203.0.113.8", "198.51.100.1"],
        )
        self.assertEqual(
            [row["ip"] for row in client_ips.recent_ips(self.connection, logged_in_only=True, limit=10)],
            ["203.0.113.8", "198.51.100.1", "203.0.113.7"],
        )
        self.assertEqual(client_ips.ip_count(self.connection), 4)
        self.assertEqual(client_ips.ip_count(self.connection, logged_in_only=True), 3)
        self.assertEqual(
            [dict(row) for row in client_ips.logged_in_networks(self.connection)],
            [
                {
                    "asn": 64500,
                    "as_org": "Example ISP",
                    "ips": 2,
                    "countries": "AU",
                    "logged_in": 2,
                    "relay_ips": None,
                    "first_logged_in": 1000,
                    "last_logged_in": 3000,
                },
                {
                    "asn": None,
                    "as_org": None,
                    "ips": 1,
                    "countries": None,
                    "logged_in": 1,
                    "relay_ips": None,
                    "first_logged_in": 2000,
                    "last_logged_in": 2000,
                },
            ],
        )

    def test_private_relay(self) -> None:
        write_relay_ranges(self.geoip_dir)
        client_ips.record(
            self.connection,
            [request(), request(ip="2001:db8:1::5"), request(ip="198.51.100.1")],
            self.geoip,
        )
        self.assertEqual(
            {
                k: self._row()[k]
                for k in (
                    "private_relay",
                    "relay_country",
                    "relay_region",
                    "relay_city",
                )
            },
            {
                "private_relay": 1,
                "relay_country": "AU",
                "relay_region": "AU-NSW",
                "relay_city": "Sydney",
            },
        )
        self.assertEqual(self._row("2001:db8:1::5")["relay_city"], "Auckland")
        self.assertEqual(self._row("198.51.100.1")["private_relay"], 0)
        # Checked again each time it's seen: no longer in the list.
        write_relay_ranges(self.geoip_dir, ["192.0.2.0/24,US,,,"])
        client_ips.record(self.connection, [request(time=2000)], self.geoip)
        self.assertEqual(self._row()["private_relay"], 0)
        self.assertIsNone(self._row()["relay_city"])
        # Without a list, what it had is kept.
        write_relay_ranges(self.geoip_dir)
        client_ips.record(self.connection, [request(time=3000)], self.geoip)
        (self.geoip_dir / ip_location.RELAY_RANGES_FILE).unlink()
        client_ips.record(self.connection, [request(time=4000)], self.geoip)
        self.assertEqual(self._row()["private_relay"], 1)
        self.assertEqual(self._row()["relay_city"], "Sydney")

    def test_fill_in_relay(self) -> None:
        client_ips.record(self.connection, [request(), request(ip="198.51.100.1")], self.geoip)
        self.assertIsNone(self._row()["private_relay"])
        self.assertEqual(client_ips.fill_in_relay(self.connection, self.geoip), 0)
        write_relay_ranges(self.geoip_dir)
        self.assertEqual(client_ips.fill_in_relay(self.connection, self.geoip), 2)
        self.assertEqual(self._row()["private_relay"], 1)
        self.assertEqual(self._row()["relay_city"], "Sydney")
        self.assertEqual(self._row("198.51.100.1")["private_relay"], 0)
        # Only those not checked yet.
        self.assertEqual(client_ips.fill_in_relay(self.connection, self.geoip), 0)

    def test_relay_ips_per_network(self) -> None:
        write_geoip(self.geoip_dir)
        write_relay_ranges(self.geoip_dir)
        client_ips.record(
            self.connection,
            [
                request(logged_in=True),
                request(ip="203.0.113.200", logged_in=True),
                request(ip="203.0.113.201", logged_in=True),
            ],
            self.geoip,
        )
        [network] = client_ips.logged_in_networks(self.connection)
        self.assertEqual((network["ips"], network["relay_ips"]), (3, 1))

    def test_upgrades_version_1(self) -> None:
        self.connection.close()
        path = self.temp / "v1.sqlite3"
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE client_ip (ip TEXT PRIMARY KEY, first_seen REAL NOT NULL,"
            " last_seen REAL NOT NULL, requests INTEGER NOT NULL,"
            " rejected INTEGER NOT NULL, logged_in INTEGER NOT NULL,"
            " first_logged_in REAL, last_logged_in REAL, last_request TEXT NOT NULL,"
            " last_status INTEGER, last_user_agent TEXT NOT NULL, country TEXT,"
            " country_name TEXT, asn INTEGER, as_org TEXT)"
        )
        old.execute(
            "INSERT INTO client_ip VALUES"
            " ('203.0.113.7', 1, 2, 3, 0, 0, NULL, NULL, 'GET /', 200, 'UA',"
            " 'AU', 'Australia', 64500, 'Example ISP')"
        )
        old.execute("PRAGMA user_version=1")
        old.commit()
        old.close()
        self.connection = connection = client_ips.connect(path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        row = dict(connection.execute("SELECT * FROM client_ip").fetchone())
        self.assertEqual(row["requests"], 3)
        self.assertIsNone(row["private_relay"])
        # And once the ranges are there, it's checked against them.
        write_relay_ranges(self.geoip_dir)
        self.assertEqual(client_ips.fill_in_relay(connection, self.geoip), 1)
        # Connecting again changes nothing.
        client_ips.connect(path).close()

    def test_read_only_connection(self) -> None:
        client_ips.record(self.connection, [request()], self.geoip)
        reader = sqlite_helpers.connect_read_only(self.db_path)
        self.addCleanup(reader.close)
        self.assertEqual(client_ips.ip_count(reader), 1)
        with self.assertRaises(sqlite3.OperationalError):
            reader.execute("DELETE FROM client_ip")


class MinMaxTests(SimpleTestCase):
    def test_none_is_ignored(self) -> None:
        for function, expected in ((client_ips._min, 1.0), (client_ips._max, 2.0)):
            with self.subTest(function=function.__name__):
                self.assertIsNone(function(None, None))
                self.assertEqual(function(None, 1.0), 1.0)
                self.assertEqual(function(1.0, None), 1.0)
                self.assertEqual(function(1.0, 2.0), expected)
                self.assertEqual(function(2.0, 1.0), expected)


class RunTests(TempDirMixin, SimpleTestCase):
    def _config(self, **kwargs: Any) -> client_ips.Config:
        return client_ips.Config(
            **{
                "db_path": self.temp / "client_ips.sqlite3",
                "request_log": self.temp / "requests.jsonl",
                "geoip_dir": self.temp / "geoip",
                "download": False,
                **kwargs,
            }
        )

    def test_records_until_master_exits(self) -> None:
        config = self._config()
        config.request_log.write_text(envoy_line() + "\nnot json\n")
        parents = iter([1, 1, 2])

        def sleep(seconds: float) -> None:
            self.assertEqual(seconds, config.interval_seconds)
            with config.request_log.open("a") as file:
                file.write(envoy_line(fly_client_ip="198.51.100.1") + "\n")

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            status = client_ips.run(config, 1, sleep=sleep, getppid=lambda: next(parents))
        self.assertEqual(status, 0)
        self.assertIn("gunicorn master 1 has exited", stderr.getvalue())
        assert config.db_path is not None
        connection = client_ips.connect(config.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(
            dict(connection.execute("SELECT ip, requests FROM client_ip ORDER BY ip").fetchall()),
            {"198.51.100.1": 2, "203.0.113.7": 1},
        )

    def test_off(self) -> None:
        self.assertEqual(client_ips.run(self._config(db_path=None), 1), 0)

    def test_failure_logged_once(self) -> None:
        config = self._config()
        parents = iter([1, 1, 2])
        with (
            mock.patch.object(
                client_ips.RequestLogReader,
                "read",
                side_effect=[OSError("nope"), OSError("nope"), []],
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: next(parents))
        self.assertEqual(stderr.getvalue().count("couldn't record client IPs"), 1)
        self.assertEqual(stderr.getvalue().count("recording client IPs again"), 1)

    def test_downloads_at_startup(self) -> None:
        config = self._config(download=True)
        assert config.db_path is not None
        connection = client_ips.connect(config.db_path)
        client_ips.record(connection, [request()], ip_location.GeoIP(None))
        connection.close()

        def update(geoip_dir: Path) -> dict[str, dict[str, Any]]:
            write_geoip(geoip_dir)
            return {
                "dbip-country-lite": {"outcome": ip_location.UP_TO_DATE},
                "dbip-asn-lite": {"outcome": ip_location.DOWNLOADED, "file": "x.mmdb"},
            }

        with (
            mock.patch.object(client_ips, "update_geoip", side_effect=update) as update,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2)
        update.assert_called_once_with(config.geoip_dir)
        self.assertIn("downloaded x.mmdb", stderr.getvalue())
        self.assertIn("looked up 1 IPs seen earlier", stderr.getvalue())

    def test_checks_relay_at_startup(self) -> None:
        config = self._config(download=True)
        assert config.db_path is not None
        connection = client_ips.connect(config.db_path)
        client_ips.record(connection, [request()], ip_location.GeoIP(None))
        connection.close()

        def update(geoip_dir: Path) -> dict[str, dict[str, Any]]:
            write_relay_ranges(geoip_dir)
            return {}

        with (
            mock.patch.object(client_ips, "update_geoip", side_effect=update),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2)
        self.assertIn(
            "checked 1 IPs seen earlier against the Private Relay ranges",
            stderr.getvalue(),
        )

    def test_no_download_without_geoip_dir(self) -> None:
        config = self._config(download=True, geoip_dir=None)
        with (
            mock.patch.object(client_ips, "update_geoip") as update,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2)
        update.assert_not_called()

    def test_download_failure_is_logged(self) -> None:
        config = self._config(download=True)
        with (
            mock.patch.object(
                client_ips,
                "update_geoip",
                side_effect=PermissionError("read-only"),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2), 0)
        self.assertIn(
            "couldn't update the DB-IP databases: PermissionError('read-only')",
            stderr.getvalue(),
        )

    def test_cut_off_download_is_logged(self) -> None:
        # A gzip stream that ends early: GzipFile raises EOFError.
        config = self._config(download=True)

        def response() -> mock.MagicMock:
            response = mock.MagicMock(status_code=200, raw=io.BytesIO(gzip.compress(b"x" * 1000)[:-20]))
            response.__enter__.return_value = response
            return response

        with (
            mock.patch.object(requests, "get", side_effect=lambda *a, **k: response()),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2), 0)
        self.assertIn("couldn't download dbip-country-lite: EOFError", stderr.getvalue())
        self.assertIn("couldn't download dbip-asn-lite: EOFError", stderr.getvalue())
        # Nothing but the status file, which records the failures.
        assert config.geoip_dir is not None
        self.assertEqual(
            [path.name for path in config.geoip_dir.iterdir()],
            [ip_location.GEOIP_STATUS_NAME],
        )
        statuses = ip_location.read_geoip_status(config.geoip_dir)
        assert statuses is not None
        self.assertEqual(statuses["dbip-asn-lite"]["outcome"], ip_location.FAILED)
        self.assertIn("EOFError", statuses["dbip-asn-lite"]["traceback"])

    def test_fill_in_failure_is_logged(self) -> None:
        config = self._config(download=True)
        with (
            mock.patch.object(client_ips, "update_geoip", return_value={}),
            mock.patch.object(
                client_ips,
                "fill_in_locations",
                side_effect=sqlite3.OperationalError("locked"),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2), 0)
        self.assertIn("couldn't look up IPs seen earlier: locked", stderr.getvalue())

    def test_failed_requests_are_retried(self) -> None:
        config = self._config()
        config.request_log.write_text(envoy_line() + "\n")
        parents = iter([1, 1, 2])
        record = client_ips.record
        failures = [sqlite3.OperationalError("full")]

        def record_failing_once(*args: Any) -> int:
            if failures:
                raise failures.pop()
            return record(*args)

        with (
            mock.patch.object(client_ips, "record", side_effect=record_failing_once),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):

            def sleep(seconds: float) -> None:
                # The second batch arrives while the first is still pending.
                with config.request_log.open("a") as file:
                    file.write(envoy_line(fly_client_ip="198.51.100.1") + "\n")

            client_ips.run(config, 1, sleep=sleep, getppid=lambda: next(parents))
        assert config.db_path is not None
        connection = client_ips.connect(config.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(
            sorted(row[0] for row in connection.execute("SELECT ip FROM client_ip")),
            ["198.51.100.1", "203.0.113.7"],
        )

    def test_spawn(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            self.assertIsNone(client_ips.spawn(1, {}))
            popen.assert_not_called()
            environ = {"LIFETRACKER_DATA_DIR": str(self.temp)}
            self.assertIs(client_ips.spawn(1234, environ), popen.return_value)
        args = popen.call_args
        self.assertEqual(args.args[0][1:], ["-m", "lifetracker.core.client_ips", "1234"])
        self.assertEqual(args.kwargs["env"], environ)

    def test_main(self) -> None:
        # TMPDIR too, as the request log's default depends on it and
        # patch.dict leaves the rest of os.environ (e.g. macOS's TMPDIR) in place.
        environ = {
            "LIFETRACKER_CLIENT_IP_DB": str(self.temp / "x.sqlite3"),
            "TMPDIR": str(self.temp),
        }
        with (
            mock.patch.dict(os.environ, environ),
            mock.patch("signal.signal") as signal_,
            mock.patch.object(client_ips, "run", return_value=0) as run,
        ):
            self.assertEqual(client_ips.main(["1234"]), 0)
        run.assert_called_once_with(client_ips.config_from_env(environ), 1234)
        handler = signal_.call_args.args[1]
        with self.assertRaises(SystemExit) as cm:
            handler(15, None)
        self.assertEqual(cm.exception.code, 0)

    def test_main_usage(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(client_ips.main([]), 2)
        self.assertIn("usage:", stderr.getvalue())


class UpdateGeoIPCommandTests(TempDirMixin, SimpleTestCase):
    def test_not_configured(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": "off"}):
            with self.assertRaisesMessage(CommandError, "aren't recorded"):
                call_command("update_geoip")

    def test_downloads_and_fills_in(self) -> None:
        db_path = self.temp / "client_ips.sqlite3"
        connection = client_ips.connect(db_path)
        self.addCleanup(connection.close)
        client_ips.record(connection, [request()], ip_location.GeoIP(None))

        def update(geoip_dir: Path) -> dict[str, dict[str, Any]]:
            write_geoip(geoip_dir)
            return {
                "dbip-country-lite": {"outcome": ip_location.UP_TO_DATE},
                "dbip-asn-lite": {
                    "outcome": ip_location.DOWNLOADED,
                    "file": "dbip-asn-lite-2026-10.mmdb",
                },
            }

        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": str(db_path)}),
            mock.patch.object(ip_location, "update_geoip", side_effect=update),
        ):
            call_command("update_geoip", stdout=stdout)
        self.assertEqual(
            stdout.getvalue(),
            "Downloaded dbip-asn-lite-2026-10.mmdb\n"
            "Looked up 1 IPs without a location\n"
            "Checked 0 IPs not checked yet against the Private Relay ranges\n",
        )
        row = connection.execute("SELECT asn FROM client_ip").fetchone()
        self.assertEqual(row["asn"], 64500)

    def test_up_to_date(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"LIFETRACKER_CLIENT_IP_DB": str(self.temp / "client_ips.sqlite3")},
            ),
            mock.patch.object(
                ip_location,
                "update_geoip",
                return_value={name: {"outcome": ip_location.UP_TO_DATE} for name in ip_location.GEOIP_DATABASES},
            ),
        ):
            call_command("update_geoip", stdout=stdout)
        self.assertIn("Already up to date", stdout.getvalue())

    def test_failure(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"LIFETRACKER_CLIENT_IP_DB": str(self.temp / "client_ips.sqlite3")},
            ),
            mock.patch.object(
                ip_location,
                "update_geoip",
                return_value={
                    "dbip-country-lite": {
                        "outcome": ip_location.FAILED,
                        "error": "ConnectionError('x')",
                    },
                    "dbip-asn-lite": {"outcome": ip_location.UP_TO_DATE},
                },
            ),
        ):
            with self.assertRaisesMessage(CommandError, "Couldn't download dbip-country-lite"):
                call_command("update_geoip", stdout=stdout, stderr=stderr)
        self.assertEqual(
            stderr.getvalue(),
            "Couldn't download dbip-country-lite: ConnectionError('x')\n",
        )
        # It still looked up what it could.
        self.assertIn("Looked up 0 IPs", stdout.getvalue())
        self.assertNotIn("Already up to date", stdout.getvalue())

    def test_unwritable(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"LIFETRACKER_CLIENT_IP_DB": str(self.temp / "client_ips.sqlite3")},
            ),
            mock.patch.object(ip_location, "update_geoip", side_effect=PermissionError("read-only")),
        ):
            with self.assertRaisesMessage(CommandError, "Couldn't update the DB-IP databases: read-only"):
                call_command("update_geoip")


class ClientIPsExportCommandTests(TempDirMixin, SimpleTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.db_path = self.temp / "client_ips.sqlite3"
        patcher = mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": str(self.db_path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _export(self, *args: str) -> list[dict[str, Any]]:
        stdout = io.StringIO()
        call_command("client_ips_export", *args, stdout=stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_not_configured(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": "off"}):
            with self.assertRaisesMessage(CommandError, "aren't recorded"):
                call_command("client_ips_export")

    def test_no_database(self) -> None:
        with self.assertRaisesMessage(CommandError, "Couldn't open"):
            call_command("client_ips_export")
        self.assertFalse(self.db_path.exists())

    def test_exports_every_ip_first_seen_first(self) -> None:
        connection = client_ips.connect(self.db_path)
        self.addCleanup(connection.close)
        geoip_dir = self.temp / "geoip"
        write_geoip(geoip_dir)
        geoip = ip_location.GeoIP(geoip_dir)
        self.addCleanup(geoip.close)
        client_ips.record(
            connection,
            [
                request(ip="198.51.100.1", time=1761200001.5, logged_in=True),
                request(time=1761200000.25, user_agent='a "quoted"\nUA'),
            ],
            geoip,
        )
        rows = self._export()
        self.assertEqual([row["ip"] for row in rows], ["203.0.113.7", "198.51.100.1"])
        self.assertEqual(
            list(rows[0]),
            [description[0] for description in connection.execute("SELECT * FROM client_ip").description],
        )
        self.assertEqual(rows[0]["first_seen"], "2025-10-23T06:13:20.250Z")
        self.assertEqual(rows[0]["last_seen"], "2025-10-23T06:13:20.250Z")
        self.assertIsNone(rows[0]["first_logged_in"])
        self.assertEqual(rows[0]["last_user_agent"], 'a "quoted"\nUA')
        self.assertEqual(rows[0]["country"], "AU")
        self.assertIn("private_relay", rows[0])
        self.assertEqual(rows[1]["first_logged_in"], "2025-10-23T06:13:21.500Z")
        self.assertEqual(rows[1]["logged_in"], 1)

        self.assertEqual([row["ip"] for row in self._export("--logged-in")], ["198.51.100.1"])

    def test_empty(self) -> None:
        client_ips.connect(self.db_path).close()
        self.assertEqual(self._export(), [])


class ClientIPsAdminViewTests(TempDirMixin, TestCase):
    URL = "/admin/client-ips/"

    @override
    def setUp(self) -> None:
        super().setUp()
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        self.db_path = self.temp / "client_ips.sqlite3"
        patcher = mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": str(self.db_path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self) -> None:
        write_geoip(self.temp / "geoip")
        connection = client_ips.connect(self.db_path)
        geoip = ip_location.GeoIP(self.temp / "geoip")
        client_ips.record(
            connection,
            [
                request(time=1761200000, logged_in=True, user_agent="Firefox"),
                request(
                    ip="198.51.100.1",
                    time=1761200100,
                    rejected=True,
                    request_line="GET /wp-login.php",
                    status=302,
                ),
            ],
            geoip,
        )
        geoip.close()
        connection.close()

    def _ips(self, response: TestResponse) -> list[str]:
        return [
            ip.replace("<wbr>", "") for ip in re.findall(r'<td class="lt-ip">(.*?)</td>', response.content.decode())
        ]

    def test_url(self) -> None:
        self.assertEqual(reverse("admin:client_ips"), self.URL)

    def test_top_bar_links_to_page_for_superusers_only(self) -> None:
        link = f'<a href="{self.URL}">Client IPs</a>'
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

    def test_lists_ips_and_logged_in_networks(self) -> None:
        self._record()
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ips(response), ["198.51.100.1", "203.0.113.7"])
        self.assertContains(response, "All IPs (2;")
        self.assertContains(response, "<code>GET /wp-login.php</code> 302")
        self.assertContains(response, '<abbr title="Australia">AU</abbr>', html=True)
        self.assertContains(response, "AS64500 Example ISP")
        self.assertContains(
            response,
            '<time data-lt-local datetime="2025-10-23T06:13:20Z">2025-10-23T06:13:20Z</time>',
            html=True,
        )
        self.assertContains(
            response,
            "Locations from <code>dbip-country-lite-2026-10.mmdb</code>, <code>dbip-asn-lite-2026-10.mmdb</code> (",
        )
        self.assertContains(response, "IP Geolocation by DB-IP")
        networks = response.content.decode().split("<h2>IPs</h2>")[0]
        self.assertIn("AS64500", networks)
        self.assertNotIn("198.51.100.1", networks)

    def test_private_relay(self) -> None:
        write_geoip(self.temp / "geoip")
        write_relay_ranges(self.temp / "geoip")
        connection = client_ips.connect(self.db_path)
        geoip = ip_location.GeoIP(self.temp / "geoip")
        client_ips.record(
            connection,
            [
                request(logged_in=True),
                request(ip="2001:db8:1::5"),
                request(ip="198.51.100.1"),
            ],
            geoip,
        )
        connection.execute("UPDATE client_ip SET relay_city = NULL, relay_region = NULL WHERE ip = '2001:db8:1::5'")
        geoip.close()
        connection.close()
        response = self.client.get(self.URL)
        self.assertContains(
            response,
            '<strong class="lt-relay">iCloud Private Relay</strong>',
            count=2,
            html=True,
        )
        self.assertContains(response, "(Sydney, AU-NSW)")
        self.assertContains(response, "(NZ)")
        self.assertContains(response, ip_location.RELAY_RANGES_URL)
        networks = response.content.decode().split("<h2>IPs</h2>")[0]
        self.assertIn('<th scope="col" class="lt-num">Private Relay IPs</th>', networks)
        self.assertIn('<td class="lt-num">1</td>', networks)

    def test_ipv6_wraps_after_colons(self) -> None:
        connection = client_ips.connect(self.db_path)
        client_ips.record(connection, [request(ip="2001:db8::1")], ip_location.GeoIP(None))
        connection.close()
        response = self.client.get(self.URL)
        self.assertContains(response, '<td class="lt-ip">2001:<wbr>db8:<wbr>:<wbr>1</td>')

    def test_logged_in_only(self) -> None:
        self._record()
        response = self.client.get(self.URL, {"logged_in": "1"})
        self.assertEqual(self._ips(response), ["203.0.113.7"])
        self.assertContains(response, "Only IPs with logged-in requests (1;")

    def test_limit(self) -> None:
        self._record()
        with mock.patch("lifetracker.core.admin_views._CLIENT_IPS_LIMIT", 1):
            response = self.client.get(self.URL)
        self.assertEqual(self._ips(response), ["198.51.100.1"])
        self.assertContains(response, "Showing the 1 seen most recently.")

    def test_nothing_recorded_yet(self) -> None:
        response = self.client.get(self.URL)
        self.assertContains(response, "Nothing recorded yet.")
        self.assertContains(response, "No DB-IP databases in")

    def test_not_recorded(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": "off"}):
            response = self.client.get(self.URL)
        self.assertContains(response, "Client IPs aren't recorded")

    def test_unreadable_database(self) -> None:
        self.db_path.write_bytes(b"not a database" * 100)
        response = self.client.get(self.URL)
        self.assertContains(response, f"Couldn&#x27;t read {self.db_path}")

    def _write_geoip_status(self, contents: str) -> None:
        (self.temp / "geoip").mkdir(exist_ok=True)
        (self.temp / "geoip" / ip_location.GEOIP_STATUS_NAME).write_text(contents)

    def test_geoip_status(self) -> None:
        now = datetime.datetime(2026, 10, 2, tzinfo=datetime.UTC).timestamp()
        country = ip_location.GEOIP_URL.format(name="dbip-country-lite", month="2026-10")

        def download(url: str, destination: Path) -> bool:
            if url == country:
                raise requests.ConnectionError("offline")
            return False

        (self.temp / "geoip").mkdir()
        (self.temp / "geoip" / "dbip-asn-lite-2026-09.mmdb").write_bytes(b"db")
        ip_location.update_geoip(self.temp / "geoip", now=now, download=download)
        response = self.client.get(self.URL)
        self.assertContains(response, "<strong>Failed</strong>: ConnectionError(&#x27;offline&#x27;)")
        self.assertContains(response, f"<code>{country}</code>", html=True)
        self.assertContains(response, "<summary>Traceback</summary>")
        self.assertContains(response, "requests.exceptions.ConnectionError")
        self.assertContains(response, "Never")
        self.assertContains(response, "Up to date")
        self.assertContains(response, "this month's isn't out yet")
        self.assertContains(
            response,
            '<time data-lt-local datetime="2026-10-02T00:00:00Z">2026-10-02T00:00:00Z</time>',
            # Each database's attempt and the ASN and relay ranges' successes.
            count=5,
            html=True,
        )

    def test_no_geoip_status_yet(self) -> None:
        response = self.client.get(self.URL)
        self.assertContains(response, "No download tried yet.")

    def test_no_geoip_status_with_download_off(self) -> None:
        with mock.patch.dict(os.environ, {"LIFETRACKER_GEOIP_DOWNLOAD": "0"}):
            response = self.client.get(self.URL)
        self.assertContains(
            response,
            "No download tried yet (<code>LIFETRACKER_GEOIP_DOWNLOAD=0</code>).",
        )

    def test_unreadable_geoip_status(self) -> None:
        self._write_geoip_status('{"databases": []}')
        response = self.client.get(self.URL)
        self.assertContains(response, "Couldn&#x27;t read ")
        self.assertContains(response, "doesn&#x27;t hold each database&#x27;s status")
        self.assertNotContains(response, "No download tried yet")

    def test_unexpected_geoip_status_times(self) -> None:
        self._write_geoip_status(json.dumps({"databases": {"dbip-asn-lite": {"attempted": "yesterday"}}}))
        response = self.client.get(self.URL)
        self.assertContains(response, "Unexpected contents in ")
