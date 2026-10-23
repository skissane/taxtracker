"""Re-lock a Dependabot branch's uv.lock with this repo's own uv.

Dependabot runs its own uv, which can write uv.lock in a newer format than the
uv CI pins (e.g. a higher `revision`, or simplified markers), so its PRs churn
lines unrelated to the bump. This takes the package versions from the branch's
uv.lock, puts back the uv.lock the branch started from (at its merge base with
the base ref, so changes the base branch made since aren't undone), and re-locks
it with the pinned uv, upgrading only the packages whose versions changed, to
exactly those versions. It then checks that the result locks the same versions
as Dependabot's did; if not, or if re-locking fails, it puts Dependabot's uv.lock
back.

Usage (from the repo root, base ref already fetched):
    uv run --no-project python scripts/dependabot_relock.py origin/master
"""

import subprocess
import sys
import tomllib
from pathlib import Path

LOCK_PATH = Path("uv.lock")


def _locked_versions(lock_text: str) -> dict[str, tuple[str, ...]]:
    lock = tomllib.loads(lock_text)
    versions = {}
    for package in lock.get("package", []):
        version = package.get("version")
        if version is None:
            continue
        versions.setdefault(package["name"], set()).add(version)
    return {name: tuple(sorted(v)) for name, v in versions.items()}


def _upgrade_args(
    base: dict[str, tuple[str, ...]], target: dict[str, tuple[str, ...]]
) -> list[str]:
    # Packages Dependabot dropped need no argument: re-locking drops them if
    # nothing needs them any more, and the check after it catches it if not.
    args = []
    for name, versions in sorted(target.items()):
        if base.get(name) == versions:
            continue
        # A package locked at one version is pinned to it; one locked at several
        # (forked resolution) can only be let upgrade, and the check after
        # re-locking catches it if that doesn't land on Dependabot's versions.
        if len(versions) == 1:
            args += ["--upgrade-package", f"{name}=={versions[0]}"]
        else:
            args += ["--upgrade-package", name]
    return args


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def main(base_ref: str) -> int:
    branch_lock = LOCK_PATH.read_text(encoding="utf-8")
    merge_base = _git("merge-base", "HEAD", base_ref).strip()
    base_lock = _git("show", f"{merge_base}:{LOCK_PATH}")
    target = _locked_versions(branch_lock)
    upgrade_args = _upgrade_args(_locked_versions(base_lock), target)

    LOCK_PATH.write_text(base_lock, encoding="utf-8")
    try:
        subprocess.run(["uv", "lock", *upgrade_args], check=True)
    except BaseException:
        LOCK_PATH.write_text(branch_lock, encoding="utf-8")
        raise

    relocked = _locked_versions(LOCK_PATH.read_text(encoding="utf-8"))
    if relocked != target:
        for name in sorted(relocked.keys() | target.keys()):
            if relocked.get(name) != target.get(name):
                print(
                    f"{name}: Dependabot locked {target.get(name)}, "
                    f"re-locking gave {relocked.get(name)}",
                    file=sys.stderr,
                )
        LOCK_PATH.write_text(branch_lock, encoding="utf-8")
        print("Re-locked versions differ; left Dependabot's uv.lock", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <base-ref>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
