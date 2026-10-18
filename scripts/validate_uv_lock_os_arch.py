import re
import tomllib
from enum import StrEnum
from pathlib import Path


# How _classify_platform treats an (OS, arch) pair, as split by _split_platform.
# SUPPORTED platforms are ones we intend to support, matching `[tool.uv]
# environments` in pyproject.toml; TOLERATED ones are platforms uv keeps wheels
# for even though `environments` excludes them. uv.lock may only have wheels for
# these two.
class PlatformClassification(StrEnum):
    SUPPORTED = "supported"
    TOLERATED = "tolerated"
    UNSUPPORTED = "unsupported"


def _classify_platform(os_arch: tuple[str, str | None]) -> PlatformClassification:
    os, arch = os_arch
    if os == "any" and arch is None:
        return PlatformClassification.SUPPORTED
    if "linux" in os and arch in ("x86_64", "aarch64"):
        return PlatformClassification.SUPPORTED
    if os == "macosx":
        return PlatformClassification.SUPPORTED
    if os in ("ios", "pyemscripten"):
        return PlatformClassification.TOLERATED
    return PlatformClassification.UNSUPPORTED


# An OS name, any version glued onto it (`manylinux2014`) or following it in
# `_<number>` parts (`macosx_10_15`), then the arch, which may itself contain
# `_` (`x86_64`) but never starts with a digit.
_PLATFORM_RE = re.compile(r"(?P<os>[a-z]+?)\d*(?:_\d+)*_(?P<arch>[a-z].*)")


def _split_platform(platform: str) -> tuple[str, str | None]:
    if platform == "any":
        return "any", None
    if platform == "win32":  # the one tag with no "_<arch>" suffix
        return "win", "x86"
    m = _PLATFORM_RE.fullmatch(platform)
    if m is None:
        raise ValueError(f"unrecognised wheel platform tag: {platform}")
    return m["os"], m["arch"]


def _wheel_platforms(filename: str) -> list[str]:
    # A wheel's platform tag is its last `-`-separated field, and may hold
    # several tags joined by `.` (e.g. `manylinux_2_17_x86_64.manylinux2014_x86_64`).
    return filename.removesuffix(".whl").split("-")[-1].split(".")


def main() -> int:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    disallowed = []
    packages: list[dict] = lock.get("package", [])
    for package in packages:
        wheels: list[dict] = package.get("wheels", [])
        for wheel in wheels:
            # Registry wheels have a `url`; local ones a `path` or `filename`.
            location: str | None = (
                wheel.get("url") or wheel.get("path") or wheel.get("filename")
            )
            if location is None:
                raise ValueError(
                    f"wheel in {package['name']} has no url, path or filename"
                )
            filename = location.rsplit("/", 1)[-1]
            for platform in _wheel_platforms(filename):
                try:
                    classification = _classify_platform(_split_platform(platform))
                except ValueError:
                    classification = PlatformClassification.UNSUPPORTED
                if classification == PlatformClassification.UNSUPPORTED:
                    disallowed.append((package["name"], filename, platform))

    if disallowed:
        print("uv.lock has wheels for platforms that aren't allowed:")
        for name, filename, platform in disallowed:
            print(f"- {name}: {filename} ({platform})")
        print(
            "Check `[tool.uv] environments` in pyproject.toml. If the platform "
            "should be supported, add it there and make _classify_platform in "
            "scripts/validate_uv_lock_os_arch.py return SUPPORTED for it; if uv "
            "keeps its wheels despite `environments`, make it return TOLERATED."
        )
        return 1
    print("uv.lock only has wheels for allowed platforms.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
