import contextlib
import io
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from django.test import SimpleTestCase

from scripts.check_screenshots_dir import EXPECTED as SCREENSHOTS_GITIGNORE
from scripts.check_screenshots_dir import main as screenshots_dir_main


class CheckScreenshotsDirTests(SimpleTestCase):
    def _run(self, setup: Callable[[Path], None]) -> tuple[int, str]:
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

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], check=True, capture_output=True)

    def test_only_gitignore_passes(self) -> None:
        code, stdout = self._run(lambda path: None)
        self.assertEqual(code, 0)
        self.assertIn("unchanged", stdout)

    def test_force_added_file_fails(self) -> None:
        code, stdout = self._run(lambda path: self._git("add", "-f", "screenshots/index-light.png"))
        self.assertEqual(code, 1)
        self.assertIn(
            "- screenshots/index-light.png is tracked; only screenshots/.gitignore should be",
            stdout,
        )

    def test_deleted_gitignore_fails(self) -> None:
        def setup(path: Path) -> None:
            (path / "screenshots" / ".gitignore").unlink()
            self._git("add", "-A")

        code, stdout = self._run(setup)
        self.assertEqual(code, 1)
        self.assertIn("- screenshots/index-light.png is tracked", stdout)
        self.assertIn("- screenshots/.gitignore isn't tracked", stdout)
        self.assertIn("- screenshots/.gitignore is missing from the working tree", stdout)

    def test_unstaged_deletion_fails(self) -> None:
        code, stdout = self._run(lambda path: (path / "screenshots" / ".gitignore").unlink())
        self.assertEqual(code, 1)
        self.assertNotIn("isn't tracked", stdout)
        self.assertIn("- screenshots/.gitignore is missing from the working tree", stdout)

    def test_modified_gitignore_fails(self) -> None:
        def setup(path: Path) -> None:
            (path / "screenshots" / ".gitignore").write_text("*.png\n")

        code, stdout = self._run(setup)
        self.assertEqual(code, 1)
        self.assertIn("unexpected contents in the working tree", stdout)
        self.assertNotIn("staged", stdout)

        def setup_staged(path: Path) -> None:
            setup(path)
            self._git("add", "screenshots/.gitignore")

        code, stdout = self._run(setup_staged)
        self.assertEqual(code, 1)
        self.assertIn("- screenshots/.gitignore has unexpected staged contents", stdout)
