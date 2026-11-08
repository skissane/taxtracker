import contextlib
import io
import json
import subprocess
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts.recreate_pr import main as recreate_pr_main
from scripts.recreate_pr import parse_pr_url


class RecreatePrTests(SimpleTestCase):
    URL = "https://github.com/o/r/pull/5"

    def pr_json(self, **overrides: object) -> str:
        data: dict[str, object] = {
            "number": 5,
            "title": "T",
            "body": "B",
            "headRefName": "feat",
            "baseRefName": "master",
            "isCrossRepository": False,
            "headRepositoryOwner": {"login": "o"},
            "state": "OPEN",
            "labels": [{"name": "type:bug"}],
            "milestone": {"title": "M1"},
        }
        data.update(overrides)
        return json.dumps(data)

    def run_main(
        self,
        argv: list[str],
        view_json: str | None = None,
        create_error: bool = False,
        reopen_error: bool = False,
        comment_error: bool = False,
        list_json: str = "[]",
        list_error: bool = False,
    ) -> tuple[str | int | None, list[tuple[list[str], str | None]], str]:
        """Run main() with `gh` mocked; return (exit code or None, calls, stderr)."""
        calls = []

        def fake_run(cmd: list[str], input: str | None = None, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append((cmd[1:], input))
            sub = cmd[2]
            if sub == "view":
                return subprocess.CompletedProcess(cmd, 0, stdout=view_json or self.pr_json(), stderr="")
            if sub == "list":
                if list_error:
                    raise subprocess.CalledProcessError(1, cmd, stderr="list failed")
                return subprocess.CompletedProcess(cmd, 0, stdout=list_json, stderr="")
            if sub == "create" and create_error:
                raise subprocess.CalledProcessError(1, cmd, stderr="create failed")
            if sub == "comment" and comment_error:
                raise subprocess.CalledProcessError(1, cmd, stderr="comment failed")
            if sub == "reopen" and reopen_error:
                raise subprocess.CalledProcessError(1, cmd, stderr="reopen failed")
            out = "https://github.com/o/r/pull/9\n" if sub == "create" else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        err = io.StringIO()
        code = None
        with (
            mock.patch("scripts.recreate_pr.subprocess.run", side_effect=fake_run),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                recreate_pr_main(argv)
            except SystemExit as e:
                code = e.code
        return code, calls, err.getvalue()

    def test_parse_pr_url(self) -> None:
        self.assertEqual(parse_pr_url("https://github.com/o/r/pull/5/files"), ("github.com", "o/r", "5"))
        self.assertEqual(parse_pr_url("https://ghe.example.com/o/r/pull/5"), ("ghe.example.com", "o/r", "5"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_pr_url("https://github.com/o/r/issues/5")

    def test_success_closes_creates_and_comments(self) -> None:
        code, calls, _ = self.run_main([self.URL])
        self.assertIsNone(code)
        subs = [c[0][1] for c in calls]
        self.assertEqual(subs, ["view", "close", "create", "comment"])
        create_args, body = calls[2]
        self.assertEqual(body, "B")
        self.assertIn("--draft", create_args)
        self.assertEqual(create_args[create_args.index("--label") + 1], "type:bug")
        self.assertEqual(create_args[create_args.index("--milestone") + 1], "M1")
        # Regression guard: the original's assignees (often a bot) must not be copied
        self.assertNotIn("--assignee", create_args)
        # Every call is pinned to the URL's host, so none can go to another host
        for args, _ in calls[1:]:
            self.assertEqual(args[args.index("--repo") + 1], "github.com/o/r")
        self.assertIn("Superseded by #9", calls[3][0])

    def test_refuses_fork(self) -> None:
        code, calls, err = self.run_main([self.URL], view_json=self.pr_json(isCrossRepository=True))
        self.assertEqual(code, 1)
        self.assertIn("fork", err)
        self.assertEqual(len(calls), 1)

    def test_refuses_non_open(self) -> None:
        code, calls, err = self.run_main([self.URL], view_json=self.pr_json(state="MERGED"))
        self.assertEqual(code, 1)
        self.assertIn("MERGED", err)
        self.assertEqual(len(calls), 1)

    def test_refuses_number_mismatch(self) -> None:
        code, _, err = self.run_main([self.URL], view_json=self.pr_json(number=6))
        self.assertEqual(code, 1)
        self.assertIn("PR #6", err)

    def test_create_failure_reopens_original(self) -> None:
        code, calls, err = self.run_main([self.URL], create_error=True)
        self.assertEqual(code, 1)
        self.assertEqual([c[0][1] for c in calls], ["view", "close", "create", "list", "reopen"])
        self.assertIn("create failed", err)
        list_args = calls[3][0]
        self.assertEqual(list_args[list_args.index("--head") + 1], "feat")
        self.assertEqual(list_args[list_args.index("--base") + 1], "master")

    def test_create_failure_after_creating_does_not_reopen(self) -> None:
        code, calls, err = self.run_main(
            [self.URL], create_error=True, list_json='[{"url": "https://github.com/o/r/pull/9"}]'
        )
        self.assertEqual(code, 1)
        self.assertEqual([c[0][1] for c in calls], ["view", "close", "create", "list"])
        self.assertIn("https://github.com/o/r/pull/9 was created anyway", err)

    def test_create_failure_list_failure_aborts_without_reopening(self) -> None:
        code, calls, err = self.run_main([self.URL], create_error=True, list_error=True)
        self.assertEqual(code, 1)
        self.assertEqual([c[0][1] for c in calls], ["view", "close", "create", "list"])
        self.assertIn("list failed", err)
        self.assertIn("could not check whether the new PR was created anyway", err)

    def test_create_failure_bad_list_output_aborts_without_reopening(self) -> None:
        for output in ("not json", "{}"):
            with self.subTest(output=output):
                code, calls, err = self.run_main([self.URL], create_error=True, list_json=output)
                self.assertEqual(code, 1)
                self.assertEqual([c[0][1] for c in calls], ["view", "close", "create", "list"])
                self.assertIn("could not check whether the new PR was created anyway", err)

    def test_moved_repo_is_not_a_fork(self) -> None:
        # An old URL still resolves after a rename or transfer, so the head repo can differ from the URL's
        code, calls, _ = self.run_main([self.URL], view_json=self.pr_json(headRepositoryOwner={"login": "new"}))
        self.assertIsNone(code)
        self.assertEqual([c[0][1] for c in calls], ["view", "close", "create", "comment"])

    def test_reopen_failure_says_to_reopen_manually(self) -> None:
        code, _, err = self.run_main([self.URL], create_error=True, reopen_error=True)
        self.assertEqual(code, 1)
        self.assertIn("still CLOSED", err)

    def test_comment_failure_reports_new_pr(self) -> None:
        code, calls, err = self.run_main([self.URL], comment_error=True)
        self.assertEqual(code, 1)
        self.assertEqual([c[0][1] for c in calls], ["view", "close", "create", "comment"])
        self.assertIn("comment failed", err)
        self.assertIn("created https://github.com/o/r/pull/9 and closed PR #5", err)

    def test_gh_missing(self) -> None:
        err = io.StringIO()
        with (
            mock.patch("scripts.recreate_pr.subprocess.run", side_effect=FileNotFoundError),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit),
        ):
            recreate_pr_main([self.URL])
        self.assertIn("'gh' CLI not found", err.getvalue())

    def test_gh_command_failure(self) -> None:
        error = subprocess.CalledProcessError(1, ["gh"], stderr="boom")
        err = io.StringIO()
        with (
            mock.patch("scripts.recreate_pr.subprocess.run", side_effect=error),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit),
        ):
            recreate_pr_main([self.URL])
        self.assertIn("boom", err.getvalue())
