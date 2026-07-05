# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`; run `just ci` to run
everything CI runs, or `just --list` to see individual recipes (`lint`, `fmt-check`,
`lint-templates`, `fmt-check-templates`, `coverage`, `sync`, `lock-check`). `just ci` drifts from
`ci.yml` if the workflow changes without a matching justfile edit; `run_workflow_local.py`
avoids that by parsing the workflow YAML directly and running its `run:` steps as written
(see its docstring for what it does and doesn't support). After `just coverage` runs, CI uploads
`coverage.xml` to Codecov (`codecov/codecov-action`); this step needs a `CODECOV_TOKEN` repo
secret from codecov.io and is skipped by `run_workflow_local.py` since it's a `uses:` step.

```bash
# Start the app (sync deps, upgrade legacy DB schema if needed, migrate,
# create superuser, runserver)
./run.sh

# Run all tests
uv run python manage.py test lifetracker.core lifetracker.taxtracker lifetracker.cli

# Run a single test class or method
uv run python manage.py test lifetracker.taxtracker.tests.ItemModelTests
uv run python manage.py test lifetracker.taxtracker.tests.ItemModelTests.test_str_root

# Run tests under coverage, print a report, and write coverage.xml (no minimum enforced)
just coverage

# Lint (matches `just lint`/`just fmt-check`, which check the whole repo root)
uv run ruff check .
uv run ruff format .

# Template lint
uv run djlint --check src/lifetracker/taxtracker/templates/

# CLI utility scripts (email/PDF/zip helpers, unrelated to the Django app)
uv run python cli.py --help
```

## Architecture

**Stack:** Django 6.0, Python 3.14, SQLite, `uv` for package management. The entire UI is the Django admin — there are no custom non-admin views (except file serving). No frontend framework.

**Package layout:** `src/lifetracker/` is the Django project (settings, urls). It contains two apps:
- `src/lifetracker/core/` (label `core`) — generic infrastructure any future module can reuse: the file-type registry (`FileType`/`MimeType`/`FileExtension`) and DB-backed file storage (`DBStoredFile`/`DatabaseStorage`), plus the file-serving admin view.
- `src/lifetracker/taxtracker/` (label `taxtracker`) — the tax-tracking module: `FinancialYear`/`Item`/`Attachment`, archive import, and their admin views/templates. This was the original (and currently only) module; more modules (banking, portfolio, insurance, etc.) may be added as siblings of `taxtracker` under `lifetracker` in future.
- `src/lifetracker/cli/` — standalone `click`-based command-line utilities (`.eml`/HAR/ZIP extraction and conversion helpers) unrelated to the Django app; not a Django app, not in `INSTALLED_APPS`. Invoked via the root launcher `cli.py` (e.g. `uv run python cli.py extract-eml <file> <out.zip>`), which inserts `src/` onto `sys.path` the same way `manage.py` does.

`taxtracker` depends on `core` (via the `Attachment.file_type` FK and `DatabaseStorage`); `core` has no dependency on `taxtracker`.

**File storage:** All uploaded attachments are stored in the SQLite database, not on disk. `DatabaseStorage` (in `core/models.py`) is a custom Django storage backend that writes file content to `DBStoredFile` rows. Storage paths have the form `db/<pk>/<filename>`. Files are served through `DBStoredFileAdmin.serve_file_view` at `/admin/core/dbstoredfile/file/<pk>/`.

**Legacy database upgrade:** databases created before the `lifetracker`/`core`/`taxtracker` split still have the old `tracker_*` tables and `django_migrations`/`django_content_type` rows under app label `tracker`. `taxtracker`'s `upgrade_legacy_db` management command renames those tables and re-labels the content types in place; it is idempotent (no-op on fresh or already-upgraded databases) and `run.sh` runs it unconditionally before `migrate`.

## Models

- **`FinancialYear`** (taxtracker) — Australian FY ending 30 June of `year`. E.g. `year=2024` = 1 Jul 2023–30 Jun 2024. Default lodgement date is 31 Oct; overridable.
- **`Item`** (taxtracker) — tax checklist item, self-referential `parent` (tree). Child items always inherit their parent's `year_id`, enforced in both `clean()` and `save()`. Cascade update propagates when a root item's year changes.
- **`FileType` / `MimeType` / `FileExtension`** (core) — file type registry. Each `FileType` must have exactly one primary MIME type and one primary extension; enforced by `_AtLeastOnePrimaryFormSet`.
- **`DBStoredFile`** (core) — raw binary storage row; managed by `DatabaseStorage`.
- **`Attachment`** (taxtracker) — file attached to an `Item`. On `save()`, auto-populates `title` from filename, `file_type` by extension lookup, and `date` by regex-extracting ISO/compact dates from the filename.

## Admin extensions

`FinancialYearAdmin` adds:
- **Summary view** (`/admin/taxtracker/financialyear/<pk>/summary/`) — item tree with done/pending counts.
- **Single-year ZIP** (`download-zip/`) — all attachments in a folder hierarchy with an `index.md`.
- **Multi-year ZIP** (`download-multi-zip/`) — multiple FYs, each under a top-level `FY<year>/` prefix. Also supports "View Index (Markdown)" action.
- **DB backup** (`download-db-backup/`) — superuser-only SQLite serialise download.
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

The Django `SECRET_KEY` is stored in `~/.config/lifetracker/secret_key` (auto-generated on first run). Override with `LIFETRACKER_SECRET_KEY_FILE`. On first run under the new config location, if a key from the pre-rename `~/.config/taxtracker/secret_key` exists, it's copied forward automatically so existing sessions/logins survive.

## Linting config

Ruff: rules E, F, W, I, UP; line length 88; migrations excluded. djlint: profile=django, indent=2.

## Pitfalls

**`except A, B, C:` is valid Python 3 syntax.** Do not flag comma-separated exception types (without parentheses) as "Python 2-style syntax" or a SyntaxError. This is valid in Python 3, and ruff format will restore it if you remove it.

**Legacy-upgraded databases keep old `tracker_`-prefixed index names.** SQLite's `ALTER TABLE ... RENAME TO` (used by `upgrade_legacy_db`) has no equivalent for indexes, so a database that went through the upgrade keeps index names like `tracker_item_parent_id_d6e76dee` on the renamed `taxtracker_item` table, instead of matching what a fresh install would generate. This is cosmetic only — Django never looks up indexes by name at runtime — do not "fix" it.

**Always use `uv run python` for ad-hoc scripts.** Use `uv run python -c '...'` not `python -c` or `python3 -c` — the bare commands won't use the project's virtualenv and may not even exist on the system.
