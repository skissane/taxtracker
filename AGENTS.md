# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

**`CLAUDE.md` and `.github/copilot-instructions.md` are symlinks to this file**, not separate files. Always edit `AGENTS.md` directly. If you only see one of the symlink paths, resolve it back to `AGENTS.md` first — don't write through the symlink, and don't treat the symlinked path as a distinct file that also needs updating.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`; run `just ci` to run
everything CI runs, or `just --list` to see individual recipes (`whitespace-check`, `screenshots-dir-check`, `lint`, `fmt-check`,
`lint-templates`, `fmt-check-templates`, `coverage`, `screenshots`, `screenshots-post`, `sync`, `lock-check`, `lock-os-arch-check`,
`requirements-check`, `requirements-fix`). `just ci` drifts from
`ci.yml` if the workflow changes without a matching justfile edit; `run_workflow_local.py`
avoids that by parsing the workflow YAML directly and running its `run:` steps as written
(see its docstring for what it does and doesn't support). After `just coverage` runs, CI uploads
`coverage.xml` to Codecov (`codecov/codecov-action`), and also uploads the `junit.xml` test results
it writes for Codecov Test Analytics (`report_type: test_results`, even if the tests failed; not
`codecov/test-results-action`, which is deprecated); these steps need a `CODECOV_TOKEN` repo
secret from codecov.io and are skipped by `run_workflow_local.py` since they're `uses:` steps. `ci.yml` has seven jobs, `test`, `screenshots` and `post-screenshots` (see Screenshots), `dependabot-fix`, `check-fly`, `publish` and `deploy` (see Deployment);
`run_workflow_local.py` runs the first job in the file unless given `--job`, so keep `test` first.
Don't run `--job dependabot-fix`, `--job publish` or `--job deploy` locally: it doesn't evaluate
`if:`, so it would push commits to the current branch, push to GHCR or deploy to production.

**Whitespace:** `just whitespace-check` (part of `just check`, `just ci` and the `test` job;
`scripts/check_whitespace.py`, stdlib-only) checks every file `git ls-files` lists, except ones
Git's own detection calls binary (`git ls-files --eol` shows `w/-text`) and symlinks: no trailing
whitespace on any line, no leading blank line, and exactly one newline at the end of a non-empty
file (none in an empty one). It checks the working tree copy, so it also catches unstaged edits. For a
Markdown hard line break, end the line with a backslash rather than two trailing spaces.

**Screenshots directory:** `just screenshots-dir-check` (part of `just check`, `just ci` and the
`test` job; `scripts/check_screenshots_dir.py`, stdlib-only) fails unless `screenshots/.gitignore`
is the only file `git ls-files` lists under `screenshots/`, and its staged and working tree copies
both have the contents the script expects. Without it, `rm -rf screenshots` before a screenshot run,
then `git add -A`, commits the PNGs and the `.gitignore`'s deletion (as happened on PR #137). There's
deliberately no escape hatch: a change that really needs to alter `screenshots/.gitignore` updates
the script's `EXPECTED` along with it.

**Dependency minimums:** `pyproject.toml`'s dependency lower bounds (`>=`) are kept equal to the
versions uv resolved, by `scripts/requirements_min_versions.py`: `just requirements-check` (part of
`just ci` and the `test` job) fails if any in `[project].dependencies`, any optional extra or any
dependency group differ from the versions in `uv.lock`, and `just requirements-fix`
rewrites them; run `uv lock` afterwards. Dependabot only edits `pyproject.toml` when the new
version falls outside the existing constraint, which a `>=` minimum never excludes, so its PRs
usually bump only `uv.lock`, even with `versioning-strategy: "increase"`, and would fail that check
(see <https://github.com/dependabot/dependabot-core/issues/12788#issuecomment-3606306953>). The
`dependabot-fix` job, which runs only on PRs whose `github.actor` is
`dependabot[bot]`, runs `requirements-fix` and `uv lock` on the PR branch and pushes any change as a
`chore: sync dependency minimum versions` commit. It pushes with the `DEPENDABOT_FIX_TOKEN` secret
(a token with `contents: write` on the repo; it must be a Dependabot secret, as Dependabot runs
don't get Actions secrets, and the job fails if it's unset), not `GITHUB_TOKEN`, because pushes
made with `GITHUB_TOKEN` don't trigger new workflow runs. As the branch's bumped dependency isn't
trusted, only the final push step sees the token: checkout runs with `persist-credentials: false`,
and the job never syncs or installs anything (`requirements-check`/`requirements-fix` use
`uv run --no-project`, as the script needs only the standard library). `test` waits on it and runs only if it
was skipped or pushed nothing; if it pushed, that run's `test` is skipped and the run the push
triggers tests the fixed commit.

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

# Screenshot the admin login, index, backups, cloud backup list and idle watchdog pages, in light and dark mode, into screenshots/ (gitignored)
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

**Stack:** Django 6.x, Python 3.14, SQLite, `uv` for package management. The entire UI is the Django admin — there are no custom non-admin views (except file serving), and `/` redirects to `/admin/`. No frontend framework.

**Package layout:** `src/lifetracker/` is the Django project (settings, urls). It contains three apps:
- `src/lifetracker/core/` (label `core`) — generic infrastructure any future module can reuse: the file-type registry (`FileType`/`MimeType`/`FileExtension`), DB-backed file storage (`DBStoredFile`/`DatabaseStorage`) and the file-serving admin view, shared attachment behaviour (`core/attachments.py`: filename date/file-type autofill and stored-file cleanup on delete) and `FlexibleDateField` (`core/forms.py`), shared tree behaviour for self-referential models (`core/trees.py`'s `TreeNodeMixin`: pending/done status choices and `is_done`, the `A > B > C` path for `__str__` and `get_folder_path()`, and parent cycle detection for `clean()`), whole-database backups (DB download and cloud backup, `core/backup.py`, with `CloudBackupStatus`), the idle watchdog (`core/idle_watchdog.py`, see Deployment), and `core/admin_pages.css` for custom admin pages.
- `src/lifetracker/taxtracker/` (label `taxtracker`) — the tax-tracking module: `FinancialYear`/`Item`/`Attachment`, archive import, and their admin views/templates. This was the original module; more modules (banking, portfolio, insurance, etc.) may be added as siblings of `taxtracker` under `lifetracker` in future.
- `src/lifetracker/tasktracker/` (label `tasktracker`) — the task-tracking module: `TaskType`/`Task`/`TaskAttachment`, with plain admin views (no custom templates).
- `src/lifetracker/cli/` — standalone `click`-based command-line utilities (`.eml`/HAR/ZIP extraction and conversion helpers) unrelated to the Django app; not a Django app, not in `INSTALLED_APPS`. Invoked via the `lifetracker-cli` console script (e.g. `uv run lifetracker-cli extract-eml <file> <out.zip>`), an entry point (`[project.scripts]` in `pyproject.toml`) generated by `uv sync` from the editable-installed `lifetracker` package.

`taxtracker` and `tasktracker` depend on `core` (via their attachments' `file_type` FK and `DatabaseStorage`); `core` depends on neither, and they don't depend on each other.

Tests live outside `src/`, in a top-level `tests/` tree mirroring the package layout (`tests/core/tests.py`,
`tests/taxtracker/tests.py`, `tests/tasktracker/tests.py`, `tests/cli/tests.py`), each importing the code under test via absolute
`lifetracker.*` imports. The exception is `tests/scripts/tests.py`, which tests the standalone
scripts in `scripts/` via `scripts.*` imports, e.g. `from scripts.requirements_min_versions
import main`. `scripts/` has no `__init__.py`; it resolves as an implicit namespace package from
the repo root, which both pytest and `manage.py test` put on `sys.path`. (Importing by bare module
name with `scripts` on `pytest`'s `pythonpath` also works at runtime, but Pylance can't resolve
it.) Coverage measures `scripts` alongside `src/lifetracker`. Run via `pytest` (using `pytest-django`, configured in
`pyproject.toml`'s `[tool.pytest.ini_options]` with `testpaths = ["tests"]`), parallelised across CPU
cores by `pytest-xdist` (`-n auto`, set as a default in `addopts`). `manage.py test tests.core tests.taxtracker tests.tasktracker tests.cli tests.scripts` still works as an unparallelised fallback,
referencing these as `tests.*` labels rather than the `lifetracker.*` app labels.

**Screenshots:** `screenshot_tests/` holds Playwright tests that drive a headless Chromium
against pytest-django's `live_server` (empty test DB) and save full-page PNGs to the gitignored
`screenshots/` directory (each screen as `<name>-light.png` and `<name>-dark.png`), for eyeballing UI changes. `just screenshots` sets `LIFETRACKER_COMMIT_ID` to `git rev-parse HEAD` unless it's already set, so the admin header shows the commit; the `screenshots` CI job sets it to the PR's head commit (`github.event.pull_request.head.sha`, falling back to `github.sha` off PRs), since on a PR `actions/checkout` checks out a merge commit. The tests are run by `just screenshots`
(which also runs `playwright install chromium`): they're outside `testpaths`, so `pytest`,
`just coverage` and the `test` job never collect or measure them, and Playwright is in its own
`screenshots` dependency group, which only the `screenshots` CI job installs. That job (which
first runs `playwright install --with-deps chromium` for Chromium's system libraries) runs
`just screenshots` and uploads `screenshots/*.png` as a `screenshots` workflow artifact (kept 14 days),
downloadable from the run's summary page. Like `test`, it waits on
`dependabot-fix`. They assert nothing beyond the pages loading. On a PR, once it succeeds,
`post-screenshots` downloads that artifact and runs `just screenshots-post`
(`scripts/post_screenshots.py`, stdlib-only, run with `uv run --no-project`). It uploads each
PNG as a user attachment, then writes one `github-actions[bot]` PR comment embedding them,
editing the previous one in place if there is one (found by a hidden
`<!-- lifetracker-screenshots -->` marker and the `--author` login). The upload uses the
undocumented `POST https://uploads.github.com/user-attachments/assets` endpoint, sending the
same request `gh ... --attach` does (cli/cli `internal/attachments/client.go`), which only
accepts user tokens, not installation tokens such as `GITHUB_TOKEN`. So uploads use the
`SCREENSHOTS_TOKEN` Actions secret, a fine-grained PAT for this repo with only Pull requests:
read and write (the job fails if it's unset), and the comment uses `GITHUB_TOKEN` with
`pull-requests: write`. Locally both tokens default to `gh auth token`. It's skipped for fork
PRs and Dependabot, which don't get the secret; it runs the PR branch's script but never syncs
or installs the project, so a bumped dependency never runs with the tokens.

**File storage:** All uploaded attachments are stored in the SQLite database, not on disk. When a row of any model with a `DatabaseStorage` `FileField` is deleted, `core.attachments.delete_stored_files` deletes its `DBStoredFile` too: a `post_delete` receiver, which `CoreConfig.ready()` connects to each such model (not to every sender, which would turn off fast bulk deletes everywhere), so cascades and `QuerySet.delete()` (e.g. the admin's "Delete selected") clean up as well, which a `delete()` override wouldn't. Rows deleted before that receiver existed left their `DBStoredFile`s behind: core's `purge_orphaned_files` management command reports those that no such field on any model refers to (`core.attachments.orphaned_stored_file_pks`, which finds the fields via `database_storage_file_fields`, so core needn't know the modules) with their count and total size, and deletes them only with `--delete`. `DatabaseStorage` (in `core/models.py`) is a custom Django storage backend that writes file content to `DBStoredFile` rows. Storage paths have the form `db/<pk>/<filename>`. Files are served through `DBStoredFileAdmin.serve_file_view` at `/admin/core/dbstoredfile/file/<pk>/`.

**Legacy database upgrade:** databases created before the `lifetracker`/`core`/`taxtracker` split still have the old `tracker_*` tables and `django_migrations`/`django_content_type` rows under app label `tracker`. `taxtracker`'s `upgrade_legacy_db` management command renames those tables and re-labels the content types in place; it is idempotent (no-op on fresh or already-upgraded databases) and `run.sh` runs it unconditionally before `migrate`.

## Models

- **`FinancialYear`** (taxtracker) — Australian FY ending 30 June of `year`. E.g. `year=2024` = 1 Jul 2023–30 Jun 2024. Default lodgement date is 31 Oct; overridable.
- **`Item`** (taxtracker) — tax checklist item, self-referential `parent` (tree). Child items always inherit their parent's `year_id`, enforced in both `clean()` and `save()`. Cascade update propagates when a root item's year changes. Its tree behaviour (path, status, cycle check) comes from `core.trees.TreeNodeMixin`, shared with `Task`.
- **`FileType` / `MimeType` / `FileExtension`** (core) — file type registry. Each `FileType` must have exactly one primary MIME type and one primary extension; enforced by `_AtLeastOnePrimaryFormSet`.
- **`DBStoredFile`** (core) — raw binary storage row; managed by `DatabaseStorage`.
- **`Attachment`** (taxtracker) — file attached to exactly one of an `Item` or a `ReceivedDocument`. On `save()`, `core.attachments.AttachmentAutofillMixin` auto-populates a blank `title` from the filename, `file_type` by extension lookup (`file_type_for_filename`), and `date` by regex-extracting ISO/compact dates from the filename (`extract_date_from_filename`).
- **`ReceivedDocument`** (taxtracker) — a document received back from the tax agent/office for a `FinancialYear`, with its own attachments.
- **`CloudBackupStatus`** (core) — single row (`CLOUD_BACKUP_STATUS_PK`) recording the time, S3 object key and size (bytes) of the last successful cloud backup. Moved from taxtracker: core's `0002` creates its table and taxtracker's `0004` copies the row across and drops the old one (and the reverse).
- **`TaskType`** (tasktracker) — kind of task, optionally restricted to linking to one model (`content_type`). The built-in "Generic" type (`TaskType.GENERIC_PK`, created by a data migration) is the default, can't be renamed, linked to a model or deleted.
- **`Task`** (tasktracker) — pending/done task, self-referential `parent` (tree), optionally linked to one object via a generic FK (`content_type`/`object_id`); `clean()` only allows a link of its task type's model, and rejects parent cycles (via `TreeNodeMixin`, as for `Item`).
- **`TaskAttachment`** (tasktracker) — file attached to a `Task`, stored via `DatabaseStorage`. It uses the same `AttachmentAutofillMixin` as `Attachment`, and its admin form (`TaskAttachmentForm`) the same `core.forms.FlexibleDateField`.

## Admin extensions

`admin.site` is a `LifetrackerAdminSite` (`src/lifetracker/admin_site.py`), installed by listing `lifetracker.apps.LifetrackerAdminConfig` in `INSTALLED_APPS` in place of `django.contrib.admin`. Its `each_context` builds the admin header (via `_build_admin_site_header`) and `site_title` per request, since they depend on whether the visitor is logged in; don't set them on `admin.site`, which is shared across requests. The header shows the site name, the running commit and the instance name: `LIFETRACKER_COMMIT_ID` as a short ID, linking to `LIFETRACKER_COMMIT_URL_BASE` (default: this repo's GitHub commit URL) only for logged-in users, and `LIFETRACKER_BACKUP_HOSTNAME` (see the backup pages below). The site name (header and page titles) is "Life Tracker" for logged-in users, else `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` (default also "Life Tracker"). `run.sh` sets the commit ID from `git rev-parse HEAD`; `just docker-build` (which CI uses) bakes it into the image as a build arg. With no commit ID, only the instance name is shown.

`CloudBackupStatusAdmin` (core) hosts the backup pages, which cover the whole database rather than one module. It replaces the model's usual admin URLs with these, all superuser-only (`has_module_permission`/`has_view_permission` are superuser-only too, so only superusers see its **Backups** entry under Core on the admin index). The logic lives in `core/backup.py` as plain functions (`run_cloud_backup`, `list_cloud_backups`, `serialize_database`, etc.), not admin methods, so e.g. scheduled backups can reuse it:
- **Backups** (`/admin/core/cloudbackupstatus/`, the changelist URL) — cloud backup status page with a "Run Backup Now" button that uploads the serialised SQLite DB to an S3-compatible bucket (via `boto3`) as `<hostname>/db-backup.<YYYY-MM-DD.HH.MM.SS>.sqlite3`, where `<hostname>` is `LIFETRACKER_BACKUP_HOSTNAME` if set, else `socket.gethostname()` (`just docker-run` sets it to `docker.$(hostname)`, as the container's DB is separate from local dev's and its own hostname is a random ID); the page shows it. The `cloud_backup` management command (core) does the same from the command line via the same `run_cloud_backup`, printing the object key, and raises `CommandError` on the same failures (`CLOUD_BACKUP_ERRORS`, shared with the admin views), including when the upload succeeded but recording `CloudBackupStatus` didn't. Manual only; nothing schedules it. Configured by the `LIFETRACKER_BACKUP_S3_*` env vars read in `settings.py`: `BUCKET_NAME`, `ACCESS_KEY_ID` and `SECRET_ACCESS_KEY` are required (the button is disabled and the missing names are listed until they're set); `ENDPOINT_URL` (blank = AWS) and `REGION_NAME` are optional. Locally, these normally go in `.env` (see below).
- **DB backup** (`download-db-backup/`) — SQLite serialise download, linked from the backups page.
- **Cloud backup list** (`list/`) — linked from the backups page: lists the bucket's objects (`list_objects_v2`, paginated) with key, size and last-modified time (shown in UTC), newest first. The `prefix` query parameter picks what to list: absent means this instance's `<hostname>/`, empty means the whole bucket; links to each top-level folder (one per hostname, from a `Delimiter="/"` listing) let you switch. Missing settings and S3 errors are shown on the page. Both cloud backup pages get their client from `cloud_backup_s3_client`.

The superuser-only **Idle Watchdog** page (`/admin/idle-watchdog/`, URL name `admin:idle_watchdog`) belongs to no model, so it has no admin index entry; instead `LifetrackerAdminSite.get_urls` serves `core.admin.idle_watchdog_view` there, and `src/lifetracker/templates/admin/base_site.html` (in `TEMPLATES`' `DIRS`, so ahead of the admin's own templates, which it extends) links it from the top bar for superusers, before "View site". Add any further model-less pages the same way, rather than giving them a tableless model; if the links grow numerous, make them a menu. `idle_watchdog_page_context` (in `core/admin.py`) builds the page from `idle_watchdog.config_from_env()` (the web process has the watchdog's environment), the status file (`idle_watchdog.read_status`) and `/proc/uptime` (`system_uptime`). It shows durations to the second (`_format_duration`), since `timesince` rounds anything under a minute to "0 minutes". It warns that the watchdog has probably stopped when its last check, or its start if it hasn't checked yet, is more than three check intervals old. Invalid settings, an unreadable status file and unexpected contents are shown as errors on the page. Times are in the site's time zone.

Every admin page's footer shows how long the page took to generate ("Page generated in 0.042 seconds"). `core/timing.py`'s `RequestTimingMiddleware`, first in `MIDDLEWARE` so it covers the others, records when Django started on the request; `base_site.html`'s `footer` block shows the time so far via the `page_generation_time` tag (`core/templatetags/page_timing.py`, empty without the middleware or a request in the context), styled by its `extrastyle` block. The middleware also adds a `Server-Timing: app;dur=<ms>` header to every response with the total, which also covers what follows the footer, so it's a little more.

Custom admin pages in any module link `core/admin_pages.css` (static), which sizes buttons consistently and provides the `lt-info-box`/`lt-table`/`lt-label` box of labelled values.

`FinancialYearAdmin` adds:
- **Summary view** (`/admin/taxtracker/financialyear/<pk>/summary/`) — item tree with done/pending counts.
- **Single-year ZIP** (`download-zip/`) — all attachments in a folder hierarchy with an `index.md`.
- **Multi-year ZIP** (`download-multi-zip/`) — multiple FYs, each under a top-level `FY<year>/` prefix. Also supports "View Index (Markdown)" action.
- **Copy to new year** (`copy-to-new-year/`) — clones the item tree (without notes/status/attachments) into `year+1`.

`ItemAdmin` adds:
- **Import archive** (`<pk>/import-archive/`) — uploads a `.har` (Fidelity) or `.zip` file and creates `Attachment` records for each extracted PDF. Skips duplicates by filename.
- **Reassign attachments** (`<pk>/reassign-attachments/`) — detects attachments whose `date` is outside the item's FY and offers to move them to the equivalent item in the correct FY (matched by title/order path signature).
- Prev/Next Year navigation links on the change form (matched by path signature).

## ZIP export rules

- Spaces in item titles → underscores in ZIP paths (`_safe_component`).
- Items appear in the index only if they have attachments or notes.
- ATX headings in `item.notes` are re-levelled so the shallowest heading becomes `item_heading_level + 1` (implemented in `_adjust_notes_headings`). In `_write_fy_to_zip`, notes appear *after* attachments; in `_build_fy_index_md` (used for the View Index action), notes appear *before* attachments.

## Archive import formats

`archives.py` dispatches on file extension:
- **`.har`** — Fidelity HAR: extracts PDFs embedded as base64 `fileContent` in JSON responses from `netbenefitsww.fidelity.com` activity URLs.
- **`.zip`** — extracts `.pdf`/`.PDF` files only; non-PDF entries are reported as skipped.

## Secret key

The Django `SECRET_KEY` is stored in `~/.config/lifetracker/secret_key` (auto-generated on first run). Override with `LIFETRACKER_SECRET_KEY_FILE`. On first run under the new config location, if a key from the pre-rename `~/.config/taxtracker/secret_key` exists, it's copied forward automatically so existing sessions/logins survive. The key's directory is `settings.LIFETRACKER_CONFIG_DIR`: `$LIFETRACKER_CONFIG_DIR` if set, else `$LIFETRACKER_DATA_DIR` if set (the Docker image sets it to `/data`), else `~/.config/lifetracker`; the legacy-key copy only happens for the `~/.config` default. `LIFETRACKER_DATA_DIR` also moves the database to `$LIFETRACKER_DATA_DIR/db.sqlite3` (overridable with `LIFETRACKER_DB_PATH`). The `ensure_superuser` command takes an optional initial admin password from `$LIFETRACKER_INITIAL_ADMIN_PASSWORD` if non-blank, else from `initial_admin_password` in the config directory, overridable with `LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE`; it's only used when creating the user, and never printed.

`run.sh` loads a `.env` file from the repo root, if present, by exporting `UV_ENV_FILE=.env` so every `uv run` it makes sees the same variables (skipped if `UV_ENV_FILE` is already set). uv parses it as dotenv, not shell, so it isn't executed. Variables already set in the shell override the file. A missing file is a hard error in uv, hence the existence check. For ad-hoc commands outside `run.sh`, use `UV_ENV_FILE=.env uv run python manage.py ...`. `.env` and `.env.*` are gitignored. Its main use is holding the cloud backup's `LIFETRACKER_BACKUP_S3_*` credentials, so they don't have to be exported in every shell. `just docker-run` also passes it to the container via `--env-file`; Docker keeps quotes as part of the value (uv strips them), so values in `.env` should be unquoted. The recipe's own `-e` flags override the file.

## Deployment

Production runs on Fly.io (Sydney), configured by `fly.toml`. The app's name is deliberately kept out of the public repo: `fly.toml` has no `app` line, CI passes it to `flyctl` as `FLY_APP` from the `FLY_APP` repo secret, and for manual `fly` commands use `./flyctl.sh`, which exports `FLY_APP` from `.env` (exiting 1 if it's missing or blank there) and execs `flyctl` with its arguments, or export `FLY_APP` (or pass `-a`). `./flyssh.sh` runs `./flyctl.sh ssh console` with a command that drops to `lifetracker` via `env HOME=/home/lifetracker USER=lifetracker LOGNAME=lifetracker setpriv --reuid=lifetracker --regid=lifetracker --init-groups`, as `docker-entrypoint.sh` does, then `cd`s to `${LIFETRACKER_APP_DIR:-/app}` and execs its arguments (each single-quoted for Fly's word-splitting of `--command`; no shell parses them after that), or plain `bash` with none. Not `su -` or `bash -l`: a login shell drops the Machine's environment (`LIFETRACKER_DATA_DIR`, the venv on `PATH`, Fly secrets), and Debian's `/etc/profile` resets `PATH`. `USER`/`LOGNAME` are set too, as the SSH session leaves them unset. It passes `--pty` only if stdin and stdout are both terminals. It first starts the Machine if `./flyctl.sh machine list --json` says it isn't `started` (`machine start`, then `machine wait --state started`; for `starting` it only waits, and for `stopping`/`suspending` it first waits for `stopped`/`suspended`), as `fly ssh console` doesn't, and connects to that Machine with `--machine`. `./fly-machine-id.sh` prints the Machine's ID (the first `id` in `./flyctl.sh machine list --json`, via `jq`; the app has only one), e.g. for `./flyctl.sh machine status $(./fly-machine-id.sh)`; it fails rather than print `null` if there's no Machine. Don't add the name to any committed file. The human-facing setup steps are in README.md's Fly.io section; keep the two in sync. `just fly-setup` (`fly-setup.sh`) idempotently creates the app, volume, Fly secrets (`LIFETRACKER_INITIAL_ADMIN_PASSWORD`, the S3 settings and, if present, `LIFETRACKER_UNAUTHENTICATED_SITE_NAME` and the `LIFETRACKER_GOOGLE_SSO_*` settings other than `CALLBACK_DOMAIN`, which production shares with local dev via one OAuth client with both redirect URIs) and `FLY_APP`/`FLY_API_TOKEN` GitHub secrets from `.env` (`--replace-token` overwrites `FLY_API_TOKEN` with a fresh deploy token; it checks a new token with the same `flyctl status` call as CI's `check-fly` job before storing it); it reads the volume's name and region from `fly.toml`. It runs locally, so it may print the app name; only CI output (where GitHub masks secret values) must not reveal it. On the Machine, `LIFETRACKER_ALLOWED_HOSTS` and `LIFETRACKER_BACKUP_HOSTNAME` both default to `<app>.fly.dev` from `FLY_APP_NAME`, which Fly sets at runtime. It runs one Machine with the image CI publishes to GHCR, with a Fly volume mounted at `/data` for the database and secret key, and auto-stop/start when idle. Fly Proxy's auto-stop only sees traffic through the proxy, not SSH (which goes over the private network), so it can stop the Machine mid-session (issue #135). The replacement is the idle watchdog, `core/idle_watchdog.py`. `lifetracker/gunicorn_conf.py` (which `docker-entrypoint.sh` passes as `--config python:lifetracker.gunicorn_conf`) starts it from gunicorn's `when_ready` hook, which runs only in the master. It's a separate process started with `subprocess.Popen`, not a thread (the master forks replacement workers later, and forking a multi-threaded process can deadlock) and not `os.fork()` (it would inherit gunicorn's signal handlers and listening sockets). Every `LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS` it counts ESTABLISHED connections to the local ports in `LIFETRACKER_IDLE_STOP_PORTS` (default `$PORT` or 8000, and 22) in both `/proc/net/tcp` and `/proc/net/tcp6`; on Fly, HTTP from the proxy arrives over IPv4 and SSH over IPv6. That only sees connections open at the instant of a check, which misses most HTTP requests (sync workers close the connection after each one), so `gunicorn_conf`'s `post_request` hook, which runs in the workers, touches `LIFETRACKER_IDLE_STOP_REQUEST_FILE` (default `/tmp/lifetracker-last-request`, path read once in the master at import) via `record_request`, and a modification time newer than the last activity also counts as activity (one from before the watchdog started is ignored). Every check, it rewrites a JSON status file atomically (`LIFETRACKER_IDLE_STOP_STATUS_FILE`, default `/tmp/lifetracker-idle-watchdog.json`). In `stop` mode, after `LIFETRACKER_IDLE_STOP_MINUTES` with no activity, it sends the master SIGTERM, so gunicorn exits 0. `fly.toml`'s `[[restart]]` policy `on-failure` (also what `fly deploy` uses by default) then leaves the Machine stopped until the proxy starts it. In `monitor` mode it only logs and writes the status. It exits if its parent PID stops being the master's. `LIFETRACKER_IDLE_STOP` is `off` by default, and a bad setting or a missing `/proc/net/tcp` logs and leaves it off rather than failing gunicorn's startup. `fly.toml` sets `monitor` while Fly's own auto-stop is still on; the plan is to switch to `stop` with `auto_stop_machines = "off"` once SSH sessions are confirmed to show on port 22. Other non-secret settings are in `fly.toml`'s `[env]`; the `LIFETRACKER_BACKUP_S3_*` credentials are Fly secrets (`fly secrets set`), never in `fly.toml`. A volume attaches to one Machine, so the app must never be scaled past one; CI's deploy passes `--ha=false` so that if the app has no Machine yet (e.g. first deploy) it creates one, not two, and manual `fly deploy`s should too. `.env` is in `.dockerignore` so it's never baked into an image. On every push to `master`, once tests pass, CI deploys the exact image the `test` job built rather than rebuilding it: `test` saves it (`docker save`) as a workflow artifact, `publish` (the only job with `packages: write`, and it runs no repo code) loads it and pushes it to GHCR (public) as `ghcr.io/skissane/taxtracker:<sha>` and `:latest`, and `deploy` runs `flyctl deploy --ha=false --image ghcr.io/skissane/taxtracker@<digest>`, so Fly pulls it by digest. `deploy` uses the `FLY_API_TOKEN` repo secret (a deploy token scoped to the app); deploys queue rather than cancel each other. On every run, PRs included, the `check-fly` job fails the build unless `FLY_APP` and `FLY_API_TOKEN` are set and `flyctl status -a "$FLY_APP"` succeeds with that token (not `flyctl apps list`, which a deploy token isn't authorised for), and `deploy` also waits on it; it's skipped when `github.actor` is `dependabot[bot]`, whose runs don't get the repo's Actions secrets, but PRs from forks (likewise without them) fail it. `fly.toml`'s `[build] image` is `:latest`, so a manual `fly deploy` redeploys master's most recently published image; to deploy local changes, publish them through CI rather than building on Fly. Every boot runs `migrate` against the only copy of the data, so run a cloud backup before merging a risky migration (Fly's daily volume snapshots are the only automatic fallback).

## Linting config

Ruff: rules E, F, W, I, UP; line length 88; migrations excluded. djlint: profile=django, indent=2.

## Test warnings policy

`[tool.pytest.ini_options]` sets `filterwarnings = ["error"]`, so any warning raised during the test suite fails that test. This is deliberate: it forces whoever's change introduces a new warning to deal with it immediately, rather than letting it sit as unread noise in pytest's warnings summary. When a test hits a warning:

- If it's a real signal (e.g. a deprecation warning), fix the underlying cause so it stops firing.
- If it's expected and benign — e.g. Django's `UserWarning` ("Overriding setting DATABASES...") whenever a test uses `override_settings(DATABASES=...)` — assert it explicitly with `pytest.warns(WarningType, match=...)` around the minimal block that triggers it, rather than adding a `filterwarnings` ignore entry (globally in `pyproject.toml` or via `@pytest.mark.filterwarnings("ignore:...")`). `pytest.warns` is a two-sided check: it also fails if the warning *stops* occurring, so if the underlying code later changes and the warning goes away, the test fails and forces someone to remove the now-stale wrapper. A `filterwarnings` ignore rule has no such mechanism — it just keeps silently suppressing nothing, orphaned, indefinitely. Reserve global ignore entries for warnings that are pervasive and not tied to one specific assertable call site.

## Pitfalls

**Only Linux (x86_64 and aarch64) and macOS are supported; Windows deliberately isn't.** `pyproject.toml`'s `[tool.uv] environments` restricts resolution to `darwin`, and to `linux` on `x86_64`/`aarch64` only, which keeps wheels for other Linux architectures (ppc64le, s390x, armv7l, riscv64) out of `uv.lock`. `just lock-os-arch-check` (part of `just ci` and the `test` job; `scripts/validate_uv_lock_os_arch.py`, stdlib-only) fails if `uv.lock` has a wheel whose platform tag splits into an (OS, arch) pair that its `_classify_platform` doesn't classify as `SUPPORTED` (the platforms `environments` is meant to allow) or `TOLERATED` (iOS and Emscripten, which aren't supported but whose wheels uv keeps despite `environments`). Don't flag or "fix" Windows portability issues (e.g. relying on `HOME`, POSIX shell syntax in the `justfile` or scripts, `/` path separators, file permission bits), and don't add Windows-specific workarounds.

**`except A, B, C:` is valid Python 3 syntax.** Do not flag comma-separated exception types (without parentheses) as "Python 2-style syntax" or a SyntaxError. This is valid in Python 3, and ruff format will restore it if you remove it.

**Legacy-upgraded databases keep old `tracker_`-prefixed index names.** SQLite's `ALTER TABLE ... RENAME TO` (used by `upgrade_legacy_db`) has no equivalent for indexes, so a database that went through the upgrade keeps index names like `tracker_item_parent_id_d6e76dee` on the renamed `taxtracker_item` table, instead of matching what a fresh install would generate. This is cosmetic only — Django never looks up indexes by name at runtime — do not "fix" it.

**Always use `uv run python` for ad-hoc scripts.** Use `uv run python -c '...'` not `python -c` or `python3 -c` — the bare commands won't use the project's virtualenv and may not even exist on the system.

**The Docker container starts as root on purpose, then drops to `lifetracker`.** `docker-entrypoint.sh` needs root only to renumber the `lifetracker` user/group to `LIFETRACKER_UID`/`LIFETRACKER_GID` and re-own `/data`, then re-runs itself via `setpriv`. Don't replace this with a Dockerfile `USER` line: the UID/GID couldn't then change at runtime, which `just docker-run` relies on to read the host's mode-0600 `secret_key`. Use `docker exec -u lifetracker` (or `just docker-shell`) so commands don't run as root and leave root-owned files in `/data`.

**`justfile` recipe doc comments must be one line.** `just --list` shows only the last line of the comment directly above a recipe, so a multi-line comment there turns into a meaningless fragment in the listing. Keep that comment to a single-line summary; put any longer explanation in a separate comment block above it, separated by a blank line (see `docker-run`). Check the result with `just --list`.

**A job downstream of a skipped job needs an explicit `if:` in `ci.yml`.** An `if:` with no status function (`always()`, `success()`, `failure()`, `cancelled()`) is treated as `success() && ...`, and `success()` checks *all* ancestor jobs, not just `needs`. `dependabot-fix` is skipped on every non-Dependabot run, so any job depending (directly or via `test`) on it is silently skipped unless its `if:` overrides that with `always()` or `!cancelled()` and checks `needs.<job>.result` itself, as `test`, `publish` and `deploy` do. PRs never run `publish`/`deploy`, so a regression only shows up after merging.
