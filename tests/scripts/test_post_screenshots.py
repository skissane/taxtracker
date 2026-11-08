import contextlib
import io
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts.post_screenshots import MARKER
from scripts.post_screenshots import main as post_screenshots_main


class PostScreenshotsTests(SimpleTestCase):
    def _run(
        self,
        filenames: list[str],
        *args: str,
        comments: Sequence[dict[str, Any]] = (),
        env: dict[str, str] | None = None,
        fail: tuple[Any, ...] | None = None,
        asset_url: bool = True,
        head: str = "abc1234def",
    ) -> tuple[int, str, list[tuple[Any, ...]]]:
        """Run main() on PR 7 in a temp dir whose screenshots/ holds the given
        files, with the API faked: uploads return a URL based on the file name,
        and the PR has the given head commit and comments. fail names a (method,
        URL prefix) to answer with a 403. Return the exit code, stderr and each
        request as (method, url, token, json_body, data)."""
        requests: list[tuple[Any, ...]] = []

        def fake_request(
            method: str, url: str, token: str, *, json_body: object = None, data: bytes | None = None, **kw: Any
        ) -> Any:
            requests.append((method, url, token, json_body, data))
            if fail and method == fail[0] and url.startswith(fail[1]):
                if len(fail) > 2:
                    raise fail[2]
                raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO(b"nope"))
            if url == "https://api.github.com/user":
                return {"login": "me"}
            if url == "https://api.github.com/repos/o/r":
                return {"id": 42}
            if url.startswith("https://uploads.github.com/"):
                name = url.split("name=")[1].split("&")[0]
                return {"url": f"https://example.com/{name}"} if asset_url else {}
            if url == "https://api.github.com/repos/o/r/pulls/7":
                return {"head": {"sha": head}}
            if method == "GET" and "/issues/7/comments" in url:
                page = int(url.rsplit("page=", 1)[1])
                return list(comments[(page - 1) * 100 : page * 100])
            return None

        if env is None:
            env = {
                "SCREENSHOTS_TOKEN": "pat",
                "GITHUB_TOKEN": "ghs",
                "GITHUB_REPOSITORY": "o/r",
            }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "screenshots").mkdir()
            for filename in filenames:
                (path / "screenshots" / filename).write_bytes(filename.encode())
            stderr = io.StringIO()
            with (
                contextlib.chdir(path),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
                mock.patch.dict("os.environ", env, clear=True),
                mock.patch("scripts.github_comments.request", fake_request),
            ):
                code = post_screenshots_main(["7", *args])
        return code, stderr.getvalue(), requests

    def _writes(self, requests: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [r for r in requests if r[0] in ("POST", "PATCH", "DELETE")]

    def test_uploads_with_pat_and_posts_with_github_token(self) -> None:
        code, _, requests = self._run(
            ["login.png", "index.png", "notes.txt"],
            "--commit",
            "abc1234",
            "--run-url",
            "https://example.com/run",
            "--author",
            "github-actions[bot]",
        )
        self.assertEqual(code, 0)
        uploads = [r for r in requests if r[1].startswith("https://uploads.")]
        self.assertEqual(
            [(r[1], r[2], r[4]) for r in uploads],
            [
                (
                    "https://uploads.github.com/user-attachments/assets"
                    f"?name={name}&content_type=image%2Fpng&repository_id=42",
                    "pat",
                    name.encode(),
                )
                for name in ["index.png", "login.png"]
            ],
        )
        # Everything but the uploads uses the comment token.
        self.assertEqual({r[2] for r in requests if r not in uploads}, {"ghs"})
        # No user lookup when --author is given (an installation token can't).
        self.assertNotIn("https://api.github.com/user", [r[1] for r in requests])
        (post,) = self._writes(requests)[2:]
        self.assertEqual(post[:2], ("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        body = post[3]["body"]
        self.assertTrue(body.startswith(MARKER + "\n"))
        self.assertIn(
            "Taken for abc1234 by [this workflow run](https://example.com/run).",
            body,
        )
        self.assertLess(
            body.index(
                '<details><summary>index</summary><img src="https://example.com/index.png" alt="index"></details>'
            ),
            body.index(
                '<details><summary>login</summary><img src="https://example.com/login.png" alt="login"></details>'
            ),
        )
        self.assertNotIn("notes", body)

    def test_checks_head_commit(self) -> None:
        code, _, requests = self._run(["index.png"], "--commit", "abc1234def")
        self.assertEqual(code, 0)
        self.assertIn(
            ("GET", "https://api.github.com/repos/o/r/pulls/7"),
            [r[:2] for r in requests],
        )
        self.assertEqual(self._writes(requests)[-1][0], "POST")

    def test_stale_commit_posts_nothing(self) -> None:
        # An older commit's run finishing after the newer commit's has posted.
        comments = [{"id": 1, "user": {"login": "me"}, "body": MARKER + "\nnew"}]
        code, _, requests = self._run(["index.png"], "--commit", "0ld0ld0", comments=comments)
        self.assertEqual(code, 0)
        # Not even the uploads.
        self.assertEqual(self._writes(requests), [])

    def test_edits_own_previous_comment_in_place(self) -> None:
        comments = [
            {"id": 1, "user": {"login": "me"}, "body": MARKER + "\nold"},
            {"id": 2, "user": {"login": "me"}, "body": "a review reply"},
            {"id": 3, "user": {"login": "someone"}, "body": MARKER + "\ntheirs"},
            {"id": 4, "user": {"login": "me"}, "body": MARKER + "\nduplicate"},
        ]
        code, _, requests = self._run(["index.png"], comments=comments)
        self.assertEqual(code, 0)
        comments_url = "https://api.github.com/repos/o/r/issues/comments"
        self.assertEqual(
            [r[:2] for r in self._writes(requests)[1:]],
            [("PATCH", f"{comments_url}/1"), ("DELETE", f"{comments_url}/4")],
        )

    def test_paginates_comments(self) -> None:
        comments = [{"id": n, "user": {"login": "me"}, "body": "chatter"} for n in range(150)]
        comments[120]["body"] = MARKER + "\nold"
        code, _, requests = self._run(["index.png"], comments=comments)
        self.assertEqual(code, 0)
        self.assertEqual(
            [r[1].rsplit("&", 1)[1] for r in requests if r[0] == "GET" and "/7/" in r[1]],
            ["page=1", "page=2"],
        )
        self.assertEqual(
            self._writes(requests)[-1][:2],
            ("PATCH", "https://api.github.com/repos/o/r/issues/comments/120"),
        )

    def test_failed_upload_posts_nothing(self) -> None:
        code, stderr, requests = self._run(["index.png"], fail=("POST", "https://uploads."))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", stderr)
        self.assertIn("nope", stderr)
        self.assertEqual(self._writes(requests)[1:], [])

    def test_network_error_reported(self) -> None:
        code, stderr, _ = self._run(
            ["index.png"],
            fail=("POST", "https://uploads.", urllib.error.URLError("unreachable")),
        )
        self.assertEqual(code, 1)
        self.assertIn("unreachable", stderr)

    def test_upload_without_url_fails(self) -> None:
        code, stderr, requests = self._run(["index.png"], asset_url=False)
        self.assertEqual(code, 1)
        self.assertIn("returned no asset URL", stderr)
        self.assertEqual(self._writes(requests)[1:], [])

    def test_tokens_and_repo_default_to_gh(self) -> None:
        def fake_gh(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            out = "o/r" if cmd[1] == "repo" else "gho_local"
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n")

        with mock.patch("scripts.github_comments.subprocess.run", fake_gh):
            code, _, requests = self._run(["index.png"], env={})
        self.assertEqual(code, 0)
        self.assertEqual({r[2] for r in requests}, {"gho_local"})

    def test_gh_failure_reported(self) -> None:
        def fake_gh(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, cmd, stderr="not logged in")

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("scripts.github_comments.subprocess.run", fake_gh),
            tempfile.TemporaryDirectory() as tmp,
            contextlib.chdir(tmp),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            Path("screenshots").mkdir()
            Path("screenshots/index.png").touch()
            code = post_screenshots_main(["7"])
        self.assertEqual(code, 1)
        self.assertIn("not logged in", stderr.getvalue())

    def test_no_screenshots(self) -> None:
        code, stderr, requests = self._run(["notes.txt"])
        self.assertEqual(code, 1)
        self.assertIn("no screenshots", stderr)
        self.assertEqual(requests, [])

    def test_unsafe_names_rejected(self) -> None:
        for name in ["--help.png", "a b.png", "x](y).png", "Index.png"]:
            with self.subTest(name):
                # Not index.png, which is the same file as Index.png on
                # case-insensitive filesystems (macOS's default).
                code, stderr, requests = self._run(["login.png", name])
                self.assertEqual(code, 1)
                self.assertIn("doesn't match", stderr)
                self.assertEqual(requests, [])
