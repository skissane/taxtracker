import contextlib
import io
import tempfile
import textwrap
from pathlib import Path

from django.test import SimpleTestCase
from requirements_min_versions import main


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
