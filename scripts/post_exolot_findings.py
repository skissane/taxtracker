"""Post exolot's findings on this repo's code to a PR as a comment, hiding the
comments earlier runs posted.

CI makes the report with `exolot --format github-comment` in the job that runs
exolot, and runs this in a job that has a write-capable token but runs
nothing else, so it uses only the stdlib."""

import argparse
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

from scripts import github_comments

# Marks the comments this posts, so the next run can find and hide them.
MARKER = "<!-- lifetracker-exolot-findings -->"


def _post(args: argparse.Namespace) -> None:
    body = f"{MARKER}\n{args.body.read_text()}"
    token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
    repo = args.repo or github_comments.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if args.commit:
        # An older commit's run can finish after a newer one's, and mustn't
        # post over its report.
        head = github_comments.pr_head(repo, args.pr, token)
        if not head.startswith(args.commit):
            print(f"Not posting: the report is for {args.commit}, but PR #{args.pr}'s head is now {head}.")
            return
    # Post first, so a failure leaves the old report showing rather than none.
    posted = github_comments.request(
        "POST", f"{github_comments.API_URL}/repos/{repo}/issues/{args.pr}/comments", token, json_body={"body": body}
    )
    hidden = github_comments.hide_comments(repo, args.pr, MARKER, token, keep=posted["node_id"])
    print(f"Posted exolot's findings to PR #{args.pr}, and hid {hidden} earlier report(s).")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Comments with $GITHUB_TOKEN, defaulting to `gh auth token`.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("body", type=Path, help="the report, from `exolot --format github-comment`")
    parser.add_argument(
        "--commit",
        help="commit the report is for; if it isn't (a prefix of) the PR's head, nothing is posted",
    )
    args = parser.parse_args(argv)

    try:
        _post(args)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
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
