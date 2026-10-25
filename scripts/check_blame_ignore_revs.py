"""Check that .git-blame-ignore-revs lists only real commits from the target branch's history.

Git silently ignores an entry naming no commit, so a typo or a pre-squash hash
would leave the reformatting commit it was meant to hide in `git blame`. This
fails unless every non-comment line is a full lowercase hash of a commit that's
an ancestor of the target (so CI needs the full history).

The target is HEAD unless given as the only argument. On a PR, CI passes the
base branch's commit: as PRs are squash-merged, a commit from the PR itself
is an ancestor of the PR's head but won't be on the base branch once merged.
"""

import re
import subprocess
import sys
from pathlib import Path

IGNORE_REVS = Path(".git-blame-ignore-revs")
HASH_RE = re.compile(r"[0-9a-f]{40}")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True)


def _problems(target: str) -> list[str]:
    if not IGNORE_REVS.is_file():
        return [f"{IGNORE_REVS} is missing"]
    problems = []
    for line_number, line in enumerate(IGNORE_REVS.read_text(encoding="utf-8").splitlines(), 1):
        rev = line.strip()
        if not rev or rev.startswith("#"):
            continue
        if HASH_RE.fullmatch(rev) is None:
            problems.append(f"{IGNORE_REVS}:{line_number}: not a full 40-character lowercase hex hash: {rev!r}")
            continue
        object_type = _git("cat-file", "-t", rev)
        if object_type.returncode != 0:
            problems.append(f"{IGNORE_REVS}:{line_number}: {rev} doesn't name an existing Git object")
            continue
        if object_type.stdout.strip() != "commit":
            problems.append(f"{IGNORE_REVS}:{line_number}: {rev} is a {object_type.stdout.strip()}, not a commit")
            continue
        ancestor = _git("merge-base", "--is-ancestor", rev, target)
        if ancestor.returncode == 1:
            problems.append(f"{IGNORE_REVS}:{line_number}: {rev} is not an ancestor of {target}")
        elif ancestor.returncode != 0:
            problems.append(
                f"{IGNORE_REVS}:{line_number}: couldn't check whether {rev} is an ancestor of {target} "
                f"(exit {ancestor.returncode})"
            )
    return problems


def main(target: str = "HEAD") -> int:
    problems = _problems(target)
    if problems:
        print("Problems in .git-blame-ignore-revs:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f".git-blame-ignore-revs only lists real commit hashes that are ancestors of {target}.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2:
        print(f"usage: {sys.argv[0]} [<target>]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:]))
