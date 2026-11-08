"""List the source files over the line limit in a Markdown report, and
optionally post it to a PR as a comment, editing the one posted before.

The files and limits come from pyproject.toml's [tool.lifetracker.long-files].
Files the PR changes (compared with the merge base of --base and HEAD) come
first, with their line counts on the base too, so a PR's report shows whether
it made a file longer or shorter. Stdlib-only, as CI runs it with a
write-capable token without installing the project."""

import argparse
import fnmatch
import os
import subprocess
import sys
import tomllib
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import TypeIs

from scripts import github_comments

# Marks the comment this posts, so the next run can find and edit it.
MARKER = "<!-- lifetracker-long-files -->"

POLICY = (
    "**For information only.** We aim for files under the limit, but only split one where splitting makes the "
    "code better, e.g. moving out a cohesive piece that has no reason to sit there, not just to get under the "
    "number. A long file that is already one cohesive unit can stay as it is, and needs no reply or explanation. "
    "See [AGENTS.md](https://github.com/skissane/taxtracker/blob/master/AGENTS.md) for more."
)


@dataclass(frozen=True)
class Config:
    include: list[str]
    exclude: list[str]
    limit: int
    # Pattern → limit; the first pattern matching a path wins over `limit`.
    limits: dict[str, int]

    def limit_for(self, path: str) -> int:
        return next((n for pattern, n in self.limits.items() if fnmatch.fnmatchcase(path, pattern)), self.limit)

    def covers(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, p) for p in self.include) and not any(
            fnmatch.fnmatchcase(path, p) for p in self.exclude
        )


@dataclass(frozen=True)
class LongFile:
    path: str
    lines: int
    limit: int
    # Lines on the base, for a file the PR changes: None if it's new there.
    base_lines: int | None = None


@dataclass(frozen=True)
class Report:
    changed: list[LongFile]
    unchanged: list[LongFile]
    default_limit: int


def load_config(pyproject: Path) -> Config:
    """[tool.lifetracker.long-files] from pyproject, raising ValueError if it's
    missing or malformed."""
    with pyproject.open("rb") as f:
        table = tomllib.load(f).get("tool", {}).get("lifetracker", {}).get("long-files")
    if not isinstance(table, dict):
        raise ValueError(f"{pyproject} has no [tool.lifetracker.long-files] table")
    include, exclude = table.get("include"), table.get("exclude", [])
    limit, limits = table.get("limit"), table.get("limits", {})
    if not (isinstance(include, list) and all(isinstance(p, str) for p in include)):
        raise ValueError("[tool.lifetracker.long-files] include must be a list of glob patterns")
    if not (isinstance(exclude, list) and all(isinstance(p, str) for p in exclude)):
        raise ValueError("[tool.lifetracker.long-files] exclude must be a list of glob patterns")
    if not _is_limit(limit):
        raise ValueError("[tool.lifetracker.long-files] limit must be a positive integer")
    if not (isinstance(limits, dict) and all(_is_limit(n) for n in limits.values())):
        raise ValueError("[tool.lifetracker.long-files] limits must map glob patterns to positive integers")
    return Config(include=include, exclude=exclude, limit=limit, limits=limits)


def _is_limit(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _count_lines(data: bytes) -> int:
    """Lines as `wc -l` counts them, plus a last line without a newline."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _base_paths(cwd: Path, merge_base: str) -> dict[str, str | None]:
    """Each path the working tree changes from merge_base, mapped to its path
    there (another for a rename or copy, None if it's new)."""
    output = _git(cwd, "diff", "--name-status", "-z", "--find-renames", merge_base, "--")
    fields = output.split("\0")
    changed: dict[str, str | None] = {}
    i = 0
    while i < len(fields) - 1:
        status = fields[i]
        if status[0] in "RC":
            changed[fields[i + 2]] = fields[i + 1]
            i += 3
        else:
            changed[fields[i + 1]] = None if status[0] == "A" else fields[i + 1]
            i += 2
    return changed


def find_long_files(cwd: Path, config: Config, base: str | None) -> Report:
    """The files config covers that are over their limit in the working tree,
    or were on base and the working tree changes them. Without base, every
    file counts as unchanged."""
    changed_paths: dict[str, str | None] = {}
    merge_base = None
    if base:
        merge_base = _git(cwd, "merge-base", base, "HEAD").strip()
        changed_paths = _base_paths(cwd, merge_base)
    changed, unchanged = [], []
    for path in _git(cwd, "ls-files", "-z").split("\0"):
        file = cwd / path
        if not path or not config.covers(path) or file.is_symlink() or not file.is_file():
            continue
        lines, limit = _count_lines(file.read_bytes()), config.limit_for(path)
        if path not in changed_paths:
            if lines > limit:
                unchanged.append(LongFile(path, lines, limit))
            continue
        base_path = changed_paths[path]
        base_lines = None
        if base_path is not None:
            base_lines = _count_lines(
                subprocess.run(
                    ["git", "show", f"{merge_base}:{base_path}"], cwd=cwd, capture_output=True, check=True
                ).stdout
            )
        if lines > limit or (base_lines or 0) > limit:
            changed.append(LongFile(path, lines, limit, base_lines))
    changed.sort(key=lambda f: (-f.lines, f.path))
    unchanged.sort(key=lambda f: (-f.lines, f.path))
    return Report(changed, unchanged, config.limit)


def _table(files: list[LongFile], *, default_limit: int, show_base: bool, link: str | None) -> list[str]:
    show_limit = any(f.limit != default_limit for f in files)
    header = ["File", "Lines"] + (["Limit"] if show_limit else [])
    rows = ["| " + " | ".join(header) + " |", "| --- |" + " ---: |" * (len(header) - 1)]
    for f in files:
        name = f"[`{f.path}`]({link}/{f.path})" if link else f"`{f.path}`"
        lines = str(f.lines)
        if show_base:
            lines = f"{'new' if f.base_lines is None else f.base_lines} → {f.lines}"
            if f.lines <= f.limit:
                lines += " (now under)"
        rows.append(f"| {name} | {lines} |" + (f" {f.limit} |" if show_limit else ""))
    return rows


def render(report: Report, *, base: str | None, commit: str | None, repo: str | None) -> str:
    """report as the Markdown of a PR comment, without the marker."""
    link = f"https://github.com/{repo}/blob/{commit}" if repo and commit else None
    total = len(report.changed) + len(report.unchanged)
    if any(f.limit != report.default_limit for f in report.changed + report.unchanged):
        title = f"## Files over their line limit ({report.default_limit} unless shown)"
    else:
        title = f"## Files over {report.default_limit} lines"
    lines = [title, "", POLICY, ""]
    if commit:
        lines += [f"Counted at {commit}" + (f", compared with `{base}`." if base else "."), ""]
    if not total:
        lines.append("No files are over the limit.")
        return "\n".join(lines) + "\n"
    if base:
        lines.append(f"### Changed in this PR ({len(report.changed)})")
        lines.append("")
        if report.changed:
            lines += _table(report.changed, default_limit=report.default_limit, show_base=True, link=link)
        else:
            lines.append("None of the files this PR changes are over the limit.")
        lines.append("")
    if report.unchanged:
        title = "Unchanged files" if base else "Files"
        lines += [f"<details><summary>{title} ({len(report.unchanged)})</summary>", ""]
        lines += _table(report.unchanged, default_limit=report.default_limit, show_base=False, link=link)
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def _post(args: argparse.Namespace, repo: str, report: Report, body: str) -> None:
    token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
    author = args.author or github_comments.request("GET", f"{github_comments.API_URL}/user", token)["login"]
    if args.commit:
        # An older commit's run can finish after a newer one's, and mustn't
        # overwrite its report.
        head = github_comments.pr_head(repo, args.post, token)
        if not head.startswith(args.commit):
            print(f"Not posting: the report is for {args.commit}, but PR #{args.post}'s head is now {head}.")
            return
    # With no long files, only replace an earlier report, never start a new one.
    create = bool(report.unchanged) or any(f.lines > f.limit for f in report.changed)
    if github_comments.upsert_comment(repo, args.post, author, MARKER, f"{MARKER}\n{body}", token, create=create):
        print(f"Posted the long files report to PR #{args.post}.")
    else:
        print(f"Not posting: no files are over the limit, and PR #{args.post} has no earlier report.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Prints the report, or with --post comments it with $GITHUB_TOKEN (default `gh auth token`).",
    )
    parser.add_argument(
        "--base",
        default=f"origin/{os.environ.get('GITHUB_BASE_REF') or 'master'}",
        help="branch whose merge base with HEAD the changed files are compared with "
        "(default: origin/$GITHUB_BASE_REF, else origin/master; empty for no comparison)",
    )
    parser.add_argument(
        "--commit",
        help="commit the report is for, which file links name (default: HEAD); with --post, "
        "nothing is posted unless it's (a prefix of) the PR's head",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name, for file links and --post (default: $GITHUB_REPOSITORY; for --post, else gh's current repo)",
    )
    parser.add_argument("--post", type=int, metavar="PR", help="post the report to this pull request")
    parser.add_argument(
        "--author",
        help="with --post, the login $GITHUB_TOKEN comments as, e.g. github-actions[bot] (default: "
        "looked up, which only works for a user token)",
    )
    args = parser.parse_args(argv)
    cwd = Path.cwd()

    try:
        config = load_config(cwd / "pyproject.toml")
        report = find_long_files(cwd, config, args.base or None)
        commit = args.commit or _git(cwd, "rev-parse", "HEAD").strip()
        repo = args.repo
        if args.post is not None and not repo:
            repo = github_comments.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        body = render(report, base=args.base or None, commit=commit, repo=repo)
        if args.post is None:
            print(body, end="")
        else:
            assert repo
            _post(args, repo, report, body)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr
        print(f"{' '.join(e.cmd)} failed:\n{stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
