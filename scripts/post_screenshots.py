import argparse
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from pathlib import Path

from scripts import github_comments
from scripts.github_comments import API_URL

# Marks the comment this script posts, so the next run can find and edit it.
MARKER = "<!-- lifetracker-screenshots -->"

# The endpoint `gh ... --attach` (cli/cli internal/attachments/client.go) uploads
# to. It's undocumented, so this sends the same request gh 2.102.0 does.
UPLOAD_URL = "https://uploads.github.com/user-attachments/assets"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.png$")


def _screenshot_paths(directory: Path) -> list[Path]:
    """The PNGs in directory, sorted by name. Names are restricted so that one
    can't inject Markdown or HTML into the comment."""
    paths = sorted(directory.glob("*.png"))
    if not paths:
        raise ValueError(f"no screenshots (*.png) in {directory}")
    for path in paths:
        if not _NAME_RE.fullmatch(path.name):
            raise ValueError(f"screenshot name {path.name!r} doesn't match {_NAME_RE.pattern}")
    return paths


def _build_body(urls: dict[str, str], commit: str | None, run_url: str | None) -> str:
    """The comment body, given each screenshot's uploaded URL by name."""
    lines = [MARKER, "## Screenshots", ""]
    if commit or run_url:
        taken = "Taken"
        if commit:
            taken += f" for {commit}"
        if run_url:
            taken += f" by [this workflow run]({run_url})"
        lines += [f"{taken}.", ""]
    # Collapsed, so the comment stays short; an <img> tag rather than Markdown
    # image syntax, which GitHub only renders after a blank line inside <details>.
    for name, url in urls.items():
        lines += [
            f'<details><summary>{name}</summary><img src="{url}" alt="{name}"></details>',
            "",
        ]
    return "\n".join(lines)


def _upload(path: Path, repository_id: int, token: str) -> str:
    """Upload a PNG as a user attachment of the repository; return its URL."""
    query = urllib.parse.urlencode(
        {
            "name": path.name,
            "content_type": "image/png",
            "repository_id": repository_id,
        }
    )
    asset = github_comments.request(
        "POST",
        f"{UPLOAD_URL}?{query}",
        token,
        data=path.read_bytes(),
        content_type="application/octet-stream",
    )
    if not isinstance(asset, dict) or not asset.get("url"):
        raise RuntimeError(f"upload of {path} returned no asset URL")
    return asset["url"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Post screenshots to a PR as a comment, or edit the one "
        "posted before. Uploads with $SCREENSHOTS_TOKEN (attachment uploads need "
        "a user token) and comments with $GITHUB_TOKEN; each defaults to "
        "`gh auth token`."
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--dir", type=Path, default=Path("screenshots"))
    parser.add_argument(
        "--commit",
        help="commit the screenshots are of; if it isn't (a prefix of) the PR's head, nothing is posted",
    )
    parser.add_argument("--run-url", help="URL of the workflow run that took them")
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
        paths = _screenshot_paths(args.dir)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    try:
        upload_token = os.environ.get("SCREENSHOTS_TOKEN") or github_comments.gh_output("auth", "token")
        comment_token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
        repo = args.repo or github_comments.gh_output(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
        )
        author = args.author or github_comments.request("GET", f"{API_URL}/user", comment_token)["login"]
        if args.commit:
            # A run for an older commit can finish after the run for a newer
            # one (CI queues posts per PR, but not in commit order), so post
            # only screenshots of the PR's current head, rather than overwrite
            # newer ones with stale ones.
            head = github_comments.pr_head(repo, args.pr, comment_token)
            if not head.startswith(args.commit):
                print(f"Not posting: screenshots are of {args.commit}, but PR #{args.pr}'s head is now {head}.")
                return 0
        repository_id = github_comments.request("GET", f"{API_URL}/repos/{repo}", comment_token)["id"]
        urls = {path.stem: _upload(path, repository_id, upload_token) for path in paths}
        body = _build_body(urls, args.commit, args.run_url)

        github_comments.upsert_comment(repo, args.pr, author, MARKER, body, comment_token)
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
    print(f"Posted {len(paths)} screenshot(s) to PR #{args.pr}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
