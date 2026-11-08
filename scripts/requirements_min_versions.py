import dataclasses
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_locked_versions() -> dict[str, set[str]]:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    locked: dict[str, set[str]] = {}
    for package in lock.get("package", []):
        name = _normalize_package_name(package["name"])
        version = package.get("version")
        if version is None:
            continue
        locked.setdefault(name, set()).add(version)
    return locked


_MINIMUM_RE = re.compile(r"(>=\s*)([^,\s;]+)")


def _parse_dependency(dep: str) -> tuple[str, str] | None:
    dependency_name_match = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^]]+\])?", dep)
    if dependency_name_match is None:
        return None
    minimum_version_match = _MINIMUM_RE.search(dep.split(";", 1)[0])
    if minimum_version_match is None:
        return None
    return _normalize_package_name(dependency_name_match.group(1)), minimum_version_match.group(2)


class _LockError(Exception):
    """A dependency's locked version can't be determined; the message says why."""


@dataclasses.dataclass(frozen=True)
class _Mismatch:
    group_name: str
    dep: str
    minimum: str
    actual: str

    @property
    def fixed_dep(self) -> str:
        """*dep* with its minimum raised to the locked version."""
        return _MINIMUM_RE.sub(rf"\g<1>{self.actual}", self.dep, count=1)


def _dependency_groups(pyproject: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """Return ``(name, dependencies)`` for the main dependencies, each optional extra and each dependency group."""
    project = pyproject.get("project", {})
    groups = [("project.dependencies", project.get("dependencies", []))]
    for extra, deps in project.get("optional-dependencies", {}).items():
        groups.append((f"project.optional-dependencies.{extra}", deps))
    for group, deps in pyproject.get("dependency-groups", {}).items():
        groups.append((f"dependency-groups.{group}", deps))
    return groups


def _find_mismatches(groups: list[tuple[str, list[Any]]], locked: dict[str, set[str]]) -> list[_Mismatch]:
    """Return each dependency whose minimum isn't its locked version.

    Raises _LockError if one isn't in uv.lock, or is locked at more than one version.
    """
    mismatches = []
    for group_name, deps in groups:
        for dep in deps:
            # Skip `{include-group = "..."}` entries; the included group is
            # checked in its own right.
            if not isinstance(dep, str):
                continue
            parsed = _parse_dependency(dep)
            if parsed is None:
                continue
            package_name, minimum = parsed
            actual = _locked_version(locked, group_name, dep, package_name)
            if minimum != actual:
                mismatches.append(_Mismatch(group_name, dep, minimum, actual))
    return mismatches


def _locked_version(locked: dict[str, set[str]], group_name: str, dep: str, package_name: str) -> str:
    versions = locked.get(package_name)
    if versions is None:
        raise _LockError(f"{group_name}: {dep!r} not found in uv.lock")
    # Only packages we check need a single locked version; a
    # transitive dependency locked at several versions is fine.
    if len(versions) > 1:
        raise _LockError(
            f"{group_name}: uv.lock has more than one version of "
            f"{package_name!r} ({', '.join(sorted(versions))}); "
            "can't pick a single minimum"
        )
    (actual,) = versions
    return actual


def _check(mismatches: list[_Mismatch]) -> int:
    if mismatches:
        print("Dependency minimum versions are out of sync with uv.lock:")
        for m in mismatches:
            print(f"- {m.group_name}: {m.dep} (minimum {m.minimum}, locked {m.actual})")
        print("Run `just requirements-fix` to update pyproject.toml.")
        return 1
    print("Dependency minimum versions match uv.lock.")
    return 0


def _replace_dependency(text: str, old: str, new: str) -> tuple[str, int]:
    """Replace the first TOML string in *text* that is exactly *old*, in either quote style, with *new*.

    Returns the new text and the number of replacements (0 or 1).
    """
    return re.subn(
        rf"""(["']){re.escape(old)}\1""",
        lambda match: f"{match.group(1)}{new}{match.group(1)}",
        text,
        count=1,
    )


def _fix(mismatches: list[_Mismatch], pyproject_path: Path, pyproject_text: str) -> int:
    if not mismatches:
        print("No dependency minimum version updates needed.")
        return 0
    for m in mismatches:
        pyproject_text, count = _replace_dependency(pyproject_text, m.dep, m.fixed_dep)
        if count == 0:
            print(f"Couldn't find {m.dep!r} in pyproject.toml", file=sys.stderr)
            return 1
    pyproject_path.write_text(pyproject_text, encoding="utf-8")
    print("Updated dependency minimum versions in pyproject.toml:")
    for m in mismatches:
        print(f"- {m.group_name}: {m.dep} (minimum {m.minimum} -> {m.actual})")
    return 0


def main(mode: str) -> int:
    pyproject_path = Path("pyproject.toml")
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    try:
        mismatches = _find_mismatches(_dependency_groups(tomllib.loads(pyproject_text)), _parse_locked_versions())
    except _LockError as exc:
        print(exc, file=sys.stderr)
        return 1
    if mode == "check":
        return _check(mismatches)
    return _fix(mismatches, pyproject_path, pyproject_text)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"check", "fix"}:
        print("Usage: requirements_min_versions.py [check|fix]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
