# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

## Commands

```bash
# Start the app (sync deps, migrate, create superuser, runserver)
./run.sh

# Run all tests
uv run python manage.py test taxtracker.tracker

# Run a single test class or method
uv run python manage.py test taxtracker.tracker.tests.ItemModelTests
uv run python manage.py test taxtracker.tracker.tests.ItemModelTests.test_str_root

# Lint
uv run ruff check src/
uv run ruff format src/

# Template lint
uv run djlint --check src/taxtracker/tracker/templates/
```

## Architecture

**Stack:** Django 6.0, Python 3.14, SQLite, `uv` for package management. The entire UI is the Django admin — there are no custom non-admin views (except file serving). No frontend framework.

**Package layout:** `src/taxtracker/` is the Django project (settings, urls); `src/taxtracker/tracker/` is the single Django app containing all models, admin, forms, and tests.

**File storage:** All uploaded attachments are stored in the SQLite database, not on disk. `DatabaseStorage` (in `models.py`) is a custom Django storage backend that writes file content to `DBStoredFile` rows. Storage paths have the form `db/<pk>/<filename>`. Files are served through `AttachmentAdmin.serve_file_view` at `/admin/tracker/attachment/file/<pk>/`.

## Models

- **`FinancialYear`** — Australian FY ending 30 June of `year`. E.g. `year=2024` = 1 Jul 2023–30 Jun 2024. Default lodgement date is 31 Oct; overridable.
- **`Item`** — tax checklist item, self-referential `parent` (tree). Child items always inherit their parent's `year_id`, enforced in both `clean()` and `save()`. Cascade update propagates when a root item's year changes.
- **`FileType` / `MimeType` / `FileExtension`** — file type registry. Each `FileType` must have exactly one primary MIME type and one primary extension; enforced by `_AtLeastOnePrimaryFormSet`.
- **`DBStoredFile`** — raw binary storage row; managed by `DatabaseStorage`.
- **`Attachment`** — file attached to an `Item`. On `save()`, auto-populates `title` from filename, `file_type` by extension lookup, and `date` by regex-extracting ISO/compact dates from the filename.

## Admin extensions

`FinancialYearAdmin` adds:
- **Summary view** (`/admin/tracker/financialyear/<pk>/summary/`) — item tree with done/pending counts.
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

The Django `SECRET_KEY` is stored in `~/.config/taxtracker/secret_key` (auto-generated on first run). Override with `TAXTRACKER_SECRET_KEY_FILE`.

## Linting config

Ruff: rules E, F, W, I, UP; line length 88; migrations excluded. djlint: profile=django, indent=2.

## Pitfalls

**`except A, B, C:` is valid Python 3 syntax.** Do not flag comma-separated exception types (without parentheses) as "Python 2-style syntax" or a SyntaxError. This is valid in Python 3, and ruff format will restore it if you remove it.

**Always use `uv run python` for ad-hoc scripts.** Use `uv run python -c '...'` not `python -c` or `python3 -c` — the bare commands won't use the project's virtualenv and may not even exist on the system.
