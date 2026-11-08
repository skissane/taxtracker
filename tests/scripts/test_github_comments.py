import http.client
import http.server
import threading
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, override
from unittest import mock

from django.test import SimpleTestCase

from scripts.github_comments import request as github_request

# What request() opens URLs with; wait_for_base_coverage's tests use it too.
NO_REDIRECT_OPEN = "scripts.github_comments.no_redirect_opener.open"


class GitHubCommentsRequestTests(SimpleTestCase):
    def _response(self, payload: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def _redirect(self, url: str, location: str, code: int = 307) -> urllib.error.HTTPError:
        headers = Message()
        headers["Location"] = location
        return urllib.error.HTTPError(url, code, "Redirect", headers, None)

    def test_sends_json_with_token(self) -> None:
        with mock.patch(NO_REDIRECT_OPEN, return_value=self._response(b'{"a": 1}')) as urlopen:
            result = github_request("POST", "https://api.github.com/x", "tok", json_body={"b": 2})
        self.assertEqual(result, {"a": 1})
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 60})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"b": 2}')
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")

    def test_empty_response_is_none(self) -> None:
        with mock.patch(NO_REDIRECT_OPEN, return_value=self._response(b"")):
            self.assertIsNone(github_request("DELETE", "https://api.github.com/x", "tok"))

    def test_follows_redirects_dropping_token_off_host(self) -> None:
        for code in (301, 302, 307, 308):
            with self.subTest(code=code):
                self._check_follows_redirects(code)

    def _check_follows_redirects(self, code: int) -> None:
        responses = [
            self._redirect("https://uploads.github.com/a", "/b", code),
            self._redirect("https://uploads.github.com/b", "https://elsewhere.example/c", code),
            self._response(b'{"url": "u"}'),
        ]
        with mock.patch(NO_REDIRECT_OPEN, side_effect=responses) as urlopen:
            result = github_request("POST", "https://uploads.github.com/a", "tok", data=b"png")
        self.assertEqual(result, {"url": "u"})
        sent = [c.args[0] for c in urlopen.call_args_list]
        self.assertEqual(
            [r.full_url for r in sent],
            [
                "https://uploads.github.com/a",
                "https://uploads.github.com/b",
                "https://elsewhere.example/c",
            ],
        )
        self.assertEqual([r.get_method() for r in sent], ["POST"] * 3)
        self.assertEqual([r.data for r in sent], [b"png"] * 3)
        self.assertEqual(
            [r.get_header("Authorization") for r in sent],
            ["Bearer tok", "Bearer tok", None],
        )

    def test_redirect_to_http_drops_token(self) -> None:
        responses = [
            self._redirect("https://api.github.com/a", "http://api.github.com/b", 301),
            self._response(b"{}"),
        ]
        with mock.patch(NO_REDIRECT_OPEN, side_effect=responses) as urlopen:
            github_request("GET", "https://api.github.com/a", "tok")
        self.assertEqual(
            [c.args[0].get_header("Authorization") for c in urlopen.call_args_list],
            ["Bearer tok", None],
        )

    def test_too_many_redirects(self) -> None:
        redirect = self._redirect("https://api.github.com/x", "/x")
        with (
            mock.patch(NO_REDIRECT_OPEN, side_effect=[redirect] * 5),
            self.assertRaisesRegex(RuntimeError, "too many redirects"),
        ):
            github_request("GET", "https://api.github.com/x", "tok")

    def test_get_redirect_off_host_drops_token(self) -> None:
        # A real server, as urllib's own redirect handling (which would keep
        # the token) is what's being replaced; 127.0.0.1 and localhost are
        # different hosts to request().
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/a":
                    self.send_response(302)
                    assert isinstance(self.server, http.server.HTTPServer)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/b")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            @override
            def log_message(self, format: str, *args: Any) -> None:
                pass

        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                result = github_request("GET", f"http://127.0.0.1:{server.server_port}/a", "tok")
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen, [("/a", "Bearer tok"), ("/b", None)])

    def test_other_errors_raised(self) -> None:
        error = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", Message(), None)
        with (
            error,
            mock.patch(NO_REDIRECT_OPEN, side_effect=error),
            self.assertRaises(urllib.error.HTTPError),
        ):
            github_request("GET", "https://api.github.com/x", "tok")
