"""Check that AGENTS.md and README.md stay one sentence per line.

Outside code fences, tables, headings and HTML lines, this flags sentence
boundaries that appear in the middle of one line.
"""

import re
from pathlib import Path

FILES = ("AGENTS.md", "README.md")
ABBREVIATIONS = ("e.g.", "i.e.", "etc.")
BOUNDARY_RE = re.compile(r'[.!?][)"*_]* [A-Z`\[\(\*]')


def _fence_marker(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip()
    if stripped.startswith("```"):
        return ("`", len(stripped) - len(stripped.lstrip("`")))
    if stripped.startswith("~~~"):
        return ("~", len(stripped) - len(stripped.lstrip("~")))
    return None


def _mask_inline_code(line: str) -> str:
    result: list[str] = []
    i = 0
    code_run: int | None = None
    while i < len(line):
        if line[i] != "`":
            result.append(" " if code_run else line[i])
            i += 1
            continue
        run_start = i
        while i < len(line) and line[i] == "`":
            i += 1
        run = i - run_start
        result.extend(" " * run)
        if code_run is None:
            code_run = run
        elif code_run == run:
            code_run = None
    return "".join(result)


def _has_boundary(line: str) -> bool:
    masked = _mask_inline_code(line)
    for match in BOUNDARY_RE.finditer(masked):
        boundary = match.start()
        if line[boundary] == "." and boundary > 0 and line[boundary - 1] == ".":
            continue
        if line[boundary] == "." and re.fullmatch(r"\s*\d+\.", line[: boundary + 1]):
            continue
        if line[boundary] == "." and any(line[: boundary + 1].lower().endswith(abbrev) for abbrev in ABBREVIATIONS):
            continue
        return True
    return False


def _line_ignored(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return True
    if "|" in line:
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
            elif marker[0] == fence[0] and marker[1] >= fence[1]:
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
