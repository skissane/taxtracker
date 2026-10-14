import re
import subprocess
import sys
import tomllib
from pathlib import Path


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_installed_versions() -> dict[str, str]:
    freeze = subprocess.run(
        ["uv", "pip", "freeze", "--exclude-editable"],
        check=True,
        capture_output=True,
        text=True,
    )
    installed = {}
    for raw_line in freeze.stdout.splitlines():
        line = raw_line.strip()
        if not line or "==" not in line:
            continue
        name, version = line.split("==", 1)
        installed[_normalize_package_name(name)] = version
    return installed


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
    installed = _parse_installed_versions()

    dependency_groups = [
        ("project.dependencies", pyproject.get("project", {}).get("dependencies", [])),
        (
            "dependency-groups.dev",
            pyproject.get("dependency-groups", {}).get("dev", []),
        ),
    ]

    mismatches = []
    replacements = []

    for group_name, deps in dependency_groups:
        for dep in deps:
            parsed = _parse_dependency(dep)
            if parsed is None:
                continue
            package_name, minimum = parsed
            actual = installed.get(package_name)
            if actual is None:
                print(
                    f"{group_name}: {dep!r} not found in uv pip freeze output",
                    file=sys.stderr,
                )
                return 1
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
            print(
                "Dependency minimum versions are out of sync with installed versions:"
            )
            for group_name, dep, minimum, actual in mismatches:
                print(f"- {group_name}: {dep} (minimum {minimum}, installed {actual})")
            print("Run `just requirements-fix` to update pyproject.toml.")
            return 1
        print("Dependency minimum versions match installed versions.")
        return 0

    if not mismatches:
        print("No dependency minimum version updates needed.")
        return 0

    for old_dep, new_dep in replacements:
        pyproject_text = pyproject_text.replace(f'"{old_dep}"', f'"{new_dep}"', 1)

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
