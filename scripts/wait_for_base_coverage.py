"""Wait for a PR's base commit to have its coverage report on Codecov.

Codecov compares a PR's coverage against its base commit's report, and works
out that comparison (and sets its statuses) only when the PR's own upload is
processed; it never revisits it when the base's report turns up later. When a
stacked PR is rebased onto master straight after its parent merges, its CI and
master's run side by side, and master's ``test`` job uploads last (it saves
the Docker image first), so the PR gets "Please upload report for BASE" and
has to be re-run.

CI's ``test`` job runs this on PRs just before uploading coverage. It polls
Codecov's API for the base commit's totals and returns as soon as they're
there, which is the first request unless the base's CI is still running. Until
then it checks the base's latest ``test`` check run on GitHub: once that has
succeeded (or if there is none), Codecov gets ``--grace`` seconds to process
the upload before this gives up, and if it failed, was cancelled or was skipped,
no report is coming, so it gives up straight away.

It never fails the job: giving up (then, after ``--grace``, or after
``--timeout`` in all) prints a GitHub Actions warning and exits 0, as the base's CI failing (e.g.
master is broken and this PR fixes it) mustn't fail the PR's own ``test``
check, which branch protection requires. Codecov's ``if_not_found: failure``
then fails ``codecov/project``.

Standard library only, run with ``uv run --no-project``.
"""

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CODECOV_API = "https://api.codecov.io/api/v2/github"
GITHUB_API = "https://api.github.com"


def _get_json(url: str, headers: dict[str, str], timeout: float) -> tuple[int, object]:
    """GET ``url``, returning its status and JSON body (``None`` if not JSON)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def codecov_coverage(repo: str, sha: str, timeout: float) -> tuple[bool, str]:
    """Whether Codecov has processed a coverage report for ``sha``, and why not."""
    owner, name = repo.split("/", 1)
    url = f"{CODECOV_API}/{owner}/repos/{name}/totals/?{urllib.parse.urlencode({'sha': sha})}"
    try:
        status, body = _get_json(url, {}, timeout)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return False, f"request failed: {getattr(e, 'reason', e)}"
    if status == 404:
        # Both an unknown commit and one without a processed report.
        return False, "no report yet"
    if status != 200:
        return False, f"HTTP {status}"
    totals = body.get("totals") if isinstance(body, dict) else None
    if not isinstance(totals, dict) or totals.get("coverage") is None:
        return False, "no coverage in report"
    return True, f"coverage {totals['coverage']}%"


def base_ci_state(repo: str, sha: str, check_name: str, token: str | None, timeout: float) -> tuple[str, str]:
    """The state of ``sha``'s latest ``check_name`` check run, and a description.

    The state is ``running`` (also when it can't be fetched), ``succeeded``,
    ``failed`` (any other conclusion, e.g. ``cancelled`` or ``skipped``: either
    way, it uploaded no coverage) or ``none`` (there's no such check run).
    """
    query = urllib.parse.urlencode({"check_name": check_name, "filter": "latest"})
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs?{query}"
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        status, body = _get_json(url, headers, timeout)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return "running", f"request failed: {getattr(e, 'reason', e)}"
    runs = body.get("check_runs") if status == 200 and isinstance(body, dict) else None
    if not isinstance(runs, list):
        return "running", f"HTTP {status}"
    if not runs:
        return "none", f"no {check_name} check run"
    # filter=latest keeps one run per name; take the newest in case of more.
    run = max(runs, key=lambda r: r.get("id", 0))
    if run.get("status") != "completed":
        return "running", f"{check_name} {run.get('status')}"
    conclusion = run.get("conclusion")
    return ("succeeded" if conclusion == "success" else "failed"), f"{check_name} {conclusion}"


def main(argv=None, *, clock=time.monotonic, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="the repository, as owner/name")
    parser.add_argument("sha", help="the PR's base commit")
    parser.add_argument(
        "--check-name",
        default="test",
        help="the CI job that uploads coverage (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds to wait in all (default: %(default)s)",
    )
    parser.add_argument(
        "--grace",
        type=float,
        default=120,
        help="seconds to wait for Codecov once the base's CI has finished (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10,
        help="seconds between attempts (default: %(default)s)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30,
        help="seconds to wait for one response (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or None

    deadline = clock() + args.timeout
    finished_at = None
    attempt = 0
    while True:
        attempt += 1
        # Don't let slow requests run far past the deadline.
        has_coverage, coverage = codecov_coverage(
            args.repo, args.sha, min(args.request_timeout, max(deadline - clock(), 1))
        )
        if has_coverage:
            print(f"Attempt {attempt}: Codecov has the base report for {args.sha} ({coverage})")
            return 0
        state, ci = base_ci_state(
            args.repo, args.sha, args.check_name, token, min(args.request_timeout, max(deadline - clock(), 1))
        )
        print(f"Attempt {attempt}: Codecov: {coverage}; base CI: {ci}", flush=True)
        if state == "failed":
            print(f"::warning::Codecov has no coverage for base commit {args.sha}, whose CI didn't succeed ({ci})")
            return 0
        if state in ("succeeded", "none"):
            if finished_at is None:
                finished_at = clock()
        else:
            # E.g. the base's CI was re-run after finishing.
            finished_at = None
        if finished_at is not None and clock() - finished_at >= args.grace:
            print(f"::warning::Codecov has no coverage for base commit {args.sha}, whose CI has finished ({ci})")
            return 0
        if clock() + args.interval > deadline:
            print(
                f"::warning::Codecov still has no coverage for base commit {args.sha} "
                f"after {args.timeout:g} seconds ({ci})"
            )
            return 0
        sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
