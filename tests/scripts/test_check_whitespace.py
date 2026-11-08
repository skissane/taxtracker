import contextlib
import io
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scripts.check_whitespace import _problems
from scripts.check_whitespace import main as whitespace_main


class CheckWhitespaceTests(SimpleTestCase):
    def test_problems(self) -> None:
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

    def _run(self, files: dict[str, bytes], untracked: dict[str, bytes]) -> tuple[int, str]:
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

    def test_clean_files_pass(self) -> None:
        code, stdout = self._run(
            {"empty.py": b"", "ok.md": b"# Title\n\ntext\n"},
            {"untracked.txt": b"bad \n\n"},
        )
        self.assertEqual(code, 0)
        self.assertIn("clean whitespace", stdout)

    def test_problems_reported_and_binary_skipped(self) -> None:
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
