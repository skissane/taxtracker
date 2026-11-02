#!/usr/bin/env python3
"""
Recreate a GitHub PR as a draft PR and close the original as superseded.

The new PR keeps the original's title, body, head and base branches, labels and milestone.
Assignees and reviewers aren't copied.
The original is often bot-opened, and GitHub rejects assigning a bot (`replaceActorsForAssignable`).
Passing `--assignee` for such a PR makes `gh pr create` fail after it has already created the PR.
So assignees are left out.
"""

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import urlparse


def run_gh(args: list[str]) -> str:
    """Run a gh CLI command and return its stdout."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        print("Error: 'gh' CLI not found. Please install it first.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error executing 'gh {' '.join(args)}':", file=sys.stderr)
        print(e.stderr.strip(), file=sys.stderr)
        sys.exit(1)


def parse_pr_url(url: str) -> tuple[str, str, str]:
    """
    Extract the host, 'owner/repo' and PR number from a GitHub PR URL.
    Works for GitHub Enterprise hosts too.
    """
    parsed = urlparse(url)
    match = re.match(r"^/([^/]+/[^/]+)/pull/(\d+)(?:/.*)?$", parsed.path)
    if not match:
        print(
            f"Error: Invalid GitHub PR URL '{url}'. Expected format: https://github.com/<owner>/<repo>/pull/<number>",
            file=sys.stderr,
        )
        sys.exit(1)
    return parsed.netloc, match.group(1), match.group(2)


def find_open_pr(gh_repo: str, head_branch: str, base_branch: str) -> str | None:
    """
    Return the URL of an open PR from head_branch into base_branch, or None if there's none.
    Raises subprocess.CalledProcessError or ValueError if the lookup itself fails, so a failure isn't taken for "none".
    """
    out = subprocess.run(
        ["gh", "pr", "list", "--repo", gh_repo, "--head", head_branch, "--base", base_branch]
        + ["--state", "open", "--json", "url"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    prs = json.loads(out)
    if not isinstance(prs, list):
        raise ValueError(f"unexpected gh pr list output: {out!r}")
    return prs[0]["url"] if prs else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Recreate a GitHub PR as a draft and close the original.")
    parser.add_argument("pr_url", help="The URL of the GitHub Pull Request")
    args = parser.parse_args(argv)

    # 1. Extract repo and PR number from the URL for generic operation
    host, repo_slug, pr_num_from_url = parse_pr_url(args.pr_url)
    # gh's --repo takes [HOST/]OWNER/REPO; always passing the URL's host stops later calls going to a different host
    gh_repo = f"{host}/{repo_slug}"

    # 2. Fetch PR details
    fields = "number,title,body,headRefName,baseRefName,isCrossRepository,headRepositoryOwner,state,labels,milestone"
    pr_data = json.loads(run_gh(["pr", "view", args.pr_url, "--json", fields]))

    old_number = pr_data["number"]
    if str(old_number) != pr_num_from_url:
        print(f"Error: URL names PR #{pr_num_from_url} but GitHub returned PR #{old_number}.", file=sys.stderr)
        sys.exit(1)
    title = pr_data["title"]
    body = pr_data.get("body") or ""
    head_branch = pr_data["headRefName"]
    base_branch = pr_data["baseRefName"]
    state = pr_data["state"]

    # 3. Refuse to operate on fork PRs
    # Not a comparison of the head repo's name with the URL's: after a rename, an old URL still resolves
    if pr_data.get("isCrossRepository", False):
        head_owner = (pr_data.get("headRepositoryOwner") or {}).get("login", "")
        print(
            f"Error: PR #{old_number} comes from a fork ({head_owner or 'cross-repository'}). "
            "Refusing to operate on fork PRs.",
            file=sys.stderr,
        )
        sys.exit(1)

    if state != "OPEN":
        print(
            f"Error: PR #{old_number} is currently '{state}', not OPEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    create_args = [
        "pr",
        "create",
        "--repo",
        gh_repo,
        "--head",
        head_branch,
        "--base",
        base_branch,
        "--title",
        title,
        "--body-file",
        "-",
        "--draft",
    ]
    for label in pr_data.get("labels") or []:
        create_args += ["--label", label["name"]]
    if pr_data.get("milestone"):
        create_args += ["--milestone", pr_data["milestone"]["title"]]

    # 4. Close the original PR first.
    # GitHub rejects creating a new PR if an open PR already exists for the same head and base.
    print(f"Closing original PR #{old_number} in {repo_slug}...")
    run_gh(["pr", "close", str(old_number), "--repo", gh_repo])

    # 5. Create the new draft PR, reopening the original if that fails
    print(f"Creating new draft PR from '{head_branch}' into '{base_branch}'...")
    try:
        new_pr_url = subprocess.run(
            ["gh"] + create_args,
            input=body,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        print("Error creating new draft PR:", file=sys.stderr)
        print(e.stderr.strip(), file=sys.stderr)
        # gh can fail after creating the PR (e.g. adding labels), so check before reopening the original
        try:
            existing = find_open_pr(gh_repo, head_branch, base_branch)
        except (subprocess.CalledProcessError, ValueError) as lookup_error:
            # Reopening when the new PR may exist could leave both open, so leave it to the user
            detail = lookup_error.stderr.strip() if isinstance(lookup_error, subprocess.CalledProcessError) else ""
            print(detail or str(lookup_error), file=sys.stderr)
            print(
                f"Error: could not check whether the new PR was created anyway. PR #{old_number} is still CLOSED; "
                f"check for an open PR from '{head_branch}' and reopen #{old_number} manually if there's none.",
                file=sys.stderr,
            )
            sys.exit(1)
        if existing:
            print(
                f"Error: {existing} was created anyway, but may lack labels or milestone; "
                f"PR #{old_number} stays closed and was not commented on.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Attempting to reopen original PR #{old_number}...", file=sys.stderr)
        try:
            subprocess.run(
                ["gh", "pr", "reopen", str(old_number), "--repo", gh_repo],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as reopen_error:
            print(reopen_error.stderr.strip(), file=sys.stderr)
            print(
                f"Error: could not reopen PR #{old_number}; it is still CLOSED and must be reopened manually.",
                file=sys.stderr,
            )
        sys.exit(1)

    # 6. Extract the new PR number and comment on the original PR (the new PR already exists, so report failures here)
    _, _, new_pr_number = parse_pr_url(new_pr_url)
    print(f"Created draft PR #{new_pr_number}: {new_pr_url}")

    print(f"Commenting on original PR #{old_number}...")
    try:
        subprocess.run(
            ["gh", "pr", "comment", str(old_number), "--repo", gh_repo, "--body", f"Superseded by #{new_pr_number}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(e.stderr.strip(), file=sys.stderr)
        print(
            f"Error: created {new_pr_url} and closed PR #{old_number}, but could not comment on it.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Success!")


if __name__ == "__main__":
    main()
