import contextlib
import io
import tempfile
import textwrap
from pathlib import Path

from django.test import SimpleTestCase

from scripts.check_sentence_per_line import _problems as sentence_per_line_problems
from scripts.check_sentence_per_line import main as sentence_per_line_main


class CheckSentencePerLineTests(SimpleTestCase):
    def test_problem_detection_respects_exceptions(self) -> None:
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

            ~~~
            Tilde fence. Another sentence.
            ~~~
            Prose with `a | b` in code. Another sentence.
            Two spaces.  Another sentence.
            Done. `Code` starts this one.
            A stray ` backtick. Another sentence.
            ```
            ```text
            Still code. Not prose.
            ```
            ``Code with ` inside. Still code`` is one sentence.
            - 1. Bulleted ordered marker is ignored.
            > 1. Quoted ordered marker is ignored.
            A word like fetc. Is not an abbreviation.
            Capitalised E.g. Still an abbreviation.
            """)
        self.assertEqual(
            sentence_per_line_problems(text),
            [
                (2, "This line has two sentences. Another starts here."),
                (17, "Prose with `a | b` in code. Another sentence."),
                (18, "Two spaces.  Another sentence."),
                (19, "Done. `Code` starts this one."),
                (20, "A stray ` backtick. Another sentence."),
                (28, "A word like fetc. Is not an abbreviation."),
            ],
        )

    def _run(self, files: dict[str, str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name, contents in files.items():
                (path / name).write_text(contents, encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.chdir(path), contextlib.redirect_stdout(stdout):
                code = sentence_per_line_main()
        return code, stdout.getvalue()

    def test_main_passes(self) -> None:
        code, stdout = self._run({"AGENTS.md": "One sentence.\n", "README.md": "Another sentence.\n"})
        self.assertEqual(code, 0)
        self.assertIn("one-sentence-per-line formatting", stdout)

    def test_main_reports_failures(self) -> None:
        code, stdout = self._run(
            {
                "AGENTS.md": "One sentence. Another sentence.\n",
                "README.md": "Only one sentence.\n",
            }
        )
        self.assertEqual(code, 1)
        self.assertIn("AGENTS.md:1: sentence boundary mid-line", stdout)
        self.assertNotIn("README.md:1", stdout)
