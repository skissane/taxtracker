import contextlib
import http.client
import http.server
import io
import urllib.error
import urllib.request
from email.message import Message
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts.check_deploy import check_once
from scripts.check_deploy import main as check_deploy_main


class CheckDeployTests(SimpleTestCase):
    URL = "https://app.example/healthz/"
    COMMIT = "abc123"

    def _response(self, status: int, payload: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        response.__enter__.return_value.read.return_value = payload
        return response

    def _http_error(self, status: int, payload: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(self.URL, status, "Error", Message(), io.BytesIO(payload))

    def _check(self, result: object) -> str | None:
        kwargs: dict[str, Any] = {"side_effect" if isinstance(result, Exception) else "return_value": result}
        with mock.patch("urllib.request.urlopen", **kwargs) as urlopen:
            problem = check_once(self.URL, self.COMMIT, 7)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 7})
        return problem

    def test_healthy(self) -> None:
        body = b'{"status": "ok", "commit": "abc123"}'
        self.assertIsNone(self._check(self._response(200, body)))

    def test_other_commit(self) -> None:
        body = b'{"status": "ok", "commit": "old"}'
        self.assertEqual(
            self._check(self._response(200, body)),
            "running commit 'old', not 'abc123'",
        )

    def test_unhealthy(self) -> None:
        body = b'{"status": "error", "problem": "unapplied migrations"}'
        self.assertEqual(
            self._check(self._http_error(503, body)),
            "HTTP 503, status 'error': unapplied migrations",
        )

    def test_not_json(self) -> None:
        self.assertEqual(self._check(self._http_error(502, b"<html>")), "HTTP 502, not JSON")
        self.assertEqual(self._check(self._response(200, b"[]")), "HTTP 200, unexpected JSON")

    def test_request_failed(self) -> None:
        self.assertEqual(
            self._check(urllib.error.URLError("timed out")),
            "request failed: timed out",
        )
        self.assertEqual(self._check(TimeoutError("slow")), "request failed: slow")
        self.assertEqual(
            self._check(http.client.IncompleteRead(b"")),
            "request failed: IncompleteRead(0 bytes read)",
        )

    def _main(self, problems: list[str | None], *args: str) -> tuple[int, list[float], str, str]:
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("scripts.check_deploy.check_once", side_effect=problems),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = check_deploy_main([self.URL, self.COMMIT, *args], clock=lambda: now[0], sleep=sleep)
        return code, sleeps, out.getvalue(), err.getvalue()

    def test_retries_until_healthy(self) -> None:
        code, sleeps, out, _ = self._main(["request failed: x", None])
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [5])
        self.assertEqual(
            out,
            "Attempt 1: request failed: x\nAttempt 2: healthy, running abc123\n",
        )
        self.assertNotIn(self.URL, out)

    def test_gives_up_after_timeout(self) -> None:
        code, sleeps, out, err = self._main(["down"] * 10, "--timeout", "10", "--interval", "4")
        self.assertEqual(code, 1)
        self.assertEqual(sleeps, [4, 4])
        self.assertEqual(out.count("down"), 3)
        self.assertIn("not healthy after 10 seconds", err)

    def test_request_timeout_capped_by_deadline(self) -> None:
        now = [0.0]
        timeouts = []

        def check(url: str, commit: str, timeout: float) -> str:
            timeouts.append(timeout)
            now[0] += 8
            return "down"

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with (
            mock.patch("scripts.check_deploy.check_once", side_effect=check),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = check_deploy_main(
                [self.URL, self.COMMIT, "--timeout", "20", "--interval", "1"],
                clock=lambda: now[0],
                sleep=sleep,
            )
        self.assertEqual(code, 1)
        self.assertEqual(timeouts, [20, 11, 2])
