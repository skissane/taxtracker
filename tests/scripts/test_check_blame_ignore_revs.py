import contextlib
import io
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from scripts import check_blame_ignore_revs
from scripts.check_blame_ignore_revs import main as blame_ignore_revs_main


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
            if target == "main":
                target = branch
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

    def test_valid_hashes_pass(self) -> None:
        code, stdout = self._run(["# A merged change", "", "{ancestor}"])
        self.assertEqual(code, 0)
        self.assertIn("real commit hashes that are ancestors of HEAD", stdout)

    def test_invalid_hash_format_fails(self) -> None:
        code, stdout = self._run(["not-a-hash"])
        self.assertEqual(code, 1)
        self.assertIn("not a full 40-character lowercase hex hash", stdout)

    def test_nonexistent_hash_fails(self) -> None:
        code, stdout = self._run(["0000000000000000000000000000000000000000"])
        self.assertEqual(code, 1)
        self.assertIn("doesn't name an existing Git object", stdout)

    def test_non_commit_hash_fails(self) -> None:
        code, stdout = self._run(["{tree}"])
        self.assertEqual(code, 1)
        self.assertIn("is a tree, not a commit", stdout)

    def test_non_ancestor_hash_fails(self) -> None:
        code, stdout = self._run(["{side}"])
        self.assertEqual(code, 1)
        self.assertIn("is not an ancestor of HEAD", stdout)

    def test_target_given(self) -> None:
        # As on a PR: the side branch's own commit is an ancestor of HEAD but not of the base branch.
        code, stdout = self._run(["{side}"], checkout="side")
        self.assertEqual(code, 0)
        code, stdout = self._run(["{side}"], checkout="side", target="main")
        self.assertEqual(code, 1)
        self.assertRegex(stdout, r"is not an ancestor of \S+")
        code, stdout = self._run(["{ancestor}"], checkout="side", target="main")
        self.assertEqual(code, 0)

    def test_ancestor_check_error_fails(self) -> None:
        real_git = check_blame_ignore_revs._git

        def fake_git(*args: str) -> subprocess.CompletedProcess[str]:
            if args[0] == "merge-base":
                return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: bad object\n")
            return real_git(*args)

        with mock.patch.object(check_blame_ignore_revs, "_git", fake_git):
            code, stdout = self._run(["{ancestor}"])
        self.assertEqual(code, 1)
        self.assertIn("couldn't check whether", stdout)
        self.assertIn("(exit 128)", stdout)

    def test_missing_file_fails(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp), contextlib.redirect_stdout(stdout):
            code = blame_ignore_revs_main()
        self.assertEqual(code, 1)
        self.assertIn(".git-blame-ignore-revs is missing", stdout.getvalue())
