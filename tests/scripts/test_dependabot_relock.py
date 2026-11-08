import contextlib
import io
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from scripts.dependabot_relock import main as relock_main
from tests.scripts.test_requirements_min_versions import _lock


class DependabotRelockTests(SimpleTestCase):
    BASE = _lock(("certifi", "1.0"), ("click", "8.0"))

    def _run(self, branch_lock: str, relocked: str | None) -> tuple[int | None, str, list[list[str]], str]:
        """Run main() in a temp dir whose uv.lock is branch_lock, with
        `git show` of the merge base giving BASE and `uv lock` writing relocked
        (or failing, if None); return the exit code (None if `uv lock`'s
        failure was raised), stderr, the `uv lock` command line and the
        resulting uv.lock."""
        uv_calls = []

        def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
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

    def test_relocks_changed_packages_from_base(self) -> None:
        # A virtual project has no version, and is never upgraded.
        virtual = '[[package]]\nname = "virtual"\nsource = { virtual = "." }\n\n'
        branch = "revision = 5\n" + _lock(("certifi", "2.0"), ("click", "8.0")) + virtual
        relocked = _lock(("certifi", "2.0"), ("click", "8.0"))
        code, _, uv_calls, result = self._run(branch, relocked)
        self.assertEqual(code, 0)
        self.assertEqual(uv_calls, [["uv", "lock", "--upgrade-package", "certifi==2.0"]])
        self.assertEqual(result, relocked)

    def test_added_and_multi_version_packages(self) -> None:
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

    def test_no_version_changes_still_relocks(self) -> None:
        code, _, uv_calls, _ = self._run(self.BASE, self.BASE)
        self.assertEqual(code, 0)
        self.assertEqual(uv_calls, [["uv", "lock"]])

    def test_mismatch_restores_branch_lock(self) -> None:
        branch = _lock(("certifi", "2.0"), ("click", "8.0"))
        relocked = _lock(("certifi", "2.0"), ("click", "8.1"))
        code, stderr, _, result = self._run(branch, relocked)
        self.assertEqual(code, 1)
        self.assertIn("click: Dependabot locked ('8.0',), re-locking gave ('8.1',)", stderr)
        self.assertEqual(result, branch)

    def test_uv_lock_failure_restores_branch_lock(self) -> None:
        branch = _lock(("certifi", "2.0"), ("click", "8.0"))
        code, _, _, result = self._run(branch, None)
        self.assertIsNone(code)
        self.assertEqual(result, branch)
