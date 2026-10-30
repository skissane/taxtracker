import contextlib
import http.client
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from scripts import check_blame_ignore_revs
from scripts.check_blame_ignore_revs import main as blame_ignore_revs_main
from scripts.check_deploy import check_once
from scripts.check_deploy import main as check_deploy_main
from scripts.check_screenshots_dir import EXPECTED as SCREENSHOTS_GITIGNORE
from scripts.check_screenshots_dir import main as screenshots_dir_main
from scripts.check_sentence_per_line import _problems as sentence_per_line_problems
from scripts.check_sentence_per_line import main as sentence_per_line_main
from scripts.check_whitespace import _problems
from scripts.check_whitespace import main as whitespace_main
from scripts.dependabot_relock import main as relock_main
from scripts.github_comments import request as github_request
from scripts.post_patch_coverage import MARKER as PATCH_COVERAGE_MARKER
from scripts.post_patch_coverage import MAX_BODY_CHARS, _build_body
from scripts.post_patch_coverage import main as post_patch_coverage_main
from scripts.post_screenshots import MARKER
from scripts.post_screenshots import main as post_screenshots_main
from scripts.requirements_min_versions import main
from scripts.validate_uv_lock_os_arch import (
    PlatformClassification,
    _classify_platform,
    _split_platform,
)
from scripts.validate_uv_lock_os_arch import main as os_arch_main
from scripts.wait_for_base_coverage import base_ci_state, codecov_coverage
from scripts.wait_for_base_coverage import main as wait_for_base_coverage_main


def _lock(*packages: tuple[str, str]) -> str:
    return "".join(f'[[package]]\nname = "{name}"\nversion = "{version}"\n\n' for name, version in packages)


class RequirementsMinVersionsTests(SimpleTestCase):
    def _run(self, mode: str, pyproject: str, lock: str) -> tuple[int, str, str, str]:
        """Run main(mode) in a temp dir holding the given pyproject.toml and
        uv.lock; return its exit code, stdout, stderr and the resulting
        pyproject.toml."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
            (path / "uv.lock").write_text(lock, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.chdir(path),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = main(mode)
            result = (path / "pyproject.toml").read_text(encoding="utf-8")
        return code, stdout.getvalue(), stderr.getvalue(), result

    def test_fix_rewrites_minimums(self):
        cases = [
            ("double quotes", '"click>=8.0"', '"click>=8.1.7"'),
            ("single quotes", "'click>=8.0'", "'click>=8.1.7'"),
            ("extras", '"click[extra]>=8.0"', '"click[extra]>=8.1.7"'),
            ("space after >=", '"click>= 8.0"', '"click>= 8.1.7"'),
            ("upper bound", '"click>=8.0,<9"', '"click>=8.1.7,<9"'),
            (
                "marker",
                "\"click>=8.0; python_version >= '3.10'\"",
                "\"click>=8.1.7; python_version >= '3.10'\"",
            ),
            ("name normalisation", '"Click_>=8.0"', '"Click_>=8.1.7"'),
        ]
        lock = _lock(("click", "8.1.7"), ("click-", "8.1.7"))
        for name, before, after in cases:
            with self.subTest(name):
                pyproject = f"[project]\ndependencies = [{before}]\n"
                code, stdout, _, result = self._run("fix", pyproject, lock)
                self.assertEqual(code, 0)
                self.assertEqual(result, f"[project]\ndependencies = [{after}]\n")
                self.assertIn("-> 8.1.7", stdout)

    def test_fix_covers_extras_and_groups(self):
        pyproject = textwrap.dedent("""\
            [project]
            dependencies = ["click>=8.0"]

            [project.optional-dependencies]
            s3 = ["boto3>=1.0"]

            [dependency-groups]
            dev = ["ruff>=0.1", "click>=8.0", {include-group = "lint"}]
            lint = ["ruff>=0.1"]
            """)
        lock = _lock(("click", "8.1.7"), ("boto3", "1.2.3"), ("ruff", "0.5.0"))
        code, _, _, result = self._run("fix", pyproject, lock)
        self.assertEqual(code, 0)
        self.assertEqual(
            result,
            textwrap.dedent("""\
                [project]
                dependencies = ["click>=8.1.7"]

                [project.optional-dependencies]
                s3 = ["boto3>=1.2.3"]

                [dependency-groups]
                dev = ["ruff>=0.5.0", "click>=8.1.7", {include-group = "lint"}]
                lint = ["ruff>=0.5.0"]
                """),
        )

    def test_ignores_dependencies_without_minimum(self):
        pyproject = '[project]\ndependencies = ["click==8.0", "boto3~=1.0", "ruff", ""]\n'
        code, stdout, _, result = self._run("check", pyproject, _lock())
        self.assertEqual(code, 0)
        self.assertIn("match uv.lock", stdout)
        code, stdout, _, result = self._run("fix", pyproject, _lock())
        self.assertEqual(code, 0)
        self.assertIn("No dependency minimum version updates needed", stdout)
        self.assertEqual(result, pyproject)

    def test_check_reports_mismatch_without_writing(self):
        pyproject = '[project]\ndependencies = ["click>=8.1.7", "ruff>=0.1"]\n'
        lock = _lock(("click", "8.1.7"), ("ruff", "0.5.0"))
        code, stdout, _, result = self._run("check", pyproject, lock)
        self.assertEqual(code, 1)
        self.assertIn("ruff>=0.1 (minimum 0.1, locked 0.5.0)", stdout)
        self.assertNotIn("click", stdout)
        self.assertEqual(result, pyproject)

    def test_minimum_compared_as_exact_string(self):
        pyproject = '[project]\ndependencies = ["click>=8.1"]\n'
        code, _, _, _ = self._run("check", pyproject, _lock(("click", "8.1.0")))
        self.assertEqual(code, 1)

    def test_errors(self):
        cases = [
            (
                "missing from lock",
                '"click>=8.0"',
                _lock(),
                "not found in uv.lock",
            ),
            (
                "several locked versions",
                '"click>=8.0"',
                _lock(("click", "8.1.7"), ("click", "7.0")),
                "more than one version of 'click' (7.0, 8.1.7)",
            ),
            (
                # tomllib decodes the escape, so the parsed string isn't in
                # the file's text.
                "string not found in text",
                '"click' + "\\" + 'u003e=8.0"',
                _lock(("click", "8.1.7")),
                "Couldn't find 'click>=8.0'",
            ),
        ]
        for name, dep, lock, message in cases:
            with self.subTest(name):
                pyproject = f"[project]\ndependencies = [{dep}]\n"
                code, _, stderr, result = self._run("fix", pyproject, lock)
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
                self.assertEqual(result, pyproject)

    def test_other_packages_with_several_versions_are_ignored(self):
        pyproject = '[project]\ndependencies = ["click>=8.1.7"]\n'
        # A versionless entry, as uv writes for e.g. a dynamic-version
        # workspace member, is skipped.
        lock = _lock(("click", "8.1.7"), ("six", "1.0"), ("six", "1.16.0"))
        lock += '[[package]]\nname = "lifetracker"\n\n'
        code, _, _, _ = self._run("check", pyproject, lock)
        self.assertEqual(code, 0)


class ValidateUvLockOsArchTests(SimpleTestCase):
    def test_split_platform(self):
        cases = [
            ("any", ("any", None)),
            ("win32", ("win", "x86")),
            ("win_amd64", ("win", "amd64")),
            ("linux_armv7l", ("linux", "armv7l")),
            ("manylinux1_x86_64", ("manylinux", "x86_64")),
            ("manylinux2014_ppc64le", ("manylinux", "ppc64le")),
            ("manylinux_2_17_s390x", ("manylinux", "s390x")),
            ("musllinux_1_2_aarch64", ("musllinux", "aarch64")),
            ("macosx_10_15_x86_64", ("macosx", "x86_64")),
            ("macosx_11_0_arm64", ("macosx", "arm64")),
            ("ios_13_0_arm64_iphoneos", ("ios", "arm64_iphoneos")),
            ("android_21_arm64_v8a", ("android", "arm64_v8a")),
            ("pyemscripten_2026_0_wasm32", ("pyemscripten", "wasm32")),
        ]
        for platform, expected in cases:
            with self.subTest(platform):
                self.assertEqual(_split_platform(platform), expected)

    def test_split_platform_rejects_unrecognised(self):
        with self.assertRaisesRegex(ValueError, "unrecognised"):
            _split_platform("2_17_x86_64")

    def test_classify_platform(self):
        supported = PlatformClassification.SUPPORTED
        tolerated = PlatformClassification.TOLERATED
        unsupported = PlatformClassification.UNSUPPORTED
        cases = [
            (("any", None), supported),
            (("manylinux", "x86_64"), supported),
            (("manylinux", "aarch64"), supported),
            (("musllinux", "x86_64"), supported),
            (("musllinux", "aarch64"), supported),
            (("macosx", "x86_64"), supported),
            (("macosx", "arm64"), supported),
            (("macosx", "universal2"), supported),
            (("linux", "x86_64"), supported),
            (("macosx", "intel"), supported),
            (("macosx", "i386"), supported),
            (("ios", "arm64_iphoneos"), tolerated),
            (("ios", "arm64_iphonesimulator"), tolerated),
            (("ios", "x86_64_iphonesimulator"), tolerated),
            (("pyemscripten", "wasm32"), tolerated),
            (("manylinux", "s390x"), unsupported),
            (("musllinux", "armv7l"), unsupported),
            (("linux", "riscv64"), unsupported),
            (("win", "amd64"), unsupported),
            (("android", "arm64_v8a"), unsupported),
        ]
        for os_arch, expected in cases:
            with self.subTest(os_arch):
                self.assertEqual(_classify_platform(os_arch), expected)

    def _run(self, *filenames: str) -> tuple[int, str]:
        """Run main() in a temp dir whose uv.lock has one package with a wheel
        for each of the given filenames; return its exit code and stdout."""
        return self._run_wheels(*(f'url = "https://example.com/{f}"' for f in filenames))

    def _run_wheels(self, *wheels: str) -> tuple[int, str]:
        """Like _run, but each wheel is given as the body of its TOML inline
        table, e.g. `path = "dist/pkg-1.0-py3-none-any.whl"`."""
        lock = (
            '[[package]]\nname = "pkg"\nversion = "1.0"\nwheels = [\n'
            + "".join(f"  {{ {w} }},\n" for w in wheels)
            + "]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "uv.lock").write_text(lock, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.chdir(path), contextlib.redirect_stdout(stdout):
                code = os_arch_main()
        return code, stdout.getvalue()

    def test_allowed_platforms_pass(self):
        code, stdout = self._run(
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-cp314-cp314-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            "pkg-1.0-cp314-cp314-macosx_11_0_arm64.whl",
        )
        self.assertEqual(code, 0)
        self.assertIn("only has wheels for allowed platforms", stdout)

    def test_disallowed_platforms_fail(self):
        code, stdout = self._run(
            "pkg-1.0-py3-none-any.whl",
            "pkg-1.0-cp314-cp314-win_amd64.whl",
            "pkg-1.0-cp314-cp314-manylinux_2_17_s390x.manylinux2014_s390x.whl",
            "pkg-1.0-cp314-cp314-weird.whl",
        )
        self.assertEqual(code, 1)
        self.assertIn("pkg-1.0-cp314-cp314-win_amd64.whl (win_amd64)", stdout)
        self.assertIn("(manylinux_2_17_s390x)", stdout)
        self.assertIn("(manylinux2014_s390x)", stdout)
        self.assertIn("(weird)", stdout)
        self.assertNotIn("any.whl", stdout)

    def test_local_wheels_checked(self):
        code, stdout = self._run_wheels(
            'path = "dist/pkg-1.0-py3-none-any.whl"',
            'path = "dist/pkg-1.0-cp314-cp314-win_amd64.whl"',
            'filename = "pkg-1.0-cp314-cp314-manylinux_2_17_s390x.whl"',
        )
        self.assertEqual(code, 1)
        self.assertIn("pkg-1.0-cp314-cp314-win_amd64.whl (win_amd64)", stdout)
        self.assertIn("(manylinux_2_17_s390x)", stdout)
        self.assertNotIn("any.whl", stdout)

    def test_wheel_without_location_rejected(self):
        with self.assertRaisesRegex(ValueError, "no url, path or filename"):
            self._run_wheels('hash = "sha256:00"')


class PostScreenshotsTests(SimpleTestCase):
    def _run(
        self,
        filenames,
        *args,
        comments=(),
        env=None,
        fail=None,
        asset_url=True,
        head="abc1234def",
    ) -> tuple[int, str, list[tuple]]:
        """Run main() on PR 7 in a temp dir whose screenshots/ holds the given
        files, with the API faked: uploads return a URL based on the file name,
        and the PR has the given head commit and comments. fail names a (method,
        URL prefix) to answer with a 403. Return the exit code, stderr and each
        request as (method, url, token, json_body, data)."""
        requests = []

        def fake_request(method, url, token, *, json_body=None, data=None, **kw):
            requests.append((method, url, token, json_body, data))
            if fail and method == fail[0] and url.startswith(fail[1]):
                if len(fail) > 2:
                    raise fail[2]
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"nope"))
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

    def _writes(self, requests):
        return [r for r in requests if r[0] in ("POST", "PATCH", "DELETE")]

    def test_uploads_with_pat_and_posts_with_github_token(self):
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

    def test_checks_head_commit(self):
        code, _, requests = self._run(["index.png"], "--commit", "abc1234def")
        self.assertEqual(code, 0)
        self.assertIn(
            ("GET", "https://api.github.com/repos/o/r/pulls/7"),
            [r[:2] for r in requests],
        )
        self.assertEqual(self._writes(requests)[-1][0], "POST")

    def test_stale_commit_posts_nothing(self):
        # An older commit's run finishing after the newer commit's has posted.
        comments = [{"id": 1, "user": {"login": "me"}, "body": MARKER + "\nnew"}]
        code, _, requests = self._run(["index.png"], "--commit", "0ld0ld0", comments=comments)
        self.assertEqual(code, 0)
        # Not even the uploads.
        self.assertEqual(self._writes(requests), [])

    def test_edits_own_previous_comment_in_place(self):
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

    def test_paginates_comments(self):
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

    def test_failed_upload_posts_nothing(self):
        code, stderr, requests = self._run(["index.png"], fail=("POST", "https://uploads."))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", stderr)
        self.assertIn("nope", stderr)
        self.assertEqual(self._writes(requests)[1:], [])

    def test_network_error_reported(self):
        code, stderr, _ = self._run(
            ["index.png"],
            fail=("POST", "https://uploads.", urllib.error.URLError("unreachable")),
        )
        self.assertEqual(code, 1)
        self.assertIn("unreachable", stderr)

    def test_upload_without_url_fails(self):
        code, stderr, requests = self._run(["index.png"], asset_url=False)
        self.assertEqual(code, 1)
        self.assertIn("returned no asset URL", stderr)
        self.assertEqual(self._writes(requests)[1:], [])

    def test_tokens_and_repo_default_to_gh(self):
        def fake_gh(cmd, **kwargs):
            out = "o/r" if cmd[1] == "repo" else "gho_local"
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n")

        with mock.patch("scripts.github_comments.subprocess.run", fake_gh):
            code, _, requests = self._run(["index.png"], env={})
        self.assertEqual(code, 0)
        self.assertEqual({r[2] for r in requests}, {"gho_local"})

    def test_gh_failure_reported(self):
        def fake_gh(cmd, **kwargs):
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

    def test_no_screenshots(self):
        code, stderr, requests = self._run(["notes.txt"])
        self.assertEqual(code, 1)
        self.assertIn("no screenshots", stderr)
        self.assertEqual(requests, [])

    def test_unsafe_names_rejected(self):
        for name in ["--help.png", "a b.png", "x](y).png", "Index.png"]:
            with self.subTest(name):
                # Not index.png, which is the same file as Index.png on
                # case-insensitive filesystems (macOS's default).
                code, stderr, requests = self._run(["login.png", name])
                self.assertEqual(code, 1)
                self.assertIn("doesn't match", stderr)
                self.assertEqual(requests, [])


class PostPatchCoverageTests(SimpleTestCase):
    HEAD = (
        "# Diff Coverage\n## Diff: origin/master...HEAD\n\n- a&#46;py (50.0%): Missing lines 2\n\n"
        "## Summary\n\n- **Total**: 2 lines\n- **Missing**: 1 line\n- **Coverage**: 50%\n"
    )
    REPORT = HEAD + "\n\n\n## a&#46;py\n\nLines 1-2\n\n```python\n  1 a = 1\n! 2 b = 2\n```\n\n\n---\n"

    def _run(self, *args, report=REPORT, comments=(), fail=None, head="abc1234def") -> tuple[int, str, list[tuple]]:
        """Run main() on PR 7 in a temp dir holding patch-coverage.md (unless
        report is None), with the API faked as in PostScreenshotsTests. Return
        the exit code, stderr and each request as (method, url, token, json_body)."""
        requests = []

        def fake_request(method, url, token, *, json_body=None, **kw):
            requests.append((method, url, token, json_body))
            if fail and method == fail[0] and url.startswith(fail[1]):
                if len(fail) > 2:
                    raise fail[2]
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"nope"))
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

    def _writes(self, requests):
        return [r for r in requests if r[0] in ("POST", "PATCH", "DELETE")]

    def test_posts_report(self):
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

    def test_no_lines(self):
        report = "# Diff Coverage\n## Diff: origin/master...HEAD\n\nNo lines with coverage information in this diff.\n"
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertEqual(
            self._writes(requests)[0][3]["body"],
            f"{PATCH_COVERAGE_MARKER}\n## Diff Coverage\n\n### Diff: origin/master...HEAD\n\n"
            "No lines with coverage information in this diff.",
        )

    def test_unexpected_report_fails(self):
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
            self.HEAD.replace("- **Coverage**: 50%\n", ""): "line 10: expected '- **Coverage**: ...', found the end",
        }
        for report, error in cases.items():
            with self.subTest(report):
                code, stderr, requests = self._run(report=report)
                self.assertEqual(code, 1)
                self.assertIn(f"diff-cover report {error}", stderr)
                self.assertEqual(requests, [])

    def test_stale_commit_posts_nothing(self):
        code, _, requests = self._run("--commit", "0ld0ld0")
        self.assertEqual(code, 0)
        self.assertEqual(self._writes(requests), [])

    def test_edits_previous_comment(self):
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

    def test_lists_become_tables(self):
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

    def test_long_report_truncated(self):
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

    def test_missing_report(self):
        code, stderr, requests = self._run(report=None)
        self.assertEqual(code, 1)
        self.assertIn("patch-coverage.md", stderr)
        self.assertEqual(requests, [])

    def test_network_error_reported(self):
        code, stderr, _ = self._run(fail=("GET", "https://api.github.com/user", urllib.error.URLError("unreachable")))
        self.assertEqual(code, 1)
        self.assertIn("unreachable", stderr)

    def test_http_error_reported(self):
        code, stderr, _ = self._run(fail=("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", stderr)
        self.assertIn("nope", stderr)

    def test_gh_failure_reported(self):
        def fake_gh(cmd, **kwargs):
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

            def git(*args):
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

    def test_missing_lines(self):
        body = _build_body(self._report("a = 1\nb = 2\nc = 3\nd = 4\n", {1: 1, 2: 0, 3: 1, 4: 0}), "abc", None)
        self.assertIn("| `a.py` | 33.3% | 2, 4 |", body)
        self.assertIn("| 3 lines | 2 lines | 33% |", body)
        self.assertIn("! 2 b = 2", body)

    def test_all_covered(self):
        body = _build_body(self._report("a = 1\nb = 2\n", {1: 1, 2: 1}), "abc", None)
        self.assertIn("| `a.py` | 100% |  |", body)
        self.assertIn("| 1 line | 0 lines | 100% |", body)

    def test_no_lines(self):
        body = _build_body(self._report("a = 1\n# comment\n", {1: 1}), "abc", None)
        self.assertTrue(body.endswith("No lines with coverage information in this diff."))


class GitHubCommentsRequestTests(SimpleTestCase):
    def _response(self, payload: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def _redirect(self, url, location):
        return urllib.error.HTTPError(url, 307, "Redirect", {"Location": location}, None)

    def test_sends_json_with_token(self):
        with mock.patch("urllib.request.urlopen", return_value=self._response(b'{"a": 1}')) as urlopen:
            result = github_request("POST", "https://api.github.com/x", "tok", json_body={"b": 2})
        self.assertEqual(result, {"a": 1})
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 60})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"b": 2}')
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")

    def test_empty_response_is_none(self):
        with mock.patch("urllib.request.urlopen", return_value=self._response(b"")):
            self.assertIsNone(github_request("DELETE", "https://api.github.com/x", "tok"))

    def test_follows_redirects_dropping_token_off_host(self):
        responses = [
            self._redirect("https://uploads.github.com/a", "/b"),
            self._redirect("https://uploads.github.com/b", "https://elsewhere.example/c"),
            self._response(b'{"url": "u"}'),
        ]
        with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
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
        self.assertEqual([r.data for r in sent], [b"png"] * 3)
        self.assertEqual(
            [r.get_header("Authorization") for r in sent],
            ["Bearer tok", "Bearer tok", None],
        )

    def test_too_many_redirects(self):
        redirect = self._redirect("https://api.github.com/x", "/x")
        with (
            mock.patch("urllib.request.urlopen", side_effect=[redirect] * 5),
            self.assertRaisesRegex(RuntimeError, "too many redirects"),
        ):
            github_request("GET", "https://api.github.com/x", "tok")

    def test_other_errors_raised(self):
        error = urllib.error.HTTPError("https://api.github.com/x", 404, "Not Found", {}, None)
        with (
            error,
            mock.patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(urllib.error.HTTPError),
        ):
            github_request("GET", "https://api.github.com/x", "tok")


class CheckWhitespaceTests(SimpleTestCase):
    def test_problems(self):
        cases = [
            (b"", []),
            (b"a\nb\n", []),
            (b"a\n\nb\n", []),
            (b"\n", [(None, "has only newlines; an empty file should have none")]),
            (b"\n\n", [(None, "has only newlines; an empty file should have none")]),
            (b"a", [(None, "no newline at end of file")]),
            (b"a\n\n", [(None, "more than one newline at end of file")]),
            (b"\na\n", [(1, "starts with a blank line")]),
            (
                b"a \nb\nc\t\nd\r\n",
                [
                    (1, "trailing whitespace"),
                    (3, "trailing whitespace"),
                    (4, "trailing whitespace"),
                ],
            ),
            (
                b"\na \n\n",
                [
                    (1, "starts with a blank line"),
                    (2, "trailing whitespace"),
                    (None, "more than one newline at end of file"),
                ],
            ),
        ]
        for data, expected in cases:
            with self.subTest(data):
                self.assertEqual(_problems(data), expected)

    def _run(self, files: dict[str, bytes], untracked: dict[str, bytes]):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name, data in (files | untracked).items():
                (path / name).write_bytes(data)
            subprocess.run(["git", "init", "-q"], cwd=path, check=True)
            subprocess.run(["git", "add", *files], cwd=path, check=True)
            stdout = io.StringIO()
            with contextlib.chdir(path), contextlib.redirect_stdout(stdout):
                code = whitespace_main()
        return code, stdout.getvalue()

    def test_clean_files_pass(self):
        code, stdout = self._run(
            {"empty.py": b"", "ok.md": b"# Title\n\ntext\n"},
            {"untracked.txt": b"bad \n\n"},
        )
        self.assertEqual(code, 0)
        self.assertIn("clean whitespace", stdout)

    def test_problems_reported_and_binary_skipped(self):
        code, stdout = self._run(
            {
                "logo.svg": b"<svg/>",
                "notes.md": b"ok\nbad \n",
                "image.png": b"\x89PNG\x00\n\n",
            },
            {},
        )
        self.assertEqual(code, 1)
        self.assertIn("- logo.svg: no newline at end of file", stdout)
        self.assertIn("- notes.md:2: trailing whitespace", stdout)
        self.assertNotIn("image.png", stdout)


class CheckSentencePerLineTests(SimpleTestCase):
    def test_problem_detection_respects_exceptions(self):
        text = textwrap.dedent("""\
            This is one sentence.
            This line has two sentences. Another starts here.
            This line mentions e.g. A thing and is still one sentence.
            1. Ordered list marker is ignored.
            # Heading. Another heading sentence.
            | Column. One | Column Two. |
            <p>HTML. Another sentence.</p>
            Ellipsis ... (still one sentence).
            `Code. Another sentence.`
            ```python
            print("Sentence. Another")
            ```
            """)
        self.assertEqual(sentence_per_line_problems(text), [(2, "This line has two sentences. Another starts here.")])

    def _run(self, files: dict[str, str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name, contents in files.items():
                (path / name).write_text(contents, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.chdir(path), contextlib.redirect_stdout(stdout):
                code = sentence_per_line_main()
        return code, stdout.getvalue()

    def test_main_passes(self):
        code, stdout = self._run({"AGENTS.md": "One sentence.\n", "README.md": "Another sentence.\n"})
        self.assertEqual(code, 0)
        self.assertIn("one-sentence-per-line formatting", stdout)

    def test_main_reports_failures(self):
        code, stdout = self._run(
            {
                "AGENTS.md": "One sentence. Another sentence.\n",
                "README.md": "Only one sentence.\n",
            }
        )
        self.assertEqual(code, 1)
        self.assertIn("AGENTS.md:1: sentence boundary mid-line", stdout)
        self.assertNotIn("README.md:1", stdout)


class CheckScreenshotsDirTests(SimpleTestCase):
    def _run(self, setup):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "screenshots").mkdir()
            gitignore = path / "screenshots" / ".gitignore"
            gitignore.write_bytes(SCREENSHOTS_GITIGNORE)
            (path / "screenshots" / "index-light.png").write_bytes(b"\x89PNG")
            subprocess.run(["git", "init", "-q"], cwd=path, check=True)
            subprocess.run(["git", "add", "-A"], cwd=path, check=True)
            with contextlib.chdir(path):
                setup(path)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = screenshots_dir_main()
        return code, stdout.getvalue()

    def _git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    def test_only_gitignore_passes(self):
        code, stdout = self._run(lambda path: None)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)

    def test_force_added_file_fails(self):
        code, stdout = self._run(lambda path: self._git("add", "-f", "screenshots/index-light.png"))
        self.assertEqual(code, 1)
        self.assertIn(
            "- screenshots/index-light.png is tracked; only screenshots/.gitignore should be",
            stdout,
        )

    def test_deleted_gitignore_fails(self):
        def setup(path):
            (path / "screenshots" / ".gitignore").unlink()
            self._git("add", "-A")

        code, stdout = self._run(setup)
        self.assertEqual(code, 1)
        self.assertIn("- screenshots/index-light.png is tracked", stdout)
        self.assertIn("- screenshots/.gitignore isn't tracked", stdout)
        self.assertIn("- screenshots/.gitignore is missing from the working tree", stdout)

    def test_unstaged_deletion_fails(self):
        code, stdout = self._run(lambda path: (path / "screenshots" / ".gitignore").unlink())
        self.assertEqual(code, 1)
        self.assertNotIn("isn't tracked", stdout)
        self.assertIn("- screenshots/.gitignore is missing from the working tree", stdout)

    def test_modified_gitignore_fails(self):
        def setup(path):
            (path / "screenshots" / ".gitignore").write_text("*.png\n")

        code, stdout = self._run(setup)
        self.assertEqual(code, 1)
        self.assertIn("unexpected contents in the working tree", stdout)
        self.assertNotIn("staged", stdout)

        def setup_staged(path):
            setup(path)
            self._git("add", "screenshots/.gitignore")

        code, stdout = self._run(setup_staged)
        self.assertEqual(code, 1)
        self.assertIn("- screenshots/.gitignore has unexpected staged contents", stdout)


class CheckBlameIgnoreRevsTests(SimpleTestCase):
    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def _commit(self, cwd: Path, filename: str, contents: str, message: str) -> str:
        (cwd / filename).write_text(contents, encoding="utf-8")
        self._git(cwd, "add", filename)
        self._git(cwd, "commit", "-qm", message)
        return self._git(cwd, "rev-parse", "HEAD")

    def _run(self, lines: list[str], *, checkout: str | None = None, target: str | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self._git(path, "init", "-q")
            self._git(path, "config", "user.name", "Test User")
            self._git(path, "config", "user.email", "test@example.invalid")
            branch = self._git(path, "branch", "--show-current")
            ancestor = self._commit(path, "tracked.txt", "base\n", "base")
            self._git(path, "checkout", "-qb", "side-branch")
            side = self._commit(path, "tracked.txt", "side\n", "side")
            self._git(path, "checkout", "-q", branch)
            self._commit(path, "tracked.txt", "head\n", "head")
            if checkout == "side":
                self._git(path, "checkout", "-q", "side-branch")
            target = {"main": branch}.get(target, target)
            (path / ".git-blame-ignore-revs").write_text(
                "\n".join(
                    line.format(
                        ancestor=ancestor,
                        side=side,
                        tree=self._git(path, "rev-parse", f"{ancestor}^{{tree}}"),
                    )
                    for line in lines
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.chdir(path), contextlib.redirect_stdout(stdout):
                code = blame_ignore_revs_main() if target is None else blame_ignore_revs_main(target)
        return code, stdout.getvalue()

    def test_valid_hashes_pass(self):
        code, stdout = self._run(["# A merged change", "", "{ancestor}"])
        self.assertEqual(code, 0)
        self.assertIn("real commit hashes that are ancestors of HEAD", stdout)

    def test_invalid_hash_format_fails(self):
        code, stdout = self._run(["not-a-hash"])
        self.assertEqual(code, 1)
        self.assertIn("not a full 40-character lowercase hex hash", stdout)

    def test_nonexistent_hash_fails(self):
        code, stdout = self._run(["0000000000000000000000000000000000000000"])
        self.assertEqual(code, 1)
        self.assertIn("doesn't name an existing Git object", stdout)

    def test_non_commit_hash_fails(self):
        code, stdout = self._run(["{tree}"])
        self.assertEqual(code, 1)
        self.assertIn("is a tree, not a commit", stdout)

    def test_non_ancestor_hash_fails(self):
        code, stdout = self._run(["{side}"])
        self.assertEqual(code, 1)
        self.assertIn("is not an ancestor of HEAD", stdout)

    def test_target_given(self):
        # As on a PR: the side branch's own commit is an ancestor of HEAD but not of the base branch.
        code, stdout = self._run(["{side}"], checkout="side")
        self.assertEqual(code, 0)
        code, stdout = self._run(["{side}"], checkout="side", target="main")
        self.assertEqual(code, 1)
        self.assertRegex(stdout, r"is not an ancestor of \S+")
        code, stdout = self._run(["{ancestor}"], checkout="side", target="main")
        self.assertEqual(code, 0)

    def test_ancestor_check_error_fails(self):
        real_git = check_blame_ignore_revs._git

        def fake_git(*args):
            if args[0] == "merge-base":
                return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: bad object\n")
            return real_git(*args)

        with mock.patch.object(check_blame_ignore_revs, "_git", fake_git):
            code, stdout = self._run(["{ancestor}"])
        self.assertEqual(code, 1)
        self.assertIn("couldn't check whether", stdout)
        self.assertIn("(exit 128)", stdout)

    def test_missing_file_fails(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp), contextlib.redirect_stdout(stdout):
            code = blame_ignore_revs_main()
        self.assertEqual(code, 1)
        self.assertIn(".git-blame-ignore-revs is missing", stdout.getvalue())


class DependabotRelockTests(SimpleTestCase):
    BASE = _lock(("certifi", "1.0"), ("click", "8.0"))

    def _run(self, branch_lock: str, relocked: str | None) -> tuple[int | None, str, list[list[str]], str]:
        """Run main() in a temp dir whose uv.lock is branch_lock, with
        `git show` of the merge base giving BASE and `uv lock` writing relocked
        (or failing, if None); return the exit code (None if `uv lock`'s
        failure was raised), stderr, the `uv lock` command line and the
        resulting uv.lock."""
        uv_calls = []

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "merge-base"]:
                self.assertEqual(args, ["git", "merge-base", "HEAD", "origin/master"])
                return subprocess.CompletedProcess(args, 0, stdout="abc123\n")
            if args[0] == "git":
                self.assertEqual(args, ["git", "show", "abc123:uv.lock"])
                return subprocess.CompletedProcess(args, 0, stdout=self.BASE)
            uv_calls.append(args)
            if relocked is None:
                raise subprocess.CalledProcessError(1, args)
            self.assertEqual(Path("uv.lock").read_text(encoding="utf-8"), self.BASE)
            Path("uv.lock").write_text(relocked, encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "uv.lock").write_text(branch_lock, encoding="utf-8")
            stderr = io.StringIO()
            with (
                contextlib.chdir(path),
                contextlib.redirect_stderr(stderr),
                mock.patch("scripts.dependabot_relock.subprocess.run", fake_run),
            ):
                try:
                    code = relock_main("origin/master")
                except subprocess.CalledProcessError:
                    code = None
            result = (path / "uv.lock").read_text(encoding="utf-8")
        return code, stderr.getvalue(), uv_calls, result

    def test_relocks_changed_packages_from_base(self):
        # A virtual project has no version, and is never upgraded.
        virtual = '[[package]]\nname = "virtual"\nsource = { virtual = "." }\n\n'
        branch = "revision = 5\n" + _lock(("certifi", "2.0"), ("click", "8.0")) + virtual
        relocked = _lock(("certifi", "2.0"), ("click", "8.0"))
        code, _, uv_calls, result = self._run(branch, relocked)
        self.assertEqual(code, 0)
        self.assertEqual(uv_calls, [["uv", "lock", "--upgrade-package", "certifi==2.0"]])
        self.assertEqual(result, relocked)

    def test_added_and_multi_version_packages(self):
        branch = _lock(
            ("certifi", "1.0"),
            ("click", "8.0"),
            ("new", "1"),
            ("six", "1"),
            ("six", "2"),
        )
        code, _, uv_calls, _ = self._run(branch, branch)
        self.assertEqual(code, 0)
        self.assertEqual(
            uv_calls,
            [
                [
                    "uv",
                    "lock",
                    "--upgrade-package",
                    "new==1",
                    "--upgrade-package",
                    "six",
                ]
            ],
        )

    def test_no_version_changes_still_relocks(self):
        code, _, uv_calls, _ = self._run(self.BASE, self.BASE)
        self.assertEqual(code, 0)
        self.assertEqual(uv_calls, [["uv", "lock"]])

    def test_mismatch_restores_branch_lock(self):
        branch = _lock(("certifi", "2.0"), ("click", "8.0"))
        relocked = _lock(("certifi", "2.0"), ("click", "8.1"))
        code, stderr, _, result = self._run(branch, relocked)
        self.assertEqual(code, 1)
        self.assertIn("click: Dependabot locked ('8.0',), re-locking gave ('8.1',)", stderr)
        self.assertEqual(result, branch)

    def test_uv_lock_failure_restores_branch_lock(self):
        branch = _lock(("certifi", "2.0"), ("click", "8.0"))
        code, _, _, result = self._run(branch, None)
        self.assertIsNone(code)
        self.assertEqual(result, branch)


class CheckDeployTests(SimpleTestCase):
    URL = "https://app.example/healthz/"
    COMMIT = "abc123"

    def _response(self, status: int, payload: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        response.__enter__.return_value.read.return_value = payload
        return response

    def _http_error(self, status: int, payload: bytes):
        return urllib.error.HTTPError(self.URL, status, "Error", {}, io.BytesIO(payload))

    def _check(self, result):
        kwargs = {"side_effect" if isinstance(result, Exception) else "return_value": result}
        with mock.patch("urllib.request.urlopen", **kwargs) as urlopen:
            problem = check_once(self.URL, self.COMMIT, 7)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 7})
        return problem

    def test_healthy(self):
        body = b'{"status": "ok", "commit": "abc123"}'
        self.assertIsNone(self._check(self._response(200, body)))

    def test_other_commit(self):
        body = b'{"status": "ok", "commit": "old"}'
        self.assertEqual(
            self._check(self._response(200, body)),
            "running commit 'old', not 'abc123'",
        )

    def test_unhealthy(self):
        body = b'{"status": "error", "problem": "unapplied migrations"}'
        self.assertEqual(
            self._check(self._http_error(503, body)),
            "HTTP 503, status 'error': unapplied migrations",
        )

    def test_not_json(self):
        self.assertEqual(self._check(self._http_error(502, b"<html>")), "HTTP 502, not JSON")
        self.assertEqual(self._check(self._response(200, b"[]")), "HTTP 200, unexpected JSON")

    def test_request_failed(self):
        self.assertEqual(
            self._check(urllib.error.URLError("timed out")),
            "request failed: timed out",
        )
        self.assertEqual(self._check(TimeoutError("slow")), "request failed: slow")
        self.assertEqual(
            self._check(http.client.IncompleteRead(b"")),
            "request failed: IncompleteRead(0 bytes read)",
        )

    def _main(self, problems, *args):
        now = [0.0]
        sleeps = []

        def sleep(seconds):
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

    def test_retries_until_healthy(self):
        code, sleeps, out, _ = self._main(["request failed: x", None])
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [5])
        self.assertEqual(
            out,
            "Attempt 1: request failed: x\nAttempt 2: healthy, running abc123\n",
        )
        self.assertNotIn(self.URL, out)

    def test_gives_up_after_timeout(self):
        code, sleeps, out, err = self._main(["down"] * 10, "--timeout", "10", "--interval", "4")
        self.assertEqual(code, 1)
        self.assertEqual(sleeps, [4, 4])
        self.assertEqual(out.count("down"), 3)
        self.assertIn("not healthy after 10 seconds", err)

    def test_request_timeout_capped_by_deadline(self):
        now = [0.0]
        timeouts = []

        def check(url, commit, timeout):
            timeouts.append(timeout)
            now[0] += 8
            return "down"

        def sleep(seconds):
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


class WaitForBaseCoverageTests(SimpleTestCase):
    REPO = "owner/repo"
    SHA = "abc123"

    def _response(self, status: int, payload: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        response.__enter__.return_value.read.return_value = payload
        return response

    def _http_error(self, status: int, payload: bytes):
        return urllib.error.HTTPError("https://example/", status, "Error", {}, io.BytesIO(payload))

    def _call(self, func, result, *args):
        kwargs = {"side_effect" if isinstance(result, Exception) else "return_value": result}
        with mock.patch("urllib.request.urlopen", **kwargs) as urlopen:
            outcome = func(self.REPO, self.SHA, *args, 7)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 7})
        return outcome, urlopen.call_args.args[0]

    def test_codecov_coverage(self):
        outcome, request = self._call(codecov_coverage, self._response(200, b'{"totals": {"coverage": 98.5}}'))
        self.assertEqual(outcome, (True, "coverage 98.5%"))
        self.assertEqual(request.full_url, "https://api.codecov.io/api/v2/github/owner/repos/repo/totals/?sha=abc123")

    def test_codecov_no_coverage(self):
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

    def _check_runs(self, *runs, token=None):
        body = json.dumps({"total_count": len(runs), "check_runs": list(runs)}).encode()
        with mock.patch("urllib.request.urlopen", return_value=self._response(200, body)) as urlopen:
            outcome = base_ci_state(self.REPO, self.SHA, "test", token, 7)
        return outcome, urlopen.call_args.args[0]

    def test_base_ci_running(self):
        outcome, request = self._check_runs({"id": 1, "status": "in_progress", "conclusion": None})
        self.assertEqual(outcome, ("running", "test in_progress"))
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/owner/repo/commits/abc123/check-runs?check_name=test&filter=latest",
        )
        self.assertFalse(request.has_header("Authorization"))

    def test_base_ci_finished(self):
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

    def test_base_ci_errors(self):
        self.assertEqual(
            self._call(base_ci_state, self._http_error(403, b'{"message": "no"}'), "test", None)[0],
            ("running", "HTTP 403"),
        )
        self.assertEqual(
            self._call(base_ci_state, http.client.IncompleteRead(b""), "test", None)[0],
            ("running", "request failed: IncompleteRead(0 bytes read)"),
        )

    def _main(self, coverage, ci, *args):
        now = [0.0]
        sleeps = []

        def sleep(seconds):
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

    def test_base_already_has_coverage(self):
        code, sleeps, out, ci = self._main([(True, "coverage 98%")], [])
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(out, "Attempt 1: Codecov has the base report for abc123 (coverage 98%)\n")
        ci.assert_not_called()

    def test_waits_for_base_ci(self):
        code, sleeps, out, ci = self._main(
            [(False, "no report yet"), (False, "no report yet"), (True, "coverage 98%")],
            [("running", "test in_progress"), ("succeeded", "test success")],
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("Attempt 1: Codecov: no report yet; base CI: test in_progress\n", out)
        self.assertNotIn("::warning::", out)
        self.assertEqual(ci.call_args.args, (self.REPO, self.SHA, "test", "t0k", 30))

    def test_gives_up_after_grace(self):
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

    def test_gives_up_at_once_if_base_ci_failed(self):
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

    def test_grace_restarts_if_base_ci_reruns(self):
        code, sleeps, _, _ = self._main(
            [(False, "no report yet")] * 10,
            [("none", "no test check run"), ("running", "test queued"), ("succeeded", "test success")] * 2,
            "--grace",
            "10",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10, 10])

    def test_gives_up_after_timeout(self):
        code, sleeps, out, _ = self._main(
            [(False, "no report yet")] * 10,
            [("running", "test in_progress")] * 10,
            "--timeout",
            "25",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("::warning::Codecov still has no coverage for base commit abc123 after 25 seconds", out)

    def test_request_timeout_capped_by_deadline(self):
        now = [0.0]
        timeouts = []

        def request(*args):
            timeouts.append(args[-1])
            now[0] += 4
            return False, "slow"

        def sleep(seconds):
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
