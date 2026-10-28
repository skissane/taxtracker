# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

**`CLAUDE.md` and `.github/copilot-instructions.md` are symlinks to this file**, not separate files.
Always edit `AGENTS.md` directly.
If you only see one of the symlink paths, resolve it back to `AGENTS.md` first — don't write through the symlink, and don't treat the symlinked path as a distinct file that also needs updating.

This file and `README.md` are written one sentence per line, not wrapped at a fixed width.
Git merges line by line, so two changes to the same paragraph then only conflict if they touch the same or adjacent sentences.
Keep to this when editing them: put each new sentence on its own line, don't rewrap neighbouring lines, and indent a list item's later lines to line up with its text.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`; run `just ci` to run everything CI runs, or `just --list` to see individual recipes (`whitespace-check`, `screenshots-dir-check`, `blame-ignore-revs-check`, `lint`, `fmt-check`, `lint-templates`, `fmt-check-templates`, `lint-workflows`, `justfile-check`, `coverage`, `screenshots`, `screenshots-post`, `sync`, `lock-check`, `lock-os-arch-check`, `requirements-check`, `requirements-fix`, `dependabot-relock`, `docker-build`, `docker-smoke`).
`just ci` drifts from `ci.yml` if the workflow changes without a matching justfile edit; `run_workflow_local.py` avoids that by parsing the workflow YAML directly and running its `run:` steps as written (see its docstring for what it does and doesn't support).
After `just coverage` runs, CI uploads `coverage.xml` to Codecov (`codecov/codecov-action`), and also uploads the `junit.xml` test results it writes for Codecov Test Analytics (`report_type: test_results`, even if the tests failed; not `codecov/test-results-action`, which is deprecated); these steps need a `CODECOV_TOKEN` repo secret from codecov.io and are skipped by `run_workflow_local.py` since they're `uses:` steps.
Codecov works out a PR's comparison with its base commit's report only when the PR's own upload is processed, and never redoes it when the base's report arrives later, so a stacked PR rebased onto `master` as its parent merges (whose CI races `master`'s, which uploads last as it saves the Docker image first) got "Please upload report for BASE".
So on PRs, the `test` job first runs `scripts/wait_for_base_coverage.py` (stdlib-only, `uv run --no-project`; it needs `checks: read`): it polls Codecov's public API (`/api/v2/github/<owner>/repos/<repo>/totals/?sha=<base>`, a 404 until the report is processed) every 10 seconds until the base has coverage, and while it doesn't, the base's latest `test` check run on GitHub; once that has succeeded (or if there's none), it gives Codecov 2 more minutes, and 10 minutes in all; if it failed, was cancelled or was skipped, no report is coming, so it stops at once.
It never fails the job, only prints a warning annotation when it gives up, as a broken `master` mustn't fail the required `test` check of the PR fixing it.
`codecov.yml` sets `if_not_found: failure` on the project status, so then `codecov/project` fails (it passes by default); re-run the PR's `test` job once the base has uploaded.
That only applies when Codecov knows the base commit but has no report for it; for a commit it has never heard of, Codecov compares with the newest earlier commit on the base branch that passed CI instead (`allow_pseudo_compare`, which despite Codecov's docs affects only its web UI, not the statuses).
`ci.yml` has seven jobs, `test`, `screenshots` and `post-screenshots` (see Screenshots), `dependabot-fix`, `check-fly`, `publish` and `deploy` (see Deployment); `run_workflow_local.py` runs the first job in the file unless given `--job`, so keep `test` first.
Don't run `--job dependabot-fix`, `--job publish` or `--job deploy` locally: it doesn't evaluate `if:`, so it would push commits to the current branch, push to GHCR or deploy to production.

**PR Fixes:** `.github/workflows/fix.yml` (issue #254) is run by hand (`workflow_dispatch`) on an open PR's branch.
A PR comment from the repo owner (`author_association` `OWNER`) that is only `/autofix`, give or take whitespace, also runs it: `.github/workflows/autofix-comment.yml` (issue #261) checks the whole body against `^[[:space:]]*/autofix[[:space:]]*$`, so a comment that merely mentions `/autofix` doesn't, then runs `gh workflow run fix.yml --ref <head branch>` and reacts 🚀 to the comment.
It dispatches rather than giving `fix.yml` an `issue_comment` trigger, as an `issue_comment` run's `github.ref` is always the default branch; the dispatched run is the same as one run by hand, and the comment workflow never checks out the branch (`GITHUB_TOKEN` may trigger `workflow_dispatch`, unlike other events).
It runs only on newly created comments, not edits, and fails for a closed or fork PR.
It fails unless the branch has an open PR from this repo (`gh pr list --head`), merges the PR's base branch in (failing on conflicts), runs `just sync` and `just fix`, commits any changes to tracked files (`git add -u`) as `chore: apply automated fixes`, and pushes if that or the merge moved `HEAD`.
`fmt-templates` reruns djlint with `--check` if `--reformat` fails, as `--reformat` exits 1 whenever it changes a file.
Like `dependabot-fix`, it checks out with `persist-credentials: false`, as `just fix` runs the branch's own `justfile` and dependencies, and only the push step sees its token, the `AUTOFIX_TOKEN` Actions secret (a fine-grained PAT with Contents: read and write; the job fails if it's unset), not `GITHUB_TOKEN`, whose pushes wouldn't trigger CI.
If the branch moves after checkout, the push isn't a fast-forward and fails.

**Whitespace:** `just whitespace-check` (part of `just check`, `just ci` and the `test` job; `scripts/check_whitespace.py`, stdlib-only) checks every file `git ls-files` lists, except ones Git's own detection calls binary (`git ls-files --eol` shows `w/-text`) and symlinks: no trailing whitespace on any line, no leading blank line, and exactly one newline at the end of a non-empty file (none in an empty one).
It checks the working tree copy, so it also catches unstaged edits.
For a Markdown hard line break, end the line with a backslash rather than two trailing spaces.

**Workflow lint:** `just lint-workflows` (part of `just check`, `just ci` and the `test` job) runs `jactionlint --profile correctness` over the workflows, composite actions and Dependabot config.
It uses the `correctness` profile (what actionlint checks); jactionlint's default profile also enables security rules (`unpinned-uses`, `artipacked`, `missing-timeout`, etc.) that the existing workflows don't yet satisfy.

**Git config and `git blame`:** the `justfile`'s top-level `_ := shell('./scripts/git-config-setup.sh')` runs that script whenever `just` runs a recipe (plain `just` included, as it runs `default`; not `just --list`, which evaluates no assignments), so it sets the repo's local `blame.ignoreRevsFile` to `.git-blame-ignore-revs` if it isn't already, printing an `INFO:` line to stderr when it changes it.
Outside a Git checkout (e.g. a `git archive` copy) the script does nothing, rather than failing every recipe.
With Git 2.53 or later it sets `:(optional).git-blame-ignore-revs`, so `git blame` on a checkout from before the file existed doesn't fail with `could not open object name list`; older Git keeps the plain path, as before 2.52 the prefix would be taken as part of the file name, and 2.52.0's blame reads an uninitialised pointer when an optional file is missing.
`.git-blame-ignore-revs` lists commits `git blame` should skip, such as bulk reformatting; add a commit's hash from `master` once it has merged, as a squash merge gives it a new one.
`set minimum-version` makes older `just` versions refuse the `justfile`.
The assignment works only because `just` evaluates every top-level assignment eagerly; under `set lazy` the unused `_` would never be evaluated and the script would never run.
So `just justfile-check` (part of `just check`, `just ci` and the `test` job) fails if `just --json` reports `set lazy` on; it needs `jq`.
`just blame-ignore-revs-check` (part of `just check`, `just ci` and the `test` job; `scripts/check_blame_ignore_revs.py`, stdlib-only) fails if `.git-blame-ignore-revs` is missing, or unless every non-empty, non-comment line in it is a full 40-character lowercase hex hash naming a commit that exists and is an ancestor of its target (so the `test` job checks out the full history).
The target is `HEAD` unless given (`just blame-ignore-revs-check <rev>`); on a PR the `test` job passes the base branch's commit (`github.event.pull_request.base.sha`), so a commit from the PR itself fails the PR's check: with squash merges it's an ancestor of the PR's head but never lands on `master`, so checked against `HEAD` it would pass on the PR and fail only once merged.
Locally it defaults to `HEAD`, so that mistake is caught only in CI unless you pass the base branch.

**Screenshots directory:** `just screenshots-dir-check` (part of `just check`, `just ci` and the `test` job; `scripts/check_screenshots_dir.py`, stdlib-only) fails unless `screenshots/.gitignore` is the only file `git ls-files` lists under `screenshots/`, and its staged and working tree copies both have the contents the script expects.
Without it, `rm -rf screenshots` before a screenshot run, then `git add -A`, commits the PNGs and the `.gitignore`'s deletion (as happened on PR #137).
There's deliberately no escape hatch: a change that really needs to alter `screenshots/.gitignore` updates the script's `EXPECTED` along with it.

**Dependency minimums:** `pyproject.toml`'s dependency lower bounds (`>=`) are kept equal to the versions uv resolved, by `scripts/requirements_min_versions.py`: `just requirements-check` (part of `just ci` and the `test` job) fails if any in `[project].dependencies`, any optional extra or any dependency group differ from the versions in `uv.lock`, and `just requirements-fix` rewrites them; run `uv lock` afterwards.
Dependabot only edits `pyproject.toml` when the new version falls outside the existing constraint, which a `>=` minimum never excludes, so its PRs usually bump only `uv.lock`, even with `versioning-strategy: "increase"`, and would fail that check (see <https://github.com/dependabot/dependabot-core/issues/12788#issuecomment-3606306953>).
The `dependabot-fix` job, which runs only on PRs whose `github.actor` is `dependabot[bot]`, runs `dependabot-relock`, `requirements-fix` and `uv lock` on the PR branch and pushes any change as a `chore: re-lock with pinned uv and sync dependency minimum versions` commit.
Dependabot runs its own uv, which can write `uv.lock` in a newer format than ours (a higher `revision`, simplified markers), changing lines that have nothing to do with the bump; `uv lock` alone never rewrites a lock that's already satisfied.
So `just dependabot-relock origin/<base>` (`scripts/dependabot_relock.py`, stdlib-only) puts back the `uv.lock` the branch started from (at its merge base with the base branch, so the base branch's own later lock changes aren't undone) and re-locks it with our uv, passing `--upgrade-package <name>==<version>` for each package whose locked versions Dependabot changed, then fails, restoring Dependabot's lock, unless the result locks exactly Dependabot's versions (or if `uv lock` fails).
The job checks out with `fetch-depth: 0` so the base branch and the merge base are there.
It pushes with the `DEPENDABOT_FIX_TOKEN` secret (a token with `contents: write` on the repo; it must be a Dependabot secret, as Dependabot runs don't get Actions secrets, and the job fails if it's unset), not `GITHUB_TOKEN`, because pushes made with `GITHUB_TOKEN` don't trigger new workflow runs.
As the branch's bumped dependency isn't trusted, only the final push step sees the token: checkout runs with `persist-credentials: false`, and the job never syncs or installs anything (`requirements-check`/`requirements-fix` and `dependabot-relock` use `uv run --no-project`, as the scripts need only the standard library).
Its `uv lock` runs can still build a dependency's sdist to read its metadata, running that package's build backend, but no step before the push has the token.
`test` waits on it and runs only if it was skipped or pushed nothing; if it pushed, that run's `test` is skipped and the run the push triggers tests the fixed commit.

```bash
# Start the app (sync deps, upgrade legacy DB schema if needed, migrate,
# create superuser, runserver)
./run.sh

# Run all tests (pytest-django + pytest-xdist, parallelised across CPU cores)
uv run pytest

# Run a single test class or method
uv run pytest tests/taxtracker/tests.py::ItemModelTests
uv run pytest tests/taxtracker/tests.py::ItemModelTests::test_str_root

# Run tests under coverage, print a report, and write coverage.xml and junit.xml (no minimum enforced)
just coverage

# Screenshot the admin login, index, backups, cloud backup list, idle watchdog, uptime, uptime day, client IPs and app icon pages, in light and dark mode, into screenshots/ (gitignored)
just screenshots

# Post screenshots/ to PR 123 as a comment, or update it (as gh's logged-in user)
just screenshots-post 123

# Lint (matches `just lint`/`just fmt-check`, which check the whole repo root)
uv run ruff check .
uv run ruff format .

# Template lint
uv run djlint --check src/lifetracker/taxtracker/templates/

# CLI utility scripts (email/PDF/zip helpers, unrelated to the Django app)
uv run lifetracker-cli --help
```

## Architecture

**Stack:** Django 6.x, Python 3.14, SQLite, `uv` for package management.
`pyproject.toml`'s `[tool.uv] required-version` sets the oldest uv allowed (older ones write `uv.lock` in an older format); the `version:` of each `setup-uv` step in `ci.yml` and the `ghcr.io/astral-sh/uv` image tag in the `Dockerfile` pin one exact uv, so raise all three together.
The entire UI is the Django admin — there are no custom non-admin views (except file serving, the `/healthz/` health check, and the web app manifest and app icon), and `/` redirects to `/admin/`.
No frontend framework.

**Package layout:** `src/lifetracker/` is the Django project (settings, urls).
It contains three apps:
- `src/lifetracker/core/` (label `core`) — generic infrastructure any future module can reuse: the file-type registry (`FileType`/`MimeType`/`FileExtension`), DB-backed file storage (`DBStoredFile`/`DatabaseStorage`) and the file-serving admin view, shared attachment behaviour (`core/attachments.py`: filename date/file-type autofill and stored-file cleanup on delete) and `FlexibleDateField` (`core/forms.py`), shared tree behaviour for self-referential models (`core/trees.py`'s `TreeNodeMixin`: pending/done status choices and `is_done`, the `A > B > C` path for `__str__` and `get_folder_path()`, and parent cycle detection for `clean()`), whole-database backups (DB download and cloud backup, `core/backup.py`, with `CloudBackupStatus`), the idle watchdog (`core/idle_watchdog.py`) and the uptime log it writes (`core/uptime_log.py`; see Deployment for both), the client IP record (`core/client_ips.py`; see Client IPs), the JWT cookie for the authenticating front proxy and that proxy's config (`core/jwt_auth.py`, `core/auth_whitelist.py` and `core/envoy_config.py`; see Auth front layer), a client for the Fly Machines API over the Machine's `/.fly/api` socket (`core/fly_api.py`, no Django imports; see Deployment), and `core/admin_pages.css` for custom admin pages.
- `src/lifetracker/taxtracker/` (label `taxtracker`) — the tax-tracking module: `FinancialYear`/`Item`/`Attachment`, archive import, and their admin views/templates.
  This was the original module; more modules (banking, portfolio, insurance, etc.) may be added as siblings of `taxtracker` under `lifetracker` in future.
- `src/lifetracker/tasktracker/` (label `tasktracker`) — the task-tracking module: `TaskType`/`Task`/`TaskAttachment`, with plain admin views (no custom templates).
- `src/lifetracker/cli/` — standalone `click`-based command-line utilities (`.eml`/HAR/ZIP extraction and conversion helpers) unrelated to the Django app; not a Django app, not in `INSTALLED_APPS`.
  Its `fly-api` command wraps `core/fly_api.py`, and `process-dates` uses `taxtracker/financial_year.py`; these are its only imports from the app packages, and neither needs Django.
  Invoked via the `lifetracker-cli` console script (e.g. `uv run lifetracker-cli extract-eml <file> <out.zip>`), an entry point (`[project.scripts]` in `pyproject.toml`) generated by `uv sync` from the editable-installed `lifetracker` package.

`taxtracker` and `tasktracker` depend on `core` (via their attachments' `file_type` FK and `DatabaseStorage`); `core` depends on neither, and they don't depend on each other.

Tests live outside `src/`, in a top-level `tests/` tree mirroring the package layout (`tests/core/tests.py`, `tests/taxtracker/tests.py`, `tests/tasktracker/tests.py`, `tests/cli/tests.py`), each importing the code under test via absolute `lifetracker.*` imports.
Envoy's tests are in `tests/core/test_envoy.py` (see Auth front layer).
The exception is `tests/scripts/tests.py`, which tests the standalone scripts in `scripts/` via `scripts.*` imports, e.g. `from scripts.requirements_min_versions import main`.
`scripts/` has no `__init__.py`; it resolves as an implicit namespace package from the repo root, which both pytest and `manage.py test` put on `sys.path`.
(Importing by bare module name with `scripts` on `pytest`'s `pythonpath` also works at runtime, but Pylance can't resolve it.)
Coverage measures `scripts` alongside `src/lifetracker`.
Run via `pytest` (using `pytest-django`, configured in `pyproject.toml`'s `[tool.pytest.ini_options]` with `testpaths = ["tests"]`), parallelised across CPU cores by `pytest-xdist` (`-n auto`, set as a default in `addopts`).
`manage.py test tests.core tests.taxtracker tests.tasktracker tests.cli tests.scripts` still works as an unparallelised fallback, referencing these as `tests.*` labels rather than the `lifetracker.*` app labels.

**Screenshots:** `screenshot_tests/` holds Playwright tests that drive a headless Chromium against pytest-django's `live_server` (empty test DB) and save full-page PNGs to the gitignored `screenshots/` directory (each screen as `<name>-light.png` and `<name>-dark.png`), for eyeballing UI changes.
`just screenshots` sets `LIFETRACKER_COMMIT_ID` to `git rev-parse HEAD` unless it's already set, so the admin header shows the commit; the `screenshots` CI job sets it to the PR's head commit (`github.event.pull_request.head.sha`, falling back to `github.sha` off PRs), since on a PR `actions/checkout` checks out a merge commit.
The tests are run by `just screenshots` (which also runs `playwright install chromium`): they're outside `testpaths`, so `pytest`, `just coverage` and the `test` job never collect or measure them, and Playwright is in its own `screenshots` dependency group, which only the `screenshots` CI job installs.
That job (which first runs `playwright install chromium` as a step of its own, without `--with-deps`, as the runner image already has Chromium's system libraries and installing them with apt was sometimes slowed to minutes by the Ubuntu mirror) runs `just screenshots` and uploads `screenshots/*.png` as a `screenshots` workflow artifact (kept 14 days), downloadable from the run's summary page.
Like `test`, it waits on `dependabot-fix`.
They assert nothing beyond the pages loading.
On a PR, once it succeeds, `post-screenshots` downloads that artifact and runs `just screenshots-post` (`scripts/post_screenshots.py`, stdlib-only, run with `uv run --no-project`).
It uploads each PNG as a user attachment, then writes one `github-actions[bot]` PR comment embedding them, editing the previous one in place if there is one (found by a hidden `<!-- lifetracker-screenshots -->` marker and the `--author` login).
Given `--commit` (CI passes the PR's head commit), it first checks that commit is still the PR's head, and otherwise posts nothing and exits 0: the job's per-PR `concurrency` group only stops two posts running at once, not an older commit's slow run posting after a newer one's, which would overwrite newer screenshots with stale ones.
The upload uses the undocumented `POST https://uploads.github.com/user-attachments/assets` endpoint, sending the same request `gh ... --attach` does (cli/cli `internal/attachments/client.go`), which only accepts user tokens, not installation tokens such as `GITHUB_TOKEN`.
So uploads use the `SCREENSHOTS_TOKEN` Actions secret, a fine-grained PAT for this repo with only Pull requests: read and write (the job fails if it's unset), and the comment uses `GITHUB_TOKEN` with `pull-requests: write`.
Locally both tokens default to `gh auth token`.
It's skipped for fork PRs and Dependabot, which don't get the secret; it runs the PR branch's script but never syncs or installs the project, so a bumped dependency never runs with the tokens.

**File storage:** All uploaded attachments are stored in the SQLite database, not on disk.
When a row of any model with a `DatabaseStorage` `FileField` is deleted, `core.attachments.delete_stored_files` deletes its `DBStoredFile` too: a `post_delete` receiver, which `CoreConfig.ready()` connects to each such model (not to every sender, which would turn off fast bulk deletes everywhere), so cascades and `QuerySet.delete()` (e.g. the admin's "Delete selected") clean up as well, which a `delete()` override wouldn't.
Rows deleted before that receiver existed left their `DBStoredFile`s behind: core's `purge_orphaned_files` management command reports those that no such field on any model refers to (`core.attachments.orphaned_stored_file_pks`, which finds the fields via `database_storage_file_fields`, so core needn't know the modules) with their count and total size, and deletes them only with `--delete`.
`DatabaseStorage` (in `core/models.py`) is a custom Django storage backend that writes file content to `DBStoredFile` rows.
Storage paths have the form `db/<pk>/<filename>`.
`get_available_name` shortens a long filename, keeping its extension, so the whole path fits the `FileField`'s `max_length` even with the longest `BigAutoField` pk (19 digits), so with the default 100 a filename keeps at most 77 characters.
Files are served through `DBStoredFileAdmin.serve_file_view` at `/admin/core/dbstoredfile/file/<pk>/`.
A superuser can open any file; anyone else needs view permission (the model admin's `has_view_permission`) on a row of a registered model whose `DatabaseStorage` field holds `db/<pk>/...`, so orphaned files and those of unregistered models such as `AppIcon` are superuser-only (issue #148).
It guesses the type from the filename (`application/octet-stream` if it can't, or if the name gives an encoding such as `.gz`) and shows only `INLINE_CONTENT_TYPES` (PDF, common raster images, plain text) inline; anything else, HTML and SVG included, is sent as an `attachment` download, so an attached file can't run script on the admin origin with the viewer's session (issue #140).
Every response has `X-Content-Type-Options: nosniff`, and all but PDFs `Content-Security-Policy: sandbox; default-src 'none'`; PDFs are left out because browsers' built-in PDF viewers won't run in a sandboxed document.

**Logging:** `settings.LOGGING` sends the `django` and `lifetracker` loggers (INFO and up) to one stderr `StreamHandler`, whatever `DEBUG` is, so production 500s' tracebacks and the app's own `logging` calls reach the container log and `fly logs`.
Django's default config prints only while `DEBUG` is on and otherwise mails `ADMINS`, which is unset here.
Get module loggers with `logging.getLogger(__name__)`, so they fall under `lifetracker`.
Lines are `<asctime> <LEVEL> <logger>: <message>`; `LIFETRACKER_LOG_TIMESTAMPS=0` (set in `fly.toml`, as Fly stamps each line itself) drops the timestamp.
Configuring `django` resets Django's default `django.server` logger (`runserver`'s coloured request log), so `LOGGING` also re-declares it as Django's defaults do, with its own `ServerFormatter` handler and `propagate: False`.
Gunicorn's access log (`--access-logfile -` in `docker-entrypoint.sh`) is separate: `lifetracker/gunicorn_conf.py` sets `access_log_format` to gunicorn's default with `%({fly-client-ip}i)s` in front, the client's IP as Fly Proxy sets it in the `Fly-Client-IP` header (`-` without it, e.g. locally).
The default's own `%(h)s` is the connection's peer, which in the container is always Envoy (`127.0.0.1`; see Deployment); gunicorn never takes it from `X-Forwarded-For`, and `forwarded_allow_ips` only affects the scheme.
Django's `REMOTE_ADDR` is `127.0.0.1` too; nothing reads it yet, so nothing rewrites it from `Fly-Client-IP`.
Envoy passes `Fly-Client-IP` through untouched, and its own access log, which covers only requests with a response flag (ones Envoy answered itself or failed to pass on, e.g. `UF` when gunicorn refused the connection) and ones `jwt_authn` turned away (found by the `jwt_failure` key its `failed_status_in_metadata` writes, as it sets no flag; the reason is in `%RESPONSE_CODE_DETAILS%`), so not duplicating gunicorn's, starts `envoy: ` and the same client IP.
Envoy also logs every request as JSON to a file, for the client IP record (see Client IPs).

**Auth front layer:** issue #176 puts an authenticating proxy (Envoy) in front of gunicorn, rejecting any request without a valid Django-issued JWT, except a small whitelist.
Envoy is in the container (#178; see Deployment), Django issues the JWT (#177) and Envoy checks it (#179).
`core/jwt_auth.py`'s `JWTCookieMiddleware` (after `AuthenticationMiddleware` and before `MessageMiddleware` in `MIDDLEWARE`, so its response handling comes after anything else that can change the session) sets the `lifetracker_jwt` cookie (HttpOnly, SameSite=Lax, path `/`, Secure and `max_age` like the session cookie) on any response to a logged-in session, so password and Google SSO logins are both covered, and deletes it from a response to a request that sent one without a logged-in session.
The token is EdDSA (Ed25519, PyJWT), with claims `iss` (`lifetracker`) and `aud` (`lifetracker-web`; tokens for any future API or MCP clients are to get their own audience, so neither can stand in for the other), `sub` (the user's pk), `sid` (`session_id_claim()`: a SHA-256 `salted_hmac` of the session key, so the cookie doesn't reveal it), `iat`, `jti`, and `exp` equal to `request.session.get_expiry_date()`.
Django moves a session's expiry forward only when it saves the session, so the token is re-issued whenever the session is modified (logging in, which cycles the session key, included; or on every request with `SESSION_SAVE_EVERY_REQUEST`) or its user changes, and never expires before the session does.
On every request, a logged-in session that has been given a token has its JWT cookie checked by `token_problem()`: signature (against the current key), `exp`, `iss`, `aud`, and `sub` and `sid` matching the session; a missing or failing cookie logs the session out with a warning naming the reason, so the admin sends it to the login page, and this binds the token to the session even if the proxy is bypassed.
The session stores only `{"sub"}` under `jwt_auth.SESSION_KEY`, recording that it's been given a token, never the token or its hash: the session is saved whole, last writer wins, so with stored hashes two overlapping requests that both re-issue could leave the browser holding a token the session no longer knew (as the first version of #196 did); now every token issued for the session stays valid until its `exp`.
That includes whitelisted requests, unlike the issue's sketch: the admin redirects a logged-in visitor from the login page to the index, so a session the proxy rejects but Django kept would loop between them.
A session with no `SESSION_KEY` record (e.g. from before this existed) isn't checked and gets a token on that request, so deploying this logs no one out.
Nothing rotates the signing key; a new one appears only if the private key file is missing at startup, and then every existing token fails its signature check, logging every session out once.
No token is issued on a 500, as `SessionMiddleware` wouldn't save the session.
The private key is created on first use (`signing_key()`, cached per process; linked into place so concurrent workers can't overwrite each other's) as `LIFETRACKER_JWT_PRIVATE_KEY_FILE` (default `jwt_private_key.pem` in the config directory, mode 0600), and the public key written as a one-key JWKS (`kid` is its RFC 7638 thumbprint) to `LIFETRACKER_JWT_JWKS_FILE` (default `jwt_jwks.json` there, mode 0644) for the proxy; the JWKS is only rewritten if its contents differ, so a read-only config directory works once both exist.
The `ensure_jwt_key` command (core) creates them; `run.sh` runs it after `ensure_superuser`, `docker-entrypoint.sh` too when given arguments (otherwise `envoy_config` does the same), and `just docker-run` runs it on the host, as the container's `/config` is read-only.
`core/auth_whitelist.py`'s `auth_whitelist()` is the one list of requests that need no login, for the proxy: the admin login (GET/HEAD/POST), logout (POST), the Google SSO start and callback (GET/HEAD), by exact path from `reverse()`, everything under `STATIC_URL` (GET/HEAD; WhiteNoise serves those before the middleware anyway), the health check, `/healthz/` (GET/HEAD), the web app manifest, `/manifest.webmanifest` (GET/HEAD), and the uploaded app icon, `/app-icon.png` (GET/HEAD).
The `auth_whitelist` command prints the list as JSON.
Envoy's config (`envoy/envoy.yaml`) is a template that `core/envoy_config.py`'s `render()` fills in, via the `envoy_config` command (core), which `docker-entrypoint.sh` runs before starting Envoy: each `@NAME@` placeholder becomes a JSON value (valid YAML, so nothing needs escaping), and an unknown one is an error.
Besides the listen address and gunicorn's port, it fills in from Django the JWT's issuer, audience and cookie name (`jwt_auth`'s constants), the JWKS path (`LIFETRACKER_JWT_JWKS_FILE`), the login redirect (`reverse("admin:login")`) and `jwt_rules()`, `jwt_authn`'s rules: one per whitelist entry and method (exact `:method` header, `path` or `prefix`), needing no JWT, then `prefix: "/"` (the route's own match, so every request that reaches gunicorn matched a rule) requiring the `django` provider.
So the whitelist can't drift between Django and Envoy.
The `envoy_config` command also calls `signing_key()`, so the JWKS exists before Envoy reads it, which it does only at startup (`local_jwks` `filename`).
The provider takes the token from the `lifetracker_jwt` cookie or `Authorization: Bearer` and checks its signature, `exp` (60 seconds' leeway, `jwt_authn`'s default), `iss` and `aud`, with `forward: true`; not `sub` or `sid`, which only Django can check.
Any invalid token present fails the request, even with a valid one in the other place; of duplicate cookies, Envoy checks only the first (Django uses the last, and logs out a session whose cookie doesn't match).
A whitelisted request's token isn't checked at all, so a stale cookie can't keep anyone from the login page.
The connection manager normalises the path before the filters and the route see it, and gunicorn gets the result: `normalize_path` (dot segments, including `%2e`, removed; percent-encoded unreserved characters decoded; a `\` becomes `/`), `merge_slashes`, and `path_with_escaped_slashes_action: REJECT_REQUEST` (a 400 for `%2F` or `%5C`, which Django would decode into a separator), so no spelling of a path matches a whitelist entry but reaches a different view.
`jwt_authn` answers a missing or invalid token with a 401 (a 403 for a token with the wrong audience); `local_reply_config` turns either into a 302 to `/admin/login/?next=<path and query>`, as Django's admin redirects, unless the request has an `Authorization` header (not a browser), which keeps the 401/403.
Local reply mappers only apply to replies Envoy generates itself, never gunicorn's.
Envoy's formatters can't percent-encode, so a Lua filter before `jwt_authn` puts the encoded `:path` (all but unreserved characters and `/`, like `redirect_to_login`) in the `lifetracker` dynamic metadata's `next`, which the redirect's `Location` reads with `%DYNAMIC_METADATA(lifetracker:next)%`.
`tests/core/test_envoy.py` unit-tests `render()` and the command, and `EnvoyAuthTests` runs the real Envoy with the rendered config in front of a fake upstream that echoes each request: no, invalid (garbage, expired, wrong audience/issuer/key, unsigned) and valid JWTs in the cookie and in `Authorization`, every whitelist entry, other methods on whitelisted paths, near-misses, and normalisation tricks (`..`, `%2e%2e`, `//`, `\`, `%2F`, `%5C`).
It runs the `envoy` binary at `$LIFETRACKER_ENVOY` if that's set, else the Envoy image the `Dockerfile` names (its `FROM envoyproxy/envoy:... AS envoy` line, so no drift; pulled first if missing), with `docker run --network host`, so `127.0.0.1` is the same inside the container and out on Linux and macOS alike, with the test's temporary directory (config and JWKS) mounted read-only at the same path and Envoy running as the test's own UID/GID so it can read it.
On macOS that needs Docker Desktop's host networking (Settings → Resources → Network, 4.34 or later), without which Envoy never answers and the test says to check it.
It waits for a request to get through to the upstream rather than just for the port to open, then `docker rm -f`s the container afterwards.
It's skipped without Docker (no `docker` command, or `docker info` failing) unless `LIFETRACKER_REQUIRE_ENVOY` is set, as the `test` job's `just coverage` step sets it.
`just docker-smoke` checks the built image's Envoy: `/healthz/` answers without a login, and a request for a page that doesn't exist gets Envoy's redirect (Django would 404).
`tests/conftest.py` has an autouse fixture pointing both key file settings at a per-process temporary directory, so tests never write keys to the real config directory.

**Legacy database upgrade:** databases created before the `lifetracker`/`core`/`taxtracker` split still have the old `tracker_*` tables and `django_migrations`/`django_content_type` rows under app label `tracker`.
`taxtracker`'s `upgrade_legacy_db` management command renames those tables and re-labels the content types in place; it is idempotent (no-op on fresh or already-upgraded databases) and `run.sh` runs it unconditionally before `migrate`.

**Client IPs:** issue #201 keeps a lasting record of where requests come from, as `fly logs` keeps only a short window; alerting on it (e.g. a logged-in request from a new ASN) is #213, after #212 builds a way to send alerts.
Envoy sees every request, including those `jwt_authn` turns away, which never reach gunicorn, so a second access log in `envoy/envoy.yaml` (a file logger with `json_format`) writes one JSON line per request to `@REQUEST_LOG@`: start time, `Fly-Client-IP`, the direct peer, method, path, status, user agent, `jwt_failure` (`failed_status_in_metadata`) and `jwt_sub` (the `sub` from `payload_in_metadata: jwt_payload`), each missing value `null`.
The `envoy_config` command fills in `client_ips.config_from_env().request_log` (default `$TMPDIR/lifetracker-envoy-requests.jsonl`, `LIFETRACKER_CLIENT_IP_REQUEST_LOG`), deleting it first, or `/dev/null` if recording is off; Envoy restarting under the entrypoint's loop keeps appending to it.
Envoy flushes file access logs every 10 seconds (its default).
`core/client_ips.py` (no Django imports, like the idle watchdog) is a collector process that `gunicorn_conf.when_ready` starts with `client_ips.spawn` alongside the watchdog, and which exits when its parent stops being the master.
Every 5 seconds it reads the complete lines added since last time (`RequestLogReader`) and records them; requests it couldn't record (e.g. a full disk) stay pending in memory, newest `MAX_PENDING_REQUESTS` (100,000) kept, and are retried with the next batch, as the reader has already moved past them.
Once it has read the whole file and that's at least 1 MiB, it truncates it (Envoy appends, so carries on at offset 0; a line written in between is lost, which is accepted).
`parse_line` takes the IP from `Fly-Client-IP` if it's a valid IP address, else the peer (e.g. under `just docker-run`); nothing trusts it beyond this record.
`record` adds each batch up per IP in one `BEGIN IMMEDIATE` transaction in its own SQLite file, `LIFETRACKER_CLIENT_IP_DB` (default `$LIFETRACKER_DATA_DIR/client_ips.sqlite3`, unset without `LIFETRACKER_DATA_DIR`; `off` turns it off), deliberately not the main database: table `client_ip`, one row per IP with first/last seen (POSIX times), request, rejected (`jwt_failure` set) and logged-in (`jwt_sub` set: a valid JWT, though only Django checks it against the session) counts, first/last logged-in times, the latest request line, status and user agent (cut to 500 characters), country, country name, ASN and AS organisation, and whether it's an iCloud Private Relay egress address (`private_relay`: 1, 0, or NULL if not checked yet) with the relay's country, region and city; WAL mode, `user_version` 2.
`connect` adds version 2's relay columns to an older table with `ALTER TABLE` (NULL, so not checked yet).
A row's location is looked up when first seen and kept; rows without one are looked up again when next seen, and by `fill_in_locations`.
Its Private Relay columns are rechecked every time it's seen (so they're as of when last seen; kept as they were if there are no ranges to check against), and rows not checked yet, including those recorded before this existed, are checked by `fill_in_relay`.
Locations come from DB-IP's Lite country and ASN `.mmdb` files (`maxminddb`), chosen over MaxMind GeoLite2 as they need no account or licence key; no city database, as it's over 100 MB and ASN is the more useful signal.
`update_geoip` keeps `<name>-<YYYY-MM>.mmdb` for each in `LIFETRACKER_GEOIP_DIR` (default `geoip` beside the record): it downloads this month's (`https://download.db-ip.com/free/<name>-<YYYY-MM>.mmdb.gz`, via `requests`) unless it's there, falling back to last month's on a 404, checks it opens before renaming it into place, and deletes older months'.
It tries each database even if the other fails, catching any exception (a cut-off download can raise `EOFError` or `zlib.error`, not just `requests`' and `maxminddb`'s errors), and writes each one's outcome to `status.json` in the same directory (atomically, with `idle_watchdog.write_status`; read back by `read_geoip_status`): attempt time, last URL tried, `downloaded`, `up to date` or `failed`, whether it fell back to last month's, the file in use, the last success (kept from the previous file across failures), and a failure's `repr` and traceback.
The collector runs it, then `fill_in_locations` and `fill_in_relay`, at startup unless `LIFETRACKER_GEOIP_DOWNLOAD=0`, logging each failure, and any exception it raises (only `OSError`, from creating the directory or writing the status file), rather than exiting, as nothing would restart it; so in production it's refreshed at the first boot each month once that month's files are out, and until then each boot costs one 404 per database (it isn't fetched in the Dockerfile, which would make builds depend on the date).
Core's `update_geoip` management command does the same by hand, failing (after filling in locations and checking IPs against the relay ranges) if a download did.
Core's `client_ips_export` management command writes every row to stdout as JSON Lines (`export_rows`: `SELECT *` in the table's column order, so columns a later schema adds come last, first seen first; `TIME_COLUMNS` as ISO 8601 UTC with milliseconds and `Z`), only those with logged-in requests with `--logged-in`, reading the record read-only; it's the machine-readable export until a REST API (#46) exists.
`GeoIP` opens the newest file for each and reopens when a newer one appears.
`update_geoip` also downloads Apple's iCloud Private Relay egress ranges (`RELAY_RANGES_URL`, `https://mask-api.icloud.com/egress-ip-ranges.csv`: an RFC 8805 geofeed, `prefix,country,region,city,`, about 12 MB and 285,000 prefixes in October 2026, mostly IPv6 /64s), recorded in `status.json` as `icloud-private-relay`.
Apple's server answers `If-None-Match` with a 304, so it sends the ETag (kept in the status, and only while the file it came with is there) and the list is checked at every boot but only downloaded when it changes.
`build_relay_ranges` streams it into a new SQLite file, `icloud-private-relay.sqlite3` in the GeoIP directory (about 18 MB; table `relay_range`, each range's first and last address as 16-byte big-endian blobs, IPv4 as IPv4-mapped IPv6, so they sort as addresses do), skipping lines that aren't prefixes, failing if none are, and renaming it into place; it takes about 3 seconds.
`RelayRanges` (owned by `GeoIP`, as `GeoIP.relay`) opens it read-only and reopens it when it's replaced, and finds an IP's range with one indexed query (the last range starting at or before it, then a check of its end; Apple's ranges don't overlap).
Tests never download it: `tests/conftest.py` replaces `_download_relay_ranges` with one that always reports the list unchanged.
Nothing prunes `client_ip`: each new IP adds a row of a few hundred bytes, which is fine for this app's traffic, but a scanner rotating through an IPv6 range could grow it; add pruning (e.g. rows with no logged-in requests after N days) if it ever matters.
`just docker-smoke` passes `LIFETRACKER_GEOIP_DOWNLOAD=0` (so CI doesn't download from db-ip.com every run) and waits for the smoke test's requests to show up in the record.
`tests/core/mmdb_writer.py` writes small test databases, for `tests/core/test_client_ips.py` and the screenshot tests, and `EnvoyAuthTests.test_request_log` checks the real Envoy's lines parse as expected.

## Models

- **`FinancialYear`** (taxtracker) — Australian FY ending 30 June of `year`.
  E.g. `year=2024` = 1 Jul 2023–30 Jun 2024.
  `taxtracker/financial_year.py`'s `financial_year_for_date` (no Django imports, so the CLI uses it too) gives a date's `year`; use it rather than repeating the 1 July rule.
  Default lodgement date is 31 Oct; overridable.
- **`Item`** (taxtracker) — tax checklist item, self-referential `parent` (tree).
  Child items always inherit their parent's `year_id`, enforced in both `clean()` and `save()`.
  Whenever an item's year changes, whether a root's year is edited or the item is re-parented under another year's item, `save()` moves all its descendants to the new year too.
  Its tree behaviour (path, status, cycle check) comes from `core.trees.TreeNodeMixin`, shared with `Task`.
- **`FileType` / `MimeType` / `FileExtension`** (core) — file type registry.
  Each `FileType` must have exactly one primary MIME type and one primary extension; enforced by `_AtLeastOnePrimaryFormSet`.
- **`DBStoredFile`** (core) — raw binary storage row; managed by `DatabaseStorage`.
- **`Attachment`** (taxtracker) — file attached to exactly one of an `Item` or a `ReceivedDocument`.
  On `save()`, `core.attachments.AttachmentAutofillMixin` auto-populates a blank `title` from the filename, `file_type` by extension lookup (`file_type_for_filename`), and `date` by regex-extracting ISO/compact dates from the filename (`extract_date_from_filename`).
- **`ReceivedDocument`** (taxtracker) — a document received back from the tax agent/office for a `FinancialYear`, with its own attachments.
- **`CloudBackupStatus`** (core) — single row (`CLOUD_BACKUP_STATUS_PK`) recording the time, S3 object key and size (bytes) of the last successful cloud backup.
  Moved from taxtracker: core's `0002` creates its table and taxtracker's `0004` copies the row across and drops the old one (and the reverse).
- **`AppIcon`** (core) — single row (`APP_ICON_PK`) holding the uploaded app icon (a `DatabaseStorage` file), its size and SHA-256; no row means the default icon (see Admin extensions).
- **`TaskType`** (tasktracker) — kind of task, optionally restricted to linking to one model (`content_type`).
  The built-in "Generic" type (`TaskType.GENERIC_PK`, created by a data migration) is the default, can't be renamed, linked to a model or deleted.
- **`Task`** (tasktracker) — pending/done task, self-referential `parent` (tree), optionally linked to one object via a generic FK (`content_type`/`object_id`); `clean()` only allows a link of its task type's model, and rejects parent cycles (via `TreeNodeMixin`, as for `Item`).
- **`TaskAttachment`** (tasktracker) — file attached to a `Task`, stored via `DatabaseStorage`.
  It uses the same `AttachmentAutofillMixin` as `Attachment`, and its admin form (`TaskAttachmentForm`) the same `core.forms.FlexibleDateField`.

## Admin extensions

`admin.site` is a `LifetrackerAdminSite` (`src/lifetracker/admin_site.py`), installed by listing `lifetracker.apps.LifetrackerAdminConfig` in `INSTALLED_APPS` in place of `django.contrib.admin`.
Its `each_context` builds the admin header (via `_build_admin_site_header`) and `site_title` per request, since they depend on whether the visitor is logged in; don't set them on `admin.site`, which is shared across requests.
The header shows the site name, the running commit and the instance name: `LIFETRACKER_COMMIT_ID` as a short ID, linking to `LIFETRACKER_COMMIT_URL_BASE` (default: this repo's GitHub commit URL) only for logged-in users, and `LIFETRACKER_BACKUP_HOSTNAME` (see the backup pages below).
The site name (header and page titles) is "Life Tracker" for logged-in users, else `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` (default also "Life Tracker").
`run.sh` sets the commit ID from `git rev-parse HEAD`; `just docker-build` (which CI uses) bakes it into the image as a build arg.
With no commit ID, only the instance name is shown.

`CloudBackupStatusAdmin` (core) hosts the backup pages, which cover the whole database rather than one module.
It replaces the model's usual admin URLs with these, all superuser-only (`has_module_permission`/`has_view_permission` are superuser-only too, so only superusers see its **Backups** entry under Core on the admin index).
The logic lives in `core/backup.py` as plain functions (`run_cloud_backup`, `list_cloud_backups`, `backup_database`, etc.), not admin methods, so e.g. scheduled backups can reuse it:
- **Backups** (`/admin/core/cloudbackupstatus/`, the changelist URL) — cloud backup status page with a "Run Backup Now" button that uploads a copy of the SQLite DB to an S3-compatible bucket (via `boto3`'s `upload_fileobj`, which streams it, in parts once it's big enough) as `<hostname>/db-backup.<YYYY-MM-DD.HH.MM.SS>.sqlite3`, where `<hostname>` is `LIFETRACKER_BACKUP_HOSTNAME` if set, else `socket.gethostname()` (`just docker-run` sets it to `docker.$(hostname)`, as the container's DB is separate from local dev's and its own hostname is a random ID); the page shows it.
  The `cloud_backup` management command (core) does the same from the command line via the same `run_cloud_backup`, printing the object key, and raises `CommandError` on the same failures (`CLOUD_BACKUP_ERRORS`, shared with the admin views), including when the upload succeeded but recording `CloudBackupStatus` didn't.
  With `--if-changed` (as the automatic backups run it; see Deployment) it uploads nothing, and succeeds, unless `backup.database_changed()`: after each backup, `run_cloud_backup` runs `PRAGMA wal_checkpoint(TRUNCATE)` and records the database file's device, inode, size and `st_mtime_ns` (`database_signature()`, None unless the `-wal` file is missing or empty), with the bucket, endpoint and hostname, in `LIFETRACKER_BACKUP_STATE_FILE` (default `<database>-backup-state.json`, so `/data/db.sqlite3-backup-state.json` on Fly).
  A later commit is then either in the log or, once checkpointed, has changed the file's modification time, and reads change neither (checked: the boot's `migrate`, `ensure_superuser` and `ensure_jwt_key` don't count either); the backup's own `CloudBackupStatus` write comes before the checkpoint, so it doesn't count.
  A commit by another connection during the backup (seen as `PRAGMA data_version` on Django's connection changing between the start and after the signature is taken) might not be in the backup, so then the state file is removed rather than written, and the next `--if-changed` backs up; so does an in-memory database (the tests'), which has no signature.
  The `restore_cloud_backup <key>` command (core) restores one: `download_cloud_backup` to a temporary file, `check_backup_file` (`PRAGMA quick_check` and a `django_migrations` table), a typed `yes` unless `--no-input`, `run_cloud_backup` of the current database (failing before any change if any of these fail), then `restore_database`, then `migrate`; it puts back the `CloudBackupStatus` row as it was just before the restore, so it names that backup rather than the restored database's last one.
  `restore_database` copies the file into the live database with SQLite's online backup API through Django's connection, all in one step (`pages=-1`), so it's one write transaction: concurrent requests' writes wait for it (up to `DATABASES`' 30-second `timeout`) and readers see the old database or the new, never a mix; SQLite keeps the `-wal`/`-shm` files consistent and the database in WAL mode, so the web app needn't be stopped or locked out.
  It refuses a backup whose page size differs (SQLite can't change a WAL database's page size, and would fail with "attempt to write a readonly database"), and, like `backup_database()`, to run inside a transaction; it closes Django's connection afterwards.
  Its tests copy into a file-backed connection (`RestoreDatabaseTests`), as copying over the in-memory test database would destroy it, and the command's tests replace it.
  Manual only; nothing schedules it.
  Configured by the `LIFETRACKER_BACKUP_S3_*` env vars read in `settings.py`: `BUCKET_NAME`, `ACCESS_KEY_ID` and `SECRET_ACCESS_KEY` are required (the button is disabled and the missing names are listed until they're set); `ENDPOINT_URL` (blank = AWS) and `REGION_NAME` are optional.
  Locally, these normally go in `.env` (see below).
- **DB backup** (`download-db-backup/`) — download of the same copy (a `FileResponse`), linked from the backups page.
- **Cloud backup list** (`list/`) — linked from the backups page: lists the bucket's objects (`list_objects_v2`, paginated) with key, size and last-modified time (shown in UTC), newest first.
  The `prefix` query parameter picks what to list: absent means this instance's `<hostname>/`, empty means the whole bucket; links to each top-level folder (one per hostname, from a `Delimiter="/"` listing) let you switch.
  Missing settings and S3 errors are shown on the page.
  Both cloud backup pages get their client from `cloud_backup_s3_client`.

The cloud backup and the DB backup download both get their copy from `backup_database()`, which uses SQLite's online backup API (`sqlite3.Connection.backup()`) to write it to a temporary file and returns that file open, already unlinked, rather than `serialize()`, which would hold the whole database (every attachment included) in memory on a Machine with 512 MB.
The temporary directory (`/tmp`, or `$TMPDIR`; on Fly, the Machine's root filesystem, not the `/data` volume) needs room for one full copy of the database while a backup runs; if it fills, SQLite's `sqlite3.OperationalError` (in `CLOUD_BACKUP_ERRORS`, with `OSError`) is reported as a failed backup, not a 500.
It raises `RuntimeError` inside a transaction (`in_atomic_block`), since SQLite won't back up from a connection that's writing and `backup()` would retry forever; so tests that run a backup are `TransactionTestCase`s, not `TestCase`s.

The superuser-only **Idle Watchdog** page (`/admin/idle-watchdog/`, URL name `admin:idle_watchdog`) belongs to no model, so it has no admin index entry; instead `LifetrackerAdminSite.get_urls` serves `core.admin.idle_watchdog_view` there, and `src/lifetracker/templates/admin/base_site.html` (in `TEMPLATES`' `DIRS`, so ahead of the admin's own templates, which it extends) links it from the top bar for superusers, before "View site".
Add any further model-less pages the same way, rather than giving them a tableless model; if the links grow numerous, make them a menu.
`idle_watchdog_page_context` (in `core/admin.py`) builds the page from `idle_watchdog.config_from_env()` (the web process has the watchdog's environment), the status file (`idle_watchdog.read_status`), the request file's modification time (`last_request_time`, read directly, as the status file's copy is only as fresh as the last check; issue #172) and `/proc/uptime` (`system_uptime`).
It shows durations to the second (`_format_duration`), since `timesince` rounds anything under a minute to "0 minutes".
It warns that the watchdog has probably stopped when its last check, or its start if it hasn't checked yet, is more than three check intervals old.
Invalid settings, an unreadable status file and unexpected contents are shown as errors on the page.
Times are in the site's time zone.

The superuser-only **Uptime** page (`/admin/uptime/`, URL name `admin:uptime_report`) is served and linked the same way, by `core.admin.uptime_report_view`.
It shows the same report as the `uptime_report` command, newest day first, as both get it from `uptime_log.daily_report` (a list of `DayUptime`); its `days` query parameter (default 7, at most 366) is the command's `--days`, and an invalid one is reported on the page and the default used.
Dates are UTC days, as the files are.
Each day's Starts count (unless it's "No data") links to that day's drill-down page (`/admin/uptime/<YYYY-MM-DD>/`, URL name `admin:uptime_day`, `core.admin.uptime_day_view`; not in the top bar).
It lists the day's up periods (`uptime_log.day_periods`, from `up_periods`, one `UpPeriod` per start) with start, last second up and duration (all `HH:MM:SS`), noting a period that began the day before (up at midnight, and the previous day's last second up), continued into the next day, or may still be going (today's, if it ended within `MAX_FILLED_GAP_SECONDS` of now).
An invalid date or one after today is a 404; the page keeps the report's `days` for its links back and to the days either side.
Both pages also show times in the browser's time zone: the drill-down each period's start and last second up (after the UTC time, as e.g. `(Fri 02:59 local)`) and the UTC day's span, the report page when today's UTC day began.
The pages put UTC ISO 8601 times in `<time data-lt-local datetime="...Z">` elements, and `core/static/core/local_times.js` (loaded from the templates' `extrahead`) rewrites each as `YYYY-MM-DD HH:MM` local time, without seconds as they match UTC's (or `Fri 02:59` with `data-lt-local="weekday"`), fills each `[data-lt-time-zone]` with the zone's IANA name and unhides each `[data-lt-local-only]` element, which the page renders `hidden` so nothing shows empty without JavaScript.
The screenshot tests set the browser's time zone to `Australia/Sydney`, so their local times differ from UTC.

The superuser-only **Client IPs** page (`/admin/client-ips/`, URL name `admin:client_ips`) is served and linked the same way, by `core.admin.client_ips_view`, reading the client IP record read-only (`client_ips.connect_read_only`): a table of the networks (ASNs) requests with a valid JWT came from (`logged_in_networks`), then the `_CLIENT_IPS_LIMIT` (500) IPs seen most recently (`recent_ips`), or only those with logged-in requests with `?logged_in=1`.
Each network's row counts its Private Relay IPs, and each IP's network cell marks it **iCloud Private Relay**, with the city and region Apple's list gives (`relay_location`).
Above them, a table from `status.json` shows each DB-IP database's, and the relay ranges', last download attempt and success, its outcome and URL, and a failure's traceback in a collapsed `<details>`; an unreadable file or unexpected contents is an error note.
Times are UTC in the HTML, shown in the browser's time zone by `local_times.js`.
An IP can wrap after each colon (`_client_ip_row` adds `<wbr>`), so a 39-character IPv6 address doesn't widen the table.
It links db-ip.com, as DB-IP Lite's CC BY 4.0 licence asks of anything showing its data.

Every admin page's head links the web app manifest (`/manifest.webmanifest`, URL name `web_manifest`), which `core/web_manifest.py`'s `web_manifest` view generates, so "Add to Home Screen" on iOS opens the admin as a standalone app.
It's on the auth whitelist, as browsers fetch it without cookies, so it always names the site `LIFETRACKER_UNAUTHENTICATED_SITE_NAME`, whoever asks; that's why it's a view rather than a static file.
`base_site.html`'s `extrahead` block links it and the icon (as the favicon and the `apple-touch-icon`, via the `app_icon` template tag), and sets `apple-mobile-web-app-title` to the page's `site_title`, so the login page includes them all.
No `apple-mobile-web-app-capable` tag: it's deprecated, and the manifest's `display: standalone` replaces it.
`core/app_icon.py`'s `current_icon()` gives the icon all of these use: by default `core/static/core/app_icon.png` (512x512 and opaque, rendered from `app_icon.svg` beside it, which is the default favicon), or the PNG a superuser uploaded on the **App Icon** page (`/admin/app-icon/`, URL name `admin:app_icon_page`, `core.admin.app_icon_view`, served and linked from the top bar like the Idle Watchdog page), which also resets it to the default.
An upload must be a square PNG, at least 512x512 and at most 1 MiB; `check_icon` reads the chunk structure and IHDR with the standard library, not Pillow, and the page warns of transparency (an alpha colour type or a `tRNS` chunk), which iOS fills in with black.
It's stored as the single `AppIcon` row, whose `DatabaseStorage` file goes with it when `save_icon` or `reset_icon` deletes it, and served publicly (it's on the auth whitelist) by the `app_icon` view at `/app-icon.png`, which redirects to the default (uncached) when there's none.
Links to it add `?v=` and the first 16 hex digits of its SHA-256, and a response for the current version is cached for a year (`immutable`), any other `v` not at all; the manifest's one-hour cache can still name the previous icon for up to an hour.

Every admin page's footer shows how long the page took to generate ("Page generated in 0.042 seconds").
`core/timing.py`'s `RequestTimingMiddleware`, first in `MIDDLEWARE` so it covers the others, records when Django started on the request; `base_site.html`'s `footer` block shows the time so far via the `page_generation_time` tag (`core/templatetags/page_timing.py`, empty without the middleware or a request in the context), styled by its `extrastyle` block.
The middleware also adds a `Server-Timing: app;dur=<ms>` header to every response with the total, which also covers what follows the footer, so it's a little more.

Custom admin pages in any module link `core/admin_pages.css` (static), which sizes buttons consistently and provides the `lt-info-box`/`lt-table`/`lt-label` box of labelled values.

`FinancialYearAdmin` adds:
- **Summary view** (`/admin/taxtracker/financialyear/<pk>/summary/`) — item tree with done/pending counts.
- **Single-year ZIP** (`download-zip/`) — all attachments in a folder hierarchy with an `index.md`.
- **Multi-year ZIP** (`download-multi-zip/`) — multiple FYs, each under a top-level `FY<year>/` prefix.
  Also supports "View Index (Markdown)" action.
- **Copy to new year** (`copy-to-new-year/`) — clones the item tree (without notes/status/attachments) into `year+1`.

Both ZIPs are built in a `SpooledTemporaryFile` (in memory up to `_ZIP_SPOOL_MAX_SIZE`, 10 MB, then on disk), copying in one attachment at a time, and returned as a `FileResponse`, so they never hold every attachment in memory at once.

`ItemAdmin` adds:
- **Import archive** (`<pk>/import-archive/`) — uploads a `.har` (Fidelity) or `.zip` file and creates `Attachment` records for each extracted PDF.
  Skips a PDF whose filename is already attached to that item (not to any other item).
  `ReceivedDocumentAdmin`'s import, which picks each PDF's FY from its filename's year prefix, likewise skips only names already attached to a received document of that FY.
- **Reassign attachments** (`<pk>/reassign-attachments/`) — detects attachments whose `date` is outside the item's FY and offers to move them to the equivalent item in the correct FY (matched by title/order path signature).
- Prev/Next Year navigation links on the change form (matched by path signature).

## ZIP export rules

- Spaces in item titles → underscores in ZIP paths (`_safe_component`).
- Names that would clash in a folder, compared case-insensitively, get ` (2)`, ` (3)`... (before the extension for attachments; `_unique_folder_paths`, `_dedupe_name`): sibling items' folders, an item's attachments and its child folders, and `index.md` at the top.
  Items in a parent cycle keep `_folder_path`'s `…`-prefixed path, with only the last component suffixed.
- Items appear in the index only if they have attachments or notes.
- ATX headings in `item.notes` are re-levelled so the shallowest heading becomes `item_heading_level + 1` (implemented in `_adjust_notes_headings`).
  Lines inside fenced code blocks (```` ``` ```` or `~~~`) are left alone and don't count towards the shallowest level.
  In `_write_fy_to_zip`, notes appear *after* attachments; in `_build_fy_index_md` (used for the View Index action), notes appear *before* attachments.

## Archive import formats

`archives.py` dispatches on file extension:
- **`.har`** — Fidelity HAR: extracts PDFs embedded as base64 `fileContent` in JSON responses from `netbenefitsww.fidelity.com` activity URLs.
- **`.zip`** — extracts `.pdf`/`.PDF` files only; non-PDF entries are reported as skipped.

## Secret key

The Django `SECRET_KEY` is stored in `~/.config/lifetracker/secret_key` (auto-generated on first run).
Override with `LIFETRACKER_SECRET_KEY_FILE`.
On first run under the new config location, if a key from the pre-rename `~/.config/taxtracker/secret_key` exists, it's copied forward automatically so existing sessions/logins survive.
The JWT key pair (see Auth front layer) lives there too.
The key's directory is `settings.LIFETRACKER_CONFIG_DIR`: `$LIFETRACKER_CONFIG_DIR` if set, else `$LIFETRACKER_DATA_DIR` if set (the Docker image sets it to `/data`), else `~/.config/lifetracker`; the legacy-key copy only happens for the `~/.config` default.
`LIFETRACKER_DATA_DIR` also moves the database to `$LIFETRACKER_DATA_DIR/db.sqlite3` (overridable with `LIFETRACKER_DB_PATH`).
The `ensure_superuser` command takes an optional initial admin password from `$LIFETRACKER_INITIAL_ADMIN_PASSWORD` if non-blank, else from `initial_admin_password` in the config directory, overridable with `LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE`; it's only used when creating the user, and never printed.

`run.sh` loads a `.env` file from the repo root, if present, by exporting `UV_ENV_FILE=.env` so every `uv run` it makes sees the same variables (skipped if `UV_ENV_FILE` is already set). uv parses it as dotenv, not shell, so it isn't executed.
Variables already set in the shell override the file.
A missing file is a hard error in uv, hence the existence check.
For ad-hoc commands outside `run.sh`, use `UV_ENV_FILE=.env uv run python manage.py ...`.
`.env` and `.env.*` are gitignored.
Its main use is holding the cloud backup's `LIFETRACKER_BACKUP_S3_*` credentials, so they don't have to be exported in every shell.
`just docker-run` also passes it to the container via `--env-file`; Docker keeps quotes as part of the value (uv strips them), so values in `.env` should be unquoted.
The recipe's own `-e` flags override the file.

## Deployment

Production runs on Fly.io (Sydney), configured by `fly.toml`.
The app's name is deliberately kept out of the public repo: `fly.toml` has no `app` line, CI passes it to `flyctl` as `FLY_APP` from the `FLY_APP` repo secret, and for manual `fly` commands use `./flyctl.sh`, which exports `FLY_APP` from `.env` (exiting 1 if it's missing or blank there) and execs `flyctl` with its arguments, or export `FLY_APP` (or pass `-a`).
`./flyssh.sh` runs `./flyctl.sh ssh console` with a command that drops to `lifetracker` via `env HOME=/home/lifetracker USER=lifetracker LOGNAME=lifetracker setpriv --reuid=lifetracker --regid=lifetracker --init-groups`, as `docker-entrypoint.sh` does, then `cd`s to `${LIFETRACKER_APP_DIR:-/app}` and execs its arguments (each single-quoted for Fly's word-splitting of `--command`; no shell parses them after that), or plain `bash` with none.
A leading `--user=<name>` (checked against `^[a-z_][a-z0-9_-]*$`, as it's interpolated unquoted) drops to that user instead, with `HOME` `/home/<name>`, or `/root` for root; `--` ends its options, and any other leading `--` argument is an error.
Not `su -` or `bash -l`: a login shell drops the Machine's environment (`LIFETRACKER_DATA_DIR`, the venv on `PATH`, Fly secrets), and Debian's `/etc/profile` resets `PATH`.
`USER`/`LOGNAME` are set too, as the SSH session leaves them unset.
It passes `--pty` only if stdin and stdout are both terminals.
It first starts the Machine if `./flyctl.sh machine list --json` says it isn't `started` (`machine start`, then `machine wait --state started`; for `starting` it only waits, and for `stopping`/`suspending` it first waits for `stopped`/`suspended`), as `fly ssh console` doesn't, and connects to that Machine with `--machine`.
`./fly-machine-id.sh` prints the Machine's ID (the first `id` in `./flyctl.sh machine list --json`, via `jq`; the app has only one), e.g. for `./flyctl.sh machine status $(./fly-machine-id.sh)`; it fails rather than print `null` if there's no Machine.
Don't add the name to any committed file.
`lifetracker-cli fly-api <path>` (`cli/fly_api.py` over `core/fly_api.py`) GETs a Machines API path (or POSTs to it, with `--post`) through `/.fly/api`, the socket Fly provides on the Machine that authenticates as the Machine, so needs no token, e.g. `./flyssh.sh lifetracker-cli fly-api /v1/apps/{app}/machines`.
It replaces `{app}` and `{machine}` with `FLY_APP_NAME` and `FLY_MACHINE_ID`, as `./flyssh.sh` runs no shell on the Machine, pretty-prints a JSON body, and exits 1 on a non-2xx status.
Methods are options, not a method argument: GET by default, `--post` for POST, and any future ones likewise (e.g. `--delete`).
A POST has an empty body (`Content-Length: 0`) unless `--json` gives a JSON object, sent as `application/json`; `--json` without `--post`, invalid JSON or a non-object is a usage error.
Neither flyctl (114 MB, and it can't use the socket: it requires a token and only takes an http(s) Machines API URL) nor curl is in the image.
The human-facing setup steps are in README.md's Fly.io section; keep the two in sync.
`just fly-setup` (`fly-setup.sh`) idempotently creates the app, volume, Fly secrets (`LIFETRACKER_INITIAL_ADMIN_PASSWORD`, the S3 settings and, if present, `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` and the `LIFETRACKER_GOOGLE_SSO_*` settings other than `CALLBACK_DOMAIN`, which production shares with local dev via one OAuth client with both redirect URIs) and `FLY_APP`/`FLY_API_TOKEN` GitHub secrets from `.env` (`--replace-token` overwrites `FLY_API_TOKEN` with a fresh deploy token; it checks a new token with the same `flyctl status` call as CI's `check-fly` job before storing it); it reads the volume's name and region from `fly.toml`.
It runs locally, so it may print the app name; only CI output (where GitHub masks secret values) must not reveal it.
On the Machine, `LIFETRACKER_ALLOWED_HOSTS` and `LIFETRACKER_BACKUP_HOSTNAME` both default to `<app>.fly.dev` from `FLY_APP_NAME`, which Fly sets at runtime.
`settings.LIFETRACKER_FLY_MACHINE_URL` is the Machine's page in Fly's dashboard, `https://fly.io/apps/<FLY_APP_NAME>/machines/<FLY_MACHINE_ID>` (both set by Fly; built by `settings.fly_machine_url`), or empty unless both are non-blank; `LifetrackerAdminSite.each_context` passes it as `fly_machine_url`, and `base_site.html` links it from the top bar for superusers ("Fly Machine", in a new tab) when it's non-empty.
It runs one Machine with the image CI publishes to GHCR, with a Fly volume mounted at `/data` for the database and secret key, stopped when idle and started on the next request.
Fly Proxy's auto-stop only sees traffic through the proxy, not SSH (which goes over the private network), so it could stop the Machine mid-session (issue #135); `fly.toml` turns it off (`auto_stop_machines = "off"`, keeping `auto_start_machines`).
The replacement is the idle watchdog, `core/idle_watchdog.py`.
In the container, Envoy (`envoy/envoy.yaml`, issue #178) listens on `${LIFETRACKER_HOST:-0.0.0.0}:${PORT:-8000}`, so `fly.toml`'s `internal_port` is unchanged, and passes requests with a valid JWT, and the whitelist's (see Auth front layer), to gunicorn on `127.0.0.1:${LIFETRACKER_GUNICORN_PORT:-8001}`, which nothing outside the container can reach.
The `Dockerfile` copies the `envoy` binary (glibc-linked, nothing else needed) from the pinned `envoyproxy/envoy:distroless-v<version>` image; raise the tag to upgrade it.
`envoy/envoy.yaml` is a template, as Envoy can't read environment variables: `docker-entrypoint.sh` runs `manage.py envoy_config` (see Auth front layer), passing `LIFETRACKER_HOST`, `PORT` and `LIFETRACKER_GUNICORN_PORT` (or their defaults) as `--listen-host`, `--listen-port` and `--upstream-port`; it checks the host is an IP address and the ports are 1–65535 and differ, and writes the config to `${TMPDIR:-/tmp}/lifetracker-envoy.yaml`.
It then starts Envoy (`--concurrency 1 --disable-hot-restart --log-level warn`, no admin interface) in a background loop that restarts it a second after it exits, and `exec`s gunicorn as before, so gunicorn stays the main process: its exit (the idle watchdog's SIGTERM included) ends the container and Envoy with it.
If Envoy exits within a minute of starting 5 times in a row (e.g. a bad config or address), the loop gives up: it creates `LIFETRACKER_ENVOY_FAILED_FILE` (exported by the entrypoint as `${TMPDIR:-/tmp}/lifetracker-envoy-failed`, removed at startup) and SIGTERMs gunicorn, whose `on_exit` hook in `gunicorn_conf.py` then calls `sys.exit(1)` if the file exists.
So the container exits 1, which Fly's `on-failure` policy restarts (up to 10 times) and records as a failed exit, rather than 0, which looks like an idle stop; gunicorn's own SIGTERM exit is always 0, and `on_exit` is the last thing `Arbiter.halt` runs before `sys.exit(exit_status)`.
Given arguments, the entrypoint runs them instead, without Envoy.
Envoy starts before gunicorn listens, so the route retries `connect-failure` and `reset-before-request` (safe for any method, as gunicorn never saw the request) 20 times with 0.1–1 second backoff, about 10 seconds, and the cluster allows 100 concurrent retries (Envoy's default is 3); a request that arrives while gunicorn starts waits rather than failing.
Envoy opens a new upstream connection for each request (`max_requests_per_connection: 1`), since reusing one gunicorn is closing after its 2-second keep-alive would fail the request.
The route has no overall timeout (`timeout: 0s`; Envoy's default of 15 seconds would cut off DB backup downloads, ZIP exports and uploads), only Envoy's default 5-minute `stream_idle_timeout`, and Envoy sets no body size limit.
The connection manager trusts one hop (`use_remote_address` with `xff_num_trusted_hops: 1`), Fly Proxy: Envoy then keeps Fly's `X-Forwarded-Proto` (with no trusted hop it would overwrite it with `http`, breaking `LIFETRACKER_TRUST_X_FORWARDED_PROTO`), appends the proxy's address to `X-Forwarded-For`, and passes `Host` and `Fly-Client-IP` unchanged.
As gunicorn's peer is now `127.0.0.1`, which is in its default `forwarded_allow_ips`, gunicorn also sets the WSGI scheme from `X-Forwarded-Proto`.
`internal_address_config` lists only `0.0.0.0/32`, so no client counts as internal and Envoy strips the `x-envoy-*` request headers (e.g. `x-envoy-upstream-rq-timeout-ms`) a client sends rather than obeying them; an empty list would mean Envoy's defaults, RFC 1918 and loopback, which include Fly Proxy's and Docker's addresses (checked: a client without `X-Forwarded-For` could then set a request's timeout).
It also doesn't generate `x-request-id`, leaves the `Server` header alone, and the router adds no `x-envoy-*` response headers.
Envoy closes a downstream connection after 5 idle seconds (its default is an hour), plus its 5-second drain timeout, so an idle keep-alive connection from Fly Proxy doesn't count as activity to the idle watchdog indefinitely.
The watchdog's defaults are otherwise unchanged by Envoy: HTTP from Fly Proxy still arrives on port 8000 (now Envoy's), and gunicorn's `post_request` hook still touches the request file (but not for requests Envoy turns away for want of a JWT, which never reach gunicorn).
`./run.sh` uses `runserver` without Envoy; `just docker-run` runs the image, so goes through Envoy.
`just docker-smoke` (part of `just ci` and a `test` job step after `docker-build`) runs the built image with DEBUG off and a throwaway volume, waits up to a minute for `/admin/login/` through the published port, and checks from inside the container that port 8000 answers on the container's own address and 8001 doesn't.
`docker-entrypoint.sh` runs gunicorn with gthread workers (`--threads ${LIFETRACKER_GUNICORN_THREADS:-8}`), not its default single sync worker, which blocks on a browser's idle spare connection and leaves other requests queued (issue #172); gthread holds a thread up to 5 seconds on a new idle connection, hence several threads.
As requests can now write concurrently, `DATABASES` sets SQLite's `transaction_mode` to `IMMEDIATE` (so a transaction that reads and then writes waits for the lock instead of failing with "database is locked") and a 30-second `timeout`, and its `init_command` puts the database in WAL mode (issue #152), so readers, including management commands run over SSH such as `cloud_backup`, don't wait for a writer.
The mode persists in the file, which then has `-wal` and `-shm` files beside it (gitignored and dockerignored); `backup_database()` reads through SQLite, so backups include changes not yet checkpointed from the `-wal` file, but copying `db.sqlite3` alone while the app runs may miss them.
The tests' in-memory database stays in `memory` mode.
Under gthread, `--timeout` only restarts a worker whose main loop hangs; it never aborts a slow request.
`lifetracker/gunicorn_conf.py` (which `docker-entrypoint.sh` passes as `--config python:lifetracker.gunicorn_conf`) starts it from gunicorn's `when_ready` hook, which runs only in the master.
It's a separate process started with `subprocess.Popen`, not a thread (the master forks replacement workers later, and forking a multi-threaded process can deadlock) and not `os.fork()` (it would inherit gunicorn's signal handlers and listening sockets).
Every `LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS` it counts ESTABLISHED connections to the local ports in `LIFETRACKER_IDLE_STOP_PORTS` (default `$PORT` or 8000, and 22) in both `/proc/net/tcp` and `/proc/net/tcp6`; on Fly, HTTP from the proxy arrives over IPv4 and SSH over IPv6.
That only sees connections open at the instant of a check, which misses most HTTP requests (gunicorn closes a connection a couple of seconds after its last request at most), so `gunicorn_conf`'s `post_request` hook, which runs in the workers, touches `LIFETRACKER_IDLE_STOP_REQUEST_FILE` (default `/tmp/lifetracker-last-request`, path read once in the master at import) via `record_request`, and a modification time newer than the last activity also counts as activity (one from before the watchdog started is ignored).
Every check, it rewrites a JSON status file atomically (`LIFETRACKER_IDLE_STOP_STATUS_FILE`, default `/tmp/lifetracker-idle-watchdog.json`).
In `stop` mode, after `LIFETRACKER_IDLE_STOP_MINUTES` with no activity, it sends the master SIGTERM, so gunicorn exits 0.
`fly.toml`'s `[[restart]]` policy `on-failure` (also what `fly deploy` uses by default) then leaves the Machine stopped until the proxy starts it.
In `monitor` mode it only logs and writes the status.
It exits if its parent PID stops being the master's.
The same process also records uptime (`core/uptime_log.py`): every second it sets that second's bit in a per-UTC-day file, `<YYYY-MM-DD>.bin` (10,800 bytes, one bit per POSIX second, most significant bit first), in `LIFETRACKER_UPTIME_DIR` (default `$LIFETRACKER_DATA_DIR/uptime`, so `/data/uptime` on Fly; `off` disables it), which makes it wake every second and check for idleness every interval; it runs when either feature is on.
Each second's byte goes straight to the file with `os.pwrite` (no mmap, no file held open), with an `fsync` every check interval (even with idle checks off) and when a day ends.
Gaps of up to 5 seconds between wakes are filled in (longer ones stay as gaps, so count as a stop and start), seconds a failed write missed are recorded by the next one that works, and at startup the seconds since boot are recorded too if `/proc/uptime` is at most 300 (a fresh Machine, not a gunicorn restart or local Docker's host uptime).
Core's `uptime_report` management command prints each recent day's share up and start count, and the admin's Uptime page shows the same.
`LIFETRACKER_IDLE_STOP` is `off` by default, and a bad setting or a missing `/proc/net/tcp` logs and leaves it off rather than failing gunicorn's startup.
`fly.toml` sets `stop` (SSH sessions were confirmed to show on port 22 in `monitor` mode first).
The same process also runs automatic cloud backups (issue #83; `core/auto_backup.py`) if `LIFETRACKER_AUTO_BACKUP` is `on` (default `off`; `fly.toml` sets `on`): at startup, every `LIFETRACKER_AUTO_BACKUP_INTERVAL_HOURS` (default 6) while up, and in `stop` mode before stopping, each as a `python -m django cloud_backup --if-changed` subprocess (with `DJANGO_SETTINGS_MODULE` set, as the master hasn't loaded `wsgi.py`), so an unchanged database uploads nothing.
Not on a clock, as the Machine is usually stopped, when nothing can change; the startup backup covers stops that skipped the one before stopping (a crash, an OOM kill, or Fly stopping a running Machine on a deploy, with only its kill timeout).
They run one at a time and never block the loop, which polls a running one every 5 seconds (`POLL_SECONDS`) and kills one still running after 30 minutes (`TIMEOUT_SECONDS`), so a hung upload can't keep the Machine up indefinitely.
When the idle timeout is reached in `stop` mode, the watchdog starts the backup before stopping (after waiting for any already running) and stays in the `backing up` state until it ends; it then stops at the next check whether or not it succeeded (a failure is logged, and the next startup backs up), unless there's been activity meanwhile, which calls the stop off.
The status file's `auto_backup` section (None when off) has the latest backup's reason, start, finish and exit status and when the next is due, which the Idle Watchdog page shows.
Other non-secret settings are in `fly.toml`'s `[env]`; the `LIFETRACKER_BACKUP_S3_*` credentials are Fly secrets (`fly secrets set`), never in `fly.toml`.
A volume attaches to one Machine, so the app must never be scaled past one; CI's deploy passes `--ha=false` so that if the app has no Machine yet (e.g. first deploy) it creates one, not two, and manual `fly deploy`s should too.
`.env` is in `.dockerignore` so it's never baked into an image.
On every push to `master`, once tests pass, CI deploys the exact image the `test` job built rather than rebuilding it: `test` saves it (`docker save`) as a workflow artifact, `publish` (the only job with `packages: write`, and it runs no repo code) loads it and pushes it to GHCR (public) as `ghcr.io/skissane/taxtracker:<sha>` and `:latest`, and `deploy` runs `flyctl deploy --ha=false --image ghcr.io/skissane/taxtracker@<digest>`, so Fly pulls it by digest.
`deploy` uses the `FLY_API_TOKEN` repo secret (a deploy token scoped to the app); deploys queue rather than cancel each other.
`flyctl deploy` restarts a running Machine on the new image, smoke-checking it, but only updates a stopped one, leaving it stopped (flyctl's `shouldSkipLaunch`), which with the idle watchdog is usual; nothing would then boot the new image, and run its migrations, until the next visit.
So `deploy` then runs `scripts/check_deploy.py` (stdlib-only, `uv run --no-project`), which polls `https://$FLY_APP.fly.dev/healthz/` (Fly Proxy starts the Machine on the first request) every 5 seconds for up to 5 minutes until it answers 200 with `"status": "ok"` and `"commit"` equal to `GITHUB_SHA`, and otherwise fails the job; it never prints the URL, which holds the app's name.
`/healthz/` (`core/health.py`'s `healthz`, unauthenticated and on the auth whitelist) returns `{"status": "ok", "commit": LIFETRACKER_COMMIT_ID}`, or a 503 with `"status": "error"` and a short fixed `"problem"` (the details logged): `database error` if `SELECT 1` or loading the migration plan raises a `DatabaseError`, `unapplied migrations` if the plan isn't empty, or `unhealthy` for any other exception.
`docker-entrypoint.sh` migrates before starting gunicorn, so any answer at all means `migrate` ran.
Don't point Fly's `[[http_service.checks]]` at it: each request touches the idle watchdog's request file, so regular checks would stop the Machine ever idling out; CI's single check only keeps it up for one idle timeout.
On every run, PRs included, the `check-fly` job fails the build unless `FLY_APP` and `FLY_API_TOKEN` are set and `flyctl status -a "$FLY_APP"` succeeds with that token (not `flyctl apps list`, which a deploy token isn't authorised for), and `deploy` also waits on it; it's skipped when `github.actor` is `dependabot[bot]`, whose runs don't get the repo's Actions secrets, but PRs from forks (likewise without them) fail it.
`fly.toml`'s `[build] image` is `:latest`, so a manual `fly deploy` redeploys master's most recently published image; to deploy local changes, publish them through CI rather than building on Fly.
Every boot runs `migrate` against the only copy of the data, so run a cloud backup before merging a risky migration (Fly's daily volume snapshots are the only automatic fallback).

## Linting config

Ruff: rules E, F, W, I, UP, TID252, PLC0415, RUF100; line length 120; migrations excluded. djlint: profile=django, indent=2.

## Test warnings policy

`[tool.pytest.ini_options]` sets `filterwarnings = ["error"]`, so any warning raised during the test suite fails that test.
This is deliberate: it forces whoever's change introduces a new warning to deal with it immediately, rather than letting it sit as unread noise in pytest's warnings summary.
When a test hits a warning:

- If it's a real signal (e.g. a deprecation warning), fix the underlying cause so it stops firing.
- If it's expected and benign — e.g. Django's `UserWarning` ("Overriding setting DATABASES...") whenever a test uses `override_settings(DATABASES=...)` — assert it explicitly with `pytest.warns(WarningType, match=...)` around the minimal block that triggers it, rather than adding a `filterwarnings` ignore entry (globally in `pyproject.toml` or via `@pytest.mark.filterwarnings("ignore:...")`).
  `pytest.warns` is a two-sided check: it also fails if the warning *stops* occurring, so if the underlying code later changes and the warning goes away, the test fails and forces someone to remove the now-stale wrapper.
  A `filterwarnings` ignore rule has no such mechanism — it just keeps silently suppressing nothing, orphaned, indefinitely.
  Reserve global ignore entries for warnings that are pervasive and not tied to one specific assertable call site.

## Issue labels

Issues are labelled `dimension:value`.
When you file an issue, give it one `type:` and one `priority:` label, a `size:` label unless it's a parent issue, and any `concern:` labels that apply.

- `type:` (exactly one): `bug` (something behaves wrongly), `feature` (a new capability or module), `improvement` (makes something that already works better), `maintenance` (refactors, dead code, CI, dependencies; no visible change) or `docs` (documentation only).
- `priority:` (exactly one), defined by what to do about it: `p0` (drop everything: production is broken or data is at risk), `p1` (do next), `p2` (worth doing when convenient) or `p3` (someday/maybe).
- `size:` (exactly one, except on parent issues), by how many PRs it takes rather than hours: `s` (one small PR), `m` (one PR that needs some design) or `l` (several PRs).
  An `l` issue should become a parent issue with sub-issues; parent issues get no `size:` label, their sub-issues do.
- `concern:` (zero or more): `security` (a security risk or hardening) and `data-integrity` (could lose or corrupt data).
  These are separate from `type:`, as a security problem can be a bug, a missing feature or a maintenance job.

An open issue with no `priority:` label is untriaged; find them with `is:open -label:priority:p0 -label:priority:p1 -label:priority:p2 -label:priority:p3`.
When asked what to work on next, start from the `priority:p1` issues.
Update an issue's labels when you learn more about it, e.g. a fix turns out bigger than its `size:`, or it's split into sub-issues and so loses its `size:`.
Use only these labels: applying a label that doesn't exist creates a new grey one, so a typo or a new value silently adds to the scheme.
Propose a new dimension or value to the owner rather than inventing one.

## Pitfalls

**Only Linux (x86_64 and aarch64) and macOS are supported; Windows deliberately isn't.**
`pyproject.toml`'s `[tool.uv] environments` restricts resolution to `darwin`, and to `linux` on `x86_64`/`aarch64` only, which keeps wheels for other Linux architectures (ppc64le, s390x, armv7l, riscv64) out of `uv.lock`.
`just lock-os-arch-check` (part of `just ci` and the `test` job; `scripts/validate_uv_lock_os_arch.py`, stdlib-only) fails if `uv.lock` has a wheel whose platform tag splits into an (OS, arch) pair that its `_classify_platform` doesn't classify as `SUPPORTED` (the platforms `environments` is meant to allow) or `TOLERATED` (iOS and Emscripten, which aren't supported but whose wheels uv keeps despite `environments`).
Don't flag or "fix" Windows portability issues (e.g. relying on `HOME`, POSIX shell syntax in the `justfile` or scripts, `/` path separators, file permission bits), and don't add Windows-specific workarounds.
musl Linux (e.g. Alpine) isn't supported either, though `uv.lock` can't exclude it (environment markers can't tell glibc from musl): some dev tools, such as jactionlint, ship only glibc (manylinux) and macOS wheels, so `uv sync` fails there.
Don't move such a tool to its own dependency group, or otherwise work around it, for musl's sake.

**`except A, B, C:` is valid Python 3 syntax.**
Do not flag comma-separated exception types (without parentheses) as "Python 2-style syntax" or a SyntaxError.
This is valid in Python 3, and ruff format will restore it if you remove it.

**Legacy-upgraded databases keep old `tracker_`-prefixed index names.**
SQLite's `ALTER TABLE ... RENAME TO` (used by `upgrade_legacy_db`) has no equivalent for indexes, so a database that went through the upgrade keeps index names like `tracker_item_parent_id_d6e76dee` on the renamed `taxtracker_item` table, instead of matching what a fresh install would generate.
This is cosmetic only — Django never looks up indexes by name at runtime — do not "fix" it.

**Always use `uv run python` for ad-hoc scripts.**
Use `uv run python -c '...'` not `python -c` or `python3 -c` — the bare commands won't use the project's virtualenv and may not even exist on the system.

**The Docker container starts as root on purpose, then drops to `lifetracker`.**
`docker-entrypoint.sh` needs root only to renumber the `lifetracker` user/group to `LIFETRACKER_UID`/`LIFETRACKER_GID`, re-own `/data` and, if `/.fly/api` is a socket (the Fly Machines API, otherwise root-only; issue #211), `chgrp lifetracker` and `chmod g+rw` it, then re-runs itself via `setpriv`.
The socket step comes after `groupmod`, so it gets the final GID, and is a no-op off Fly (`just docker-run`, `just docker-smoke`).
Don't replace this with a Dockerfile `USER` line: the UID/GID couldn't then change at runtime, which `just docker-run` relies on to read the host's mode-0600 `secret_key`.
Use `docker exec -u lifetracker` (or `just docker-shell`) so commands don't run as root and leave root-owned files in `/data`.

**`justfile` recipe doc comments must be one line.**
`just --list` shows only the last line of the comment directly above a recipe, so a multi-line comment there turns into a meaningless fragment in the listing.
Keep that comment to a single-line summary; put any longer explanation in a separate comment block above it, separated by a blank line (see `docker-run`).
Check the result with `just --list`.

**A job downstream of a skipped job needs an explicit `if:` in `ci.yml`.**
An `if:` with no status function (`always()`, `success()`, `failure()`, `cancelled()`) is treated as `success() && ...`, and `success()` checks *all* ancestor jobs, not just `needs`.
`dependabot-fix` is skipped on every non-Dependabot run, so any job depending (directly or via `test`) on it is silently skipped unless its `if:` overrides that with `always()` or `!cancelled()` and checks `needs.<job>.result` itself, as `test`, `publish` and `deploy` do.
PRs never run `publish`/`deploy`, so a regression only shows up after merging.
