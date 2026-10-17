import contextlib
import io
import tempfile
import textwrap
from pathlib import Path

from django.test import SimpleTestCase

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
