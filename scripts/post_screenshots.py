import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Marks the comments this script posts, so the next run can find and replace them.
MARKER = "<!-- lifetracker-screenshots -->"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*\.png$")


def _screenshot_paths(directory: Path) -> list[Path]:
    """The PNGs in directory, sorted by name. Names are restricted so that one
    can't inject gh arguments or Markdown into the comment."""
    paths = sorted(directory.glob("*.png"))
    if not paths:
        raise ValueError(f"no screenshots (*.png) in {directory}")
    for path in paths:
        if not _NAME_RE.match(path.name):
            raise ValueError(
                f"screenshot name {path.name!r} doesn't match {_NAME_RE.pattern}"
            )
    return paths


def _build_body(paths: list[Path], commit: str | None, run_url: str | None) -> str:
    lines = [MARKER, "## Screenshots", ""]
    if commit or run_url:
        taken = "Taken"
        if commit:
            taken += f" for {commit}"
        if run_url:
            taken += f" by [this workflow run]({run_url})"
        lines += [f"{taken}.", ""]
    for path in paths:
        # gh pr comment --attach rewrites these local paths to the uploaded URLs.
        lines += [f"### {path.stem}", "", f"![{path.stem}]({path.as_posix()})", ""]
    return "\n".join(lines)


def _gh(*args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args], input=input, capture_output=True, text=True, check=True
    ).stdout


def _previous_comment_ids(pr: int, login: str) -> list[int]:
    """IDs of the screenshot comments login has already posted on pr."""
    pages = json.loads(
        _gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/issues/{pr}/comments",
        )
    )
    return [
        comment["id"]
        for page in pages
        for comment in page
        if comment["user"]["login"] == login and comment["body"].startswith(MARKER)
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Post screenshots to a PR as a comment, replacing the "
        "screenshot comment the same user posted before."
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("--dir", type=Path, default=Path("screenshots"))
    parser.add_argument("--commit", help="commit the screenshots are of")
    parser.add_argument("--run-url", help="URL of the workflow run that took them")
    args = parser.parse_args(argv)

    try:
        paths = _screenshot_paths(args.dir)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    body = _build_body(paths, args.commit, args.run_url)

    try:
        # Not `gh pr comment --edit-last`: that edits the user's last comment of
        # any kind, which with a personal token could be one they wrote by hand.
        login = _gh("api", "user", "--jq", ".login").strip()
        previous = _previous_comment_ids(args.pr, login)
        attach_args = [arg for path in paths for arg in ("--attach", str(path))]
        # The body comes from stdin, so gh resolves its paths against the
        # working directory, as it does the --attach paths.
        _gh("pr", "comment", str(args.pr), "--body-file", "-", *attach_args, input=body)
        # Delete only once the new comment is up, so a failed post loses nothing.
        for comment_id in previous:
            _gh(
                "api",
                "--method",
                "DELETE",
                f"repos/{{owner}}/{{repo}}/issues/comments/{comment_id}",
            )
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    print(f"Posted {len(paths)} screenshot(s) to PR #{args.pr}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
