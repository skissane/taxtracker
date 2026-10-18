import contextlib
import io
import subprocess
import tempfile
import textwrap
import urllib.error
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from scripts.post_screenshots import MARKER, _request
from scripts.post_screenshots import main as post_screenshots_main
from scripts.requirements_min_versions import main
from scripts.validate_uv_lock_os_arch import (
    PlatformClassification,
    _classify_platform,
    _split_platform,
)
from scripts.validate_uv_lock_os_arch import main as os_arch_main


def _lock(*packages: tuple[str, str]) -> str:
    return "".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"\n\n'
        for name, version in packages
    )


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
        pyproject = (
            '[project]\ndependencies = ["click==8.0", "boto3~=1.0", "ruff", ""]\n'
        )
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
        return self._run_wheels(
            *(f'url = "https://example.com/{f}"' for f in filenames)
        )

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
        self, filenames, *args, comments=(), env=None, fail=None, asset_url=True
    ) -> tuple[int, str, list[tuple]]:
        """Run main() on PR 7 in a temp dir whose screenshots/ holds the given
        files, with the API faked: uploads return a URL based on the file name,
        and the PR has the given comments. fail names a (method, URL prefix) to
        answer with a 403. Return the exit code, stderr and each request as
        (method, url, token, json_body, data)."""
        requests = []

        def fake_request(method, url, token, *, json_body=None, data=None, **kw):
            requests.append((method, url, token, json_body, data))
            if fail and method == fail[0] and url.startswith(fail[1]):
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", {}, io.BytesIO(b"nope")
                )
            if url == "https://api.github.com/user":
                return {"login": "me"}
            if url == "https://api.github.com/repos/o/r":
                return {"id": 42}
            if url.startswith("https://uploads.github.com/"):
                name = url.split("name=")[1].split("&")[0]
                return {"url": f"https://example.com/{name}"} if asset_url else {}
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
                mock.patch("scripts.post_screenshots._request", fake_request),
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
        self.assertEqual(
            post[:2], ("POST", "https://api.github.com/repos/o/r/issues/7/comments")
        )
        body = post[3]["body"]
        self.assertTrue(body.startswith(MARKER + "\n"))
        self.assertIn(
            "Taken for abc1234 by [this workflow run](https://example.com/run).",
            body,
        )
        self.assertLess(
            body.index(
                '<details><summary>index</summary><img src="https://example.com/'
                'index.png" alt="index"></details>'
            ),
            body.index(
                '<details><summary>login</summary><img src="https://example.com/'
                'login.png" alt="login"></details>'
            ),
        )
        self.assertNotIn("notes", body)

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
        comments = [
            {"id": n, "user": {"login": "me"}, "body": "chatter"} for n in range(150)
        ]
        comments[120]["body"] = MARKER + "\nold"
        code, _, requests = self._run(["index.png"], comments=comments)
        self.assertEqual(code, 0)
        self.assertEqual(
            [
                r[1].rsplit("&", 1)[1]
                for r in requests
                if r[0] == "GET" and "/7/" in r[1]
            ],
            ["page=1", "page=2"],
        )
        self.assertEqual(
            self._writes(requests)[-1][:2],
            ("PATCH", "https://api.github.com/repos/o/r/issues/comments/120"),
        )

    def test_failed_upload_posts_nothing(self):
        code, stderr, requests = self._run(
            ["index.png"], fail=("POST", "https://uploads.")
        )
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", stderr)
        self.assertIn("nope", stderr)
        self.assertEqual(self._writes(requests)[1:], [])

    def test_upload_without_url_fails(self):
        code, stderr, requests = self._run(["index.png"], asset_url=False)
        self.assertEqual(code, 1)
        self.assertIn("returned no asset URL", stderr)
        self.assertEqual(self._writes(requests)[1:], [])

    def test_tokens_and_repo_default_to_gh(self):
        def fake_gh(cmd, **kwargs):
            out = "o/r" if cmd[1] == "repo" else "gho_local"
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n")

        with mock.patch("scripts.post_screenshots.subprocess.run", fake_gh):
            code, _, requests = self._run(["index.png"], env={})
        self.assertEqual(code, 0)
        self.assertEqual({r[2] for r in requests}, {"gho_local"})

    def test_gh_failure_reported(self):
        def fake_gh(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="not logged in")

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("scripts.post_screenshots.subprocess.run", fake_gh),
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


class PostScreenshotsRequestTests(SimpleTestCase):
    def _response(self, payload: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def _redirect(self, url, location):
        return urllib.error.HTTPError(
            url, 307, "Redirect", {"Location": location}, None
        )

    def test_sends_json_with_token(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=self._response(b'{"a": 1}')
        ) as urlopen:
            result = _request(
                "POST", "https://api.github.com/x", "tok", json_body={"b": 2}
            )
        self.assertEqual(result, {"a": 1})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"b": 2}')
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")

    def test_empty_response_is_none(self):
        with mock.patch("urllib.request.urlopen", return_value=self._response(b"")):
            self.assertIsNone(_request("DELETE", "https://api.github.com/x", "tok"))

    def test_follows_redirects_dropping_token_off_host(self):
        responses = [
            self._redirect("https://uploads.github.com/a", "/b"),
            self._redirect(
                "https://uploads.github.com/b", "https://elsewhere.example/c"
            ),
            self._response(b'{"url": "u"}'),
        ]
        with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            result = _request(
                "POST", "https://uploads.github.com/a", "tok", data=b"png"
            )
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
            _request("GET", "https://api.github.com/x", "tok")

    def test_other_errors_raised(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/x", 404, "Not Found", {}, None
        )
        with (
            error,
            mock.patch("urllib.request.urlopen", side_effect=error),
            self.assertRaises(urllib.error.HTTPError),
        ):
            _request("GET", "https://api.github.com/x", "tok")
