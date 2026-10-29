"""GitHub API helpers for the scripts that post a PR comment and edit it in
place on later runs (post_screenshots, post_patch_coverage). Stdlib-only; run
those scripts as modules (`python -m scripts.<name>`) from the repo root, so
they can import this."""

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.github.com"


def request(
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
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        if content_type:
            req.add_header("Content-Type", content_type)
        if urllib.parse.urlsplit(url).hostname == host:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = response.read()
            return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location")
            if e.code not in (307, 308) or not location:
                raise
            e.close()
            url = urllib.parse.urljoin(url, location)
    raise RuntimeError(f"too many redirects for {method} {url}")


def gh_output(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout.strip()


def pr_head(repo: str, pr: int, token: str) -> str:
    """The PR's current head commit."""
    return request("GET", f"{API_URL}/repos/{repo}/pulls/{pr}", token)["head"]["sha"]


def marked_comments(repo: str, pr: int, author: str, marker: str, token: str) -> list[dict]:
    """The comments author has posted on pr whose body starts with marker, oldest first."""
    comments = []
    page = 1
    while True:
        batch = request(
            "GET",
            f"{API_URL}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}",
            token,
        )
        comments += [c for c in batch if c["user"]["login"] == author and c["body"].startswith(marker)]
        if len(batch) < 100:
            return comments
        page += 1


def upsert_comment(repo: str, pr: int, author: str, marker: str, body: str, token: str) -> None:
    """Edit author's comment on pr starting with marker to have body, or post
    one if there's none. body should start with marker, so the next run finds it."""
    previous = marked_comments(repo, pr, author, marker, token)
    comments_url = f"{API_URL}/repos/{repo}/issues/comments"
    if previous:
        # Edit in place, so the comment keeps its place in the conversation.
        request("PATCH", f"{comments_url}/{previous[0]['id']}", token, json_body={"body": body})
        # Keep only one, should a failed or raced run have left another.
        for comment in previous[1:]:
            request("DELETE", f"{comments_url}/{comment['id']}", token)
    else:
        request("POST", f"{API_URL}/repos/{repo}/issues/{pr}/comments", token, json_body={"body": body})
