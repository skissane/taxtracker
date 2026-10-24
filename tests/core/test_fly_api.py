import http.server
import os
import socketserver
import tempfile
import threading
from pathlib import Path

from click.testing import CliRunner
from django.test import SimpleTestCase

from lifetracker.cli import cli
from lifetracker.core import fly_api


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.requests.append((self.command, self.path, self.headers["Host"]))
        status, content_type, body = server.response
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        # A Unix socket's client address is "", not (host, port).
        return "unix"

    def log_message(self, format, *args):
        pass


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeFlyApiMixin:
    """Serves ``self.response`` on a Unix socket at ``self.socket_path``,
    recording each request's method, path and Host in ``self.requests``."""

    def setUp(self):
        super().setUp()
        # Under /tmp, as a Unix socket's path is limited to about 100 bytes.
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.socket_path = os.path.join(tmp.name, "api")
        self.server = _UnixHTTPServer(self.socket_path, _Handler)
        self.server.requests = []
        self.server.response = (200, "application/json", b'{"ok": true}')
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @property
    def requests(self):
        return self.server.requests


class GetTests(FakeFlyApiMixin, SimpleTestCase):
    def test_get(self):
        response = fly_api.get("/v1/apps/x/machines", socket_path=self.socket_path)
        self.assertEqual(
            response,
            fly_api.Response(200, "OK", "application/json", b'{"ok": true}'),
        )
        self.assertEqual(self.requests, [("GET", "/v1/apps/x/machines", "flaps")])

    def test_error_status_returned(self):
        self.server.response = (404, "text/plain", b"nope")
        response = fly_api.get("/missing", socket_path=self.socket_path)
        self.assertEqual((response.status, response.body), (404, b"nope"))

    def test_missing_socket(self):
        with self.assertRaises(FileNotFoundError):
            fly_api.get("/", socket_path=self.socket_path + "-missing")


class ExpandPathTests(SimpleTestCase):
    def test_placeholders(self):
        environ = {"FLY_APP_NAME": "app1", "FLY_MACHINE_ID": "m1"}
        self.assertEqual(
            fly_api.expand_path("/v1/apps/{app}/machines/{machine}", environ),
            "/v1/apps/app1/machines/m1",
        )

    def test_no_placeholders_needs_no_environment(self):
        self.assertEqual(fly_api.expand_path("/v1/apps/x", {}), "/v1/apps/x")

    def test_missing_variable(self):
        for environ in ({}, {"FLY_APP_NAME": " "}):
            with self.assertRaisesRegex(ValueError, "FLY_APP_NAME"):
                fly_api.expand_path("/v1/apps/{app}", environ)

    def test_relative_path(self):
        with self.assertRaisesRegex(ValueError, "must start with '/'"):
            fly_api.expand_path("v1/apps", {})


class FlyApiCommandTests(FakeFlyApiMixin, SimpleTestCase):
    def invoke(self, *args, env=None):
        return CliRunner().invoke(
            cli, ["fly-api", "--socket", self.socket_path, *args], env=env
        )

    def test_pretty_prints_json(self):
        result = self.invoke("/v1/apps/{app}/machines", env={"FLY_APP_NAME": "a"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout, '{\n  "ok": true\n}\n')
        self.assertEqual(self.requests, [("GET", "/v1/apps/a/machines", "flaps")])

    def test_non_json_body_unchanged(self):
        self.server.response = (200, "text/plain", b"plain\xff")
        result = self.invoke("/x")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, b"plain\xff")

    def test_error_status_exits_1(self):
        self.server.response = (403, "application/json", b'{"error": "no"}')
        result = self.invoke("/x")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, '{\n  "error": "no"\n}\n')
        self.assertEqual(result.stderr, "HTTP 403 Forbidden\n")

    def test_missing_placeholder_variable(self):
        result = self.invoke("/v1/apps/{app}", env={"FLY_APP_NAME": ""})
        self.assertEqual(result.exit_code, 2)
        self.assertIn("FLY_APP_NAME", result.stderr)
        self.assertEqual(self.requests, [])

    def test_unreachable_socket(self):
        missing = str(Path(self.socket_path).with_name("missing"))
        result = CliRunner().invoke(cli, ["fly-api", "--socket", missing, "/"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("FileNotFoundError", result.stderr)
