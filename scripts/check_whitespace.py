import subprocess
from pathlib import Path


def _text_files() -> list[str]:
    # `--eol` prefixes each path with Git's own view of its contents, e.g.
    # `i/lf    w/lf    attr/                 \tpath`: `w/-text` means Git thinks
    # the working tree copy is binary, and a blank `w/` means there's no
    # regular file to look at (a symlink, a submodule, or a file deleted but
    # not yet staged).
    out = subprocess.run(
        ["git", "ls-files", "-z", "--eol"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    paths = []
    for entry in out.split("\0"):
        if not entry:
            continue
        info, path = entry.split("\t", 1)
        worktree_eol = info.split()[1].removeprefix("w/")
        if worktree_eol and worktree_eol != "-text":
            paths.append(path)
    return paths


def _problems(data: bytes) -> list[tuple[int | None, str]]:
    if not data:
        return []
    if not data.strip(b"\n"):
        return [(None, "has only newlines; an empty file should have none")]
    problems: list[tuple[int | None, str]] = []
    if data.startswith(b"\n"):
        problems.append((1, "starts with a blank line"))
    lines = data.split(b"\n")
    # A final newline leaves an empty last element, which isn't a line.
    if lines[-1] == b"":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if line != line.rstrip():
            problems.append((number, "trailing whitespace"))
    if not data.endswith(b"\n"):
        problems.append((None, "no newline at end of file"))
    elif data.endswith(b"\n\n"):
        problems.append((None, "more than one newline at end of file"))
    return problems


def main() -> int:
    failures = []
    for path in _text_files():
        for line, message in _problems(Path(path).read_bytes()):
            failures.append(f"{path}:{line}: {message}" if line else f"{path}: {message}")
    if failures:
        print("Whitespace problems in Git-tracked text files:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("All Git-tracked text files have clean whitespace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
