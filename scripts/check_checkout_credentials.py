"""Check that every actions/checkout step in the workflows and local composite
actions says whether it keeps its token: `persist-credentials: false`, or
`persist-credentials: true` in a job that pushes with it.

Left unset, checkout keeps the token where every later step of the job can read
it. jactionlint's artipacked rule would catch that, but it doesn't report
actions/checkout v6 and later, which moved the token out of .git/config into a
file the git config includes (so an upload of the workspace no longer publishes
it, but any step can still read it)."""

import sys
from pathlib import Path

from scripts.check_pins import pins, workflow_files

CHECKOUT = "actions/checkout"
KEY = "persist-credentials"


def problems(root: Path) -> tuple[list[str], int]:
    """Each checkout step that doesn't set persist-credentials to true or false,
    and how many checkout steps there are. No checkout step at all is a problem
    too, as it more likely means they're no longer recognized."""
    found: list[str] = []
    count = 0
    for path in workflow_files(root):
        for value in pins(path.read_text(), CHECKOUT, KEY):
            count += 1
            if value not in ("true", "false"):
                setting = f"{KEY}: {value}" if value else f"no {KEY}"
                found.append(f"{path.relative_to(root).as_posix()}: a {CHECKOUT} step has {setting}, not false or true")
    if not count:
        found.append(f"no workflow or action has a {CHECKOUT} step")
    return found, count


def main(root: Path = Path()) -> int:
    found, count = problems(root)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    print(f"all {count} {CHECKOUT} steps set {KEY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
