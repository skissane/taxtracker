"""Check that nothing but its own .gitignore is tracked under screenshots/.

`just screenshots` writes PNGs there, kept out of Git by screenshots/.gitignore.
Deleting that file (e.g. `rm -rf screenshots`) and then `git add -A` would
commit the PNGs along with the deletion, so this fails unless Git's index
lists exactly that file under screenshots/, and both its staged and working
tree copies have the expected contents.
"""

import subprocess
from pathlib import Path

GITIGNORE = "screenshots/.gitignore"
EXPECTED = b"# Admin screenshots written by `just screenshots`\n*\n!/.gitignore\n"


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _problems() -> list[str]:
    tracked = [path.decode() for path in _git("ls-files", "-z", "--", "screenshots/").split(b"\0") if path]
    problems = [f"{path} is tracked; only {GITIGNORE} should be" for path in tracked if path != GITIGNORE]
    if GITIGNORE not in tracked:
        problems.append(f"{GITIGNORE} isn't tracked")
    elif _git("show", f":{GITIGNORE}") != EXPECTED:
        problems.append(f"{GITIGNORE} has unexpected staged contents")
    worktree = Path(GITIGNORE)
    if not worktree.is_file():
        problems.append(f"{GITIGNORE} is missing from the working tree")
    elif worktree.read_bytes() != EXPECTED:
        problems.append(f"{GITIGNORE} has unexpected contents in the working tree")
    return problems


def main() -> int:
    problems = _problems()
    if problems:
        print("Problems in screenshots/ (only its .gitignore belongs in Git):")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"Only {GITIGNORE} is tracked under screenshots/, unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
