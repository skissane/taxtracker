"""Check that AGENTS.md and README.md stay one sentence per line.

Outside code fences, tables, headings and HTML lines, this flags sentence
boundaries that appear in the middle of one line.
"""

import re
from pathlib import Path

FILES = ("AGENTS.md", "README.md")
ABBREVIATIONS = ("e.g.", "i.e.", "etc.")
# An abbreviation as a whole word, at the end of the text before a boundary.
ABBREVIATION_RE = re.compile(r"(?<![\w.])(?:" + "|".join(map(re.escape, ABBREVIATIONS)) + r")$", re.IGNORECASE)
# An ordered-list marker, optionally after bullet or blockquote markers (e.g. "- 1." or "> 1.").
LIST_MARKER_RE = re.compile(r"\s*(?:[-*+>]\s*)*\d+\.")
BOUNDARY_RE = re.compile(r'[.!?][)"*_]*[ \t]+[A-Z`\[\(\*]')
# A code span: a run of backticks, then the shortest text up to a run of the same length; an unmatched run is literal.
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def _fence_marker(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip()
    if stripped.startswith("```"):
        return ("`", len(stripped) - len(stripped.lstrip("`")))
    if stripped.startswith("~~~"):
        return ("~", len(stripped) - len(stripped.lstrip("~")))
    return None


def _mask_inline_code(line: str) -> str:
    # Blank out code spans' contents but keep their backticks, so a sentence starting with code is still seen.
    return INLINE_CODE_RE.sub(lambda m: m[1] + " " * len(m[2]) + m[1], line)


def _has_boundary(line: str) -> bool:
    masked = _mask_inline_code(line)
    for match in BOUNDARY_RE.finditer(masked):
        boundary = match.start()
        if line[boundary] == "." and boundary > 0 and line[boundary - 1] == ".":
            continue
        if line[boundary] == "." and LIST_MARKER_RE.fullmatch(line[: boundary + 1]):
            continue
        if line[boundary] == "." and ABBREVIATION_RE.search(line[: boundary + 1]):
            continue
        return True
    return False


def _line_ignored(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    if stripped.startswith("|"):
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return False


def _problems(text: str) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = _fence_marker(line)
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and marker[1] >= fence[1] and not line.strip().strip(fence[0]):
                fence = None
            continue
        if fence is not None or _line_ignored(line):
            continue
        if _has_boundary(line):
            problems.append((number, line))
    return problems


def main() -> int:
    failures = []
    for path in FILES:
        for number, line in _problems(Path(path).read_text(encoding="utf-8")):
            failures.append(f"{path}:{number}: sentence boundary mid-line: {line}")
    if failures:
        print("One-sentence-per-line check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("AGENTS.md and README.md follow one-sentence-per-line formatting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
