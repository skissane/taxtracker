"""Tests for the client IP record (core/client_ips.py), its admin page and the
update_geoip command."""

import datetime
import gzip
import io
import json
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from lifetracker.core import client_ips
from tests.core.mmdb_writer import write_geoip, write_mmdb


def envoy_line(**fields):
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


def request(ip="203.0.113.7", time=1000.0, **fields):
    return client_ips.Request(
        **{
            "ip": ip,
            "time": time,
            "request": "GET /admin/",
            "status": 200,
            "user_agent": "UA",
            "rejected": False,
            "logged_in": False,
            **fields,
        }
    )


class TempDirMixin:
    def setUp(self):
        super().setUp()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.temp = Path(tempdir.name)


class ConfigTests(SimpleTestCase):
    def test_defaults_under_data_dir(self):
        config = client_ips.config_from_env(
            {"LIFETRACKER_DATA_DIR": "/data", "TMPDIR": "/scratch"}
        )
        self.assertEqual(config.db_path, Path("/data/client_ips.sqlite3"))
        self.assertEqual(config.geoip_dir, Path("/data/geoip"))
        self.assertEqual(
            config.request_log, Path("/scratch/lifetracker-envoy-requests.jsonl")
        )
        self.assertTrue(config.download)

    def test_off_without_data_dir(self):
        config = client_ips.config_from_env({})
        self.assertIsNone(config.db_path)
        self.assertIsNone(config.geoip_dir)
        self.assertEqual(
            config.request_log, Path("/tmp/lifetracker-envoy-requests.jsonl")
        )

    def test_off(self):
        config = client_ips.config_from_env(
            {"LIFETRACKER_DATA_DIR": "/data", "LIFETRACKER_CLIENT_IP_DB": "Off"}
        )
        self.assertIsNone(config.db_path)

    def test_explicit(self):
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
    def test_rejected(self):
        line = envoy_line(jwt_failure={"code": 401, "message": "Jwt is missing"})
        self.assertEqual(
            client_ips.parse_line(line),
            client_ips.Request(
                ip="203.0.113.7",
                time=1761200000.123,
                request="GET /admin/",
                status=302,
                user_agent="curl/8",
                rejected=True,
                logged_in=False,
            ),
        )

    def test_logged_in(self):
        parsed = client_ips.parse_line(envoy_line(jwt_sub="1", status=200))
        self.assertTrue(parsed.logged_in)
        self.assertFalse(parsed.rejected)
        self.assertEqual(parsed.status, 200)

    def test_peer_without_fly_client_ip(self):
        for value in (None, "-", "not an IP"):
            with self.subTest(value):
                parsed = client_ips.parse_line(envoy_line(fly_client_ip=value))
                self.assertEqual(parsed.ip, "172.16.0.2")

    def test_ipv6_is_normalised(self):
        parsed = client_ips.parse_line(envoy_line(fly_client_ip="2001:DB8:0::1"))
        self.assertEqual(parsed.ip, "2001:db8::1")

    def test_missing_fields(self):
        parsed = client_ips.parse_line(
            envoy_line(method=None, path=None, status=None, user_agent=None)
        )
        self.assertEqual(parsed.request, "- -")
        self.assertIsNone(parsed.status)
        self.assertEqual(parsed.user_agent, "")

    def test_long_text_is_cut_off(self):
        parsed = client_ips.parse_line(
            envoy_line(path="/" + "x" * 1000, user_agent="y" * 1000)
        )
        self.assertEqual(len(parsed.request), client_ips.MAX_TEXT_LENGTH)
        self.assertEqual(len(parsed.user_agent), client_ips.MAX_TEXT_LENGTH)

    def test_not_a_request(self):
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
    def setUp(self):
        super().setUp()
        self.path = self.temp / "requests.jsonl"

    def test_missing_file(self):
        self.assertEqual(client_ips.RequestLogReader(self.path).read(), [])

    def test_reads_only_new_complete_lines(self):
        reader = client_ips.RequestLogReader(self.path)
        self.path.write_text("one\ntwo\nthr")
        self.assertEqual(reader.read(), ["one", "two"])
        self.assertEqual(reader.read(), [])
        with self.path.open("a") as file:
            file.write("ee\nfour\n")
        self.assertEqual(reader.read(), ["three", "four"])

    def test_empties_file_once_read_and_big(self):
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

    def test_not_emptied_mid_line(self):
        reader = client_ips.RequestLogReader(self.path, truncate_bytes=1)
        self.path.write_text("a\npart")
        self.assertEqual(reader.read(), ["a"])
        self.assertEqual(self.path.read_text(), "a\npart")

    def test_starts_again_when_replaced(self):
        reader = client_ips.RequestLogReader(self.path)
        self.path.write_text("first line\n")
        reader.read()
        self.path.write_text("new\n")
        self.assertEqual(reader.read(), ["new"])
        self.path.unlink()
        self.assertEqual(reader.read(), [])
        self.path.write_text("again\n")
        self.assertEqual(reader.read(), ["again"])


class GeoIPTests(TempDirMixin, SimpleTestCase):
    def test_lookup(self):
        write_geoip(self.temp)
        geoip = client_ips.GeoIP(self.temp)
        self.addCleanup(geoip.close)
        self.assertEqual(
            geoip.lookup("203.0.113.7"),
            client_ips.Location("AU", "Australia", 64500, "Example ISP"),
        )
        self.assertEqual(
            geoip.lookup("2001:db8::1"), client_ips.Location("NZ", "NZ", None, None)
        )
        self.assertFalse(geoip.lookup("198.51.100.1"))

    def test_no_databases(self):
        self.assertFalse(client_ips.GeoIP(self.temp).lookup("203.0.113.7"))
        self.assertFalse(client_ips.GeoIP(None).lookup("203.0.113.7"))

    def test_uses_newest_and_reopens_for_a_newer_one(self):
        write_geoip(self.temp, "2026-09")
        geoip = client_ips.GeoIP(self.temp)
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

    def test_unreadable_database_is_skipped(self):
        (self.temp / "dbip-asn-lite-2026-10.mmdb").write_bytes(b"junk")
        self.assertFalse(client_ips.GeoIP(self.temp).lookup("203.0.113.7"))


class UpdateGeoIPTests(TempDirMixin, SimpleTestCase):
    TODAY = datetime.date(2026, 1, 3)

    def _fake_download(self, available):
        calls = []

        def download(url, destination):
            calls.append(url)
            if url not in available:
                return False
            destination.write_bytes(b"db")
            return True

        return download, calls

    def _url(self, name, month):
        return client_ips.GEOIP_URL.format(name=name, month=month)

    def test_downloads_this_month(self):
        available = {self._url(name, "2026-01") for name in client_ips.GEOIP_DATABASES}
        download, calls = self._fake_download(available)
        (self.temp / "dbip-asn-lite-2025-11.mmdb").write_bytes(b"old")
        downloaded = client_ips.update_geoip(
            self.temp, today=self.TODAY, download=download
        )
        self.assertEqual(
            downloaded, ["dbip-country-lite-2026-01.mmdb", "dbip-asn-lite-2026-01.mmdb"]
        )
        self.assertEqual(
            sorted(path.name for path in self.temp.iterdir()),
            ["dbip-asn-lite-2026-01.mmdb", "dbip-country-lite-2026-01.mmdb"],
        )
        # Already there: nothing to do.
        self.assertEqual(
            client_ips.update_geoip(self.temp, today=self.TODAY, download=download),
            [],
        )
        self.assertEqual(len(calls), 2)

    def test_falls_back_to_last_month(self):
        available = {self._url(name, "2025-12") for name in client_ips.GEOIP_DATABASES}
        download, calls = self._fake_download(available)
        downloaded = client_ips.update_geoip(
            self.temp, today=self.TODAY, download=download
        )
        self.assertEqual(
            downloaded, ["dbip-country-lite-2025-12.mmdb", "dbip-asn-lite-2025-12.mmdb"]
        )
        # Next time, this month's is tried again, but last month's is kept.
        client_ips.update_geoip(self.temp, today=self.TODAY, download=download)
        self.assertEqual(len(calls), 6)
        self.assertEqual(
            calls[4:],
            [self._url(name, "2026-01") for name in client_ips.GEOIP_DATABASES],
        )

    def test_neither_month_available(self):
        download, _ = self._fake_download(set())
        with self.assertRaisesMessage(
            OSError, "Neither 2026-01's nor 2025-12's dbip-country-lite is available"
        ):
            client_ips.update_geoip(self.temp, today=self.TODAY, download=download)


class DownloadTests(TempDirMixin, SimpleTestCase):
    def _response(self, status, body=b""):
        response = mock.MagicMock(status_code=status, raw=io.BytesIO(body))
        response.__enter__.return_value = response
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status))
        return response

    def test_downloads_and_unzips(self):
        source = self.temp / "source.mmdb"
        write_mmdb(source, {"203.0.113.0/24": {"x": 1}})
        destination = self.temp / "dbip-asn-lite-2026-10.mmdb"
        with mock.patch.object(
            requests,
            "get",
            return_value=self._response(200, gzip.compress(source.read_bytes())),
        ) as get:
            self.assertTrue(client_ips._download("https://x/y.gz", destination))
        get.assert_called_once_with(
            "https://x/y.gz", stream=True, timeout=client_ips.GEOIP_TIMEOUT_SECONDS
        )
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o644)

    def test_not_found(self):
        destination = self.temp / "x.mmdb"
        with mock.patch.object(requests, "get", return_value=self._response(404)):
            self.assertFalse(client_ips._download("https://x/y.gz", destination))
        self.assertFalse(destination.exists())

    def test_error(self):
        with mock.patch.object(requests, "get", return_value=self._response(500)):
            with self.assertRaises(requests.HTTPError):
                client_ips._download("https://x/y.gz", self.temp / "x.mmdb")

    def test_not_a_database_is_left_out(self):
        destination = self.temp / "x.mmdb"
        with mock.patch.object(
            requests, "get", return_value=self._response(200, gzip.compress(b"junk"))
        ):
            with self.assertRaises(client_ips.maxminddb.InvalidDatabaseError):
                client_ips._download("https://x/y.gz", destination)
        self.assertEqual(list(self.temp.iterdir()), [])


class RecordTests(TempDirMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = self.temp / "client_ips.sqlite3"
        self.connection = client_ips.connect(self.db_path)
        self.addCleanup(self.connection.close)
        self.geoip_dir = self.temp / "geoip"
        self.geoip = client_ips.GeoIP(self.geoip_dir)
        self.addCleanup(self.geoip.close)

    def _row(self, ip="203.0.113.7"):
        row = self.connection.execute(
            "SELECT * FROM client_ip WHERE ip = ?", (ip,)
        ).fetchone()
        return dict(row) if row is not None else None

    def test_adds_up_requests_per_ip(self):
        write_geoip(self.geoip_dir)
        updated = client_ips.record(
            self.connection,
            [
                request(time=1002, rejected=True, request="GET /x", status=302),
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
            },
        )
        other = self._row("198.51.100.1")
        self.assertEqual((other["requests"], other["country"]), (1, None))

        client_ips.record(
            self.connection,
            [
                request(time=1005, logged_in=True, request="GET /y", user_agent="B"),
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

    def test_latest_request_kept_when_batch_is_older(self):
        client_ips.record(self.connection, [request(time=1000)], self.geoip)
        client_ips.record(
            self.connection, [request(time=500, request="GET /old")], self.geoip
        )
        self.assertEqual(self._row()["last_request"], "GET /admin/")
        self.assertEqual(self._row()["last_seen"], 1000)

    def test_failed_batch_is_rolled_back(self):
        geoip = mock.Mock(
            lookup=mock.Mock(side_effect=[client_ips.Location(), ValueError])
        )
        with self.assertRaises(ValueError):
            client_ips.record(
                self.connection, [request(), request(ip="198.51.100.1")], geoip
            )
        self.assertEqual(client_ips.ip_count(self.connection), 0)
        client_ips.record(self.connection, [request()], self.geoip)
        self.assertEqual(client_ips.ip_count(self.connection), 1)

    def test_nothing_to_record(self):
        self.assertEqual(client_ips.record(self.connection, [], self.geoip), 0)

    def test_location_kept_from_first_lookup(self):
        write_geoip(self.geoip_dir, "2026-09")
        client_ips.record(self.connection, [request()], self.geoip)
        write_mmdb(
            self.geoip_dir / "dbip-asn-lite-2026-10.mmdb",
            {"203.0.113.0/24": {"autonomous_system_number": 64501}},
        )
        client_ips.record(self.connection, [request(time=2000)], self.geoip)
        self.assertEqual(self._row()["asn"], 64500)

    def test_location_looked_up_once_available(self):
        client_ips.record(self.connection, [request()], self.geoip)
        self.assertIsNone(self._row()["asn"])
        write_geoip(self.geoip_dir)
        client_ips.record(self.connection, [request(time=2000)], self.geoip)
        self.assertEqual(self._row()["asn"], 64500)

    def test_fill_in_locations(self):
        client_ips.record(
            self.connection,
            [request(), request(ip="198.51.100.1")],
            self.geoip,
        )
        write_geoip(self.geoip_dir)
        self.assertEqual(client_ips.fill_in_locations(self.connection, self.geoip), 1)
        self.assertEqual(self._row()["country"], "AU")
        self.assertIsNone(self._row("198.51.100.1")["country"])

    def test_queries(self):
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
            [
                row["ip"]
                for row in client_ips.recent_ips(
                    self.connection, logged_in_only=True, limit=10
                )
            ],
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
                    "first_logged_in": 1000,
                    "last_logged_in": 3000,
                },
                {
                    "asn": None,
                    "as_org": None,
                    "ips": 1,
                    "countries": None,
                    "logged_in": 1,
                    "first_logged_in": 2000,
                    "last_logged_in": 2000,
                },
            ],
        )

    def test_read_only_connection(self):
        client_ips.record(self.connection, [request()], self.geoip)
        reader = client_ips.connect_read_only(self.db_path)
        self.addCleanup(reader.close)
        self.assertEqual(client_ips.ip_count(reader), 1)
        with self.assertRaises(client_ips.sqlite3.OperationalError):
            reader.execute("DELETE FROM client_ip")


class RunTests(TempDirMixin, SimpleTestCase):
    def _config(self, **kwargs):
        return client_ips.Config(
            **{
                "db_path": self.temp / "client_ips.sqlite3",
                "request_log": self.temp / "requests.jsonl",
                "geoip_dir": self.temp / "geoip",
                "download": False,
                **kwargs,
            }
        )

    def test_records_until_master_exits(self):
        config = self._config()
        config.request_log.write_text(envoy_line() + "\nnot json\n")
        parents = iter([1, 1, 2])

        def sleep(seconds):
            self.assertEqual(seconds, config.interval_seconds)
            with config.request_log.open("a") as file:
                file.write(envoy_line(fly_client_ip="198.51.100.1") + "\n")

        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            status = client_ips.run(
                config, 1, sleep=sleep, getppid=lambda: next(parents)
            )
        self.assertEqual(status, 0)
        self.assertIn("gunicorn master 1 has exited", stderr.getvalue())
        connection = client_ips.connect(config.db_path)
        self.addCleanup(connection.close)
        self.assertEqual(
            dict(
                connection.execute(
                    "SELECT ip, requests FROM client_ip ORDER BY ip"
                ).fetchall()
            ),
            {"198.51.100.1": 2, "203.0.113.7": 1},
        )

    def test_off(self):
        self.assertEqual(client_ips.run(self._config(db_path=None), 1), 0)

    def test_failure_logged_once(self):
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
            client_ips.run(
                config, 1, sleep=lambda s: None, getppid=lambda: next(parents)
            )
        self.assertEqual(stderr.getvalue().count("couldn't record client IPs"), 1)
        self.assertEqual(stderr.getvalue().count("recording client IPs again"), 1)

    def test_downloads_at_startup(self):
        config = self._config(download=True)
        connection = client_ips.connect(config.db_path)
        client_ips.record(connection, [request()], client_ips.GeoIP(None))
        connection.close()

        def update(geoip_dir):
            write_geoip(geoip_dir)
            return ["x.mmdb"]

        with (
            mock.patch.object(client_ips, "update_geoip", side_effect=update) as update,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2)
        update.assert_called_once_with(config.geoip_dir)
        self.assertIn("downloaded x.mmdb", stderr.getvalue())
        self.assertIn("looked up 1 IPs seen earlier", stderr.getvalue())

    def test_no_download_without_geoip_dir(self):
        config = self._config(download=True, geoip_dir=None)
        with (
            mock.patch.object(client_ips, "update_geoip") as update,
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2)
        update.assert_not_called()

    def test_download_failure_is_logged(self):
        config = self._config(download=True)
        with (
            mock.patch.object(
                client_ips,
                "update_geoip",
                side_effect=requests.ConnectionError("offline"),
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(
                client_ips.run(config, 1, sleep=lambda s: None, getppid=lambda: 2), 0
            )
        self.assertIn(
            "couldn't download the DB-IP databases: offline", stderr.getvalue()
        )

    def test_spawn(self):
        with mock.patch("subprocess.Popen") as popen:
            self.assertIsNone(client_ips.spawn(1, {}))
            popen.assert_not_called()
            environ = {"LIFETRACKER_DATA_DIR": str(self.temp)}
            self.assertIs(client_ips.spawn(1234, environ), popen.return_value)
        args = popen.call_args
        self.assertEqual(
            args.args[0][1:], ["-m", "lifetracker.core.client_ips", "1234"]
        )
        self.assertEqual(args.kwargs["env"], environ)

    def test_main(self):
        environ = {"LIFETRACKER_CLIENT_IP_DB": str(self.temp / "x.sqlite3")}
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

    def test_main_usage(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(client_ips.main([]), 2)
        self.assertIn("usage:", stderr.getvalue())


class UpdateGeoIPCommandTests(TempDirMixin, SimpleTestCase):
    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": "off"}):
            with self.assertRaisesMessage(CommandError, "aren't recorded"):
                call_command("update_geoip")

    def test_downloads_and_fills_in(self):
        db_path = self.temp / "client_ips.sqlite3"
        connection = client_ips.connect(db_path)
        self.addCleanup(connection.close)
        client_ips.record(connection, [request()], client_ips.GeoIP(None))

        def update(geoip_dir):
            write_geoip(geoip_dir)
            return ["dbip-asn-lite-2026-10.mmdb"]

        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": str(db_path)}),
            mock.patch.object(client_ips, "update_geoip", side_effect=update),
        ):
            call_command("update_geoip", stdout=stdout)
        self.assertEqual(
            stdout.getvalue(),
            "Downloaded dbip-asn-lite-2026-10.mmdb\n"
            "Looked up 1 IPs without a location\n",
        )
        row = connection.execute("SELECT asn FROM client_ip").fetchone()
        self.assertEqual(row["asn"], 64500)

    def test_up_to_date(self):
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"LIFETRACKER_CLIENT_IP_DB": str(self.temp / "client_ips.sqlite3")},
            ),
            mock.patch.object(client_ips, "update_geoip", return_value=[]),
        ):
            call_command("update_geoip", stdout=stdout)
        self.assertIn("Already up to date", stdout.getvalue())

    def test_failure(self):
        with (
            mock.patch.dict(
                os.environ,
                {"LIFETRACKER_CLIENT_IP_DB": str(self.temp / "client_ips.sqlite3")},
            ),
            mock.patch.object(
                client_ips, "update_geoip", side_effect=requests.ConnectionError("x")
            ),
        ):
            with self.assertRaisesMessage(
                CommandError, "Couldn't download the DB-IP databases: x"
            ):
                call_command("update_geoip")


class ClientIPsAdminViewTests(TempDirMixin, TestCase):
    URL = "/admin/client-ips/"

    def setUp(self):
        super().setUp()
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        self.db_path = self.temp / "client_ips.sqlite3"
        patcher = mock.patch.dict(
            os.environ, {"LIFETRACKER_CLIENT_IP_DB": str(self.db_path)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self):
        write_geoip(self.temp / "geoip")
        connection = client_ips.connect(self.db_path)
        geoip = client_ips.GeoIP(self.temp / "geoip")
        client_ips.record(
            connection,
            [
                request(time=1761200000, logged_in=True, user_agent="Firefox"),
                request(
                    ip="198.51.100.1",
                    time=1761200100,
                    rejected=True,
                    request="GET /wp-login.php",
                    status=302,
                ),
            ],
            geoip,
        )
        geoip.close()
        connection.close()

    def _ips(self, response):
        return [
            ip.replace("<wbr>", "")
            for ip in re.findall(
                r'<td class="lt-ip">(.*?)</td>', response.content.decode()
            )
        ]

    def test_url(self):
        self.assertEqual(reverse("admin:client_ips"), self.URL)

    def test_top_bar_links_to_page_for_superusers_only(self):
        link = f'<a href="{self.URL}">Client IPs</a>'
        self.assertContains(self.client.get(reverse("admin:index")), link, html=True)

        staff = User.objects.create_user("staff", "s@example.com", "pass")
        staff.is_staff = True
        staff.save()
        client = Client()
        client.login(username="staff", password="pass")
        self.assertNotContains(client.get(reverse("admin:index")), self.URL)
        self.assertEqual(client.get(self.URL).status_code, 403)

    def test_page_requires_login(self):
        response = Client().get(self.URL)
        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.URL}",
            fetch_redirect_response=False,
        )

    def test_lists_ips_and_logged_in_networks(self):
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
            '<time data-lt-local datetime="2025-10-23T06:13:20Z">'
            "2025-10-23T06:13:20Z</time>",
            html=True,
        )
        self.assertContains(response, "IP Geolocation by DB-IP")
        networks = response.content.decode().split("<h2>IPs</h2>")[0]
        self.assertIn("AS64500", networks)
        self.assertNotIn("198.51.100.1", networks)

    def test_ipv6_wraps_after_colons(self):
        connection = client_ips.connect(self.db_path)
        client_ips.record(
            connection, [request(ip="2001:db8::1")], client_ips.GeoIP(None)
        )
        connection.close()
        response = self.client.get(self.URL)
        self.assertContains(
            response, '<td class="lt-ip">2001:<wbr>db8:<wbr>:<wbr>1</td>'
        )

    def test_logged_in_only(self):
        self._record()
        response = self.client.get(self.URL, {"logged_in": "1"})
        self.assertEqual(self._ips(response), ["203.0.113.7"])
        self.assertContains(response, "Only IPs with logged-in requests (1;")

    def test_limit(self):
        self._record()
        with mock.patch("lifetracker.core.admin._CLIENT_IPS_LIMIT", 1):
            response = self.client.get(self.URL)
        self.assertEqual(self._ips(response), ["198.51.100.1"])
        self.assertContains(response, "Showing the 1 seen most recently.")

    def test_nothing_recorded_yet(self):
        response = self.client.get(self.URL)
        self.assertContains(response, "Nothing recorded yet.")
        self.assertContains(response, "No DB-IP databases in")

    def test_not_recorded(self):
        with mock.patch.dict(os.environ, {"LIFETRACKER_CLIENT_IP_DB": "off"}):
            response = self.client.get(self.URL)
        self.assertContains(response, "Client IPs aren't recorded")

    def test_unreadable_database(self):
        self.db_path.write_bytes(b"not a database" * 100)
        response = self.client.get(self.URL)
        self.assertContains(response, f"Couldn&#x27;t read {self.db_path}")
