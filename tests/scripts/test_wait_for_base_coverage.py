import contextlib
import http.client
import http.server
import io
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts import wait_for_base_coverage
from scripts.wait_for_base_coverage import base_ci_state, codecov_coverage
from scripts.wait_for_base_coverage import main as wait_for_base_coverage_main
from tests.scripts.test_github_comments import NO_REDIRECT_OPEN


class WaitForBaseCoverageTests(SimpleTestCase):
    REPO = "owner/repo"
    SHA = "abc123"

    def _response(self, status: int, payload: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        response.__enter__.return_value.read.return_value = payload
        return response

    def _http_error(self, status: int, payload: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://example/", status, "Error", Message(), io.BytesIO(payload))

    def _call(
        self, func: Callable[..., Any], result: object, *args: Any, target: str = "urllib.request.urlopen"
    ) -> tuple[Any, urllib.request.Request]:
        kwargs: dict[str, Any] = {"side_effect" if isinstance(result, Exception) else "return_value": result}
        with mock.patch(target, **kwargs) as urlopen:
            outcome = func(self.REPO, self.SHA, *args, 7)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 7})
        return outcome, urlopen.call_args.args[0]

    def test_codecov_coverage(self) -> None:
        outcome, request = self._call(codecov_coverage, self._response(200, b'{"totals": {"coverage": 98.5}}'))
        self.assertEqual(outcome, (True, "coverage 98.5%"))
        self.assertEqual(request.full_url, "https://api.codecov.io/api/v2/github/owner/repos/repo/totals/?sha=abc123")

    def test_codecov_no_coverage(self) -> None:
        self.assertEqual(
            self._call(codecov_coverage, self._http_error(404, b'{"detail": "No coverage report"}'))[0],
            (False, "no report yet"),
        )
        self.assertEqual(self._call(codecov_coverage, self._http_error(500, b"<html>"))[0], (False, "HTTP 500"))
        self.assertEqual(
            self._call(codecov_coverage, self._response(200, b'{"totals": {"coverage": null}}'))[0],
            (False, "no coverage in report"),
        )
        self.assertEqual(self._call(codecov_coverage, self._response(200, b"[]"))[0], (False, "no coverage in report"))
        self.assertEqual(
            self._call(codecov_coverage, urllib.error.URLError("timed out"))[0],
            (False, "request failed: timed out"),
        )

    def _check_runs(
        self, *runs: dict[str, Any], token: str | None = None
    ) -> tuple[tuple[str, str], urllib.request.Request]:
        body = json.dumps({"total_count": len(runs), "check_runs": list(runs)}).encode()
        with mock.patch(NO_REDIRECT_OPEN, return_value=self._response(200, body)) as urlopen:
            outcome = base_ci_state(self.REPO, self.SHA, "test", token, 7)
        return outcome, urlopen.call_args.args[0]

    def test_base_ci_running(self) -> None:
        outcome, request = self._check_runs({"id": 1, "status": "in_progress", "conclusion": None})
        self.assertEqual(outcome, ("running", "test in_progress"))
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/owner/repo/commits/abc123/check-runs?check_name=test&filter=latest",
        )
        self.assertFalse(request.has_header("Authorization"))

    def test_base_ci_finished(self) -> None:
        outcome, request = self._check_runs(
            {"id": 2, "status": "completed", "conclusion": "failure"},
            {"id": 1, "status": "in_progress", "conclusion": None},
            token="t0k",
        )
        self.assertEqual(outcome, ("failed", "test failure"))
        self.assertEqual(request.get_header("Authorization"), "Bearer t0k")
        self.assertEqual(
            self._check_runs({"id": 1, "status": "completed", "conclusion": "success"})[0],
            ("succeeded", "test success"),
        )
        self.assertEqual(
            self._check_runs({"id": 1, "status": "completed", "conclusion": "cancelled"})[0],
            ("failed", "test cancelled"),
        )
        self.assertEqual(self._check_runs()[0], ("none", "no test check run"))

    def test_base_ci_errors(self) -> None:
        self.assertEqual(
            self._call(
                base_ci_state, self._http_error(403, b'{"message": "no"}'), "test", None, target=NO_REDIRECT_OPEN
            )[0],
            ("running", "HTTP 403"),
        )
        self.assertEqual(
            self._call(base_ci_state, http.client.IncompleteRead(b""), "test", None, target=NO_REDIRECT_OPEN)[0],
            ("running", "request failed: IncompleteRead(0 bytes read)"),
        )

    def test_base_ci_redirect_not_followed(self) -> None:
        # A real server, as it's urllib's own redirect handling (which would
        # send the token on to another host) that mustn't run.
        seen: list[tuple[str, str | None]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                assert isinstance(self.server, http.server.HTTPServer)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with mock.patch.object(wait_for_base_coverage, "GITHUB_API", f"http://127.0.0.1:{server.server_port}"):
                    outcome = base_ci_state(self.REPO, self.SHA, "test", "t0k", 7)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(outcome, ("running", "HTTP 302"))
        self.assertEqual(
            seen, [("/repos/owner/repo/commits/abc123/check-runs?check_name=test&filter=latest", "Bearer t0k")]
        )

    def _main(
        self, coverage: list[tuple[bool, str]], ci: list[tuple[str, str]], *args: str
    ) -> tuple[int, list[float], str, mock.MagicMock]:
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        out = io.StringIO()
        with (
            mock.patch("scripts.wait_for_base_coverage.codecov_coverage", side_effect=coverage),
            mock.patch("scripts.wait_for_base_coverage.base_ci_state", side_effect=ci) as ci_mock,
            mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t0k"}),
            contextlib.redirect_stdout(out),
        ):
            code = wait_for_base_coverage_main([self.REPO, self.SHA, *args], clock=lambda: now[0], sleep=sleep)
        return code, sleeps, out.getvalue(), ci_mock

    def test_base_already_has_coverage(self) -> None:
        code, sleeps, out, ci = self._main([(True, "coverage 98%")], [])
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(out, "Attempt 1: Codecov has the base report for abc123 (coverage 98%)\n")
        ci.assert_not_called()

    def test_waits_for_base_ci(self) -> None:
        code, sleeps, out, ci = self._main(
            [(False, "no report yet"), (False, "no report yet"), (True, "coverage 98%")],
            [("running", "test in_progress"), ("succeeded", "test success")],
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("Attempt 1: Codecov: no report yet; base CI: test in_progress\n", out)
        self.assertNotIn("::warning::", out)
        self.assertEqual(ci.call_args.args, (self.REPO, self.SHA, "test", "t0k", 30))

    def test_gives_up_after_grace(self) -> None:
        code, sleeps, out, _ = self._main(
            [(False, "no report yet")] * 10,
            [("succeeded", "test success")] * 10,
            "--grace",
            "25",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10, 10])
        self.assertTrue(
            out.endswith(
                "::warning::Codecov has no coverage for base commit abc123, whose CI has finished (test success)\n"
            )
        )

    def test_gives_up_at_once_if_base_ci_failed(self) -> None:
        code, sleeps, out, _ = self._main(
            [(False, "no report yet")] * 10,
            [("running", "test in_progress"), ("failed", "test failure")],
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10])
        self.assertTrue(
            out.endswith(
                "::warning::Codecov has no coverage for base commit abc123, whose CI didn't succeed (test failure)\n"
            )
        )

    def test_grace_restarts_if_base_ci_reruns(self) -> None:
        code, sleeps, _, _ = self._main(
            [(False, "no report yet")] * 10,
            [("none", "no test check run"), ("running", "test queued"), ("succeeded", "test success")] * 2,
            "--grace",
            "10",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10, 10])

    def test_gives_up_after_timeout(self) -> None:
        code, sleeps, out, _ = self._main(
            [(False, "no report yet")] * 10,
            [("running", "test in_progress")] * 10,
            "--timeout",
            "25",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("::warning::Codecov still has no coverage for base commit abc123 after 25 seconds", out)

    def test_request_timeout_capped_by_deadline(self) -> None:
        now = [0.0]
        timeouts = []

        def request(*args: Any) -> tuple[bool, str]:
            timeouts.append(args[-1])
            now[0] += 4
            return False, "slow"

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with (
            mock.patch("scripts.wait_for_base_coverage.codecov_coverage", side_effect=request),
            mock.patch("scripts.wait_for_base_coverage.base_ci_state", side_effect=request),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = wait_for_base_coverage_main(
                [self.REPO, self.SHA, "--timeout", "20", "--interval", "1", "--request-timeout", "15"],
                clock=lambda: now[0],
                sleep=sleep,
            )
        self.assertEqual(code, 0)
        self.assertEqual(timeouts, [15, 15, 11, 7, 2, 1])
