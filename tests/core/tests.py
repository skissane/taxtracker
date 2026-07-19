import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import coverage
from django.contrib import admin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError
from django.forms import inlineformset_factory
from django.test import Client, TestCase
from django.urls import reverse

from lifetracker.asgi import application as asgi_application
from lifetracker.core.admin import (
    DBStoredFileAdmin,
    MimeTypeFormSet,
    _AtLeastOnePrimaryFormSet,
)
from lifetracker.core.models import (
    DatabaseStorage,
    DBStoredFile,
    FileExtension,
    FileType,
    MimeType,
)
from lifetracker.wsgi import application as wsgi_application


class FileTypeModelTests(TestCase):
    """Tests for FileType, MimeType, and FileExtension models."""

    def setUp(self):
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")

    def test_filetype_str(self):
        self.assertEqual(str(self.ft), "PDF")

    def test_filetype_short_name_unique(self):
        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF", full_name="Other PDF")

    def test_filetype_full_name_unique(self):
        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF2", full_name="PDF Document")

    # ------------------------------------------------------------------
    # MimeType tests
    # ------------------------------------------------------------------

    def test_mimetype_str(self):
        mt = MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        self.assertEqual(str(mt), "application/pdf")

    def test_mimetype_valid_formats(self):
        valid = [
            "application/pdf",
            "image/jpeg",
            "text/plain",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/x-pdf",
        ]
        for mt_str in valid:
            mt = MimeType(file_type=self.ft, mime_type=mt_str)
            mt.clean()  # should not raise

    def test_mimetype_invalid_no_slash(self):
        mt = MimeType(file_type=self.ft, mime_type="applicationpdf")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_invalid_two_slashes(self):
        mt = MimeType(file_type=self.ft, mime_type="application/pdf/extra")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_invalid_with_space(self):
        mt = MimeType(file_type=self.ft, mime_type="application /pdf")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_globally_unique(self):
        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(
                file_type=ft2, mime_type="application/pdf", is_primary=False
            )

    def test_at_most_one_primary_mime_type_per_file_type(self):
        MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(
                file_type=self.ft, mime_type="application/x-pdf", is_primary=True
            )

    # ------------------------------------------------------------------
    # FileExtension tests
    # ------------------------------------------------------------------

    def test_extension_str(self):
        ext = FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        self.assertEqual(str(ext), "pdf")

    def test_extension_forced_lowercase_on_save(self):
        ext = FileExtension(file_type=self.ft, extension="PDF", is_primary=True)
        ext.save()
        ext.refresh_from_db()
        self.assertEqual(ext.extension, "pdf")

    def test_extension_forced_lowercase_in_clean(self):
        ext = FileExtension(file_type=self.ft, extension="PDF")
        ext.clean()
        self.assertEqual(ext.extension, "pdf")

    def test_extension_invalid_with_dot(self):
        ext = FileExtension(file_type=self.ft, extension=".pdf")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_invalid_with_slash(self):
        ext = FileExtension(file_type=self.ft, extension="pd/f")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_invalid_with_space(self):
        ext = FileExtension(file_type=self.ft, extension="p df")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_globally_unique(self):
        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(
                file_type=ft2, extension="pdf", is_primary=False
            )

    def test_at_most_one_primary_extension_per_file_type(self):
        FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(
                file_type=self.ft, extension="pdff", is_primary=True
            )


class FileTypePrimaryValidationTests(TestCase):
    """Tests for the 'at least one primary' admin formset validation."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def _post_filetype(self, mime_types=None, extensions=None):
        """POST to FileType add view with inline mime types and extensions."""
        data = {
            "short_name": "TST",
            "full_name": "Test Type",
            # Management forms
            "mime_types-TOTAL_FORMS": str(len(mime_types or [])),
            "mime_types-INITIAL_FORMS": "0",
            "mime_types-MIN_NUM_FORMS": "0",
            "mime_types-MAX_NUM_FORMS": "1000",
            "file_extensions-TOTAL_FORMS": str(len(extensions or [])),
            "file_extensions-INITIAL_FORMS": "0",
            "file_extensions-MIN_NUM_FORMS": "0",
            "file_extensions-MAX_NUM_FORMS": "1000",
        }
        for i, mt in enumerate(mime_types or []):
            data[f"mime_types-{i}-mime_type"] = mt["mime_type"]
            data[f"mime_types-{i}-is_primary"] = "on" if mt.get("is_primary") else ""
            data[f"mime_types-{i}-id"] = ""
            data[f"mime_types-{i}-file_type"] = ""
        for i, ext in enumerate(extensions or []):
            data[f"file_extensions-{i}-extension"] = ext["extension"]
            data[f"file_extensions-{i}-is_primary"] = (
                "on" if ext.get("is_primary") else ""
            )
            data[f"file_extensions-{i}-id"] = ""
            data[f"file_extensions-{i}-file_type"] = ""
        url = reverse("admin:core_filetype_add")
        return self.client.post(url, data)

    # ------------------------------------------------------------------
    # Auto-set primary (single row, not marked primary)
    # ------------------------------------------------------------------

    def test_single_mime_type_without_primary_auto_sets_primary(self):
        """Exactly one MIME type with is_primary unchecked should be auto-set."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": False}],
            extensions=[{"extension": "tst", "is_primary": True}],
        )
        self.assertEqual(response.status_code, 302)
        ft = FileType.objects.get(short_name="TST")
        self.assertTrue(ft.mime_types.get().is_primary)

    def test_single_extension_without_primary_auto_sets_primary(self):
        """Exactly one extension with is_primary unchecked should be auto-set."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[{"extension": "tst", "is_primary": False}],
        )
        self.assertEqual(response.status_code, 302)
        ft = FileType.objects.get(short_name="TST")
        self.assertTrue(ft.file_extensions.get().is_primary)

    # ------------------------------------------------------------------
    # Multiple rows — primary required, or error
    # ------------------------------------------------------------------

    def test_multiple_mime_types_none_primary_fails(self):
        """Multiple MIME types with none marked primary must fail validation."""
        response = self._post_filetype(
            mime_types=[
                {"mime_type": "application/tst", "is_primary": False},
                {"mime_type": "application/x-tst", "is_primary": False},
            ]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "At least one MIME type must be marked as primary"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_multiple_extensions_none_primary_fails(self):
        """Multiple extensions with none marked primary must fail validation."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[
                {"extension": "tst", "is_primary": False},
                {"extension": "ts2", "is_primary": False},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "At least one file extension must be marked as primary"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # Multiple primaries — validation error instead of IntegrityError
    # ------------------------------------------------------------------

    def test_multiple_primary_mime_types_shows_validation_error(self):
        """Multiple MIME types all marked primary must produce a validation error."""
        response = self._post_filetype(
            mime_types=[
                {"mime_type": "application/tst", "is_primary": True},
                {"mime_type": "application/x-tst", "is_primary": True},
            ]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only one MIME type may be marked as primary")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_multiple_primary_extensions_shows_validation_error(self):
        """Multiple extensions all marked primary must produce a validation error."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[
                {"extension": "tst", "is_primary": True},
                {"extension": "ts2", "is_primary": True},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Only one file extension may be marked as primary"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # Successful saves
    # ------------------------------------------------------------------

    def test_with_primary_set_saves_successfully(self):
        """FileType with at least one primary mime type and extension should save."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[{"extension": "tst", "is_primary": True}],
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(FileType.objects.filter(short_name="TST").exists())

    def test_only_extensions_no_mime_types_fails(self):
        """FileType with only file extensions (no MIME types) must fail validation."""
        response = self._post_filetype(
            extensions=[{"extension": "tst", "is_primary": True}]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A file type must have at least one MIME type")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_only_mime_types_no_extensions_fails(self):
        """FileType with only MIME types (no file extensions) must fail validation."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "A file type must have at least one file extension"
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # At-least-one independent requirement
    # ------------------------------------------------------------------

    def test_no_mime_or_extension_fails_validation(self):
        """Saving a FileType with no MIME types and no extensions must fail."""
        response = self._post_filetype()
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "A file type must have at least one MIME type",
        )
        self.assertContains(
            response,
            "A file type must have at least one file extension",
        )
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())


class ServeFileViewTests(TestCase):
    """Tests for the DBStoredFile serve-file admin view."""

    def _stored_file(self, filename="served.txt", content=b"Hello attachment content"):
        return DBStoredFile.objects.create(filename=filename, content=content)

    def test_serve_file_view(self):
        """The serve_file_view should return the correct file content."""
        User.objects.create_superuser("admin", "a@b.com", "pass")
        client = Client()
        client.login(username="admin", password="pass")

        content = b"Hello attachment content"
        obj = self._stored_file("served.txt", content)

        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content)
        self.assertIn("served.txt", response.get("Content-Disposition", ""))

    def test_serve_file_view_requires_login(self):
        """Unauthenticated requests to the serve view should redirect to login."""
        obj = self._stored_file("secret.pdf", b"secret pdf bytes")
        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        response = Client().get(url)
        # Django admin redirects unauthenticated users to the login page
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])


class DatabaseStorageTests(TestCase):
    """Tests for the DatabaseStorage custom storage backend."""

    def setUp(self):
        self.storage = DatabaseStorage()

    def test_dbstoredfile_str(self):
        obj = DBStoredFile.objects.create(filename="report.pdf", content=b"data")
        self.assertEqual(str(obj), "report.pdf")

    def test_save_and_open_round_trip(self):
        name = self.storage.save("report.pdf", ContentFile(b"hello world"))
        self.assertTrue(name.startswith("db/"))
        self.assertTrue(name.endswith("/report.pdf"))
        with self.storage.open(name) as f:
            self.assertEqual(f.read(), b"hello world")

    def test_exists_true_for_saved_file(self):
        name = self.storage.save("a.txt", ContentFile(b"x"))
        self.assertTrue(self.storage.exists(name))

    def test_exists_false_for_missing_pk(self):
        self.assertFalse(self.storage.exists("db/999999/missing.txt"))

    def test_exists_false_for_malformed_name(self):
        self.assertFalse(self.storage.exists("not-a-db-path"))

    def test_size(self):
        name = self.storage.save("sized.txt", ContentFile(b"0123456789"))
        self.assertEqual(self.storage.size(name), 10)

    def test_delete_removes_stored_file(self):
        name = self.storage.save("deleteme.txt", ContentFile(b"bye"))
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))

    def test_delete_malformed_name_is_a_no_op(self):
        # Should not raise even though the name can't be parsed into a pk.
        self.storage.delete("not-a-db-path")

    def test_delete_missing_pk_is_a_no_op(self):
        # Valid path shape but no matching row — filter().delete() is a no-op.
        self.storage.delete("db/999999/missing.txt")

    def test_url(self):
        name = self.storage.save("linked.txt", ContentFile(b"y"))
        pk = DBStoredFile.objects.get().pk
        self.assertEqual(
            self.storage.url(name),
            reverse("admin:core_dbstoredfile_serve_file", args=[pk]),
        )

    def test_get_available_name_short_name_unchanged(self):
        self.assertEqual(
            self.storage.get_available_name("short.txt", max_length=100), "short.txt"
        )

    def test_get_available_name_truncates_long_stem(self):
        name = "a" * 50 + ".txt"
        result = self.storage.get_available_name(name, max_length=20)
        self.assertTrue(result.endswith(".txt"))
        self.assertLessEqual(len(result), 20)

    def test_get_available_name_truncates_hard_when_suffix_too_long(self):
        # allowed <= 0: suffix alone exceeds max_length, so we hard-truncate.
        name = "a." + "b" * 30
        result = self.storage.get_available_name(name, max_length=10)
        self.assertEqual(result, name[:10])

    def test_name_to_pk_rejects_non_str(self):
        with self.assertRaises(ValueError):
            self.storage._name_to_pk(123)

    def test_name_to_pk_rejects_missing_prefix(self):
        with self.assertRaises(ValueError):
            self.storage._name_to_pk("other/1/file.txt")

    def test_name_to_pk_rejects_non_integer_pk(self):
        with self.assertRaises(ValueError):
            self.storage._name_to_pk("db/notanumber/file.txt")


class AtLeastOnePrimaryFormSetTests(TestCase):
    """Direct unit tests for base-class branches not reachable through the
    concrete MimeTypeFormSet/FileExtensionFormSet subclasses used in the admin."""

    def setUp(self):
        self.ft = FileType.objects.create(short_name="TST", full_name="Test Type")

    def test_field_errors_short_circuit_formset_clean(self):
        """An invalid mime_type value should short-circuit clean() via
        any(self.errors)."""
        FormSet = inlineformset_factory(
            FileType,
            MimeType,
            formset=MimeTypeFormSet,
            fields=["mime_type", "is_primary"],
            extra=0,
        )
        data = {
            "mime_types-TOTAL_FORMS": "1",
            "mime_types-INITIAL_FORMS": "0",
            "mime_types-MIN_NUM_FORMS": "0",
            "mime_types-MAX_NUM_FORMS": "1000",
            "mime_types-0-mime_type": "not-a-valid-mime-type",
            "mime_types-0-is_primary": "on",
            "mime_types-0-id": "",
            "mime_types-0-file_type": "",
        }
        fs = FormSet(data, instance=self.ft, prefix="mime_types")
        self.assertFalse(fs.is_valid())
        self.assertTrue(any(fs.errors))

    def test_no_nonempty_error_returns_when_no_rows(self):
        """When a subclass leaves _nonempty_error unset, clean() is a no-op for
        an empty formset instead of raising."""

        class _NoNonemptyErrorFormSet(_AtLeastOnePrimaryFormSet):
            pass

        FormSet = inlineformset_factory(
            FileType,
            MimeType,
            formset=_NoNonemptyErrorFormSet,
            fields=["mime_type", "is_primary"],
            extra=0,
        )
        data = {
            "mime_types-TOTAL_FORMS": "0",
            "mime_types-INITIAL_FORMS": "0",
            "mime_types-MIN_NUM_FORMS": "0",
            "mime_types-MAX_NUM_FORMS": "1000",
        }
        fs = FormSet(data, instance=self.ft, prefix="mime_types")
        self.assertTrue(fs.is_valid())


class FileTypeAdminDisplayTests(TestCase):
    """Tests for FileTypeAdmin's primary_mime_type/primary_extension columns."""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def test_changelist_shows_primary_mime_type_and_extension(self):
        ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        MimeType.objects.create(
            file_type=ft, mime_type="application/pdf", is_primary=True
        )
        FileExtension.objects.create(file_type=ft, extension="pdf", is_primary=True)

        response = self.client.get(reverse("admin:core_filetype_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "application/pdf")
        self.assertContains(response, "pdf")

    def test_changelist_shows_placeholder_when_no_primary(self):
        FileType.objects.create(short_name="NOP", full_name="No Primary")

        response = self.client.get(reverse("admin:core_filetype_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "—")  # em dash placeholder


class DBStoredFileAdminPermissionTests(TestCase):
    """DBStoredFileAdmin exists only to host the serve-file view; every
    permission check should always deny."""

    def setUp(self):
        self.admin = DBStoredFileAdmin(DBStoredFile, admin.site)

    def test_permissions_all_denied(self):
        self.assertFalse(self.admin.has_add_permission(None))
        self.assertFalse(self.admin.has_change_permission(None))
        self.assertFalse(self.admin.has_delete_permission(None))


class ServeFileViewMimeTypeFallbackTests(TestCase):
    """serve_file_view should fall back to application/octet-stream for
    filenames with no guessable MIME type."""

    def test_unknown_extension_falls_back_to_octet_stream(self):
        User.objects.create_superuser("admin", "a@b.com", "pass")
        client = Client()
        client.login(username="admin", password="pass")

        obj = DBStoredFile.objects.create(
            filename="mystery.notarealext", content=b"binary data"
        )
        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")


class ProjectEntryPointTests(TestCase):
    """The asgi/wsgi entry-point modules are never imported by the app code
    itself (a real deployment imports them from outside the project), so
    nothing else exercises them."""

    def test_wsgi_application_is_importable(self):
        self.assertTrue(callable(wsgi_application))

    def test_asgi_application_is_importable(self):
        self.assertTrue(callable(asgi_application))


class SettingsSecretKeyTests(TestCase):
    """settings.py computes SECRET_KEY at import time, from a file under
    $HOME, before Django is even configured. It can only be exercised by
    importing it fresh in a subprocess with a controlled $HOME — the version
    already imported for this test run reflects whatever the real ~/.config
    looked like when the suite started."""

    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    SRC_DIR = REPO_ROOT / "src"

    def _run(self, home, extra_env=None):
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.SRC_DIR),
        }
        if extra_env:
            env.update(extra_env)

        code = "import lifetracker.settings as s; print(s.SECRET_KEY)"
        # When this test run itself is under `coverage run`, also measure the
        # child interpreter, so the many settings.py branches only reachable
        # via subprocess (see class docstring) count towards the total.
        if coverage.Coverage.current() is not None:
            env["COVERAGE_PROCESS_START"] = str(self.REPO_ROOT / "pyproject.toml")
            code = "import coverage; coverage.process_startup(); " + code

        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            cwd=str(self.REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def test_fresh_install_generates_and_persists_a_key(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            printed_key = proc.stdout.strip()
            self.assertTrue(printed_key)

            key_file = Path(home) / ".config" / "lifetracker" / "secret_key"
            self.assertEqual(key_file.read_text().strip(), printed_key)
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_old_taxtracker_key_is_copied_forward(self):
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "legacy-secret-key-content")

            new_key_file = Path(home) / ".config" / "lifetracker" / "secret_key"
            self.assertEqual(
                new_key_file.read_text().strip(), "legacy-secret-key-content"
            )

    def test_unreadable_old_key_falls_back_to_fresh_key(self):
        """An OSError while reading the legacy key must not be fatal — it
        should just skip the migration and generate a fresh key instead."""
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            old_file = old_dir / "secret_key"
            old_file.write_text("legacy-secret-key-content\n")
            old_file.chmod(0o000)
            try:
                proc = self._run(home)
            finally:
                old_file.chmod(0o600)  # so TemporaryDirectory cleanup can remove it

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotEqual(proc.stdout.strip(), "legacy-secret-key-content")

    def test_migration_write_failure_is_swallowed_and_falls_through(self):
        """If the new key's directory can't be created during migration, that
        failure is swallowed — but the identical failure then hits again
        during fresh-key generation, which is fatal."""
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            # A file where the new key's parent directory should be.
            (Path(home) / ".config" / "lifetracker").write_text("not a directory")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Could not write secret key file", proc.stderr)

    def test_unreadable_existing_key_file_raises(self):
        with tempfile.TemporaryDirectory() as home:
            new_dir = Path(home) / ".config" / "lifetracker"
            new_dir.mkdir(parents=True)
            new_file = new_dir / "secret_key"
            new_file.write_text("blocked\n")
            new_file.chmod(0o000)
            try:
                proc = self._run(home)
            finally:
                new_file.chmod(0o600)

            self.assertEqual(proc.returncode, 1)
            self.assertIn("Could not read secret key file", proc.stderr)

    def test_unwritable_key_directory_raises(self):
        with tempfile.TemporaryDirectory() as home:
            config_dir = Path(home) / ".config"
            config_dir.mkdir(parents=True)
            # A file where the new key's parent directory should be, and no
            # legacy key present — so migration is skipped entirely and the
            # fresh-key write is the first (and only) thing that fails.
            (config_dir / "lifetracker").write_text("not a directory")

            proc = self._run(home)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Could not write secret key file", proc.stderr)

    def test_override_env_var_skips_migration(self):
        with tempfile.TemporaryDirectory() as home:
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            override_path = Path(home) / "custom" / "secret_key"

            proc = self._run(
                home, extra_env={"LIFETRACKER_SECRET_KEY_FILE": str(override_path)}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotEqual(proc.stdout.strip(), "legacy-secret-key-content")
            self.assertTrue(override_path.exists())


class EnsureSuperuserCommandTests(TestCase):
    """Tests for the ensure_superuser management command."""

    def setUp(self):
        # Point the initial-admin-password lookup at a file inside a fresh
        # temp dir that doesn't exist unless a test writes to it, so this
        # never picks up a real ~/.config/lifetracker/initial_admin_password.
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.password_file = Path(tmpdir.name) / "initial_admin_password"
        patcher = mock.patch(
            "lifetracker.core.management.commands.ensure_superuser."
            "INITIAL_ADMIN_PASSWORD_FILE",
            self.password_file,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, **kwargs):
        """Call ensure_superuser and return captured stdout."""
        out = io.StringIO()
        call_command("ensure_superuser", stdout=out, **kwargs)
        return out.getvalue()

    def test_creates_new_user_with_random_password(self):
        output = self._call(username="testadmin", email="a@example.com")
        self.assertTrue(User.objects.filter(username="testadmin").exists())
        user = User.objects.get(username="testadmin")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_active)
        self.assertIn("testadmin", output)
        # Password is the last word on the first output line, 32 hex chars.
        first_line = output.splitlines()[0]
        printed_password = first_line.split()[-1]
        self.assertEqual(len(printed_password), 32)
        self.assertTrue(user.check_password(printed_password))

    def test_default_username_is_admin(self):
        output = self._call()
        self.assertTrue(User.objects.filter(username="admin").exists())
        self.assertIn("admin", output)

    def test_creates_new_user_with_password_from_file(self):
        self.password_file.parent.mkdir(parents=True, exist_ok=True)
        self.password_file.write_text("from-the-file\n")
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-file"))
        # The password itself must never be printed.
        self.assertNotIn("from-the-file", output)
        self.assertIn(str(self.password_file), output)

    def test_empty_password_file_falls_back_to_random_password(self):
        self.password_file.parent.mkdir(parents=True, exist_ok=True)
        self.password_file.write_text("   \n")
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        first_line = output.splitlines()[0]
        printed_password = first_line.split()[-1]
        self.assertEqual(len(printed_password), 32)
        self.assertTrue(user.check_password(printed_password))

    def test_existing_user_no_change_when_already_superuser_with_password(self):
        User.objects.create_superuser("admin", "", "existingpassword")
        output = self._call(username="admin")
        # No password should be printed when user already has one
        self.assertEqual(output.strip(), "")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("existingpassword"))

    def test_existing_user_flags_repaired(self):
        user = User.objects.create_user("admin", "", "existingpassword")
        user.is_active = False
        user.is_staff = False
        user.is_superuser = False
        user.save()
        self._call(username="admin")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        # Password unchanged — existing password should still work
        self.assertTrue(user.check_password("existingpassword"))

    def test_existing_user_password_left_alone_when_unusable(self):
        user = User.objects.create_superuser("admin", "", "tmp")
        user.set_unusable_password()
        user.save()
        output = self._call(username="admin")
        self.assertEqual(output.strip(), "")
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
