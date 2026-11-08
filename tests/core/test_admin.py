import typing
from typing import override

from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.forms import inlineformset_factory
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from lifetracker.core.admin import DBStoredFileAdmin, MimeTypeFormSet, _AtLeastOnePrimaryFormSet
from lifetracker.core.models import DBStoredFile, FileExtension, FileType, MimeType, database_storage

if typing.TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestResponse
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item, ReceivedDocument


class FileTypePrimaryValidationTests(TestCase):
    """Tests for the 'at least one primary' admin formset validation."""

    @override
    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def _post_filetype(
        self,
        mime_types: list[dict[str, typing.Any]] | None = None,
        extensions: list[dict[str, typing.Any]] | None = None,
    ) -> TestResponse:
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
            data[f"file_extensions-{i}-is_primary"] = "on" if ext.get("is_primary") else ""
            data[f"file_extensions-{i}-id"] = ""
            data[f"file_extensions-{i}-file_type"] = ""
        url = reverse("admin:core_filetype_add")
        return self.client.post(url, data)

    # ------------------------------------------------------------------
    # Auto-set primary (single row, not marked primary)
    # ------------------------------------------------------------------

    def test_single_mime_type_without_primary_auto_sets_primary(self) -> None:
        """Exactly one MIME type with is_primary unchecked should be auto-set."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": False}],
            extensions=[{"extension": "tst", "is_primary": True}],
        )
        self.assertEqual(response.status_code, 302)
        ft = FileType.objects.get(short_name="TST")
        self.assertTrue(ft.mime_types.get().is_primary)

    def test_single_extension_without_primary_auto_sets_primary(self) -> None:
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

    def test_multiple_mime_types_none_primary_fails(self) -> None:
        """Multiple MIME types with none marked primary must fail validation."""
        response = self._post_filetype(
            mime_types=[
                {"mime_type": "application/tst", "is_primary": False},
                {"mime_type": "application/x-tst", "is_primary": False},
            ]
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "At least one MIME type must be marked as primary")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_multiple_extensions_none_primary_fails(self) -> None:
        """Multiple extensions with none marked primary must fail validation."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[
                {"extension": "tst", "is_primary": False},
                {"extension": "ts2", "is_primary": False},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "At least one file extension must be marked as primary")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # Multiple primaries — validation error instead of IntegrityError
    # ------------------------------------------------------------------

    def test_multiple_primary_mime_types_shows_validation_error(self) -> None:
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

    def test_multiple_primary_extensions_shows_validation_error(self) -> None:
        """Multiple extensions all marked primary must produce a validation error."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[
                {"extension": "tst", "is_primary": True},
                {"extension": "ts2", "is_primary": True},
            ],
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only one file extension may be marked as primary")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # Successful saves
    # ------------------------------------------------------------------

    def test_with_primary_set_saves_successfully(self) -> None:
        """FileType with at least one primary mime type and extension should save."""
        response = self._post_filetype(
            mime_types=[{"mime_type": "application/tst", "is_primary": True}],
            extensions=[{"extension": "tst", "is_primary": True}],
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(FileType.objects.filter(short_name="TST").exists())

    def test_only_extensions_no_mime_types_fails(self) -> None:
        """FileType with only file extensions (no MIME types) must fail validation."""
        response = self._post_filetype(extensions=[{"extension": "tst", "is_primary": True}])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A file type must have at least one MIME type")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    def test_only_mime_types_no_extensions_fails(self) -> None:
        """FileType with only MIME types (no file extensions) must fail validation."""
        response = self._post_filetype(mime_types=[{"mime_type": "application/tst", "is_primary": True}])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A file type must have at least one file extension")
        self.assertFalse(FileType.objects.filter(short_name="TST").exists())

    # ------------------------------------------------------------------
    # At-least-one independent requirement
    # ------------------------------------------------------------------

    def test_no_mime_or_extension_fails_validation(self) -> None:
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

    def _stored_file(self, filename: str = "served.txt", content: bytes = b"Hello attachment content") -> DBStoredFile:
        return DBStoredFile.objects.create(filename=filename, content=content)

    def _stored_file_owned_by_attachment(
        self, filename: str = "served.txt", content: bytes = b"Hello attachment content"
    ) -> DBStoredFile:
        fy = FinancialYear.objects.create(year=2024)
        item = Item.objects.create(year=fy, title="Owned Item", order=1)
        attachment = Attachment.objects.create(
            item=item,
            title=filename,
            file=ContentFile(content, name=filename),
        )
        stored_file_pk = database_storage._name_to_pk(attachment.file.name)
        return DBStoredFile.objects.get(pk=stored_file_pk)

    def _staff_client(self, username: str, *permission_codenames: str) -> Client:
        user = User.objects.create_user(username, f"{username}@example.com", "pass")
        user.is_staff = True
        user.save()
        for codename in permission_codenames:
            user.user_permissions.add(Permission.objects.get(codename=codename))
        client = Client()
        client.force_login(user)
        return client

    def test_serve_file_view(self) -> None:
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

    def _get_as_superuser(self, obj: DBStoredFile) -> TestResponse:
        client = Client()
        client.force_login(User.objects.create_superuser(f"admin{obj.pk}"))
        return client.get(reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk]))

    def test_safe_types_are_served_inline(self) -> None:
        for filename, content_type in [
            ("doc.pdf", "application/pdf"),
            ("photo.jpg", "image/jpeg"),
            ("photo.png", "image/png"),
            ("anim.gif", "image/gif"),
            ("pic.webp", "image/webp"),
            ("notes.txt", "text/plain"),
        ]:
            with self.subTest(filename=filename):
                response = self._get_as_superuser(self._stored_file(filename))
                self.assertEqual(response["Content-Type"], content_type)
                self.assertTrue(
                    response["Content-Disposition"].startswith("inline;"),
                    response["Content-Disposition"],
                )
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_types_that_can_run_script_are_downloaded(self) -> None:
        """An HTML or SVG attachment must never render as a page on the admin
        origin, where its script would run with the viewer's session."""
        for filename in [
            "page.html",
            "page.htm",
            "image.svg",
            "data.xml",
            "page.xhtml",
            "script.js",
            "mystery.notarealext",
        ]:
            with self.subTest(filename=filename):
                response = self._get_as_superuser(self._stored_file(filename, b"<script>alert(1)</script>"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response["Content-Disposition"],
                    f"attachment; filename=\"{filename}\"; filename*=UTF-8''{filename}",
                )
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertEqual(
                    response["Content-Security-Policy"],
                    "sandbox; default-src 'none'",
                )

    def test_compressed_files_are_downloaded_as_bytes(self) -> None:
        """ "x.txt.gz" guesses as text/plain with gzip encoding; showing it as
        text would show the compressed bytes."""
        for filename in ["notes.txt.gz", "doc.pdf.bz2", "page.html.xz"]:
            with self.subTest(filename=filename):
                response = self._get_as_superuser(self._stored_file(filename))
                self.assertEqual(response["Content-Type"], "application/octet-stream")
                self.assertTrue(
                    response["Content-Disposition"].startswith("attachment;"),
                    response["Content-Disposition"],
                )

    def test_inline_images_and_text_are_sandboxed(self) -> None:
        for filename in ["photo.png", "notes.txt"]:
            with self.subTest(filename=filename):
                response = self._get_as_superuser(self._stored_file(filename))
                self.assertEqual(
                    response["Content-Security-Policy"],
                    "sandbox; default-src 'none'",
                )

    def test_pdf_is_not_sandboxed(self) -> None:
        """Browsers' PDF viewers won't run in a sandboxed document."""
        response = self._get_as_superuser(self._stored_file("doc.pdf"))
        self.assertNotIn("Content-Security-Policy", response)

    def test_filename_is_escaped_in_header(self) -> None:
        response = self._get_as_superuser(self._stored_file('a"b\\c é.html'))
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="a\\"b\\\\c .html"; filename*=UTF-8\'\'a%22b%5Cc%20%C3%A9.html',
        )

    def test_serve_file_view_requires_login(self) -> None:
        """Unauthenticated requests to the serve view should redirect to login."""
        obj = self._stored_file("secret.pdf", b"secret pdf bytes")
        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        response = Client().get(url)
        # Django admin redirects unauthenticated users to the login page
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_serve_file_view_requires_owner_model_view_permission(self) -> None:
        obj = self._stored_file_owned_by_attachment("owned.pdf", b"owned")
        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        client = self._staff_client("staff-no-view")
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_serve_file_view_allows_owner_model_view_permission(self) -> None:
        content = b"owned content"
        obj = self._stored_file_owned_by_attachment("owned.pdf", content)
        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        client = self._staff_client("staff-view", "view_attachment")
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, content)

    def test_serve_file_view_received_document_attachment(self) -> None:
        """A received document's attachment is checked through Attachment's admin."""
        fy = FinancialYear.objects.create(year=2024)
        doc = ReceivedDocument.objects.create(year=fy, title="Assessment")
        attachment = Attachment.objects.create(
            received_document=doc,
            title="assessment.pdf",
            file=ContentFile(b"assessment", name="assessment.pdf"),
        )
        pk = database_storage._name_to_pk(attachment.file.name)
        url = reverse("admin:core_dbstoredfile_serve_file", args=[pk])
        denied = self._staff_client("staff-doc-no-view", "view_receiveddocument")
        self.assertEqual(denied.get(url).status_code, 403)
        allowed = self._staff_client("staff-doc-view", "view_attachment")
        response = allowed.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"assessment")


class AtLeastOnePrimaryFormSetTests(TestCase):
    """Direct unit tests for base-class branches not reachable through the
    concrete MimeTypeFormSet/FileExtensionFormSet subclasses used in the admin."""

    @override
    def setUp(self) -> None:
        self.ft = FileType.objects.create(short_name="TST", full_name="Test Type")

    def test_field_errors_short_circuit_formset_clean(self) -> None:
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

    def test_no_nonempty_error_returns_when_no_rows(self) -> None:
        """When a subclass leaves _nonempty_error unset, clean() is a no-op for
        an empty formset instead of raising."""

        class _NoNonemptyErrorFormSet(_AtLeastOnePrimaryFormSet[MimeType]):
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

    @override
    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client = Client()
        self.client.login(username="admin", password="pass")

    def test_changelist_shows_primary_mime_type_and_extension(self) -> None:
        ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        MimeType.objects.create(file_type=ft, mime_type="application/pdf", is_primary=True)
        FileExtension.objects.create(file_type=ft, extension="pdf", is_primary=True)

        response = self.client.get(reverse("admin:core_filetype_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "application/pdf")
        self.assertContains(response, "pdf")

    def test_changelist_shows_placeholder_when_no_primary(self) -> None:
        FileType.objects.create(short_name="NOP", full_name="No Primary")

        response = self.client.get(reverse("admin:core_filetype_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "—")  # em dash placeholder


class DBStoredFileAdminPermissionTests(TestCase):
    """DBStoredFileAdmin exists only to host the serve-file view; every
    permission check should always deny."""

    @override
    def setUp(self) -> None:
        self.admin = DBStoredFileAdmin(DBStoredFile, admin.site)

    def test_permissions_all_denied(self) -> None:
        request = RequestFactory().get("/")
        self.assertFalse(self.admin.has_add_permission(request))
        self.assertFalse(self.admin.has_change_permission(request))
        self.assertFalse(self.admin.has_delete_permission(request))


class ServeFileViewMimeTypeFallbackTests(TestCase):
    """serve_file_view should fall back to application/octet-stream for
    filenames with no guessable MIME type."""

    def test_unknown_extension_falls_back_to_octet_stream(self) -> None:
        User.objects.create_superuser("admin", "a@b.com", "pass")
        client = Client()
        client.login(username="admin", password="pass")

        obj = DBStoredFile.objects.create(filename="mystery.notarealext", content=b"binary data")
        url = reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk])
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
