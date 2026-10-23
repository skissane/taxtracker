"""Wait for a deploy's health check (``/healthz/``) to report its commit.

CI's deploy job runs this after ``flyctl deploy``, which leaves a stopped
Machine stopped: the first request makes Fly Proxy start it, which runs the
migrations, and the check passes once the app answers 200 with ``"status":
"ok"`` and the deployed commit. Anything else (an error, a 503, the old commit
while a rolling update finishes) is retried until ``--timeout`` runs out, and
then it fails, so someone checks ``fly logs``.

The URL holds the app's name, which is kept out of the public repo; GitHub masks
the secret in CI logs, but this never prints the URL anyway.

Standard library only, run with ``uv run --no-project``.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def check_once(url: str, commit: str, timeout: float) -> str | None:
    """Why the deploy doesn't look healthy yet, or ``None`` if it does."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return f"request failed: {reason}"
    try:
        body = json.loads(raw)
    except ValueError:
        return f"HTTP {status}, not JSON"
    if not isinstance(body, dict):
        return f"HTTP {status}, unexpected JSON"
    if status != 200 or body.get("status") != "ok":
        return f"HTTP {status}, status {body.get('status')!r}: {body.get('problem')}"
    if body.get("commit") != commit:
        return f"running commit {body.get('commit')!r}, not {commit!r}"
    return None


def main(argv=None, *, clock=time.monotonic, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="the deployed app's /healthz/ URL")
    parser.add_argument("commit", help="the commit that was deployed")
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="seconds to keep trying (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5,
        help="seconds between attempts (default: %(default)s)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=60,
        help="seconds to wait for one response (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    deadline = clock() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        problem = check_once(args.url, args.commit, args.request_timeout)
        if problem is None:
            print(f"Attempt {attempt}: healthy, running {args.commit}")
            return 0
        print(f"Attempt {attempt}: {problem}", flush=True)
        if clock() + args.interval > deadline:
            print(
                f"Deploy not healthy after {args.timeout:g} seconds; check `fly logs`.",
                file=sys.stderr,
            )
            return 1
        sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
