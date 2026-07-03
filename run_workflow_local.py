#!/usr/bin/env python3
"""
run_workflow_local.py — Execute the `run:` steps of a GitHub Actions job
directly in your current shell environment. No Docker, no act.

Why this exists alongside the justfile: `just ci` runs recipes that were
hand-ported from the workflow, so it silently drifts out of sync if
`.github/workflows/*.yml` changes without a matching justfile edit. This
script instead reads the workflow YAML directly and runs its `run:` steps
as written, so it can't get out of sync with what CI actually does.

This deliberately does NOT try to be a full act replacement:
  - `uses:` steps (actions) are skipped with a warning — those need the
    real action code / container, which is the whole reason act exists.
  - `if:` conditions are not evaluated — all steps run. Comment out ones
    you don't want, or use --only / --skip.
  - Matrix builds are not expanded — pick one combo yourself if needed.

What it DOES support, because these are the common bits that make a
job's shell steps actually work correctly when chained together:
  - job-level and step-level `env:`
  - step-level `working-directory:`
  - step-level `shell:` (bash, sh, python; default bash -e)
  - `${{ env.FOO }}` and `${{ github.workspace }}` substitution in `run:`
  - steps writing to $GITHUB_ENV / $GITHUB_PATH (append-style, like real
    Actions) — picked up and applied to subsequent steps
  - stop-on-failure, matching GitHub's default (unless continue-on-error)

Usage:
  uv run python run_workflow_local.py .github/workflows/ci.yml
  uv run python run_workflow_local.py .github/workflows/ci.yml --job build
  uv run python run_workflow_local.py .github/workflows/ci.yml --list
  uv run python run_workflow_local.py .github/workflows/ci.yml --only "Run tests"
  uv run python run_workflow_local.py .github/workflows/ci.yml --dry-run
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

import yaml


def expand_expr(text, env: dict[str, str], workspace: str) -> str:
    """Very small subset of ${{ ... }} substitution — env.* and github.workspace."""
    if not isinstance(text, str):
        return text

    def repl(m: re.Match[str]) -> str:
        expr = m.group(1).strip()
        if expr == "github.workspace":
            return workspace
        if expr.startswith("env."):
            return env.get(expr[4:], "")
        # Unknown expression (secrets.*, matrix.*, needs.*, steps.*, etc.)
        # — leave a visible marker instead of silently guessing wrong.
        return f"<<UNRESOLVED:{expr}>>"

    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", repl, text)


def load_workflow(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def pick_job(workflow: dict, job_name: str) -> tuple[str, dict]:
    jobs: dict[str, dict] = workflow.get("jobs", {})
    if not jobs:
        sys.exit("No jobs found in workflow.")
    if job_name:
        if job_name not in jobs:
            sys.exit(f"Job {job_name!r} not found. Available: {', '.join(jobs)}")
        return job_name, jobs[job_name]
    if len(jobs) == 1:
        only = next(iter(jobs))
        return only, jobs[only]
    sys.exit(f"Multiple jobs present: {', '.join(jobs)}. Pass --job <name>.")


def apply_github_env_file(path: str, env: dict[str, str]) -> None:
    """Parse a $GITHUB_ENV-style file (KEY=value or KEY<<EOF ... EOF) into env."""
    if not os.path.exists(path):
        return
    content = open(path).read()
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            key, delim = line.split("<<", 1)
            key = key.strip()
            delim = delim.strip()
            i += 1
            val_lines = []
            while i < len(lines) and lines[i] != delim:
                val_lines.append(lines[i])
                i += 1
            env[key] = "\n".join(val_lines)
        elif "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val
        i += 1


def apply_github_path_file(path: str, env: dict[str, str]) -> None:
    if not os.path.exists(path):
        return
    extra = [ln.strip() for ln in open(path).read().splitlines() if ln.strip()]
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])


def run_step(step: dict, job_env: dict, workspace, dry_run):
    name = step.get("name", step.get("run", "unnamed step"))[:80]

    if "uses" in step:
        print(f"⚠️  SKIP (uses: {step['uses']}) — {name}")
        print("    action steps need the real action/container; not run locally")
        return True

    run_cmd = step.get("run")
    if run_cmd is None:
        print(f"—  SKIP (no run:) — {name}")
        return True

    step_env = dict(job_env)
    for k, v in (step.get("env") or {}).items():
        step_env[k] = expand_expr(str(v), job_env, workspace)

    run_cmd = expand_expr(run_cmd, step_env, workspace)

    cwd = step.get("working-directory")
    cwd = os.path.join(workspace, cwd) if cwd else workspace

    shell: str = step.get("shell", "bash")
    continue_on_error = bool(step.get("continue-on-error", False))

    print(f"\n▶ {name}")
    if cwd != workspace:
        print(f"  (cwd: {cwd})")
    print("  " + run_cmd.replace("\n", "\n  "))

    if dry_run:
        return True

    with (
        tempfile.NamedTemporaryFile(mode="w", delete=False) as env_file,
        tempfile.NamedTemporaryFile(mode="w", delete=False) as path_file,
    ):
        step_env["GITHUB_ENV"] = env_file.name
        step_env["GITHUB_PATH"] = path_file.name
        step_env["GITHUB_WORKSPACE"] = workspace

    if shell.startswith("bash"):
        argv = ["bash", "-e", "-c", run_cmd]
    elif shell == "sh":
        argv = ["sh", "-e", "-c", run_cmd]
    elif shell.startswith("python"):
        argv = ["python3", "-c", run_cmd]
    else:
        argv = [shell, "-c", run_cmd]

    result = subprocess.run(argv, cwd=cwd, env=step_env)

    apply_github_env_file(step_env["GITHUB_ENV"], job_env)
    apply_github_path_file(step_env["GITHUB_PATH"], job_env)
    os.unlink(step_env["GITHUB_ENV"])
    os.unlink(step_env["GITHUB_PATH"])

    if result.returncode != 0:
        print(f"✖ step exited {result.returncode}: {name}")
        if continue_on_error:
            print("  (continue-on-error: true — proceeding)")
            return True
        return False
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("workflow", help="path to .github/workflows/*.yml")
    ap.add_argument("--job", help="job name (required if workflow has >1 job)")
    ap.add_argument("--list", action="store_true", help="list jobs/steps and exit")
    ap.add_argument(
        "--only",
        action="append",
        help="only run steps whose name contains this substring (repeatable)",
    )
    ap.add_argument(
        "--skip",
        action="append",
        help="skip steps whose name contains this substring (repeatable)",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print commands without executing"
    )
    ap.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="dir to treat as github.workspace / step cwd (default: cwd)",
    )
    args = ap.parse_args()

    workflow = load_workflow(args.workflow)
    job_name, job = pick_job(workflow, args.job)
    steps: list[dict] = job.get("steps", [])

    if args.list:
        print(f"job: {job_name}")
        for i, s in enumerate(steps):
            tag = "uses" if "uses" in s else "run"
            print(f"  {i + 1:>2}. [{tag}] {s.get('name', '(unnamed)')}")
        return

    job_env = dict(os.environ)
    for k, v in (workflow.get("env") or {}).items():
        job_env[k] = str(v)
    for k, v in (job.get("env") or {}).items():
        job_env[k] = str(v)

    print(f"Running job {job_name!r} from {args.workflow} locally (no Docker)\n")

    for step in steps:
        step_name: str = step.get("name", "")
        if args.only and not any(
            str(o).lower() in step_name.lower() for o in args.only
        ):
            continue
        if args.skip and any(str(s).lower() in step_name.lower() for s in args.skip):
            print(f"—  SKIP (--skip match) — {step_name}")
            continue
        ok = run_step(step, job_env, args.workspace, args.dry_run)
        if not ok:
            sys.exit(1)

    print("\n✔ all steps completed")


if __name__ == "__main__":
    main()
