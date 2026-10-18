import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Marks the comment this script posts, so the next run can find and edit it.
MARKER = "<!-- lifetracker-screenshots -->"

API_URL = "https://api.github.com"
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
        if not _NAME_RE.match(path.name):
            raise ValueError(
                f"screenshot name {path.name!r} doesn't match {_NAME_RE.pattern}"
            )
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
            f'<details><summary>{name}</summary><img src="{url}" alt="{name}">'
            "</details>",
            "",
        ]
    return "\n".join(lines)


def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: object = None,
    data: bytes | None = None,
    content_type: str | None = None,
):
    """Send an API request and return its decoded JSON response (None if empty).
    A 307/308 redirect is followed with the same method and body, keeping the
    token only if the host is unchanged."""
    if json_body is not None:
        data = json.dumps(json_body).encode()
        content_type = "application/json"
    host = urllib.parse.urlsplit(url).hostname
    for _ in range(5):
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        if content_type:
            request.add_header("Content-Type", content_type)
        if urllib.parse.urlsplit(url).hostname == host:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request) as response:
                payload = response.read()
            return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location")
            if e.code not in (307, 308) or not location:
                raise
            e.close()
            url = urllib.parse.urljoin(url, location)
    raise RuntimeError(f"too many redirects for {method} {url}")


def _upload(path: Path, repository_id: int, token: str) -> str:
    """Upload a PNG as a user attachment of the repository; return its URL."""
    query = urllib.parse.urlencode(
        {
            "name": path.name,
            "content_type": "image/png",
            "repository_id": repository_id,
        }
    )
    asset = _request(
        "POST",
        f"{UPLOAD_URL}?{query}",
        token,
        data=path.read_bytes(),
        content_type="application/octet-stream",
    )
    if not isinstance(asset, dict) or not asset.get("url"):
        raise RuntimeError(f"upload of {path} returned no asset URL")
    return asset["url"]


def _screenshot_comments(repo: str, pr: int, author: str, token: str) -> list[dict]:
    """The screenshot comments author has posted on pr, oldest first."""
    comments = []
    page = 1
    while True:
        batch = _request(
            "GET",
            f"{API_URL}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}",
            token,
        )
        comments += [
            c
            for c in batch
            if c["user"]["login"] == author and c["body"].startswith(MARKER)
        ]
        if len(batch) < 100:
            return comments
        page += 1


def _gh_output(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Post screenshots to a PR as a comment, or edit the one "
        "posted before. Uploads with $SCREENSHOTS_TOKEN (attachment uploads need "
        "a user token) and comments with $GITHUB_TOKEN; each defaults to "
        "`gh auth token`."
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--dir", type=Path, default=Path("screenshots"))
    parser.add_argument("--commit", help="commit the screenshots are of")
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
        upload_token = os.environ.get("SCREENSHOTS_TOKEN") or _gh_output(
            "auth", "token"
        )
        comment_token = os.environ.get("GITHUB_TOKEN") or _gh_output("auth", "token")
        repo = args.repo or _gh_output(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
        )
        author = (
            args.author or _request("GET", f"{API_URL}/user", comment_token)["login"]
        )
        repository_id = _request("GET", f"{API_URL}/repos/{repo}", comment_token)["id"]
        urls = {path.stem: _upload(path, repository_id, upload_token) for path in paths}
        body = _build_body(urls, args.commit, args.run_url)

        previous = _screenshot_comments(repo, args.pr, author, comment_token)
        comments_url = f"{API_URL}/repos/{repo}/issues/comments"
        if previous:
            # Edit in place, so the comment keeps its place in the conversation.
            _request(
                "PATCH",
                f"{comments_url}/{previous[0]['id']}",
                comment_token,
                json_body={"body": body},
            )
            # Keep only one, should a failed or raced run have left another.
            for comment in previous[1:]:
                _request("DELETE", f"{comments_url}/{comment['id']}", comment_token)
        else:
            _request(
                "POST",
                f"{API_URL}/repos/{repo}/issues/{args.pr}/comments",
                comment_token,
                json_body={"body": body},
            )
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Posted {len(paths)} screenshot(s) to PR #{args.pr}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
