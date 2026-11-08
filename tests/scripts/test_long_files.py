import contextlib
import io
import os
import subprocess
import tempfile
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts import github_comments, long_files
from scripts.long_files import MARKER, Config, LongFile, Report, find_long_files, load_config, render

_PYPROJECT = """
[tool.lifetracker.long-files]
include = ["*.py"]
exclude = ["*/migrations/*"]
limit = 5
limits = { "tests/*" = 8 }
"""


def _lines(n: int) -> str:
    return "".join(f"x{i}\n" for i in range(n))


class _GitRepoTestCase(SimpleTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.invalid")
        self.write("pyproject.toml", _PYPROJECT)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def write(self, path: str, content: str) -> None:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)

    def commit(self, message: str = "commit") -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def config(self) -> Config:
        return load_config(self.repo / "pyproject.toml")


class FindLongFilesTests(_GitRepoTestCase):
    def test_without_base_lists_every_long_file_as_unchanged(self) -> None:
        self.write("src/big.py", _lines(7))
        self.write("src/exact.py", _lines(5))
        self.write("src/no_newline.py", _lines(5) + "last")
        self.write("src/app/migrations/0001_initial.py", _lines(50))
        self.write("notes.md", _lines(50))
        self.write("tests/test_big.py", _lines(7))  # Under its own limit of 8.
        self.write("tests/test_bigger.py", _lines(9))
        os.symlink("big.py", self.repo / "src/link.py")
        self.commit()
        self.write("src/untracked.py", _lines(50))

        report = find_long_files(self.repo, self.config(), None)

        self.assertEqual(report.changed, [])
        self.assertEqual(
            report.unchanged,
            [
                LongFile("tests/test_bigger.py", 9, 8),
                LongFile("src/big.py", 7, 5),
                LongFile("src/no_newline.py", 6, 5),
            ],
        )
        self.assertEqual(report.default_limit, 5)

    def test_with_base_splits_changed_files_with_base_counts(self) -> None:
        self.write("src/grown.py", _lines(6))
        self.write("src/split.py", _lines(9))
        self.write("src/old_name.py", _lines(20))
        self.write("src/untouched.py", _lines(6))
        self.write("src/small.py", _lines(2))
        self.commit()
        self.git("checkout", "-qb", "feature")
        self.write("src/grown.py", _lines(8))
        self.write("src/split.py", _lines(3))
        self.git("mv", "src/old_name.py", "src/new_name.py")
        self.write("src/new_name.py", _lines(21))
        self.write("src/added.py", _lines(6))
        self.write("src/small.py", _lines(3))
        self.commit()
        # A later commit on main isn't part of the PR.
        self.git("checkout", "-q", "main")
        self.write("src/untouched.py", _lines(7))
        self.commit()
        self.git("checkout", "-q", "feature")
        # Working tree edits count too.
        self.write("src/grown.py", _lines(10))

        report = find_long_files(self.repo, self.config(), "main")

        self.assertEqual(
            report.changed,
            [
                LongFile("src/new_name.py", 21, 5, 20),
                LongFile("src/grown.py", 10, 5, 6),
                LongFile("src/added.py", 6, 5, None),
                LongFile("src/split.py", 3, 5, 9),
            ],
        )
        self.assertEqual(report.unchanged, [LongFile("src/untouched.py", 6, 5)])


class LoadConfigTests(_GitRepoTestCase):
    def test_reads_the_table(self) -> None:
        config = self.config()
        self.assertEqual(config.limit_for("tests/test_x.py"), 8)
        self.assertEqual(config.limit_for("src/x.py"), 5)
        self.assertTrue(config.covers("src/x.py"))
        self.assertFalse(config.covers("src/app/migrations/0001_x.py"))
        self.assertFalse(config.covers("README.md"))

    def test_defaults(self) -> None:
        self.write("pyproject.toml", '[tool.lifetracker.long-files]\ninclude = ["*.py"]\nlimit = 3\n')
        self.assertEqual(self.config(), Config(include=["*.py"], exclude=[], limit=3, limits={}))

    def test_rejects_bad_tables(self) -> None:
        cases = {
            "": r"no \[tool\.lifetracker\.long-files\] table",
            "limit = 5": "include must be",
            'include = "*.py"\nlimit = 5': "include must be",
            'include = ["*.py"]\nexclude = [1]\nlimit = 5': "exclude must be",
            'include = ["*.py"]': "limit must be",
            'include = ["*.py"]\nlimit = 0': "limit must be",
            'include = ["*.py"]\nlimit = true': "limit must be",
            'include = ["*.py"]\nlimit = 5\nlimits = { "a" = "b" }': "limits must map",
        }
        for body, message in cases.items():
            with self.subTest(body=body):
                self.write("pyproject.toml", f"[tool.lifetracker.long-files]\n{body}\n" if body else "")
                with self.assertRaisesRegex(ValueError, message):
                    self.config()


class RenderTests(SimpleTestCase):
    def test_changed_and_unchanged(self) -> None:
        report = Report(
            changed=[LongFile("a.py", 12, 10, 8), LongFile("b.py", 11, 10, None), LongFile("c.py", 4, 10, 20)],
            unchanged=[LongFile("d.py", 30, 10)],
            default_limit=10,
        )
        body = render(report, base="origin/master", commit="abc123", repo="o/r")
        self.assertIn("## Files over 10 lines\n", body)
        self.assertIn(long_files.POLICY, body)
        self.assertIn("Counted at abc123, compared with `origin/master`.", body)
        self.assertIn("### Changed in this PR (3)", body)
        self.assertIn("| [`a.py`](https://github.com/o/r/blob/abc123/a.py) | 8 → 12 |", body)
        self.assertIn("| [`b.py`](https://github.com/o/r/blob/abc123/b.py) | new → 11 |", body)
        self.assertIn("| [`c.py`](https://github.com/o/r/blob/abc123/c.py) | 20 → 4 (now under) |", body)
        self.assertIn("<details><summary>Unchanged files (1)</summary>", body)
        self.assertIn("| [`d.py`](https://github.com/o/r/blob/abc123/d.py) | 30 |", body)
        self.assertNotIn("Limit", body)
        self.assertLess(body.index("a.py"), body.index("d.py"))

    def test_no_changed_long_files(self) -> None:
        report = Report(changed=[], unchanged=[LongFile("d.py", 30, 10)], default_limit=10)
        body = render(report, base="origin/master", commit=None, repo=None)
        self.assertIn("### Changed in this PR (0)\n\nNone of the files this PR changes", body)
        self.assertIn("| `d.py` | 30 |", body)
        self.assertNotIn("Counted at", body)

    def test_without_base(self) -> None:
        report = Report(changed=[], unchanged=[LongFile("d.py", 30, 10)], default_limit=10)
        body = render(report, base=None, commit="abc123", repo=None)
        self.assertNotIn("Changed in this PR", body)
        self.assertIn("Counted at abc123.", body)
        self.assertIn("<details><summary>Files (1)</summary>", body)

    def test_limit_column_when_a_limit_differs(self) -> None:
        report = Report(changed=[], unchanged=[LongFile("t.py", 30, 20), LongFile("d.py", 12, 10)], default_limit=10)
        body = render(report, base=None, commit=None, repo=None)
        self.assertIn("| File | Lines | Limit |\n| --- | ---: | ---: |", body)
        self.assertIn("| `t.py` | 30 | 20 |", body)

    def test_none_over(self) -> None:
        body = render(Report([], [], 10), base="origin/master", commit=None, repo=None)
        self.assertTrue(body.endswith("No files are over the limit.\n"))
        self.assertNotIn("Changed in this PR", body)


class FakeGitHub:
    """Answers github_comments.request for PR 7 of o/r, with head "abc123" and
    the given issue comments."""

    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        self.calls.append((method, url, json_body))
        if url.endswith("/pulls/7"):
            return {"head": {"sha": "abc123"}}
        if method == "GET" and "/issues/7/comments" in url:
            return self.comments
        return {}

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


class MainTests(_GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("src/big.py", _lines(7))
        self.commit()

    def _run(self, argv: list[str], request: Any = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "o/r"}),
            mock.patch.object(github_comments, "request", request or FakeGitHub([])),
            mock.patch.object(Path, "cwd", return_value=self.repo),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = long_files.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_prints_report(self) -> None:
        head = self.git("rev-parse", "HEAD").strip()
        code, out, err = self._run(["--base", ""])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"| [`src/big.py`](https://github.com/o/r/blob/{head}/src/big.py) | 7 |", out)

    def test_posts_new_comment(self) -> None:
        gh = FakeGitHub([{"id": 1, "user": {"login": "someone"}, "body": MARKER + "\nold"}])
        code, out, _ = self._run(["--base", "main", "--commit", "abc", "--post", "7", "--author", "bot"], gh)
        self.assertEqual(code, 0)
        self.assertIn("Posted the long files report to PR #7.", out)
        [(method, url, body)] = gh.writes()
        self.assertEqual((method, url), ("POST", f"{github_comments.API_URL}/repos/o/r/issues/7/comments"))
        self.assertTrue(body["body"].startswith(MARKER + "\n## Files over 5 lines"))

    def test_edits_earlier_comment(self) -> None:
        gh = FakeGitHub([{"id": 3, "user": {"login": "bot"}, "body": MARKER + "\nold"}])
        code, _, _ = self._run(["--base", "main", "--post", "7", "--author", "bot"], gh)
        self.assertEqual(code, 0)
        self.assertEqual(
            [(m, u) for m, u, _ in gh.writes()], [("PATCH", f"{github_comments.API_URL}/repos/o/r/issues/comments/3")]
        )

    def test_stale_commit_posts_nothing(self) -> None:
        gh = FakeGitHub([])
        code, out, _ = self._run(["--base", "", "--commit", "def", "--post", "7", "--author", "bot"], gh)
        self.assertEqual(code, 0)
        self.assertIn("Not posting: the report is for def, but PR #7's head is now abc123.", out)
        self.assertEqual(gh.writes(), [])

    def test_none_over_only_replaces_an_earlier_report(self) -> None:
        self.write("src/big.py", _lines(2))
        self.commit()
        with self.subTest("no earlier report"):
            gh = FakeGitHub([])
            code, out, _ = self._run(["--base", "", "--post", "7", "--author", "bot"], gh)
            self.assertEqual(code, 0)
            self.assertIn("Not posting: no files are over the limit, and PR #7 has no earlier report.", out)
            self.assertEqual(gh.writes(), [])
        with self.subTest("earlier report"):
            gh = FakeGitHub([{"id": 3, "user": {"login": "bot"}, "body": MARKER + "\nold"}])
            code, _, _ = self._run(["--base", "", "--post", "7", "--author", "bot"], gh)
            self.assertEqual(code, 0)
            [(method, _, body)] = gh.writes()
            self.assertEqual(method, "PATCH")
            self.assertIn("No files are over the limit.", body["body"])

    def test_bad_base_fails(self) -> None:
        code, _, err = self._run(["--base", "no-such-branch"])
        self.assertEqual(code, 1)
        self.assertIn("git merge-base no-such-branch HEAD failed", err)

    def test_bad_config_fails(self) -> None:
        self.write("pyproject.toml", "")
        code, _, err = self._run([])
        self.assertEqual(code, 1)
        self.assertIn("no [tool.lifetracker.long-files] table", err)

    def test_http_error_fails(self) -> None:
        def request(*args: object, **kwargs: object) -> Any:
            raise urllib.error.HTTPError("https://x", 403, "Forbidden", Message(), io.BytesIO(b"nope"))

        code, _, err = self._run(["--base", "", "--post", "7", "--author", "bot"], request)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403 from https://x:\nnope", err)
