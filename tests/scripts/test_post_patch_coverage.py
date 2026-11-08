import contextlib
import io
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts.post_patch_coverage import MARKER as PATCH_COVERAGE_MARKER
from scripts.post_patch_coverage import MAX_BODY_CHARS, _build_body
from scripts.post_patch_coverage import main as post_patch_coverage_main
from scripts.post_screenshots import MARKER


class PostPatchCoverageTests(SimpleTestCase):
    HEAD = (
        "# Diff Coverage\n## Diff: origin/master...HEAD\n\n- a&#46;py (50.0%): Missing lines 2\n\n"
        "## Summary\n\n- **Total**: 2 lines\n- **Missing**: 1 line\n- **Coverage**: 50%\n"
    )
    REPORT = HEAD + "\n\n\n## a&#46;py\n\nLines 1-2\n\n```python\n  1 a = 1\n! 2 b = 2\n```\n\n\n---\n"

    def _run(
        self,
        *args: str,
        report: str | None = REPORT,
        comments: Sequence[dict[str, Any]] = (),
        fail: tuple[Any, ...] | None = None,
        head: str = "abc1234def",
    ) -> tuple[int, str, list[tuple[Any, ...]]]:
        """Run main() on PR 7 in a temp dir holding patch-coverage.md (unless
        report is None), with the API faked as in PostScreenshotsTests. Return
        the exit code, stderr and each request as (method, url, token, json_body)."""
        requests: list[tuple[Any, ...]] = []

        def fake_request(method: str, url: str, token: str, *, json_body: object = None, **kw: Any) -> Any:
            requests.append((method, url, token, json_body))
            if fail and method == fail[0] and url.startswith(fail[1]):
                if len(fail) > 2:
                    raise fail[2]
                raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO(b"nope"))
            if url == "https://api.github.com/user":
                return {"login": "me"}
            if url == "https://api.github.com/repos/o/r/pulls/7":
                return {"head": {"sha": head}}
            if method == "GET" and "/issues/7/comments" in url:
                return list(comments)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            if report is not None:
                (Path(tmp) / "patch-coverage.md").write_text(report)
            stderr = io.StringIO()
            with (
                contextlib.chdir(tmp),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
                mock.patch.dict("os.environ", {"GITHUB_TOKEN": "ghs", "GITHUB_REPOSITORY": "o/r"}, clear=True),
                mock.patch("scripts.github_comments.request", fake_request),
            ):
                code = post_patch_coverage_main(["7", *args])
        return code, stderr.getvalue(), requests

    def _writes(self, requests: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [r for r in requests if r[0] in ("POST", "PATCH", "DELETE")]

    def test_posts_report(self) -> None:
        code, _, requests = self._run(
            "--commit",
            "abc1234",
            "--run-url",
            "https://example.com/run",
            "--author",
            "github-actions[bot]",
        )
        self.assertEqual(code, 0)
        self.assertEqual({r[2] for r in requests}, {"ghs"})
        self.assertNotIn("https://api.github.com/user", [r[1] for r in requests])
        (post,) = self._writes(requests)
        self.assertEqual(post[:2], ("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        self.assertEqual(
            post[3]["body"],
            f"{PATCH_COVERAGE_MARKER}\n## Diff Coverage\n\n"
            "Patch coverage of abc1234, from [this workflow run](https://example.com/run).\n\n"
            "### Diff: origin/master...HEAD\n\n"
            "| File | Coverage | Missing lines |\n| --- | ---: | --- |\n| `a.py` | 50.0% | 2 |\n\n"
            "### Summary\n\n| Total | Missing | Coverage |\n| ---: | ---: | ---: |\n| 2 lines | 1 line | 50% |\n\n\n\n"
            "### a&#46;py\n\nLines 1-2\n\n```python\n  1 a = 1\n! 2 b = 2\n```\n\n\n---",
        )

    def test_no_lines(self) -> None:
        report = "# Diff Coverage\n## Diff: origin/master...HEAD\n\nNo lines with coverage information in this diff.\n"
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertEqual(
            self._writes(requests)[0][3]["body"],
            f"{PATCH_COVERAGE_MARKER}\n## Diff Coverage\n\n### Diff: origin/master...HEAD\n\n"
            "No lines with coverage information in this diff.",
        )

    def test_unexpected_report_fails(self) -> None:
        no_lines = "# Diff Coverage\n## Diff: x\n\nNo lines with coverage information in this diff.\n"
        cases = {
            "": "line 1: expected '# Diff Coverage', found ''",
            "# Coverage\n": "line 1: expected '# Diff Coverage'",
            "# Diff Coverage\n": "line 2: expected '## Diff: ...', found the end of the report",
            "# Diff Coverage\n## Diff: x\n\n* a.py (50%)\n": "line 4: expected a file's coverage",
            "# Diff Coverage\n## Diff: x\n\n- a.py (50%): Missing 2\n": "line 4: expected a file's coverage",
            no_lines + "more\n": "line 5: expected nothing more, found 'more'",
            self.HEAD.replace("## Summary", "## Totals"): "line 6: expected '## Summary', found '## Totals'",
            self.HEAD.replace("**Missing**", "**Uncovered**"): "line 9: expected '- **Missing**: ...'",
            self.HEAD.replace("**Total**: 2 lines", "**Coverage**: 50%"): "line 8: expected '- **Total**: ...'",
            self.HEAD.replace("- **Coverage**: 50%\n", ""): "line 10: expected '- **Coverage**: ...', found the end",
        }
        for report, error in cases.items():
            with self.subTest(report):
                code, stderr, requests = self._run(report=report)
                self.assertEqual(code, 1)
                self.assertIn(f"diff-cover report {error}", stderr)
                self.assertEqual(requests, [])

    def test_stale_commit_posts_nothing(self) -> None:
        code, _, requests = self._run("--commit", "0ld0ld0")
        self.assertEqual(code, 0)
        self.assertEqual(self._writes(requests), [])

    def test_edits_previous_comment(self) -> None:
        comments = [
            {"id": 1, "user": {"login": "me"}, "body": MARKER + "\nscreenshots"},
            {"id": 2, "user": {"login": "me"}, "body": PATCH_COVERAGE_MARKER + "\nold"},
        ]
        code, _, requests = self._run(comments=comments)
        self.assertEqual(code, 0)
        self.assertEqual(
            [r[:2] for r in self._writes(requests)],
            [("PATCH", "https://api.github.com/repos/o/r/issues/comments/2")],
        )

    def test_lists_become_tables(self) -> None:
        report = (
            "# Diff Coverage\n## Diff: origin/master...HEAD\n\n"
            "- scripts/a&#46;py (100%)\n- b&#46;py (40.0%): Missing lines 3,5-9,12\n\n"
            "## Summary\n\n- **Total**: 10 lines\n- **Missing**: 6 lines\n- **Coverage**: 40%\n\n"
            "## b&#46;py\n\n```python\n# not a heading\n```\n- something else\n"
        )
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertEqual(
            self._writes(requests)[0][3]["body"].split("\n")[3:],
            [
                "### Diff: origin/master...HEAD",
                "",
                "| File | Coverage | Missing lines |",
                "| --- | ---: | --- |",
                "| `scripts/a.py` | 100% |  |",
                "| `b.py` | 40.0% | 3, 5-9, 12 |",
                "",
                "### Summary",
                "",
                "| Total | Missing | Coverage |",
                "| ---: | ---: | ---: |",
                "| 10 lines | 6 lines | 40% |",
                "",
                "### b&#46;py",
                "",
                "```python",
                "# not a heading",
                "```",
                "- something else",
            ],
        )

    def test_awkward_path(self) -> None:
        # A pipe would end the cell, and a backtick the code span.
        report = self.HEAD.replace("- a&#46;py (50.0%)", "- a|b`c&#46;py (50.0%)")
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertIn("| `` a\\|b`c.py `` | 50.0% | 2 |", self._writes(requests)[0][3]["body"])

    def test_long_report_truncated(self) -> None:
        lines = "\n".join(f"  {n} x = {n}" for n in range(20_000))
        report = f"{self.HEAD}\n## a.py\n\n```python\n{lines}\n```\n"
        code, _, requests = self._run("--run-url", "https://example.com/run", report=report)
        self.assertEqual(code, 0)
        body = self._writes(requests)[0][3]["body"]
        self.assertLess(len(body), 65_536)
        before, note = body.rsplit("\n\n", 1)
        self.assertLessEqual(len(before), MAX_BODY_CHARS + 4)
        self.assertTrue(before.endswith("\n```"))
        self.assertEqual(before.count("```"), 2)
        self.assertIn("| 2 lines | 1 line | 50% |", before)
        self.assertEqual(
            note,
            "_Report truncated; the whole of it is in the `patch-coverage` artifact of "
            "[this workflow run](https://example.com/run)._",
        )

    def test_missing_report(self) -> None:
        code, stderr, requests = self._run(report=None)
        self.assertEqual(code, 1)
        self.assertIn("patch-coverage.md", stderr)
        self.assertEqual(requests, [])

    def test_network_error_reported(self) -> None:
        code, stderr, _ = self._run(fail=("GET", "https://api.github.com/user", urllib.error.URLError("unreachable")))
        self.assertEqual(code, 1)
        self.assertIn("unreachable", stderr)

    def test_http_error_reported(self) -> None:
        code, stderr, _ = self._run(fail=("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", stderr)
        self.assertIn("nope", stderr)

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
            Path("patch-coverage.md").write_text(self.REPORT)
            code = post_patch_coverage_main(["7"])
        self.assertEqual(code, 1)
        self.assertIn("not logged in", stderr.getvalue())


class PatchCoverageRealDiffCoverTests(SimpleTestCase):
    """post_patch_coverage on reports from the installed diff-cover, so a
    diff-cover upgrade (e.g. Dependabot's) that changes the Markdown format
    the script expects fails here, rather than only in post-patch-coverage,
    which doesn't run on Dependabot PRs."""

    def _report(self, new_source: str, hits: dict[int, int]) -> str:
        """diff-cover's Markdown report for a branch that changes a.py from
        one line to new_source, with coverage.xml giving each line's hits."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                    cwd=path,
                    check=True,
                    capture_output=True,
                )

            git("init", "-q", "-b", "main")
            (path / "a.py").write_text("a = 1\n")
            git("add", "a.py")
            git("commit", "-qm", "base")
            git("checkout", "-qb", "change")
            (path / "a.py").write_text(new_source)
            git("commit", "-qam", "change")
            lines = "".join(f'<line number="{n}" hits="{h}"/>' for n, h in hits.items())
            (path / "coverage.xml").write_text(
                '<?xml version="1.0" ?><coverage version="7"><sources><source>.</source></sources>'
                '<packages><package name="."><classes><class name="a.py" filename="a.py">'
                f"<lines>{lines}</lines></class></classes></package></packages></coverage>"
            )
            subprocess.run(
                [
                    Path(sys.executable).parent / "diff-cover",
                    "coverage.xml",
                    "--compare-branch=main",
                    "--format",
                    "markdown:report.md",
                ],
                cwd=path,
                check=True,
                capture_output=True,
            )
            return (path / "report.md").read_text()

    def test_missing_lines(self) -> None:
        body = _build_body(self._report("a = 1\nb = 2\nc = 3\nd = 4\n", {1: 1, 2: 0, 3: 1, 4: 0}), "abc", None)
        self.assertIn("| `a.py` | 33.3% | 2, 4 |", body)
        self.assertIn("| 3 lines | 2 lines | 33% |", body)
        self.assertIn("! 2 b = 2", body)

    def test_all_covered(self) -> None:
        body = _build_body(self._report("a = 1\nb = 2\n", {1: 1, 2: 1}), "abc", None)
        self.assertIn("| `a.py` | 100% |  |", body)
        self.assertIn("| 1 line | 0 lines | 100% |", body)

    def test_no_lines(self) -> None:
        body = _build_body(self._report("a = 1\n# comment\n", {1: 1}), "abc", None)
        self.assertTrue(body.endswith("No lines with coverage information in this diff."))
