import argparse
import os
import re
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from scripts import github_comments

# Marks the comment this script posts, so the next run can find and edit it.
MARKER = "<!-- lifetracker-patch-coverage -->"

# GitHub rejects a comment body over 65,536 characters; leave room for the
# truncation note.
MAX_BODY_CHARS = 65_000


# The lines diff-cover's markdown_coverage_report.md template writes before
# its per-file snippets: each file as `- <path> (<pct>%)`, then
# `: Missing lines <lines>` if any are (the path's dots escaped as `&#46;` so
# GitHub doesn't link it), and the summary as `- **<name>**: <value>`. Anything
# else there fails the script, so a diff-cover upgrade that changes the format
# fails the tests (PatchCoverageRealDiffCoverTests) rather than going unnoticed.
NO_LINES = "No lines with coverage information in this diff."
_FILE_RE = re.compile(r"^- (?P<path>.+) \((?P<percent>[0-9.]+%)\)(?:: Missing lines (?P<missing>[0-9,-]+))?$")
_SUMMARY_RE = re.compile(r"^- \*\*(?P<name>Total|Missing|Coverage)\*\*: (?P<value>.+)$")


class UnexpectedReport(ValueError):
    pass


def _file_table(matches: list[re.Match[str]]) -> list[str]:
    rows = ["| File | Coverage | Missing lines |", "| --- | ---: | --- |"]
    for m in matches:
        path = m["path"].replace("&#46;", ".")
        # A space after each comma, so a long list can wrap.
        missing = ", ".join((m["missing"] or "").split(","))
        rows.append(f"| `{path}` | {m['percent']} | {missing} |")
    return rows


def _summary_table(matches: list[re.Match[str]]) -> list[str]:
    return [
        "| " + " | ".join(m["name"] for m in matches) + " |",
        "|" + " ---: |" * len(matches),
        "| " + " | ".join(m["value"] for m in matches) + " |",
    ]


def _demote_headings(lines: list[str]) -> list[str]:
    """lines with each ATX heading outside a code block one level deeper."""
    demoted = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            line = "#" + line
        demoted.append(line)
    return demoted


class _Lines:
    """A cursor over a report's lines, raising UnexpectedReport for one that
    isn't what the parser expects."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.pos = 0

    def peek(self) -> str | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def fail(self, what: str) -> NoReturn:
        line = self.peek()
        found = "the end of the report" if line is None else repr(line)
        raise UnexpectedReport(f"diff-cover report line {self.pos + 1}: expected {what}, found {found}")

    def take(self, ok: Callable[[str], bool], what: str) -> str:
        """The current line, moving past it, if ok() accepts it."""
        line = self.peek()
        if line is None or not ok(line):
            self.fail(what)
        self.pos += 1
        return line

    def accept(self, text: str) -> bool:
        """Whether the current line is text, moving past it if so."""
        if self.peek() != text:
            return False
        self.pos += 1
        return True

    def match(
        self, regex: re.Pattern[str], ok: Callable[[re.Match[str]], bool] = lambda m: True
    ) -> re.Match[str] | None:
        """regex's match of the current line, moving past it, if there is one
        and ok() accepts it."""
        line = self.peek()
        m = None if line is None else regex.match(line)
        if m is None or not ok(m):
            return None
        self.pos += 1
        return m

    def skip_blank(self) -> None:
        while self.peek() == "":
            self.pos += 1

    def rest(self) -> list[str]:
        return self.lines[self.pos :]


def _reformat(report: str) -> tuple[str, list[str]]:
    """diff-cover's report as its title and the lines after it, with its file
    list and summary as tables and its headings one level deeper, so its
    sections nest under the comment's title. Raises UnexpectedReport if it
    isn't in the shape diff-cover's template writes."""
    lines = _Lines(report.rstrip("\n").split("\n"))
    title = lines.take(lambda line: line == "# Diff Coverage", "'# Diff Coverage'")
    out = ["#" + lines.take(lambda line: line.startswith("## Diff: "), "'## Diff: ...'"), ""]
    lines.skip_blank()
    if lines.accept(NO_LINES):
        lines.skip_blank()
        if lines.peek() is not None:
            lines.fail("nothing more")
        return title, out + [NO_LINES]
    files = list(iter(lambda: lines.match(_FILE_RE), None))
    if not files:
        lines.fail("a file's coverage or " + repr(NO_LINES))
    out += _file_table(files) + [""]
    lines.skip_blank()
    lines.take(lambda line: line == "## Summary", "'## Summary'")
    out += ["### Summary", ""]
    lines.skip_blank()
    summary = []
    for name in ("Total", "Missing", "Coverage"):
        m = lines.match(_SUMMARY_RE, lambda m: m["name"] == name)
        if m is None:
            lines.fail(f"'- **{name}**: ...'")
        summary.append(m)
    out += _summary_table(summary)
    # The rest is each file's snippets of missing lines, kept as they are.
    return title, out + _demote_headings(lines.rest())


def _build_body(report: str, commit: str | None, run_url: str | None) -> str:
    """The comment body: diff-cover's Markdown report reformatted, with a
    line saying what it was measured for after its title, cut to fit a comment
    if need be. Raises UnexpectedReport for a report it can't reformat."""
    title, rest = _reformat(report)
    head = [MARKER, "#" + title, ""]
    if commit or run_url:
        measured = "Patch coverage"
        if commit:
            measured += f" of {commit}"
        if run_url:
            measured += f", from [this workflow run]({run_url})"
        head += [f"{measured}.", ""]
    body = "\n".join(head + rest)
    if len(body) <= MAX_BODY_CHARS:
        return body
    # The head always has a newline, so this never finds none.
    cut = body[: body.rfind("\n", 0, MAX_BODY_CHARS)]
    # Close a code block left open by the cut, so the note isn't inside it.
    if cut.count("\n```") % 2:
        cut += "\n```"
    where = f"[this workflow run]({run_url})" if run_url else "the workflow run"
    return f"{cut}\n\n_Report truncated; the whole of it is in the `patch-coverage` artifact of {where}._"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Post diff-cover's Markdown patch coverage report to a PR as a "
        "comment, or edit the one posted before. Comments with $GITHUB_TOKEN, "
        "defaulting to `gh auth token`."
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--report", type=Path, default=Path("patch-coverage.md"))
    parser.add_argument(
        "--commit",
        help="commit the report is for; if it isn't (a prefix of) the PR's head, nothing is posted",
    )
    parser.add_argument("--run-url", help="URL of the workflow run that measured it")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument(
        "--author",
        help="login $GITHUB_TOKEN comments as, e.g. github-actions[bot] (default: "
        "looked up, which only works for a user token)",
    )
    args = parser.parse_args(argv)

    try:
        body = _build_body(args.report.read_text(), args.commit, args.run_url)
    except (OSError, UnexpectedReport) as e:
        print(e, file=sys.stderr)
        return 1

    try:
        token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
        repo = args.repo or github_comments.gh_output(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
        )
        author = args.author or github_comments.request("GET", f"{github_comments.API_URL}/user", token)["login"]
        if args.commit:
            # As in post_screenshots: an older commit's run can finish after a
            # newer one's, and mustn't overwrite its report.
            head = github_comments.pr_head(repo, args.pr, token)
            if not head.startswith(args.commit):
                print(f"Not posting: the report is for {args.commit}, but PR #{args.pr}'s head is now {head}.")
                return 0
        github_comments.upsert_comment(repo, args.pr, author, MARKER, body, token)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Posted the patch coverage report to PR #{args.pr}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
