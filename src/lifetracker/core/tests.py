from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from .models import DBStoredFile, FileExtension, FileType, MimeType


class FileTypeModelTests(TestCase):
    """Tests for FileType, MimeType, and FileExtension models."""

    def setUp(self):
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")

    def test_filetype_str(self):
        self.assertEqual(str(self.ft), "PDF")

    def test_filetype_short_name_unique(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF", full_name="Other PDF")

    def test_filetype_full_name_unique(self):
        from django.db import IntegrityError

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
        from django.db import IntegrityError

        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        MimeType.objects.create(
            file_type=self.ft, mime_type="application/pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(
                file_type=ft2, mime_type="application/pdf", is_primary=False
            )

    def test_at_most_one_primary_mime_type_per_file_type(self):
        from django.db import IntegrityError

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
        from django.db import IntegrityError

        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        FileExtension.objects.create(
            file_type=self.ft, extension="pdf", is_primary=True
        )
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(
                file_type=ft2, extension="pdf", is_primary=False
            )

    def test_at_most_one_primary_extension_per_file_type(self):
        from django.db import IntegrityError

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
