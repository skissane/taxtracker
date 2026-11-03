import http.server
import os
import socketserver
import tempfile
import threading
from pathlib import Path
from typing import Any

from click.testing import CliRunner, Result
from django.test import SimpleTestCase

from lifetracker.cli import cli
from lifetracker.core import fly_api


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _UnixHTTPServer)
        server.requests.append((self.command, self.path, self.headers["Host"]))
        if self.command == "POST":
            length = self.headers["Content-Length"]
            body = self.rfile.read(int(length)) if length is not None else None
            server.posts.append((length, self.headers["Content-Type"], body))
        status, content_type, body = server.response
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def address_string(self) -> str:
        # A Unix socket's client address is "", not (host, port).
        return "unix"

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    requests: list[tuple[str, str, str | None]]
    posts: list[tuple[str | None, str | None, bytes | None]]
    response: tuple[int, str, bytes]


class FakeFlyApiMixin(SimpleTestCase):
    """Serves ``self.response`` on a Unix socket at ``self.socket_path``,
    recording each request's method, path and Host in ``self.requests``, and
    each POST's Content-Length, Content-Type and body in ``self.posts``."""

    def setUp(self) -> None:
        super().setUp()
        # Under /tmp, as a Unix socket's path is limited to about 100 bytes.
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.socket_path = os.path.join(tmp.name, "api")
        self.server = _UnixHTTPServer(self.socket_path, _Handler)
        self.server.requests = []
        self.server.posts = []
        self.server.response = (200, "application/json", b'{"ok": true}')
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @property
    def requests(self) -> list[tuple[str, str, str | None]]:
        return self.server.requests

    @property
    def posts(self) -> list[tuple[str | None, str | None, bytes | None]]:
        return self.server.posts


class GetTests(FakeFlyApiMixin, SimpleTestCase):
    def test_get(self) -> None:
        response = fly_api.get("/v1/apps/x/machines", socket_path=self.socket_path)
        self.assertEqual(
            response,
            fly_api.Response(200, "OK", "application/json", b'{"ok": true}'),
        )
        self.assertEqual(self.requests, [("GET", "/v1/apps/x/machines", "flaps")])

    def test_error_status_returned(self) -> None:
        self.server.response = (404, "text/plain", b"nope")
        response = fly_api.get("/missing", socket_path=self.socket_path)
        self.assertEqual((response.status, response.body), (404, b"nope"))

    def test_missing_socket(self) -> None:
        with self.assertRaises(FileNotFoundError):
            fly_api.get("/", socket_path=self.socket_path + "-missing")


class PostTests(FakeFlyApiMixin, SimpleTestCase):
    def test_post_without_body(self) -> None:
        response = fly_api.post("/v1/apps/x/machines/m/stop", socket_path=self.socket_path)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.requests, [("POST", "/v1/apps/x/machines/m/stop", "flaps")])
        self.assertEqual(self.posts, [("0", None, b"")])

    def test_post_json(self) -> None:
        fly_api.post("/x", {"name": "café", "n": 1}, socket_path=self.socket_path)
        self.assertEqual(
            self.posts,
            [("25", "application/json", '{"name": "café", "n": 1}'.encode())],
        )

    def test_post_empty_object_is_a_body(self) -> None:
        fly_api.post("/x", {}, socket_path=self.socket_path)
        self.assertEqual(self.posts, [("2", "application/json", b"{}")])


class ExpandPathTests(SimpleTestCase):
    def test_placeholders(self) -> None:
        environ = {"FLY_APP_NAME": "app1", "FLY_MACHINE_ID": "m1"}
        self.assertEqual(
            fly_api.expand_path("/v1/apps/{app}/machines/{machine}", environ),
            "/v1/apps/app1/machines/m1",
        )

    def test_no_placeholders_needs_no_environment(self) -> None:
        self.assertEqual(fly_api.expand_path("/v1/apps/x", {}), "/v1/apps/x")

    def test_missing_variable(self) -> None:
        for environ in ({}, {"FLY_APP_NAME": " "}):
            with self.assertRaisesRegex(ValueError, "FLY_APP_NAME"):
                fly_api.expand_path("/v1/apps/{app}", environ)

    def test_relative_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "must start with '/'"):
            fly_api.expand_path("v1/apps", {})


class FlyApiCommandTests(FakeFlyApiMixin, SimpleTestCase):
    def invoke(self, *args: str, env: dict[str, str] | None = None) -> Result:
        return CliRunner().invoke(cli, ["fly-api", "--socket", self.socket_path, *args], env=env)

    def test_pretty_prints_json(self) -> None:
        result = self.invoke("/v1/apps/{app}/machines", env={"FLY_APP_NAME": "a"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout, '{\n  "ok": true\n}\n')
        self.assertEqual(self.requests, [("GET", "/v1/apps/a/machines", "flaps")])

    def test_non_ascii_json_not_escaped(self) -> None:
        self.server.response = (200, "application/json", '{"name": "café"}'.encode())
        result = self.invoke("/x")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, '{\n  "name": "café"\n}\n'.encode())

    def test_non_json_body_unchanged(self) -> None:
        self.server.response = (200, "text/plain", b"plain\xff")
        result = self.invoke("/x")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout_bytes, b"plain\xff")

    def test_error_status_exits_1(self) -> None:
        self.server.response = (403, "application/json", b'{"error": "no"}')
        result = self.invoke("/x")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, '{\n  "error": "no"\n}\n')
        self.assertEqual(result.stderr, "HTTP 403 Forbidden\n")

    def test_get_by_default(self) -> None:
        self.invoke("/x")
        self.assertEqual(self.requests, [("GET", "/x", "flaps")])
        self.assertEqual(self.posts, [])

    def test_post_without_body(self) -> None:
        env = {"FLY_APP_NAME": "a", "FLY_MACHINE_ID": "m"}
        result = self.invoke("--post", "/v1/apps/{app}/machines/{machine}/stop", env=env)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stdout, '{\n  "ok": true\n}\n')
        self.assertEqual(self.requests, [("POST", "/v1/apps/a/machines/m/stop", "flaps")])
        self.assertEqual(self.posts, [("0", None, b"")])

    def test_post_json(self) -> None:
        result = self.invoke("/x", "--post", "--json", '{"signal": "SIGHUP"}')
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.posts, [("20", "application/json", b'{"signal": "SIGHUP"}')])

    def test_json_needs_post(self) -> None:
        result = self.invoke("/x", "--json", "{}")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--json needs --post", result.stderr)
        self.assertEqual(self.requests, [])

    def test_invalid_json(self) -> None:
        for text, message in (
            ("{nope", "--json isn't valid JSON"),
            ("[1]", "--json must be a JSON object"),
            ('"x"', "--json must be a JSON object"),
        ):
            with self.subTest(text=text):
                result = self.invoke("/x", "--post", "--json", text)
                self.assertEqual(result.exit_code, 2)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.requests, [])

    def test_missing_placeholder_variable(self) -> None:
        result = self.invoke("/v1/apps/{app}", env={"FLY_APP_NAME": ""})
        self.assertEqual(result.exit_code, 2)
        self.assertIn("FLY_APP_NAME", result.stderr)
        self.assertEqual(self.requests, [])

    def test_unreachable_socket(self) -> None:
        missing = str(Path(self.socket_path).with_name("missing"))
        result = CliRunner().invoke(cli, ["fly-api", "--socket", missing, "/"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("FileNotFoundError", result.stderr)
