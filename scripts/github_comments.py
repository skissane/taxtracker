"""GitHub API helpers for the scripts that post a PR comment and edit it in
place on later runs (post_screenshots, post_patch_coverage, long_files).
Stdlib-only; run those scripts as modules (`python -m scripts.<name>`) from
the repo root, so they can import this."""

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from typing import IO, Any

API_URL = "https://api.github.com"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise every redirect as an HTTPError, for request() to follow itself.
    urllib's own handler follows GET redirects keeping every header, the
    token included, even to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirectHandler)


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.netloc


def request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: object = None,
    data: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    """Send an API request and return its decoded JSON response (None if empty).
    A 301/302/307/308 redirect is followed with the same method and body,
    keeping the token only while the scheme, host and port are unchanged (so
    not on a downgrade to http)."""
    if json_body is not None:
        data = json.dumps(json_body).encode()
        content_type = "application/json"
    origin = _origin(url)
    for _ in range(5):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        if content_type:
            req.add_header("Content-Type", content_type)
        if _origin(url) == origin:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with _opener.open(req, timeout=60) as response:
                payload = response.read()
            return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location")
            if e.code not in (301, 302, 307, 308) or not location:
                raise
            e.close()
            url = urllib.parse.urljoin(url, location)
    raise RuntimeError(f"too many redirects for {method} {url}")


def gh_output(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout.strip()


def pr_head(repo: str, pr: int, token: str) -> str:
    """The PR's current head commit."""
    sha: str = request("GET", f"{API_URL}/repos/{repo}/pulls/{pr}", token)["head"]["sha"]
    return sha


def marked_comments(repo: str, pr: int, author: str, marker: str, token: str) -> list[dict[str, Any]]:
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


def upsert_comment(repo: str, pr: int, author: str, marker: str, body: str, token: str, *, create: bool = True) -> bool:
    """Edit author's comment on pr starting with marker to have body, or post
    one if there's none and create is true. body should start with marker, so
    the next run finds it. Returns whether it edited or posted one."""
    previous = marked_comments(repo, pr, author, marker, token)
    comments_url = f"{API_URL}/repos/{repo}/issues/comments"
    if previous:
        # Edit in place, so the comment keeps its place in the conversation.
        request("PATCH", f"{comments_url}/{previous[0]['id']}", token, json_body={"body": body})
        # Keep only one, should a failed or raced run have left another.
        for comment in previous[1:]:
            request("DELETE", f"{comments_url}/{comment['id']}", token)
    elif create:
        request("POST", f"{API_URL}/repos/{repo}/issues/{pr}/comments", token, json_body={"body": body})
    else:
        return False
    return True


def graphql(query: str, variables: dict[str, Any], token: str) -> dict[str, Any]:
    """Run a GraphQL query or mutation and return its data, raising
    RuntimeError if GitHub reports errors (which it does with HTTP 200)."""
    result = request("POST", f"{API_URL}/graphql", token, json_body={"query": query, "variables": variables})
    if result.get("errors"):
        raise RuntimeError(f"GraphQL errors: {json.dumps(result['errors'])}")
    data: dict[str, Any] = result["data"]
    return data


_MY_COMMENTS = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      comments(first: 100, after: $cursor) {
        nodes { id isMinimized viewerDidAuthor body }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

_MINIMIZE = """
mutation($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) { clientMutationId }
}"""


def hide_comments(repo: str, pr: int, marker: str, token: str, *, keep: str | None = None) -> int:
    """Hide as outdated every comment on pr that token's user posted whose body
    starts with marker and isn't hidden yet, except the one whose node ID is
    keep. Matching the author means nobody else's comment can be hidden by
    copying the marker. Returns how many it hid."""
    owner, name = repo.split("/", 1)
    hidden = 0
    cursor = None
    while True:
        variables = {"owner": owner, "name": name, "pr": pr, "cursor": cursor}
        comments = graphql(_MY_COMMENTS, variables, token)["repository"]["pullRequest"]["comments"]
        for c in comments["nodes"]:
            if c["viewerDidAuthor"] and not c["isMinimized"] and c["body"].startswith(marker) and c["id"] != keep:
                graphql(_MINIMIZE, {"id": c["id"]}, token)
                hidden += 1
        if not comments["pageInfo"]["hasNextPage"]:
            return hidden
        cursor = comments["pageInfo"]["endCursor"]
