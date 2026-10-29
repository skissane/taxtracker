import argparse
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

from scripts import github_comments

# Marks the comment this script posts, so the next run can find and edit it.
MARKER = "<!-- lifetracker-patch-coverage -->"

# GitHub rejects a comment body over 65,536 characters; leave room for the
# truncation note.
MAX_BODY_CHARS = 65_000


def _build_body(report: str, commit: str | None, run_url: str | None) -> str:
    """The comment body: diff-cover's Markdown report, with a line saying what
    it was measured for after its title, cut to fit a comment if need be."""
    lines = report.rstrip("\n").split("\n")
    title, rest = (lines[0], lines[1:]) if lines[0].startswith("# ") else ("# Diff Coverage", lines)
    head = [MARKER, title.replace("# ", "## ", 1), ""]
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
        report = args.report.read_text()
    except OSError as e:
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
        body = _build_body(report, args.commit, args.run_url)
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
