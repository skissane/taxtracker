"""Re-lock a Dependabot branch's uv.lock with this repo's own uv.

Dependabot runs its own uv, which can write uv.lock in a newer format than the
uv CI pins (e.g. a higher `revision`, or simplified markers), so its PRs churn
lines unrelated to the bump. This takes the package versions from the branch's
uv.lock, puts back the base branch's uv.lock, and re-locks it with the pinned uv,
upgrading only the packages whose versions changed, to exactly those versions.
It then checks that the result locks the same versions as Dependabot's did.

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


def main(base_ref: str) -> int:
    branch_lock = LOCK_PATH.read_text(encoding="utf-8")
    base_lock = subprocess.run(
        ["git", "show", f"{base_ref}:{LOCK_PATH}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    target = _locked_versions(branch_lock)
    upgrade_args = _upgrade_args(_locked_versions(base_lock), target)

    LOCK_PATH.write_text(base_lock, encoding="utf-8")
    subprocess.run(["uv", "lock", *upgrade_args], check=True)

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
