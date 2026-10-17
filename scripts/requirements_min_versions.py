import re
import sys
import tomllib
from pathlib import Path


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_locked_versions() -> dict[str, set[str]]:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    locked = {}
    for package in lock.get("package", []):
        name = _normalize_package_name(package["name"])
        version = package.get("version")
        if version is None:
            continue
        locked.setdefault(name, set()).add(version)
    return locked


def _parse_dependency(dep: str) -> tuple[str, str] | None:
    dependency_name_match = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^]]+\])?", dep)
    if dependency_name_match is None:
        return None
    minimum_version_match = re.search(r"(>=\s*)([^,\s;]+)", dep.split(";", 1)[0])
    if minimum_version_match is None:
        return None
    return _normalize_package_name(
        dependency_name_match.group(1)
    ), minimum_version_match.group(2)


def main(mode: str) -> int:
    pyproject_path = Path("pyproject.toml")
    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    installed = _parse_locked_versions()

    project = pyproject.get("project", {})
    dependency_groups = [("project.dependencies", project.get("dependencies", []))]
    for extra, deps in project.get("optional-dependencies", {}).items():
        dependency_groups.append((f"project.optional-dependencies.{extra}", deps))
    for group, deps in pyproject.get("dependency-groups", {}).items():
        dependency_groups.append((f"dependency-groups.{group}", deps))

    mismatches = []
    replacements = []

    for group_name, deps in dependency_groups:
        for dep in deps:
            # Skip `{include-group = "..."}` entries; the included group is
            # checked in its own right.
            if not isinstance(dep, str):
                continue
            parsed = _parse_dependency(dep)
            if parsed is None:
                continue
            package_name, minimum = parsed
            versions = installed.get(package_name)
            if versions is None:
                print(
                    f"{group_name}: {dep!r} not found in uv.lock",
                    file=sys.stderr,
                )
                return 1
            # Only packages we check need a single locked version; a
            # transitive dependency locked at several versions is fine.
            if len(versions) > 1:
                print(
                    f"{group_name}: uv.lock has more than one version of "
                    f"{package_name!r} ({', '.join(sorted(versions))}); "
                    "can't pick a single minimum",
                    file=sys.stderr,
                )
                return 1
            (actual,) = versions
            if minimum != actual:
                mismatches.append((group_name, dep, minimum, actual))
                replacements.append(
                    (
                        dep,
                        re.sub(r"(>=\s*)([^,\s;]+)", rf"\g<1>{actual}", dep, count=1),
                    )
                )

    if mode == "check":
        if mismatches:
            print("Dependency minimum versions are out of sync with uv.lock:")
            for group_name, dep, minimum, actual in mismatches:
                print(f"- {group_name}: {dep} (minimum {minimum}, locked {actual})")
            print("Run `just requirements-fix` to update pyproject.toml.")
            return 1
        print("Dependency minimum versions match uv.lock.")
        return 0

    if not mismatches:
        print("No dependency minimum version updates needed.")
        return 0

    for old_dep, new_dep in replacements:
        # Match the dependency as a whole TOML string, in either quote style.
        pyproject_text, count = re.subn(
            rf"""(["']){re.escape(old_dep)}\1""",
            lambda m: f"{m.group(1)}{new_dep}{m.group(1)}",
            pyproject_text,
            count=1,
        )
        if count == 0:
            print(f"Couldn't find {old_dep!r} in pyproject.toml", file=sys.stderr)
            return 1

    pyproject_path.write_text(pyproject_text, encoding="utf-8")

    print("Updated dependency minimum versions in pyproject.toml:")
    for group_name, dep, minimum, actual in mismatches:
        print(f"- {group_name}: {dep} (minimum {minimum} -> {actual})")

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"check", "fix"}:
        print("Usage: requirements_min_versions.py [check|fix]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
