import ast
import dataclasses
import datetime
import importlib.util
import io
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import coverage
import gunicorn.config
import gunicorn.glogging
import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.staticfiles import finders
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import ConnectionHandler
from django.forms import inlineformset_factory
from django.http import HttpResponse
from django.template import Context, Template
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone
from django.utils.log import ServerFormatter
from django_google_sso.main import GoogleAuth

from lifetracker import google_sso, gunicorn_conf
from lifetracker.admin_site import (
    _TEXT_STYLE,
    LifetrackerAdminSite,
    _build_admin_site_header,
)
from lifetracker.asgi import application as asgi_application
from lifetracker.core import client_ips, idle_watchdog, jwt_auth, uptime_log
from lifetracker.core.admin import (
    DBStoredFileAdmin,
    MimeTypeFormSet,
    _AtLeastOnePrimaryFormSet,
    _format_duration,
    idle_watchdog_page_context,
)
from lifetracker.core.attachments import (
    extract_date_from_filename,
    file_type_for_filename,
    orphaned_stored_file_pks,
)
from lifetracker.core.auth_whitelist import auth_whitelist, is_whitelisted
from lifetracker.core.backup import (
    CLOUD_BACKUP_S3_CONFIG,
    backup_database,
    run_cloud_backup,
)
from lifetracker.core.forms import FlexibleDateField
from lifetracker.core.management.commands.ensure_superuser import (
    _initial_admin_password_file,
)
from lifetracker.core.models import (
    CLOUD_BACKUP_STATUS_PK,
    CloudBackupStatus,
    DatabaseStorage,
    DBStoredFile,
    FileExtension,
    FileType,
    MimeType,
)
from lifetracker.core.timing import REQUEST_START_ATTR
from lifetracker.settings import fly_machine_url
from lifetracker.tasktracker.models import Task, TaskAttachment
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item
from lifetracker.wsgi import application as wsgi_application

_GOOGLE_SSO_ENABLED_SETTINGS = {
    "GOOGLE_SSO_ENABLED": True,
    "GOOGLE_SSO_CLIENT_ID": "test-client-id",
    "GOOGLE_SSO_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_SSO_PROJECT_ID": "test-project-id",
    "GOOGLE_SSO_CALLBACK_DOMAIN": "testserver",
}


class GoogleSSOSettingsTests(TestCase):
    """Tests for the locked-down Google SSO defaults."""

    def test_google_sso_defaults_block_new_accounts(self):
        self.assertIs(settings.GOOGLE_SSO_AUTO_CREATE_USERS, False)
        self.assertEqual(
            settings.GOOGLE_SSO_PRE_VALIDATE_CALLBACK,
            "lifetracker.google_sso.pre_validate_user",
        )

    def test_google_sso_admin_login_button_renders_when_enabled(self):
        with override_settings(**_GOOGLE_SSO_ENABLED_SETTINGS):
            response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in with Google")
        self.assertContains(response, 'href="/google_sso/login/"')
        self.assertContains(response, 'src="/static/core/google_g_logo.svg"')
        self.assertNotContains(response, "upload.wikimedia.org")

    def test_google_sso_logo_is_a_static_file(self):
        # Asserting the src attribute alone would pass even if nothing served it.
        url = settings.GOOGLE_SSO_LOGO_URL(RequestFactory().get("/"))
        path = url.removeprefix(settings.STATIC_URL)
        self.assertIsNotNone(finders.find(path))

    @override_settings(GOOGLE_SSO_ENABLED=False)
    def test_google_sso_admin_login_button_hidden_when_disabled(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sign in with Google")
        # The stock username/password form must still be there.
        self.assertContains(response, 'id="login-form"')

    def test_google_sso_start_login_redirects_to_google_when_enabled(self):
        with override_settings(**_GOOGLE_SSO_ENABLED_SETTINGS):
            response = self.client.get(reverse("django_google_sso:oauth_start_login"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].startswith("https://accounts.google.com/"),
            response["Location"],
        )

    def test_google_sso_start_login_keeps_only_admin_next(self):
        # Any other next makes the library's callback fail with "Google SSO not
        # enabled for Pages"; the login page passes on next=/ for a logged-out
        # visit to the bare domain, which the proxy redirects there.
        cases = [
            ("/admin/core/", "/admin/core/"),
            ("/", "/admin/"),
            ("null", "/admin/"),
            ("https://example.com/admin/", "/admin/"),
            (None, "/admin/"),
        ]
        for next_url, expected in cases:
            with self.subTest(next_url=next_url):
                query = {} if next_url is None else {"next": next_url}
                with override_settings(**_GOOGLE_SSO_ENABLED_SETTINGS):
                    response = self.client.get(
                        reverse("django_google_sso:oauth_start_login"), query
                    )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.client.session["sso_next_url"], expected)

    @override_settings(GOOGLE_SSO_ENABLED=False)
    def test_google_sso_routes_404_when_disabled(self):
        for name in ("oauth_start_login", "oauth_callback"):
            with self.subTest(name=name):
                response = self.client.get(reverse(f"django_google_sso:{name}"))
                self.assertEqual(response.status_code, 404)

    def test_google_sso_urls_match_library(self):
        """lifetracker.google_sso_urls must mirror django_google_sso.urls.

        Parsed rather than imported: the library module's urlpatterns depend on
        whether SSO was enabled when it was first imported. If this fails after
        upgrading django-google-sso, update lifetracker/google_sso_urls.py.
        """

        def routes(module_name):
            origin = importlib.util.find_spec(module_name).origin
            tree = ast.parse(Path(origin).read_text())
            found = set()
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "path"
                ):
                    continue
                route, view = node.args
                # Unwrap our wrappers, e.g. _require_sso_enabled(views.x), to views.x.
                while isinstance(view, ast.Call):
                    (view,) = view.args
                (name,) = (kw.value.value for kw in node.keywords if kw.arg == "name")
                found.add((route.value, ast.unparse(view), name))
            return found

        library_routes = routes("django_google_sso.urls")
        self.assertTrue(library_routes)
        self.assertEqual(routes("lifetracker.google_sso_urls"), library_routes)


@override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=[])
class GoogleSSOCallbackTests(TestCase):
    """Tests for the lifetracker.google_sso callbacks django_google_sso invokes."""

    def _validate(self, email, verified_email=True):
        return google_sso.pre_validate_user(
            {"email": email, "verified_email": verified_email}, request=None
        )

    def test_rejects_unverified_email(self):
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@x.com"]):
            for verified_email in (False, None, "true"):
                with self.subTest(verified_email=verified_email):
                    self.assertFalse(self._validate("a@x.com", verified_email))

    def test_rejects_missing_or_blank_email(self):
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@x.com"]):
            self.assertFalse(google_sso.pre_validate_user({}, request=None))
            self.assertFalse(self._validate("   "))

    def test_rejects_everyone_when_allowlist_empty(self):
        self.assertFalse(self._validate("a@example.com"))

    def test_rejects_email_not_in_allowlist(self):
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@x.com"]):
            self.assertFalse(self._validate("b@x.com"))

    def test_allows_allowlisted_email_case_insensitively(self):
        with override_settings(
            LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["x@x.com", "A@Example.com"]
        ):
            self.assertTrue(self._validate(" a@EXAMPLE.com "))

    def test_rejects_email_shared_by_several_users(self):
        User.objects.create_user("alice", email="a@example.com")
        with override_settings(LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@example.com"]):
            self.assertTrue(self._validate("a@example.com"))
            User.objects.create_user("alice2", email="A@Example.com")
            self.assertFalse(self._validate("a@example.com"))


@override_settings(
    **_GOOGLE_SSO_ENABLED_SETTINGS,
    LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=["a@example.com"],
)
class GoogleSSOLoginFlowTests(TestCase):
    """End-to-end tests of the library's callback view with our settings, with
    only the calls out to Google mocked."""

    def setUp(self):
        self.user = User.objects.create_user(
            "alice", email="A@example.com", is_staff=True
        )

    def _callback(self, **user_info):
        user_info = {"email": "a@example.com", "verified_email": True} | user_info
        session = self.client.session
        session["sso_state"] = "test-state"
        session.save()
        with (
            mock.patch.object(GoogleAuth, "flow", new_callable=mock.PropertyMock),
            mock.patch.object(GoogleAuth, "get_user_info", return_value=user_info),
        ):
            return self.client.get(
                reverse("django_google_sso:oauth_callback"),
                {"code": "test-code", "state": "test-state"},
            )

    def _logged_in_user_id(self):
        return self.client.session.get("_auth_user_id")

    def test_allowlisted_verified_existing_user_logs_in(self):
        response = self._callback()
        self.assertRedirects(response, reverse("admin:index"))
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    def test_login_issues_jwt_cookie(self):
        response = self._callback()
        token = response.cookies[jwt_auth.JWT_COOKIE_NAME].value
        claims = _decode_jwt(token)
        self.assertEqual(claims["sub"], str(self.user.pk))
        session = self.client.session
        self.assertEqual(claims["sid"], jwt_auth.session_id_claim(session.session_key))
        self.assertEqual(session[jwt_auth.SESSION_KEY], {"sub": str(self.user.pk)})

    @override_settings(SESSION_COOKIE_AGE=12345)
    def test_session_expiry_matches_password_login(self):
        self._callback()
        session = self.client.session
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))
        # No per-session expiry override, so the global policy applies.
        self.assertNotIn("_session_expiry", session)
        self.assertEqual(session.get_expiry_age(), 12345)

    def test_rejects_email_not_in_allowlist(self):
        User.objects.create_user("bob", email="b@example.com", is_staff=True)
        self._callback(email="b@example.com")
        self.assertIsNone(self._logged_in_user_id())

    def test_rejects_unverified_email(self):
        for verified in (False, None):
            with self.subTest(verified=verified):
                self._callback(verified_email=verified)
                self.assertIsNone(self._logged_in_user_id())

    def test_does_not_create_missing_user(self):
        self.user.delete()
        self._callback()
        self.assertIsNone(self._logged_in_user_id())
        self.assertFalse(User.objects.filter(email__iexact="a@example.com").exists())

    def test_rejects_inactive_user(self):
        self.user.is_active = False
        self.user.save()
        self._callback()
        self.assertIsNone(self._logged_in_user_id())

    def test_rejects_email_shared_by_several_users(self):
        User.objects.create_user("alice2", email="a@example.com", is_staff=True)
        response = self._callback()
        self.assertRedirects(
            response, reverse("admin:index"), fetch_redirect_response=False
        )
        self.assertIsNone(self._logged_in_user_id())


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

    def _get_as_superuser(self, obj):
        client = Client()
        client.force_login(User.objects.create_superuser(f"admin{obj.pk}"))
        return client.get(reverse("admin:core_dbstoredfile_serve_file", args=[obj.pk]))

    def test_safe_types_are_served_inline(self):
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

    def test_types_that_can_run_script_are_downloaded(self):
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
                response = self._get_as_superuser(
                    self._stored_file(filename, b"<script>alert(1)</script>")
                )
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

    def test_inline_images_and_text_are_sandboxed(self):
        for filename in ["photo.png", "notes.txt"]:
            with self.subTest(filename=filename):
                response = self._get_as_superuser(self._stored_file(filename))
                self.assertEqual(
                    response["Content-Security-Policy"],
                    "sandbox; default-src 'none'",
                )

    def test_pdf_is_not_sandboxed(self):
        """Browsers' PDF viewers won't run in a sandboxed document."""
        response = self._get_as_superuser(self._stored_file("doc.pdf"))
        self.assertNotIn("Content-Security-Policy", response)

    def test_filename_is_escaped_in_header(self):
        response = self._get_as_superuser(self._stored_file('a"b\\c é.html'))
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="a\\"b\\\\c .html";'
            " filename*=UTF-8''a%22b%5Cc%20%C3%A9.html",
        )

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

    def test_size_missing_pk_raises(self):
        with self.assertRaises(DBStoredFile.DoesNotExist):
            self.storage.size("db/999999/missing.txt")

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


class RootRedirectTests(TestCase):
    def test_root_redirects_to_admin_index(self):
        response = Client().get("/")
        self.assertRedirects(
            response,
            reverse("admin:index"),
            status_code=302,
            fetch_redirect_response=False,
        )


class RequestTimingTests(TestCase):
    PAGE_TIME = re.compile(
        r'<p class="lt-page-time">Page generated in \d+\.\d{3} seconds</p>'
    )

    def setUp(self):
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")

    def assert_footer_shows_time(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertRegex(content, self.PAGE_TIME)
        self.assertIn(".lt-page-time {", content)

    def test_admin_pages_show_generation_time_in_footer(self):
        year = FinancialYear.objects.create(year=2024)
        for url in [
            reverse("admin:index"),
            reverse("admin:taxtracker_financialyear_changelist"),
            reverse("admin:taxtracker_financialyear_change", args=[year.pk]),
            reverse("admin:taxtracker_financialyear_summary", args=[year.pk]),
            reverse("admin:taxtracker_item_add"),
            reverse("admin:core_cloudbackupstatus_changelist"),
            reverse("admin:idle_watchdog"),
        ]:
            with self.subTest(url=url):
                self.assert_footer_shows_time(url)

    def test_login_page_shows_generation_time_in_footer(self):
        self.client.logout()
        self.assert_footer_shows_time(reverse("admin:login"))

    def test_server_timing_header(self):
        response = Client().get("/")
        self.assertRegex(response.headers["Server-Timing"], r"^app;dur=\d+\.\d$")

    def test_tag_measures_from_request_start(self):
        request = RequestFactory().get("/")
        template = Template("{% load page_timing %}{% page_generation_time %}")
        with mock.patch("time.perf_counter", return_value=12.5):
            setattr(request, REQUEST_START_ATTR, 10.0)
            rendered = template.render(Context({"request": request}))
        self.assertEqual(rendered, "2.500")

    def test_tag_is_empty_without_middleware_or_request(self):
        template = Template("{% load page_timing %}{% page_generation_time %}")
        self.assertEqual(template.render(Context({})), "")
        request = RequestFactory().get("/")
        self.assertEqual(template.render(Context({"request": request})), "")


class AdminSiteHeaderTests(TestCase):
    COMMIT_ID = "0123abc456def7890123abc456def7890123abcd"

    def test_includes_commit_link_and_instance_name(self):
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Life Tracker",
                    link_commit=True,
                    commit_id=self.COMMIT_ID,
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            (
                f'Life Tracker <code style="{_TEXT_STYLE}"><a'
                f' href="https://github.com/skissane/taxtracker/commit/{self.COMMIT_ID}"'
                ' target="_blank" rel="noopener noreferrer"'
                f' title="{self.COMMIT_ID}" style="{_TEXT_STYLE}">0123abc</a></code>'
                f' <code style="{_TEXT_STYLE}">instance.local</code>'
            ),
        )

    def test_omits_commit_link_when_no_commit_id(self):
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Life Tracker",
                    link_commit=True,
                    commit_id="",
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            f'Life Tracker <code style="{_TEXT_STYLE}">instance.local</code>',
        )

    def test_uses_site_name_color(self):
        # The other tests compare against the constant, so pin its value here.
        self.assertEqual(_TEXT_STYLE, "color: var(--header-branding-color);")

    def test_accepts_base_url_without_trailing_slash(self):
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Life Tracker",
                    link_commit=True,
                    commit_id=self.COMMIT_ID,
                    commit_url_base="https://github.com/skissane/taxtracker/commit",
                    instance_name="instance.local",
                )
            ),
            (
                f'Life Tracker <code style="{_TEXT_STYLE}"><a'
                f' href="https://github.com/skissane/taxtracker/commit/{self.COMMIT_ID}"'
                ' target="_blank" rel="noopener noreferrer"'
                f' title="{self.COMMIT_ID}" style="{_TEXT_STYLE}">0123abc</a></code>'
                f' <code style="{_TEXT_STYLE}">instance.local</code>'
            ),
        )

    def test_omits_commit_link_when_not_linking_commit(self):
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="Example",
                    link_commit=False,
                    commit_id=self.COMMIT_ID,
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            (
                f'Example <code style="{_TEXT_STYLE}" title="{self.COMMIT_ID}">0123abc'
                f'</code> <code style="{_TEXT_STYLE}">instance.local</code>'
            ),
        )

    def test_escapes_site_name(self):
        self.assertEqual(
            str(
                _build_admin_site_header(
                    site_name="<b>",
                    link_commit=False,
                    commit_id="",
                    commit_url_base="https://github.com/skissane/taxtracker/commit/",
                    instance_name="instance.local",
                )
            ),
            f'&lt;b&gt; <code style="{_TEXT_STYLE}">instance.local</code>',
        )


@override_settings(
    LIFETRACKER_COMMIT_ID=AdminSiteHeaderTests.COMMIT_ID,
    LIFETRACKER_COMMIT_URL_BASE="https://example.invalid/commit/",
    LIFETRACKER_BACKUP_HOSTNAME="instance.local",
    LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Example Site",
)
class AdminSitePerRequestHeaderTests(TestCase):
    COMMIT_URL = f"https://example.invalid/commit/{AdminSiteHeaderTests.COMMIT_ID}"

    def test_admin_site_is_lifetracker_admin_site(self):
        self.assertIsInstance(admin.site, LifetrackerAdminSite)

    def test_logged_out_uses_unauthenticated_name_without_commit_link(self):
        response = Client().get(reverse("admin:login"))
        self.assertContains(response, "<title>Log in | Example Site</title>", html=True)
        self.assertContains(
            response,
            f'Example Site <code style="{_TEXT_STYLE}" '
            f'title="{AdminSiteHeaderTests.COMMIT_ID}">0123abc'
            f'</code> <code style="{_TEXT_STYLE}">instance.local</code>',
        )
        self.assertNotContains(response, self.COMMIT_URL)
        self.assertNotContains(response, "Life Tracker")

    def test_logged_in_uses_site_name_with_commit_link(self):
        client = Client()
        client.force_login(User.objects.create_superuser("admin", "", "pw"))
        response = client.get(reverse("admin:index"))
        self.assertContains(
            response, "<title>Administration | Life Tracker</title>", html=True
        )
        self.assertContains(
            response,
            f'Life Tracker <code style="{_TEXT_STYLE}"><a href="{self.COMMIT_URL}"',
        )
        self.assertNotContains(response, "Example Site")


class AdminSiteFlyMachineLinkTests(TestCase):
    URL = "https://fly.io/apps/example-app/machines/0123456789abcd"
    LINK = f'<a href="{URL}" target="_blank" rel="noopener noreferrer">Fly Machine</a>'

    def _index(self, user):
        client = Client()
        client.force_login(user)
        return client.get(reverse("admin:index"))

    @override_settings(LIFETRACKER_FLY_MACHINE_URL=URL)
    def test_top_bar_links_to_fly_machine_for_superusers(self):
        response = self._index(User.objects.create_superuser("admin", "", "pw"))
        self.assertContains(response, self.LINK, html=True)

    @override_settings(LIFETRACKER_FLY_MACHINE_URL=URL)
    def test_no_link_for_staff_or_logged_out(self):
        staff = User.objects.create_user("staff", "", "pw", is_staff=True)
        self.assertNotContains(self._index(staff), self.URL)
        self.assertNotContains(Client().get(reverse("admin:login")), self.URL)

    @override_settings(LIFETRACKER_FLY_MACHINE_URL="")
    def test_no_link_when_not_on_fly(self):
        response = self._index(User.objects.create_superuser("admin", "", "pw"))
        self.assertNotContains(response, "Fly Machine")


class _SettingsSubprocessMixin:
    """settings.py computes its values at import time, from files under $HOME
    and from environment variables, before Django is even configured. They can
    only be exercised by importing it fresh in a subprocess with a controlled
    $HOME and environment — the version already imported for this test run
    reflects whatever the real ~/.config and environment looked like when the
    suite started."""

    REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    SRC_DIR = REPO_ROOT / "src"

    def _run(self, home, extra_env=None, expr="s.SECRET_KEY"):
        env = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(self.SRC_DIR),
        }
        if extra_env:
            env.update(extra_env)

        code = f"import lifetracker.settings as s; print({expr})"
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


class SettingsSecretKeyTests(_SettingsSubprocessMixin, TestCase):
    """SECRET_KEY is read from (or generated into) a file under $HOME."""

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


class SettingsDataDirTests(_SettingsSubprocessMixin, TestCase):
    """LIFETRACKER_DATA_DIR supplies default locations for the database and
    config files, LIFETRACKER_CONFIG_DIR moves the config files elsewhere, and
    LIFETRACKER_DB_PATH and LIFETRACKER_SECRET_KEY_FILE override each file."""

    EXPR = "(str(s.DATABASES['default']['NAME']), str(s._key_file))"

    def test_paths_default_to_the_data_dir(self):
        with tempfile.TemporaryDirectory() as home:
            # A legacy key under $HOME must not be copied into the data dir.
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            data_dir = Path(home) / "data"
            data_dir.mkdir()

            proc = self._run(
                home,
                extra_env={"LIFETRACKER_DATA_DIR": str(data_dir)},
                expr=f"{self.EXPR} + (s.SECRET_KEY, str(s.LIFETRACKER_CONFIG_DIR))",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            db_path, key_path, key, config_dir = ast.literal_eval(proc.stdout.strip())
            self.assertEqual(config_dir, str(data_dir))
            self.assertEqual(db_path, str(data_dir / "db.sqlite3"))
            self.assertEqual(key_path, str(data_dir / "secret_key"))
            self.assertNotEqual(key, "legacy-secret-key-content")
            self.assertEqual((data_dir / "secret_key").read_text().strip(), key)
            self.assertFalse((Path(home) / ".config" / "lifetracker").exists())

    def test_explicit_paths_take_precedence(self):
        with tempfile.TemporaryDirectory() as home:
            data_dir = Path(home) / "data"
            db_path = Path(home) / "elsewhere" / "db.sqlite3"
            key_path = Path(home) / "elsewhere" / "secret_key"

            proc = self._run(
                home,
                extra_env={
                    "LIFETRACKER_DATA_DIR": str(data_dir),
                    "LIFETRACKER_DB_PATH": str(db_path),
                    "LIFETRACKER_SECRET_KEY_FILE": str(key_path),
                },
                expr=self.EXPR,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                ast.literal_eval(proc.stdout.strip()), (str(db_path), str(key_path))
            )
            self.assertFalse(data_dir.exists())

    def test_config_dir_takes_precedence_over_data_dir(self):
        with tempfile.TemporaryDirectory() as home:
            # A legacy key under $HOME must not be copied into the config dir.
            old_dir = Path(home) / ".config" / "taxtracker"
            old_dir.mkdir(parents=True)
            (old_dir / "secret_key").write_text("legacy-secret-key-content\n")
            data_dir = Path(home) / "data"
            config_dir = Path(home) / "config"

            proc = self._run(
                home,
                extra_env={
                    "LIFETRACKER_DATA_DIR": str(data_dir),
                    "LIFETRACKER_CONFIG_DIR": str(config_dir),
                },
                expr=f"{self.EXPR} + (s.SECRET_KEY, str(s.LIFETRACKER_CONFIG_DIR))",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            db_path, key_path, key, resolved_config_dir = ast.literal_eval(
                proc.stdout.strip()
            )
            self.assertEqual(resolved_config_dir, str(config_dir))
            self.assertEqual(key_path, str(config_dir / "secret_key"))
            self.assertEqual(db_path, str(data_dir / "db.sqlite3"))
            self.assertNotEqual(key, "legacy-secret-key-content")
            self.assertFalse(data_dir.exists())

    def test_without_data_dir_db_defaults_to_repo_root(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(
                home, expr=f"{self.EXPR} + (str(s.LIFETRACKER_CONFIG_DIR),)"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            db_path, _, config_dir = ast.literal_eval(proc.stdout.strip())
            self.assertEqual(db_path, str(self.REPO_ROOT / "db.sqlite3"))
            self.assertEqual(config_dir, str(Path(home) / ".config" / "lifetracker"))


class SettingsEnvTests(_SettingsSubprocessMixin, TestCase):
    """DEBUG, ALLOWED_HOSTS and the HTTPS-related settings come from
    LIFETRACKER_* environment variables."""

    EXPR = (
        "(s.DEBUG, s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS,"
        " getattr(s, 'SECURE_PROXY_SSL_HEADER', None),"
        " s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE)"
    )

    def _settings(self, **env):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def _error(self, **env):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ImproperlyConfigured", proc.stderr)
        return proc.stderr

    def test_defaults_are_local_development(self):
        self.assertEqual(self._settings(), (True, [], [], None, False, False))

    def test_debug_off_with_hosts(self):
        self.assertEqual(
            self._settings(
                LIFETRACKER_DEBUG="0",
                LIFETRACKER_ALLOWED_HOSTS=" tax.example.com, other.example.com ,",
            ),
            (
                False,
                ["tax.example.com", "other.example.com"],
                [],
                None,
                True,
                True,
            ),
        )

    def test_debug_off_without_hosts_refuses_to_start(self):
        stderr = self._error(LIFETRACKER_DEBUG="false")
        self.assertIn("LIFETRACKER_ALLOWED_HOSTS must be set", stderr)

    def test_invalid_bool_is_rejected(self):
        stderr = self._error(LIFETRACKER_DEBUG="yes")
        self.assertIn("LIFETRACKER_DEBUG must be 1/true or 0/false", stderr)

    def test_bool_is_case_insensitive(self):
        self.assertTrue(self._settings(LIFETRACKER_DEBUG="TRUE")[0])
        self.assertFalse(
            self._settings(
                LIFETRACKER_DEBUG="False", LIFETRACKER_ALLOWED_HOSTS="example.com"
            )[0]
        )

    def test_hosts_default_from_fly_app_name(self):
        self.assertEqual(
            self._settings(LIFETRACKER_DEBUG="0", FLY_APP_NAME="example-app")[1],
            ["example-app.fly.dev"],
        )

    def test_explicit_hosts_override_fly_app_name(self):
        self.assertEqual(
            self._settings(
                LIFETRACKER_DEBUG="0",
                LIFETRACKER_ALLOWED_HOSTS="tax.example.com",
                FLY_APP_NAME="example-app",
            )[1],
            ["tax.example.com"],
        )

    def test_proxy_settings(self):
        debug, _, origins, proxy_header, _, _ = self._settings(
            LIFETRACKER_DEBUG="true",
            LIFETRACKER_CSRF_TRUSTED_ORIGINS="https://tax.example.com",
            LIFETRACKER_TRUST_X_FORWARDED_PROTO="1",
        )
        self.assertTrue(debug)
        self.assertEqual(origins, ["https://tax.example.com"])
        self.assertEqual(proxy_header, ("HTTP_X_FORWARDED_PROTO", "https"))


class SettingsAdminHeaderEnvTests(_SettingsSubprocessMixin, TestCase):
    EXPR = (
        "(s.LIFETRACKER_COMMIT_URL_BASE, s.LIFETRACKER_COMMIT_ID,"
        " s.LIFETRACKER_BACKUP_HOSTNAME, s.LIFETRACKER_UNAUTHENTICATED_SITE_NAME)"
    )

    def _settings(self, **env):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def test_defaults(self):
        commit_url_base, commit_id, backup_hostname, unauthenticated_name = (
            self._settings()
        )
        self.assertEqual(
            commit_url_base, "https://github.com/skissane/taxtracker/commit/"
        )
        self.assertEqual(commit_id, "")
        self.assertTrue(backup_hostname)
        self.assertEqual(unauthenticated_name, "Life Tracker")

    def test_reads_commit_values_from_environment(self):
        self.assertEqual(
            self._settings(
                LIFETRACKER_COMMIT_URL_BASE="https://example.invalid/commit/",
                LIFETRACKER_COMMIT_ID="deadbeef",
                LIFETRACKER_BACKUP_HOSTNAME="test-instance",
                LIFETRACKER_UNAUTHENTICATED_SITE_NAME=" Example Site ",
            ),
            (
                "https://example.invalid/commit/",
                "deadbeef",
                "test-instance",
                "Example Site",
            ),
        )

    def test_blank_unauthenticated_site_name_uses_default(self):
        self.assertEqual(
            self._settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="  ")[3],
            "Life Tracker",
        )

    def test_backup_hostname_defaults_from_fly_app_name(self):
        self.assertEqual(
            self._settings(FLY_APP_NAME="example-app")[2], "example-app.fly.dev"
        )

    def test_explicit_backup_hostname_overrides_fly_app_name(self):
        self.assertEqual(
            self._settings(
                LIFETRACKER_BACKUP_HOSTNAME="test-instance", FLY_APP_NAME="example-app"
            )[2],
            "test-instance",
        )


class FlyMachineUrlTests(SimpleTestCase):
    def test_built_from_app_name_and_machine_id(self):
        self.assertEqual(
            fly_machine_url(" example-app ", "0123456789abcd "),
            "https://fly.io/apps/example-app/machines/0123456789abcd",
        )

    def test_values_are_url_quoted(self):
        self.assertEqual(
            fly_machine_url("a/b", "c?d"), "https://fly.io/apps/a%2Fb/machines/c%3Fd"
        )

    def test_empty_unless_both_are_set(self):
        for app_name, machine_id in [
            ("", ""),
            ("example-app", ""),
            ("", "0123456789abcd"),
            ("example-app", "  "),
            (" ", "0123456789abcd"),
        ]:
            with self.subTest(app_name=app_name, machine_id=machine_id):
                self.assertEqual(fly_machine_url(app_name, machine_id), "")


class SettingsFlyMachineUrlTests(_SettingsSubprocessMixin, TestCase):
    def _url(self, **env):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(
                home, extra_env=env, expr="repr(s.LIFETRACKER_FLY_MACHINE_URL)"
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def test_read_from_fly_environment_variables(self):
        self.assertEqual(
            self._url(FLY_APP_NAME="example-app", FLY_MACHINE_ID="0123456789abcd"),
            "https://fly.io/apps/example-app/machines/0123456789abcd",
        )


class SettingsGoogleSSOEnvTests(_SettingsSubprocessMixin, TestCase):
    """Google SSO is configured only from LIFETRACKER_GOOGLE_SSO_* variables."""

    EXPR = (
        "(s.GOOGLE_SSO_ENABLED, s.GOOGLE_SSO_CLIENT_ID, s.GOOGLE_SSO_CLIENT_SECRET,"
        " s.GOOGLE_SSO_PROJECT_ID, s.GOOGLE_SSO_CALLBACK_DOMAIN,"
        " s.LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS)"
    )

    def _settings(self, **env):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return ast.literal_eval(proc.stdout.strip())

    def test_defaults_disabled(self):
        self.assertEqual(self._settings(), (False, "", "", "", None, []))

    def test_reads_lifetracker_variables(self):
        self.assertEqual(
            self._settings(
                LIFETRACKER_GOOGLE_SSO_CLIENT_ID=" id ",
                LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET=" secret ",
                LIFETRACKER_GOOGLE_SSO_PROJECT_ID=" project ",
                LIFETRACKER_GOOGLE_SSO_CALLBACK_DOMAIN=" tax.example.com ",
                LIFETRACKER_GOOGLE_SSO_ALLOWED_EMAILS=" a@x.com, ,b@x.com,",
            ),
            (
                True,
                "id",
                "secret",
                "project",
                "tax.example.com",
                ["a@x.com", "b@x.com"],
            ),
        )

    def test_enabled_needs_both_id_and_secret(self):
        for env in (
            {"LIFETRACKER_GOOGLE_SSO_CLIENT_ID": "id"},
            {"LIFETRACKER_GOOGLE_SSO_CLIENT_SECRET": "secret"},
        ):
            with self.subTest(env=env):
                self.assertFalse(self._settings(**env)[0])

    def test_ignores_unprefixed_variables(self):
        self.assertEqual(
            self._settings(
                GOOGLE_SSO_CLIENT_ID="id",
                GOOGLE_SSO_CLIENT_SECRET="secret",
                GOOGLE_SSO_PROJECT_ID="project",
                GOOGLE_SSO_CALLBACK_DOMAIN="tax.example.com",
            ),
            (False, "", "", "", None, []),
        )


class LoggingTests(TestCase):
    """settings.LOGGING sends Django's and the app's logs to stderr whatever
    DEBUG is (tests run with it off, as production does), so they reach the
    Fly logs."""

    def setUp(self):
        # Both loggers share the one handler, which writes to stderr.
        (handler,) = logging.getLogger("django").handlers
        self.assertEqual(logging.getLogger("lifetracker").handlers, [handler])
        self.assertIs(type(handler), logging.StreamHandler)
        self.stream = io.StringIO()
        self.addCleanup(handler.setStream, handler.setStream(self.stream))

    def test_unhandled_exception_traceback_is_logged(self):
        client = Client(raise_request_exception=False)
        client.force_login(User.objects.create_superuser("admin", "", "pw"))
        with mock.patch(
            "lifetracker.core.admin.idle_watchdog_page_context",
            side_effect=RuntimeError("boom"),
        ):
            response = client.get(reverse("admin:idle_watchdog"))
        self.assertEqual(response.status_code, 500)
        output = self.stream.getvalue()
        self.assertIn("ERROR django.request: Internal Server Error", output)
        self.assertIn("Traceback (most recent call last):", output)
        self.assertIn("RuntimeError: boom", output)

    def test_app_logger_logs_info(self):
        logging.getLogger("lifetracker.core.admin").info("hello")
        self.assertRegex(
            self.stream.getvalue(),
            r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} "
            r"INFO lifetracker\.core\.admin: hello\n\Z",
        )

    def test_runserver_request_log_keeps_its_own_handler(self):
        # Configuring "django" resets its children, so without its own entry
        # runserver's request log would propagate to the plain handler and lose
        # the colours ServerFormatter gives it.
        server_logger = logging.getLogger("django.server")
        self.assertFalse(server_logger.propagate)
        # pytest adds its capture handlers to non-propagating loggers too.
        (handler,) = (
            h for h in server_logger.handlers if type(h).__module__ != "_pytest.logging"
        )
        self.assertIs(type(handler), logging.StreamHandler)
        self.assertIsInstance(handler.formatter, ServerFormatter)


class SettingsLogTimestampsTests(_SettingsSubprocessMixin, TestCase):
    """LIFETRACKER_LOG_TIMESTAMPS turns off the timestamp at the start of each
    log line, for Fly, which stamps every line itself."""

    EXPR = "s.LOGGING['formatters']['stderr']['format']"

    def _format(self, **env):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(home, extra_env=env, expr=self.EXPR)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_on_by_default(self):
        self.assertEqual(self._format(), "{asctime} {levelname} {name}: {message}")

    def test_off(self):
        self.assertEqual(
            self._format(LIFETRACKER_LOG_TIMESTAMPS="0"),
            "{levelname} {name}: {message}",
        )

    def test_invalid(self):
        with tempfile.TemporaryDirectory() as home:
            proc = self._run(
                home, extra_env={"LIFETRACKER_LOG_TIMESTAMPS": "no"}, expr=self.EXPR
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("LIFETRACKER_LOG_TIMESTAMPS must be", proc.stderr)


class InitialAdminPasswordFileTests(TestCase):
    """Where ensure_superuser looks for the initial admin password file."""

    def _resolve(self, env=None):
        # patch.dict restores the whole environment afterwards, including the
        # pop, so a real LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE can't leak in.
        with (
            mock.patch.dict(os.environ),
            self.settings(LIFETRACKER_CONFIG_DIR=Path("/config")),
        ):
            os.environ.pop("LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE", None)
            os.environ.update(env or {})
            return _initial_admin_password_file()

    def test_defaults_to_config_dir(self):
        self.assertEqual(self._resolve(), Path("/config/initial_admin_password"))

    def test_env_var_overrides(self):
        self.assertEqual(
            self._resolve({"LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE": "/elsewhere/pw"}),
            Path("/elsewhere/pw"),
        )

    def test_empty_env_var_is_ignored(self):
        self.assertEqual(
            self._resolve({"LIFETRACKER_INITIAL_ADMIN_PASSWORD_FILE": ""}),
            Path("/config/initial_admin_password"),
        )


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
        # Likewise a real LIFETRACKER_INITIAL_ADMIN_PASSWORD (e.g. from .env);
        # patch.dict restores the environment afterwards.
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("LIFETRACKER_INITIAL_ADMIN_PASSWORD", None)

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

    def test_creates_new_user_with_password_from_env(self):
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = " from-the-env\n"
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-env"))
        self.assertNotIn("from-the-env", output)
        self.assertIn("$LIFETRACKER_INITIAL_ADMIN_PASSWORD", output)

    def test_env_password_takes_precedence_over_file(self):
        self.password_file.write_text("from-the-file\n")
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = "from-the-env"
        self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-env"))

    def test_blank_env_password_falls_back_to_file(self):
        self.password_file.write_text("from-the-file\n")
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = "  "
        output = self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("from-the-file"))
        self.assertIn(str(self.password_file), output)

    def test_env_password_ignored_for_existing_user(self):
        User.objects.create_superuser("admin", "", "existingpassword")
        os.environ["LIFETRACKER_INITIAL_ADMIN_PASSWORD"] = "from-the-env"
        self._call(username="admin")
        user = User.objects.get(username="admin")
        self.assertTrue(user.check_password("existingpassword"))

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


class DockerEntrypointTests(TestCase):
    """Tests for the Docker entrypoint startup sequence."""

    STARTUP = [
        "python manage.py upgrade_legacy_db",
        "python manage.py migrate",
        "python manage.py ensure_superuser",
    ]

    ENTRYPOINT_ENV = {
        "LIFETRACKER_GUNICORN_PORT",
        "LIFETRACKER_GUNICORN_THREADS",
        "LIFETRACKER_GUNICORN_TIMEOUT",
        "LIFETRACKER_HOST",
        "PORT",
        "TMPDIR",
    }

    # Each fake logs its name and arguments, one invocation per line.
    LOG_COMMAND = (
        "{\n"
        "  printf '%s' {name}\n"
        '  for arg in "$@"; do\n'
        "    printf ' %s' \"$arg\"\n"
        "  done\n"
        "  printf '\\n'\n"
        '} >> "$COMMANDS_LOG_PATH"\n'
    )

    # The entrypoint starts Envoy in the background just before execing
    # gunicorn, so the fakes would race. The fake Envoy then blocks, as the
    # real one does, and the fake gunicorn waits (up to 10 seconds) until
    # Envoy has logged, then logs and kills the rest of the entrypoint's
    # process group (Envoy and the loop that would restart it), which it runs
    # in a session of its own, ignoring the signal itself.
    FAKES = {
        "python": LOG_COMMAND + "exit 0\n",
        "envoy": LOG_COMMAND + "exec sleep 60\n",
        "gunicorn": (
            "i=0\n"
            'while ! grep -q "^envoy" "$COMMANDS_LOG_PATH" && [ "$i" -lt 100 ]; do\n'
            "  sleep 0.1\n"
            "  i=$((i + 1))\n"
            "done\n" + LOG_COMMAND + "trap '' TERM\n"
            "kill -TERM 0\n"
            "exit 0\n"
        ),
    }

    def _run_entrypoint(self, *args, env=None, returncode=0, fakes=None):
        """Run the entrypoint with fake `python`, `envoy` and `gunicorn` on PATH.

        Returns the completed process, the logged lines and the path the
        entrypoint was to write Envoy's config to.
        """
        repo_root = next(
            parent
            for parent in Path(__file__).resolve().parents
            if (parent / "manage.py").exists()
        )
        script = repo_root / "docker-entrypoint.sh"

        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            log_path = temp_path / "commands.log"
            log_path.touch()
            for name, body in {**self.FAKES, **(fakes or {})}.items():
                fake_path = temp_path / name
                fake_path.write_text(
                    "#!/usr/bin/env sh\n" + body.replace("{name}", name)
                )
                fake_path.chmod(0o755)

            result = subprocess.run(
                ["sh", str(script), *args],
                cwd=repo_root,
                env={
                    # Drop the entrypoint's own settings so the caller's shell
                    # can't change what the defaults look like.
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if key not in self.ENTRYPOINT_ENV
                    },
                    "PATH": f"{tempdir}:{os.environ['PATH']}",
                    "COMMANDS_LOG_PATH": str(log_path),
                    "LIFETRACKER_APP_DIR": str(repo_root),
                    "TMPDIR": tempdir,
                    **(env or {}),
                },
                check=False,
                capture_output=True,
                text=True,
                start_new_session=True,
                timeout=30,
            )
            log_lines = log_path.read_text().splitlines()

        self.assertEqual(result.returncode, returncode, result.stderr)
        return result, log_lines, str(temp_path / "lifetracker-envoy.yaml")

    @staticmethod
    def _envoy_config_command(config_path, host="0.0.0.0", port=8000, upstream=8001):
        return (
            f"python manage.py envoy_config envoy/envoy.yaml {config_path}"
            f" --listen-host {host} --listen-port {port} --upstream-port {upstream}"
        )

    def test_entrypoint_runs_startup_commands_before_execing_arguments(self):
        result, log_lines, _ = self._run_entrypoint("sh", "-c", "printf ready")

        self.assertEqual(result.stdout, "ready")
        self.assertEqual(log_lines, [*self.STARTUP, "python manage.py ensure_jwt_key"])

    def test_entrypoint_starts_envoy_and_gunicorn_by_default(self):
        _, log_lines, envoy_config_path = self._run_entrypoint()

        self.assertEqual(
            log_lines,
            [
                *self.STARTUP,
                self._envoy_config_command(envoy_config_path),
                f"envoy --config-path {envoy_config_path} --concurrency 1"
                " --disable-hot-restart --log-level warn",
                "gunicorn --config python:lifetracker.gunicorn_conf"
                " --worker-class gthread --threads 8"
                " --access-logfile - --error-logfile - --timeout 120"
                " --bind 127.0.0.1:8001 lifetracker.wsgi",
            ],
        )

    def test_entrypoint_settings_come_from_env(self):
        _, log_lines, envoy_config_path = self._run_entrypoint(
            env={
                "LIFETRACKER_GUNICORN_PORT": "9001",
                "LIFETRACKER_GUNICORN_THREADS": "2",
                "LIFETRACKER_GUNICORN_TIMEOUT": "600",
                "LIFETRACKER_HOST": "127.0.0.1",
                "PORT": "9000",
            }
        )

        self.assertEqual(
            log_lines[-1],
            "gunicorn --config python:lifetracker.gunicorn_conf"
            " --worker-class gthread --threads 2"
            " --access-logfile - --error-logfile - --timeout 600"
            " --bind 127.0.0.1:9001 lifetracker.wsgi",
        )
        self.assertEqual(
            log_lines[len(self.STARTUP)],
            self._envoy_config_command(envoy_config_path, "127.0.0.1", 9000, 9001),
        )

    def test_entrypoint_stops_gunicorn_when_envoy_keeps_failing(self):
        # Envoy exits at once every time. Gunicorn waits (up to 20 seconds)
        # for SIGTERM, then logs whether the failure file exists.
        result, log_lines, envoy_config_path = self._run_entrypoint(
            fakes={
                "envoy": self.LOG_COMMAND + "exit 3\n",
                "gunicorn": (
                    self.LOG_COMMAND
                    + 'trap \'if [ -e "$LIFETRACKER_ENVOY_FAILED_FILE" ]; then'
                    ' echo gunicorn TERM, failed file >> "$COMMANDS_LOG_PATH"; fi;'
                    " exit 0' TERM\n"
                    "i=0\n"
                    'while [ "$i" -lt 200 ]; do\n'
                    "  sleep 0.1\n"
                    "  i=$((i + 1))\n"
                    "done\n"
                    "exit 1\n"
                ),
            }
        )

        self.assertEqual(
            [line for line in log_lines if line.startswith("envoy ")],
            [
                f"envoy --config-path {envoy_config_path} --concurrency 1"
                " --disable-hot-restart --log-level warn"
            ]
            * 5,
        )
        self.assertEqual(log_lines[-1], "gunicorn TERM, failed file")
        self.assertEqual(
            result.stderr.count("docker-entrypoint.sh: envoy exited with status 3\n"),
            5,
        )
        self.assertIn(
            "docker-entrypoint.sh: envoy exited within a minute of starting"
            " 5 times in a row; stopping gunicorn\n",
            result.stderr,
        )

    def test_entrypoint_stops_if_envoy_config_fails(self):
        _, log_lines, envoy_config_path = self._run_entrypoint(
            fakes={"python": self.LOG_COMMAND + '[ "$2" != envoy_config ] || exit 1\n'},
            returncode=1,
        )
        self.assertEqual(
            log_lines, [*self.STARTUP, self._envoy_config_command(envoy_config_path)]
        )


class SQLiteFileDatabaseTests(TestCase):
    """The production DATABASES settings, applied to a file-backed database.

    The test database is in memory, where WAL mode doesn't apply, so these
    open a separate connection to a temporary file with the same settings.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "db.sqlite3"
        handler = ConnectionHandler(
            {"default": {**settings.DATABASES["default"], "NAME": self.db_path}}
        )
        self.addCleanup(handler.close_all)
        self.connection = handler["default"]

    def test_journal_mode_is_wal(self):
        with self.connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            self.assertEqual(cursor.fetchone(), ("wal",))

    def test_backup_database_includes_uncheckpointed_wal(self):
        with self.connection.cursor() as cursor:
            # Stop SQLite copying the WAL back into the main file on commit,
            # so the row exists only in db.sqlite3-wal.
            cursor.execute("PRAGMA wal_autocheckpoint=0")
            cursor.execute("CREATE TABLE t (v TEXT)")
            cursor.execute("INSERT INTO t VALUES ('in the wal')")
        with mock.patch(
            "lifetracker.core.backup.connections", {"default": self.connection}
        ):
            with backup_database() as backup_file:
                data = backup_file.read()
        copy_path = self.db_path.with_name("copy.sqlite3")
        copy_path.write_bytes(data)
        copy = sqlite3.connect(copy_path)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT v FROM t").fetchall(), [("in the wal",)])

    def test_backup_database_refuses_inside_transaction(self):
        with mock.patch(
            "lifetracker.core.backup.connections", {"default": self.connection}
        ):
            with mock.patch.object(self.connection, "in_atomic_block", True):
                with self.assertRaisesMessage(
                    RuntimeError, "Can't back up the database inside a transaction."
                ):
                    backup_database()

    def test_backup_database_leaves_no_file_behind(self):
        with mock.patch(
            "lifetracker.core.backup.connections", {"default": self.connection}
        ):
            backup_file = backup_database()
        with backup_file:
            self.assertFalse(os.path.exists(backup_file.name))
            self.assertTrue(backup_file.read(16).startswith(b"SQLite format 3\x00"))


class CloudBackupStatusModelTests(TestCase):
    def test_str_never_backed_up(self):
        self.assertEqual(
            str(CloudBackupStatus()), "Cloud backup status: never backed up"
        )

    def test_str_with_last_backup(self):
        backup_time = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        status = CloudBackupStatus(last_backup_at=backup_time)
        self.assertEqual(
            str(status), f"Cloud backup status: last backup at {backup_time}"
        )

    def test_only_singleton_pk_allowed(self):
        CloudBackupStatus.objects.create(pk=CLOUD_BACKUP_STATUS_PK)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CloudBackupStatus.objects.create(pk=CLOUD_BACKUP_STATUS_PK + 1)


def _record_uploads(mock_client):
    """Make *mock_client*'s ``upload_fileobj`` read each file it's given.

    Returns a list it appends each upload to, as a dict of its arguments with
    the file's content as ``Body``, since the file is closed after the upload.
    """
    uploads = []

    def upload_fileobj(fileobj, bucket, key, ExtraArgs=None):
        uploads.append(
            {
                "Body": fileobj.read(),
                "Bucket": bucket,
                "Key": key,
                "ExtraArgs": ExtraArgs,
            }
        )

    mock_client.return_value.upload_fileobj.side_effect = upload_fileobj
    return uploads


class BackupAdminViewTests(TransactionTestCase):
    # Not TestCase: backup_database() refuses to run inside a transaction.

    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "password"
        )
        self.client = Client()
        self.client.login(username="admin", password="password")

    def test_admin_index_links_to_backups_for_superusers(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(
            response, reverse("admin:core_cloudbackupstatus_changelist")
        )

    def test_admin_index_hides_backups_from_non_superusers(self):
        non_super = User.objects.create_user("regular0", "r0@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular0", **{"password": "pass"})
        response = client.get(reverse("admin:index"))
        self.assertNotContains(
            response, reverse("admin:core_cloudbackupstatus_changelist")
        )

    def test_backups_page_links_to_db_download(self):
        response = self.client.get(reverse("admin:core_cloudbackupstatus_changelist"))
        self.assertContains(
            response, reverse("admin:core_cloudbackupstatus_download_db_backup")
        )

    def test_download_db_backup_view(self):
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-sqlite3")
        self.assertRegex(
            response["Content-Disposition"],
            r"db-backup\.\d{4}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{2}\.sqlite3",
        )
        # Content should be a valid SQLite database (starts with the magic header)
        content = response.getvalue()
        self.assertTrue(content.startswith(b"SQLite format 3\x00"))
        self.assertEqual(int(response["Content-Length"]), len(content))

    def test_download_db_backup_requires_superuser(self):
        non_super = User.objects.create_user("regular", "r@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular", password="pass")
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_download_db_backup_rejects_non_sqlite_engine(self):
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        with pytest.warns(UserWarning, match="Overriding setting DATABASES"):
            with override_settings(
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "irrelevant",
                    }
                }
            ):
                response = self.client.get(url, follow=True)
        self.assertRedirects(
            response, reverse("admin:core_cloudbackupstatus_changelist")
        )
        self.assertContains(
            response, "DB backup is only available for SQLite databases."
        )

    def test_download_db_backup_failure_shows_error(self):
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        with mock.patch(
            "lifetracker.core.admin.backup_database",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            response = self.client.get(url, follow=True)
        self.assertRedirects(
            response, reverse("admin:core_cloudbackupstatus_changelist")
        )
        self.assertContains(response, "DB backup failed: database or disk is full")

    @override_settings(
        LIFETRACKER_BACKUP_S3_ENDPOINT_URL="https://example.invalid",
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_REGION_NAME="auto",
        LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
    )
    def test_cloud_backup_status_view(self):
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://example.invalid")
        self.assertContains(response, "test-bucket")
        self.assertContains(response, "auto")
        self.assertContains(response, "<code>test-host.local</code>", html=True)
        self.assertContains(response, "Never")

    def test_cloud_backup_status_view_shows_time_since_last_backup(self):
        CloudBackupStatus.objects.create(
            pk=CLOUD_BACKUP_STATUS_PK,
            last_backup_at=timezone.now() - datetime.timedelta(days=4, hours=3),
            last_backup_object_key="host/db-backup.2026-01-01.00.00.00.sqlite3",
            last_backup_size=3 * 1024 * 1024,
        )
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.get(url)
        self.assertContains(response, "(4\xa0days ago)")
        self.assertNotContains(response, "hours")
        self.assertContains(response, "<td>3.0\xa0MB</td>", html=True)

    def test_cloud_backup_status_requires_superuser(self):
        non_super = User.objects.create_user("regular2", "r2@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular2", **{"password": "pass"})
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    @override_settings(
        LIFETRACKER_BACKUP_S3_ENDPOINT_URL="https://example.invalid",
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_REGION_NAME="auto",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
        LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_status_view_run_backup_updates_status(self, mock_client):
        uploads = _record_uploads(mock_client)
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.post(url, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cloud backup uploaded successfully")
        mock_client.assert_called_once_with(
            "s3",
            endpoint_url="https://example.invalid",
            region_name="auto",
            aws_access_key_id="access-key",
            aws_secret_access_key="secret-key",
            config=CLOUD_BACKUP_S3_CONFIG,
        )
        [upload] = uploads
        self.assertEqual(upload["Bucket"], "test-bucket")
        self.assertEqual(upload["ExtraArgs"], {"ContentType": "application/x-sqlite3"})
        self.assertRegex(
            upload["Key"],
            r"^test-host\.local/db-backup\."
            r"\d{4}-\d{2}-\d{2}\.\d{2}\.\d{2}\.\d{2}\.sqlite3$",
        )
        self.assertTrue(upload["Body"].startswith(b"SQLite format 3\x00"))

        status = CloudBackupStatus.objects.get(pk=CLOUD_BACKUP_STATUS_PK)
        self.assertIsNotNone(status.last_backup_at)
        self.assertEqual(status.last_backup_object_key, upload["Key"])
        self.assertEqual(status.last_backup_size, len(upload["Body"]))

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_post_with_missing_settings_shows_error(self, mock_client):
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Cloud backup settings are incomplete. Missing: "
            "LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID, "
            "LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY.",
        )
        mock_client.assert_not_called()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    def test_run_cloud_backup_rejects_missing_settings(self):
        with self.assertRaisesMessage(
            RuntimeError, "Missing: LIFETRACKER_BACKUP_S3_BUCKET_NAME."
        ):
            run_cloud_backup()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    def test_run_cloud_backup_checks_engine_before_settings(self):
        with pytest.warns(UserWarning, match="Overriding setting DATABASES"):
            with override_settings(
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "irrelevant",
                    }
                }
            ):
                with self.assertRaisesMessage(
                    RuntimeError, "only available for SQLite databases"
                ):
                    run_cloud_backup()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_rejects_non_sqlite_engine(self, mock_client):
        url = reverse("admin:core_cloudbackupstatus_changelist")
        with pytest.warns(UserWarning, match="Overriding setting DATABASES"):
            with override_settings(
                DATABASES={
                    "default": {
                        "ENGINE": "django.db.backends.postgresql",
                        "NAME": "irrelevant",
                    }
                }
            ):
                response = self.client.post(url)
        self.assertContains(
            response,
            "Cloud backup failed: Cloud backup is only available for SQLite databases.",
        )
        mock_client.assert_not_called()

    @override_settings(
        LIFETRACKER_BACKUP_S3_ENDPOINT_URL="not a url",
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    def test_cloud_backup_invalid_endpoint_shows_error(self):
        # Uses the real boto3.client, which raises ValueError for this endpoint.
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.post(url)
        self.assertContains(
            response,
            "Cloud backup failed: Invalid S3 configuration: "
            "Invalid endpoint: not a url",
        )
        self.assertFalse(
            CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).exists()
        )

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_upload_error_shows_error(self, mock_client):
        mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.post(url)
        self.assertContains(response, "Cloud backup failed:")
        self.assertContains(response, "AccessDenied")
        self.assertFalse(
            CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).exists()
        )

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_status_write_failure_shows_warning(self, mock_client):
        url = reverse("admin:core_cloudbackupstatus_changelist")
        with mock.patch.object(
            CloudBackupStatus.objects,
            "update_or_create",
            side_effect=IntegrityError("boom"),
        ):
            with self.assertLogs("lifetracker.core.admin", "ERROR") as logs:
                response = self.client.post(url)
        self.assertIn("IntegrityError: boom", logs.output[0])
        mock_client.return_value.upload_fileobj.assert_called_once()
        key = mock_client.return_value.upload_fileobj.call_args.args[2]
        self.assertContains(
            response,
            f"Cloud backup uploaded successfully as {key}, but failed to record "
            "backup status.",
        )

    def test_cloud_backup_status_links_to_list(self):
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.get(url)
        self.assertContains(response, reverse("admin:core_cloudbackupstatus_list"))

    def _mock_bucket_listing(self, mock_client, folders, objects):
        """Make the mocked S3 client's paginator return *folders* for the
        delimited top-level listing and *objects* (split across two pages)
        for the prefixed one."""

        def paginate(**kwargs):
            if kwargs.get("Delimiter") == "/":
                return [{"CommonPrefixes": [{"Prefix": f} for f in folders]}, {}]
            matching = [o for o in objects if o["Key"].startswith(kwargs["Prefix"])]
            return [{"Contents": matching[:1]}, {"Contents": matching[1:]}]

        paginator = mock_client.return_value.get_paginator.return_value
        paginator.paginate.side_effect = paginate
        return paginator

    @override_settings(
        LIFETRACKER_BACKUP_S3_ENDPOINT_URL="https://example.invalid",
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_REGION_NAME="auto",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
        LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_list_defaults_to_own_hostname(self, mock_client):
        utc = datetime.UTC
        paginator = self._mock_bucket_listing(
            mock_client,
            folders=["other-host/", "test-host.local/"],
            objects=[
                {
                    "Key": "test-host.local/db-backup.2026-01-01.00.00.00.sqlite3",
                    "Size": 1024,
                    "LastModified": datetime.datetime(2026, 1, 1, tzinfo=utc),
                },
                {
                    "Key": "test-host.local/db-backup.2026-03-01.00.00.00.sqlite3",
                    "Size": 2048,
                    "LastModified": datetime.datetime(2026, 3, 1, tzinfo=utc),
                },
                {
                    "Key": "other-host/db-backup.2026-02-01.00.00.00.sqlite3",
                    "Size": 4096,
                    "LastModified": datetime.datetime(2026, 2, 1, tzinfo=utc),
                },
            ],
        )
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        mock_client.assert_called_once_with(
            "s3",
            endpoint_url="https://example.invalid",
            region_name="auto",
            aws_access_key_id="access-key",
            aws_secret_access_key="secret-key",
            config=CLOUD_BACKUP_S3_CONFIG,
        )
        mock_client.return_value.get_paginator.assert_called_with("list_objects_v2")
        paginator.paginate.assert_any_call(Bucket="test-bucket", Delimiter="/")
        paginator.paginate.assert_any_call(
            Bucket="test-bucket", Prefix="test-host.local/"
        )
        keys = [obj["key"] for obj in response.context["objects"]]
        self.assertEqual(
            keys,
            [
                "test-host.local/db-backup.2026-03-01.00.00.00.sqlite3",
                "test-host.local/db-backup.2026-01-01.00.00.00.sqlite3",
            ],
        )
        self.assertNotContains(response, "other-host/db-backup")
        self.assertContains(response, "2 objects, 3.0\xa0KB in total.")
        self.assertContains(response, '<td class="lt-num">2.0\xa0KB</td>', html=True)
        # The other hostname's folder and "All" are offered as links.
        self.assertContains(
            response,
            '<a href="?prefix=other-host/"><code>other-host/</code></a>',
            html=True,
        )
        self.assertContains(response, '<a href="?prefix=">All</a>', html=True)
        # Last-modified times are shown in UTC, like the keys' timestamps.
        self.assertContains(
            response,
            '<td class="lt-num">2026-03-01 00:00:00 UTC</td>',
            html=True,
        )

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
        LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_list_all_prefixes(self, mock_client):
        utc = datetime.UTC
        paginator = self._mock_bucket_listing(
            mock_client,
            folders=["other-host/"],
            objects=[
                {
                    "Key": "other-host/db-backup.2026-02-01.00.00.00.sqlite3",
                    "Size": 4096,
                    "LastModified": datetime.datetime(2026, 2, 1, tzinfo=utc),
                },
            ],
        )
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url, {"prefix": ""})
        paginator.paginate.assert_any_call(Bucket="test-bucket", Prefix="")
        self.assertContains(response, "other-host/db-backup.2026-02-01.00.00.00")
        self.assertContains(response, "all objects:")
        self.assertContains(response, "<strong>All</strong>", html=True)
        # This instance's folder is offered even though it has no backups yet.
        self.assertEqual(
            response.context["folders"], ["other-host/", "test-host.local/"]
        )
        self.assertContains(response, "(this instance)")

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_list_with_missing_settings(self, mock_client):
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backups can't be listed until")
        self.assertContains(
            response, "<code>LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID</code>", html=True
        )
        self.assertIsNone(response.context["objects"])
        mock_client.assert_not_called()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_list_s3_error_shows_message(self, mock_client):
        paginator = mock_client.return_value.get_paginator.return_value
        paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "ListObjectsV2",
        )
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Failed to list cloud backups:")
        self.assertContains(response, "AccessDenied")

    @override_settings(
        LIFETRACKER_BACKUP_S3_ENDPOINT_URL="not a url",
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    def test_cloud_backup_list_invalid_endpoint_shows_message(self):
        # Uses the real boto3.client, which raises ValueError for this endpoint.
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url)
        self.assertContains(
            response,
            "Failed to list cloud backups: Invalid S3 configuration: "
            "Invalid endpoint: not a url",
        )

    def test_cloud_backup_list_requires_superuser(self):
        non_super = User.objects.create_user("regular3", "r3@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular3", **{"password": "pass"})
        url = reverse("admin:core_cloudbackupstatus_list")
        response = client.get(url)
        self.assertEqual(response.status_code, 403)


@override_settings(
    LIFETRACKER_BACKUP_S3_ENDPOINT_URL="https://example.invalid",
    LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
    LIFETRACKER_BACKUP_S3_REGION_NAME="auto",
    LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
    LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
)
class CloudBackupCommandTests(TransactionTestCase):
    # Not TestCase: backup_database() refuses to run inside a transaction.

    """Tests for the cloud_backup management command."""

    def call(self):
        """Call cloud_backup and return captured stdout."""
        out = io.StringIO()
        call_command("cloud_backup", stdout=out)
        return out.getvalue()

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_uploads_and_records_status(self, mock_client):
        uploads = _record_uploads(mock_client)
        output = self.call()
        [upload] = uploads
        key = upload["Key"]
        self.assertEqual(upload["Bucket"], "test-bucket")
        self.assertRegex(key, r"^test-host\.local/db-backup\.")
        self.assertTrue(upload["Body"].startswith(b"SQLite format 3\x00"))
        self.assertEqual(output, f"Cloud backup uploaded successfully as {key}.\n")
        status = CloudBackupStatus.objects.get(pk=CLOUD_BACKUP_STATUS_PK)
        self.assertEqual(status.last_backup_object_key, key)

    @override_settings(
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_missing_settings_fail(self, mock_client):
        with self.assertRaisesMessage(
            CommandError,
            "Cloud backup failed: Cloud backup settings are incomplete. Missing: "
            "LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID, "
            "LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY.",
        ):
            self.call()
        mock_client.assert_not_called()

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_upload_error_fails(self, mock_client):
        mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        with self.assertRaisesMessage(CommandError, "Cloud backup failed: "):
            self.call()
        self.assertFalse(CloudBackupStatus.objects.exists())

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_upload_error_closes_backup_file(self, mock_client):
        mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        backup_files = []

        def backup_database_spy():
            backup_files.append(backup_database())
            return backup_files[-1]

        with mock.patch(
            "lifetracker.core.backup.backup_database", side_effect=backup_database_spy
        ):
            with self.assertRaises(CommandError):
                self.call()
        [backup_file] = backup_files
        self.assertTrue(backup_file.closed)

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_backup_copy_error_fails(self, mock_client):
        with mock.patch(
            "lifetracker.core.backup.backup_database",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            with self.assertRaisesMessage(
                CommandError, "Cloud backup failed: database or disk is full"
            ):
                self.call()
        mock_client.return_value.upload_fileobj.assert_not_called()
        self.assertFalse(CloudBackupStatus.objects.exists())

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_status_update_error_fails_naming_uploaded_key(self, mock_client):
        with mock.patch.object(
            CloudBackupStatus.objects,
            "update_or_create",
            side_effect=IntegrityError("boom"),
        ):
            with self.assertRaises(CommandError) as cm:
                self.call()
        key = mock_client.return_value.upload_fileobj.call_args.args[2]
        self.assertEqual(
            str(cm.exception),
            f"Cloud backup uploaded successfully as {key}, "
            "but failed to record backup status.",
        )
        self.assertIsInstance(cm.exception.__cause__.__cause__, IntegrityError)


class CloudBackupStatusMoveMigrationTests(TransactionTestCase):
    """taxtracker's 0004 copies the status row into core's table, and back."""

    before = [("core", "0001_initial"), ("taxtracker", "0003_cloudbackupstatus")]
    after = [
        ("core", "0002_cloudbackupstatus"),
        ("taxtracker", "0004_move_cloudbackupstatus_to_core"),
    ]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        # Leave the schema fully migrated for the tests that follow.
        call_command("migrate", verbosity=0)

    def test_forwards_and_backwards(self):
        backup_time = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        row = {
            "pk": CLOUD_BACKUP_STATUS_PK,
            "last_backup_at": backup_time,
            "last_backup_object_key": "host/db-backup.2026-01-02.03.04.05.sqlite3",
            "last_backup_size": 1234,
        }
        fields = list(row)

        apps = self._migrate(self.before)
        apps.get_model("taxtracker", "CloudBackupStatus").objects.create(**row)

        apps = self._migrate(self.after)
        self.assertEqual(
            list(apps.get_model("core", "CloudBackupStatus").objects.values(*fields)),
            [row],
        )
        self.assertNotIn(
            "taxtracker_cloudbackupstatus", connection.introspection.table_names()
        )

        apps = self._migrate(self.before)
        self.assertEqual(
            list(
                apps.get_model("taxtracker", "CloudBackupStatus").objects.values(
                    *fields
                )
            ),
            [row],
        )
        self.assertNotIn(
            "core_cloudbackupstatus", connection.introspection.table_names()
        )


class ExtractDateFromFilenameTests(TestCase):
    def test_extract_iso_date(self):
        self.assertEqual(
            extract_date_from_filename("statement-2026-01-15.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_compact_date_no_separator(self):
        self.assertEqual(
            extract_date_from_filename("report20260115.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_date_followed_by_digits(self):
        """DD may be followed by digits – the match is still valid."""
        self.assertEqual(
            extract_date_from_filename("20260101123.pdf"),
            datetime.date(2026, 1, 1),
        )

    def test_year_preceded_by_non_digit_allowed(self):
        """Year preceded by a non-digit character (e.g. '-') is valid."""
        self.assertEqual(
            extract_date_from_filename("123-2026-01-01.pdf"),
            datetime.date(2026, 1, 1),
        )

    def test_year_preceded_by_digit_rejected(self):
        """Year directly preceded by another digit must not be matched."""
        self.assertIsNone(extract_date_from_filename("1232026-01-01.pdf"))

    def test_invalid_calendar_date_skipped(self):
        """A pattern that is not a real date (e.g. month 13) must be ignored."""
        self.assertIsNone(extract_date_from_filename("2026-13-45.pdf"))

    def test_returns_first_valid_date(self):
        """When multiple date patterns exist, the first valid one is returned."""
        self.assertEqual(
            extract_date_from_filename("2026-13-45_2025-06-30.txt"),
            datetime.date(2025, 6, 30),
        )

    def test_no_date_in_filename(self):
        self.assertIsNone(extract_date_from_filename("nodatehere.pdf"))


class FileTypeForFilenameTests(TestCase):
    def setUp(self):
        self.pdf = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        FileExtension.objects.create(file_type=self.pdf, extension="pdf")

    def test_matches_extension_case_insensitively(self):
        self.assertEqual(file_type_for_filename("Statement.PDF"), self.pdf)

    def test_unknown_extension(self):
        self.assertIsNone(file_type_for_filename("notes.xyz"))

    def test_no_extension(self):
        self.assertIsNone(file_type_for_filename("README"))


class FlexibleDateFieldTests(TestCase):
    """Tests for FlexibleDateField and the extra English date-input formats."""

    def setUp(self):
        self.field = FlexibleDateField(required=False)

    def test_iso_format_accepted(self):
        self.assertEqual(self.field.clean("2023-07-20"), datetime.date(2023, 7, 20))

    def test_day_month_year_full_name(self):
        """'20 July 2023' should parse to 2023-07-20."""
        self.assertEqual(self.field.clean("20 July 2023"), datetime.date(2023, 7, 20))

    def test_month_day_comma_year_full_name(self):
        """'June 30, 2023' should parse to 2023-06-30."""
        self.assertEqual(self.field.clean("June 30, 2023"), datetime.date(2023, 6, 30))

    def test_day_month_year_abbreviated(self):
        """'20 Jul 2023' should parse to 2023-07-20."""
        self.assertEqual(self.field.clean("20 Jul 2023"), datetime.date(2023, 7, 20))

    def test_month_day_comma_year_abbreviated(self):
        """'Jun 30, 2023' should parse to 2023-06-30."""
        self.assertEqual(self.field.clean("Jun 30, 2023"), datetime.date(2023, 6, 30))

    def test_invalid_date_string_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean("not a date")

    def test_empty_string_returns_none(self):
        self.assertIsNone(self.field.clean(""))


class PurgeOrphanedFilesTests(TestCase):
    """Tests for orphaned_stored_file_pks and the purge_orphaned_files command.

    They use taxtracker's and tasktracker's attachment models to check that
    file fields are found across apps.
    """

    def setUp(self):
        item = Item.objects.create(
            year=FinancialYear.objects.create(year=2024), title="Item", order=1
        )
        self.attachment = Attachment.objects.create(
            item=item, file=ContentFile(b"kept", name="kept.pdf")
        )
        self.task_attachment = TaskAttachment.objects.create(
            task=Task.objects.create(title="Task", order=1),
            file=ContentFile(b"also kept", name="task.pdf"),
        )
        # As left behind by a delete before delete_stored_files was connected.
        self.orphans = [
            DBStoredFile.objects.create(filename="a.pdf", content=b"12345"),
            DBStoredFile.objects.create(filename="b.pdf", content=b"123"),
        ]
        self.orphan_pks = [orphan.pk for orphan in self.orphans]

    def call(self, *args):
        """Call purge_orphaned_files and return captured stdout."""
        out = io.StringIO()
        call_command("purge_orphaned_files", *args, stdout=out)
        return out.getvalue()

    def referenced_pks(self):
        return {
            att.file.storage._name_to_pk(att.file.name)
            for att in (self.attachment, self.task_attachment)
        }

    def test_finds_only_unreferenced_files(self):
        self.assertEqual(orphaned_stored_file_pks(), self.orphan_pks)

    def test_ignores_rows_without_a_file_or_with_an_unparseable_name(self):
        # Neither refers to a stored file, so neither keeps one.
        Attachment.objects.filter(pk=self.attachment.pk).update(file="")
        TaskAttachment.objects.filter(pk=self.task_attachment.pk).update(
            file="not-a-db-path.pdf"
        )
        self.assertEqual(
            orphaned_stored_file_pks(),
            sorted(self.orphan_pks + list(self.referenced_pks())),
        )

    def test_dry_run_reports_without_deleting(self):
        output = self.call()
        self.assertEqual(
            output,
            "Found 2 orphaned stored file(s), 8 bytes. Run with --delete to delete.\n",
        )
        self.assertEqual(DBStoredFile.objects.count(), 4)

    def test_delete_removes_only_orphans(self):
        output = self.call("--delete")
        self.assertEqual(output, "Deleted 2 orphaned stored file(s), 8 bytes.\n")
        self.assertEqual(
            set(DBStoredFile.objects.values_list("pk", flat=True)),
            self.referenced_pks(),
        )
        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.file.read(), b"kept")

    def test_nothing_orphaned(self):
        DBStoredFile.objects.filter(pk__in=self.orphan_pks).delete()
        self.assertEqual(
            self.call("--delete"), "Deleted 0 orphaned stored file(s), 0 bytes.\n"
        )
        self.assertEqual(DBStoredFile.objects.count(), 2)

    @mock.patch(
        "lifetracker.core.management.commands.purge_orphaned_files._BATCH_SIZE", 1
    )
    def test_deletes_in_batches(self):
        self.assertEqual(
            self.call("--delete"), "Deleted 2 orphaned stored file(s), 8 bytes.\n"
        )
        self.assertFalse(DBStoredFile.objects.filter(pk__in=self.orphan_pks).exists())


# /proc/net/tcp and tcp6 excerpts: a listener on 8000 (state 0A), established
# connections to local ports 8000 (0x1F40) and 22 (0x0016), and an outgoing one
# from ephemeral port 0xA2C4 to a remote port 22, which mustn't count.
_PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid
   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000 10001
   1: 0100007F:1F40 0100007F:D3B2 01 00000000:00000000 00:00000000 00000000 10001
   2: 0100007F:A2C4 0200007F:0016 01 00000000:00000000 00:00000000 00000000 10001
"""
_PROC_NET_TCP6 = """\
  sl  local_address                         remote_address                        st
   0: 00000000000000000000000000000000:0016 00000000000000000000000000000000:0000 0A
   1: 00AA00FD000000000000000000000002:0016 00AA00FD000000000000000000000001:C350 01
   2: 00AA00FD000000000000000000000002:0016 00AA00FD000000000000000000000001:C351 01
"""


class IdleWatchdogConfigTests(SimpleTestCase):
    def test_defaults(self):
        config = idle_watchdog.config_from_env({})

        self.assertEqual(
            config,
            idle_watchdog.Config(
                mode="off",
                idle_seconds=600,
                interval_seconds=30,
                ports=(8000, 22),
                status_file=Path("/tmp/lifetracker-idle-watchdog.json"),
                request_file=Path("/tmp/lifetracker-last-request"),
            ),
        )

    def test_blank_values_use_defaults(self):
        config = idle_watchdog.config_from_env(
            {
                "LIFETRACKER_IDLE_STOP": " ",
                "LIFETRACKER_IDLE_STOP_MINUTES": "",
                "LIFETRACKER_IDLE_STOP_PORTS": "",
                "PORT": "",
            }
        )

        self.assertEqual(config, idle_watchdog.config_from_env({}))

    def test_default_ports_follow_gunicorn_port(self):
        config = idle_watchdog.config_from_env({"PORT": "9000"})

        self.assertEqual(config.ports, (9000, 22))

    def test_settings_from_env(self):
        config = idle_watchdog.config_from_env(
            {
                "LIFETRACKER_IDLE_STOP": "Stop",
                "LIFETRACKER_IDLE_STOP_MINUTES": "2.5",
                "LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS": "5",
                "LIFETRACKER_IDLE_STOP_PORTS": "8080, 2222,8080",
                "LIFETRACKER_IDLE_STOP_STATUS_FILE": "/run/status.json",
                "LIFETRACKER_IDLE_STOP_REQUEST_FILE": "/run/last-request",
            }
        )

        self.assertEqual(
            config,
            idle_watchdog.Config(
                mode="stop",
                idle_seconds=150,
                interval_seconds=5,
                ports=(8080, 2222),
                status_file=Path("/run/status.json"),
                request_file=Path("/run/last-request"),
            ),
        )

    def test_uptime_dir(self):
        for env, expected in [
            ({}, None),
            ({"LIFETRACKER_DATA_DIR": "/data"}, Path("/data/uptime")),
            ({"LIFETRACKER_DATA_DIR": " "}, None),
            (
                {"LIFETRACKER_DATA_DIR": "/data", "LIFETRACKER_UPTIME_DIR": "/up"},
                Path("/up"),
            ),
            (
                {"LIFETRACKER_DATA_DIR": "/data", "LIFETRACKER_UPTIME_DIR": "Off"},
                None,
            ),
        ]:
            with self.subTest(env=env):
                self.assertEqual(
                    idle_watchdog.config_from_env(env).uptime_dir, expected
                )

    def test_invalid_values_raise(self):
        for name, value, message in [
            ("LIFETRACKER_IDLE_STOP", "on", "must be one of off, monitor, stop"),
            ("LIFETRACKER_IDLE_STOP_MINUTES", "0", "must be a positive number"),
            ("LIFETRACKER_IDLE_STOP_MINUTES", "ten", "must be a positive number"),
            ("LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS", "-1", "positive number"),
            ("LIFETRACKER_IDLE_STOP_PORTS", "80,ssh", "comma-separated port"),
            ("LIFETRACKER_IDLE_STOP_PORTS", "0", "comma-separated port"),
            ("LIFETRACKER_IDLE_STOP_PORTS", "65536", "comma-separated port"),
        ]:
            with self.subTest(name=name, value=value):
                with self.assertRaisesMessage(ValueError, message):
                    idle_watchdog.config_from_env({name: value})


class IdleWatchdogConnectionTests(SimpleTestCase):
    def test_established_local_ports(self):
        self.assertEqual(
            list(idle_watchdog.established_local_ports(_PROC_NET_TCP)),
            [8000, 0xA2C4],
        )
        self.assertEqual(
            list(idle_watchdog.established_local_ports(_PROC_NET_TCP6)), [22, 22]
        )

    def test_count_connections_reads_ipv4_and_ipv6(self):
        with tempfile.TemporaryDirectory() as tempdir:
            tcp = Path(tempdir) / "tcp"
            tcp6 = Path(tempdir) / "tcp6"
            tcp.write_text(_PROC_NET_TCP)
            tcp6.write_text(_PROC_NET_TCP6)

            counts = idle_watchdog.count_connections([8000, 22, 443], paths=[tcp, tcp6])

        self.assertEqual(counts, {8000: 1, 22: 2, 443: 0})

    def test_count_connections_skips_missing_table(self):
        with tempfile.TemporaryDirectory() as tempdir:
            tcp = Path(tempdir) / "tcp"
            tcp.write_text(_PROC_NET_TCP)

            counts = idle_watchdog.count_connections(
                [8000, 22], paths=[tcp, Path(tempdir) / "tcp6"]
            )

        self.assertEqual(counts, {8000: 1, 22: 0})


def _watchdog_config(mode="stop", status_file=Path("/nonexistent/status.json")):
    return idle_watchdog.Config(
        mode=mode,
        idle_seconds=60,
        interval_seconds=10,
        ports=(8000, 22),
        status_file=status_file,
        request_file=Path("/nonexistent/last-request"),
    )


class IdleWatchdogStateTests(SimpleTestCase):
    START = 1_800_000_000  # 2027-01-15T08:00:00Z

    def test_idle_time_counts_from_startup(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)

        self.assertFalse(watchdog.check(self.START + 59, {8000: 0, 22: 0}, None))
        self.assertTrue(watchdog.check(self.START + 60, {8000: 0, 22: 0}, None))

    def test_connection_resets_idle_time(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)

        self.assertFalse(watchdog.check(self.START + 50, {8000: 0, 22: 1}, None))
        self.assertFalse(watchdog.check(self.START + 100, {8000: 0, 22: 0}, None))
        self.assertTrue(watchdog.check(self.START + 110, {8000: 0, 22: 0}, None))

    def test_request_resets_idle_time(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)
        idle = {8000: 0, 22: 0}

        # A request between checks, whose connection no check saw open.
        self.assertFalse(watchdog.check(self.START + 50, idle, self.START + 45))
        self.assertFalse(watchdog.check(self.START + 100, idle, self.START + 45))
        self.assertTrue(watchdog.check(self.START + 105, idle, self.START + 45))
        self.assertEqual(
            watchdog.status()["last_request_at"], "2027-01-15T08:00:45+00:00"
        )

    def test_request_before_startup_is_ignored(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)

        self.assertTrue(
            watchdog.check(self.START + 60, {8000: 0, 22: 0}, self.START - 3600)
        )

    def test_request_time_in_future_counts_as_now(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)
        idle = {8000: 0, 22: 0}

        watchdog.check(self.START + 10, idle, self.START + 3600)

        self.assertEqual(watchdog.idle_since, self.START + 10)

    def test_status(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1234, self.START)
        watchdog.check(self.START + 10, {8000: 2, 22: 0}, None)
        watchdog.check(self.START + 20, {8000: 0, 22: 0}, None)

        self.assertEqual(
            watchdog.status(),
            {
                "mode": "stop",
                "state": "idle",
                "pid": os.getpid(),
                "master_pid": 1234,
                "started_at": "2027-01-15T08:00:00+00:00",
                "checked_at": "2027-01-15T08:00:20+00:00",
                "check_interval_seconds": 10,
                "idle_timeout_seconds": 60,
                "last_request_at": None,
                "idle_since": "2027-01-15T08:00:10+00:00",
                "stop_due_at": "2027-01-15T08:01:10+00:00",
                "ports": [
                    {
                        "port": 8000,
                        "connections": 0,
                        "last_connection_at": "2027-01-15T08:00:10+00:00",
                    },
                    {"port": 22, "connections": 0, "last_connection_at": None},
                ],
            },
        )

    def test_status_state(self):
        watchdog = idle_watchdog.Watchdog(_watchdog_config(), 1, self.START)
        self.assertEqual(watchdog.status()["checked_at"], None)

        watchdog.check(self.START + 1, {8000: 0, 22: 1}, None)
        self.assertEqual(watchdog.status()["state"], "active")

        watchdog.stopping = True
        self.assertEqual(watchdog.status()["state"], "stopping")

    def test_write_status_replaces_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "status.json"
            path.write_text("old")

            idle_watchdog.write_status(path, {"state": "idle"})

            self.assertEqual(json.loads(path.read_text()), {"state": "idle"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(os.listdir(tempdir), ["status.json"])

    def test_write_status_removes_temp_file_on_failure(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "status.json"

            with self.assertRaises(TypeError):
                idle_watchdog.write_status(path, {"unserialisable": object()})

            self.assertEqual(os.listdir(tempdir), [])


class IdleWatchdogRunTests(SimpleTestCase):
    MASTER_PID = 4321

    def _run(self, mode, counts, *, getppid=None, status_file=None):
        """Run the loop with a fake clock that advances 10s per sleep, and
        fake connection counts taken from ``counts`` in turn."""
        now = [1_800_000_000.0]
        counts = iter(counts)
        kill = mock.Mock()
        with tempfile.TemporaryDirectory() as tempdir:
            config = _watchdog_config(
                mode, status_file or Path(tempdir) / "status.json"
            )
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                result = idle_watchdog.run(
                    config,
                    self.MASTER_PID,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    count=lambda ports: next(counts),
                    request_time=lambda path: None,
                    getppid=getppid or (lambda: self.MASTER_PID),
                    kill=kill,
                )
            status = (
                json.loads(config.status_file.read_text())
                if config.status_file.exists()
                else None
            )
        return result, kill, stderr.getvalue(), status

    def test_stop_mode_sends_sigterm_once_idle(self):
        idle = {8000: 0, 22: 0}
        busy = {8000: 0, 22: 1}

        result, kill, stderr, status = self._run(
            "stop", [busy, idle, idle, idle, idle, idle, idle, idle]
        )

        self.assertEqual(result, 0)
        kill.assert_called_once_with(self.MASTER_PID, signal.SIGTERM)
        self.assertIn("sending SIGTERM to gunicorn master 4321", stderr)
        self.assertEqual(status["state"], "stopping")
        self.assertEqual(status["checked_at"], "2027-01-15T08:01:00+00:00")

    def test_monitor_mode_reports_but_never_stops(self):
        idle = {8000: 0, 22: 0}
        busy = {8000: 1, 22: 0}
        ppids = iter([self.MASTER_PID] * 16 + [1])

        result, kill, stderr, status = self._run(
            "monitor", [idle] * 7 + [busy] + [idle] * 8, getppid=lambda: next(ppids)
        )

        self.assertEqual(result, 0)
        kill.assert_not_called()
        # Reported once per idle stretch: after startup, and after the busy check.
        self.assertEqual(stderr.count("monitor mode: not stopping"), 2)
        self.assertIn("gunicorn master 4321 has exited, exiting", stderr)
        self.assertEqual(status["state"], "idle")

    def test_status_write_failure_is_logged_and_ignored(self):
        result, kill, stderr, status = self._run(
            "stop",
            [{8000: 0, 22: 0}] * 7,
            status_file=Path("/nonexistent/status.json"),
        )

        self.assertEqual(result, 0)
        kill.assert_called_once()
        self.assertIn("couldn't write /nonexistent/status.json", stderr)
        self.assertIsNone(status)


class IdleWatchdogSpawnTests(SimpleTestCase):
    def test_off_by_default(self):
        with mock.patch("subprocess.Popen") as popen:
            self.assertIsNone(idle_watchdog.spawn(99, {}))

        popen.assert_not_called()

    def test_invalid_config_is_logged_not_raised(self):
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertIsNone(idle_watchdog.spawn(99, {"LIFETRACKER_IDLE_STOP": "yes"}))

        popen.assert_not_called()
        self.assertIn(
            "idle-watchdog: not starting: LIFETRACKER_IDLE_STOP", stderr.getvalue()
        )

    def test_not_started_without_proc_net(self):
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(
                idle_watchdog, "PROC_NET_TCP_FILES", (Path("/nonexistent/tcp"),)
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertIsNone(
                idle_watchdog.spawn(99, {"LIFETRACKER_IDLE_STOP": "monitor"})
            )

        popen.assert_not_called()
        self.assertIn("/nonexistent/tcp is missing", stderr.getvalue())

    def test_starts_for_uptime_alone(self):
        env = {"LIFETRACKER_DATA_DIR": "/data"}
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertIs(idle_watchdog.spawn(99, env), popen.return_value)

    def test_starts_module_with_master_pid(self):
        env = {"LIFETRACKER_IDLE_STOP": "stop"}
        with (
            mock.patch("subprocess.Popen") as popen,
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertIs(idle_watchdog.spawn(99, env), popen.return_value)

        popen.assert_called_once_with(
            [sys.executable, "-m", "lifetracker.core.idle_watchdog", "99"], env=env
        )

    def test_request_file_from_env(self):
        for env, expected in [
            ({}, None),
            ({"LIFETRACKER_IDLE_STOP": "bogus"}, None),
            (
                {"LIFETRACKER_IDLE_STOP": "monitor"},
                Path("/tmp/lifetracker-last-request"),
            ),
            (
                {
                    "LIFETRACKER_IDLE_STOP": "stop",
                    "LIFETRACKER_IDLE_STOP_REQUEST_FILE": "/run/last-request",
                },
                Path("/run/last-request"),
            ),
        ]:
            with self.subTest(env=env):
                self.assertEqual(idle_watchdog.request_file_from_env(env), expected)

    def test_record_request_touches_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "last-request"
            self.assertIsNone(idle_watchdog.last_request_time(path))

            idle_watchdog.record_request(path)
            first = idle_watchdog.last_request_time(path)
            os.utime(path, (first - 100, first - 100))
            idle_watchdog.record_request(path)

            self.assertIsNotNone(first)
            self.assertGreaterEqual(idle_watchdog.last_request_time(path), first)

    def test_record_request_never_raises(self):
        idle_watchdog.record_request(None)
        idle_watchdog.record_request(Path("/nonexistent/last-request"))

    def test_gunicorn_post_request_records_request(self):
        path = Path("/run/last-request")
        with (
            mock.patch.object(gunicorn_conf, "_REQUEST_FILE", path),
            mock.patch.object(idle_watchdog, "record_request") as record_request,
        ):
            gunicorn_conf.post_request(mock.Mock(), mock.Mock(), {}, mock.Mock())

        record_request.assert_called_once_with(path)

    def test_gunicorn_access_log_shows_fly_client_ip(self):
        # Gunicorn takes any module-level name in its config module that's a
        # setting's name as that setting.
        cfg = gunicorn.config.Config()
        cfg.set("access_log_format", gunicorn_conf.access_log_format)
        cfg.set("accesslog", "-")
        logger = gunicorn.glogging.Logger(cfg)
        environ = {
            "REMOTE_ADDR": "172.16.1.2",
            "REQUEST_METHOD": "GET",
            "RAW_URI": "/admin/",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        resp = mock.Mock(status="200 OK", sent=42, headers=[])

        def logged(headers):
            req = mock.Mock(headers=headers)
            with mock.patch.object(logger.access_log, "info") as info:
                logger.access(resp, req, environ, datetime.timedelta(0))
            fmt, atoms = info.call_args.args
            return fmt % atoms

        line = logged([("FLY-CLIENT-IP", "203.0.113.7")])
        self.assertRegex(
            line,
            r'^203\.0\.113\.7 172\.16\.1\.2 - - \[.*\] "GET /admin/ HTTP/1\.1" 200',
        )
        self.assertRegex(logged([]), r"^- 172\.16\.1\.2 ")

    def test_gunicorn_on_exit_exits_1_if_envoy_failed(self):
        server = mock.Mock()
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "envoy-failed"
            env = {"LIFETRACKER_ENVOY_FAILED_FILE": str(path)}
            with mock.patch.dict(os.environ, env):
                self.assertIsNone(gunicorn_conf.on_exit(server))
                path.touch()
                with self.assertRaises(SystemExit) as cm:
                    gunicorn_conf.on_exit(server)

        self.assertEqual(cm.exception.code, 1)
        server.log.error.assert_called_once_with(
            "Exiting with status 1, as Envoy kept failing"
        )

    def test_gunicorn_on_exit_without_envoy_failed_file_setting(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("LIFETRACKER_ENVOY_FAILED_FILE", None)
            self.assertIsNone(gunicorn_conf.on_exit(mock.Mock()))

    def test_gunicorn_when_ready_spawns_for_master(self):
        server = mock.Mock(pid=1234)
        with (
            mock.patch.object(idle_watchdog, "spawn") as spawn,
            mock.patch.object(client_ips, "spawn") as spawn_collector,
        ):
            gunicorn_conf.when_ready(server)

        spawn.assert_called_once_with(1234)
        spawn_collector.assert_called_once_with(1234)

    def test_main_usage(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(idle_watchdog.main([]), 2)

        self.assertIn("usage:", stderr.getvalue())

    def test_main_invalid_config(self):
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP_MINUTES": "x"}),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            mock.patch("signal.signal"),
        ):
            self.assertEqual(idle_watchdog.main(["99"]), 2)

        self.assertIn("LIFETRACKER_IDLE_STOP_MINUTES", stderr.getvalue())

    def test_main_runs_watchdog_for_master_pid(self):
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP": "monitor"}),
            mock.patch("signal.signal") as set_handler,
            mock.patch.object(idle_watchdog, "run", return_value=0) as run,
        ):
            self.assertEqual(idle_watchdog.main(["99"]), 0)

        run.assert_called_once_with(
            idle_watchdog.config_from_env({"LIFETRACKER_IDLE_STOP": "monitor"}), 99
        )
        set_handler.assert_has_calls(
            [
                mock.call(signal.SIGINT, idle_watchdog._exit_quietly),
                mock.call(signal.SIGTERM, idle_watchdog._exit_quietly),
            ]
        )

    def test_exit_quietly_exits_zero(self):
        with self.assertRaises(SystemExit) as raised:
            idle_watchdog._exit_quietly(signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 0)

    def test_real_process_stops_its_parent(self):
        """End to end: a shell stands in for gunicorn's master, starts the
        watchdog as its child, and is sent SIGTERM once it's been idle."""
        with tempfile.TemporaryDirectory() as tempdir:
            status_file = Path(tempdir) / "status.json"
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    f'"{sys.executable}" -m lifetracker.core.idle_watchdog $$ & wait',
                ],
                env={
                    **os.environ,
                    "LIFETRACKER_IDLE_STOP": "stop",
                    "LIFETRACKER_IDLE_STOP_MINUTES": "0.001",
                    "LIFETRACKER_IDLE_STOP_INTERVAL_SECONDS": "0.05",
                    # Port 1 (tcpmux): nothing will be connected to it.
                    "LIFETRACKER_IDLE_STOP_PORTS": "1",
                    "LIFETRACKER_IDLE_STOP_STATUS_FILE": str(status_file),
                    "LIFETRACKER_UPTIME_DIR": str(Path(tempdir) / "uptime"),
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            status = json.loads(status_file.read_text())
            uptime_files = list((Path(tempdir) / "uptime").glob("*.bin"))
            up = uptime_log.active_seconds(uptime_files[0].read_bytes())

        self.assertEqual(result.returncode, -signal.SIGTERM, result.stderr)
        self.assertGreaterEqual(up, 1)
        self.assertIn("sending SIGTERM", result.stderr)
        self.assertEqual(status["state"], "stopping")
        self.assertEqual(
            status["ports"], [{"port": 1, "connections": 0, "last_connection_at": None}]
        )


class IdleWatchdogUptimeRunTests(SimpleTestCase):
    MASTER_PID = 4321
    START = 1_800_000_000.25  # 2027-01-15T08:00:00.25Z

    def _run(self, mode, *, ticks, uptime_dir, booted=None, clock_step=None):
        """Run the loop for ``ticks`` wakes (then the master "exits"), with a
        fake clock that each sleep advances by what was asked plus
        ``clock_step`` (a function of the wake number), if given."""
        now = [self.START]
        ppids = iter([self.MASTER_PID] * ticks + [1])
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds + (clock_step(len(sleeps)) if clock_step else 0)

        config = dataclasses.replace(_watchdog_config(mode), uptime_dir=uptime_dir)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = idle_watchdog.run(
                config,
                self.MASTER_PID,
                clock=lambda: now[0],
                sleep=sleep,
                count=lambda ports: {8000: 1, 22: 0},
                request_time=lambda path: None,
                getppid=lambda: next(ppids),
                kill=mock.Mock(),
                uptime=lambda: booted,
            )
        return result, sleeps, stderr.getvalue()

    def _up_seconds(self, uptime_dir):
        data = uptime_log.read_day(uptime_dir, uptime_log.day_of(self.START))
        bits = int.from_bytes(data, "big")
        return [
            second
            for second in range(uptime_log.SECONDS_PER_DAY)
            if bits >> (uptime_log.SECONDS_PER_DAY - 1 - second) & 1
        ]

    def test_records_every_second(self):
        with tempfile.TemporaryDirectory() as tempdir:
            result, sleeps, stderr = self._run("off", ticks=5, uptime_dir=Path(tempdir))
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(result, 0)
        # First to the next whole second, then a second at a time.
        self.assertEqual(sleeps, [0.75, 1, 1, 1, 1])
        self.assertEqual(up, [28800, 28801, 28802, 28803, 28804])
        self.assertIn(f"recording uptime in {tempdir}", stderr)
        self.assertNotIn("started in", stderr)

    def test_nothing_to_do_exits_at_once(self):
        # e.g. run by hand with both off: without the early return the loop
        # would have nothing to wake for, and time.sleep(inf) raises.
        sleep = mock.Mock()

        result = idle_watchdog.run(
            _watchdog_config("off"), self.MASTER_PID, sleep=sleep
        )

        self.assertEqual(result, 0)
        sleep.assert_not_called()

    def test_checks_still_every_interval(self):
        with tempfile.TemporaryDirectory() as tempdir:
            status_file = Path(tempdir) / "status.json"
            config = dataclasses.replace(
                _watchdog_config("monitor", status_file),
                uptime_dir=Path(tempdir) / "uptime",
            )
            now = [self.START]
            ppids = iter([self.MASTER_PID] * 25 + [1])
            count = mock.Mock(return_value={8000: 0, 22: 1})
            with mock.patch("sys.stderr", new_callable=io.StringIO):
                idle_watchdog.run(
                    config,
                    self.MASTER_PID,
                    clock=lambda: now[0],
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    count=count,
                    request_time=lambda path: None,
                    getppid=lambda: next(ppids),
                    kill=mock.Mock(),
                    uptime=lambda: None,
                )
            status = json.loads(status_file.read_text())

        # Checks at 0, 10 and 20 seconds, of 25 wakes.
        self.assertEqual(count.call_count, 3)
        self.assertEqual(status["checked_at"], "2027-01-15T08:00:20+00:00")

    def test_uptime_alone_still_syncs_every_interval(self):
        with (
            tempfile.TemporaryDirectory() as tempdir,
            mock.patch.object(uptime_log.UptimeLog, "sync") as sync,
        ):
            self._run("off", ticks=25, uptime_dir=Path(tempdir))

        # Once on taking up the first day (when there's nothing to sync), then
        # every 10s interval: the wakes 11 and 21 seconds in, of 25.
        self.assertEqual(sync.call_count, 3)

    def test_short_gaps_are_filled_long_ones_not(self):
        # The 2nd sleep oversleeps by 2s, the 4th by 10s.
        with tempfile.TemporaryDirectory() as tempdir:
            self._run(
                "off",
                ticks=6,
                uptime_dir=Path(tempdir),
                clock_step=lambda n: {2: 2, 4: 10}.get(n, 0),
            )
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(up, [28800, 28801, 28802, 28803, 28804, 28805, 28816, 28817])

    def test_fresh_boot_is_backfilled(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self._run("off", ticks=1, uptime_dir=Path(tempdir), booted=3.5)
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(up, [28796, 28797, 28798, 28799, 28800])

    def test_long_uptime_is_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self._run("off", ticks=1, uptime_dir=Path(tempdir), booted=3600)
            up = self._up_seconds(Path(tempdir))

        self.assertEqual(up, [28800])

    def test_write_failure_is_logged_once_and_retried(self):
        with tempfile.TemporaryDirectory() as tempdir:
            blocker = Path(tempdir) / "uptime"
            blocker.write_text("not a directory")

            def unblock(n):
                if n == 3:
                    blocker.unlink()
                return 0

            result, sleeps, stderr = self._run(
                "off", ticks=5, uptime_dir=blocker, clock_step=unblock
            )
            up = self._up_seconds(blocker)

        self.assertEqual(result, 0)
        self.assertEqual(stderr.count("couldn't record uptime"), 1)
        self.assertIn("recording uptime again", stderr)
        # The seconds that failed are recorded once it works again.
        self.assertEqual(up, [28800, 28801, 28802, 28803, 28804])


class UptimeLogTests(SimpleTestCase):
    DAY = 20_748  # 2026-10-22
    MIDNIGHT = DAY * uptime_log.SECONDS_PER_DAY

    def test_file_layout(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT + 0.9)
            log.record(self.MIDNIGHT + 9)
            log.record(self.MIDNIGHT + 86_399)
            path = Path(tempdir) / "2026-10-22.bin"
            data = path.read_bytes()

        self.assertEqual(len(data), 10_800)
        # Most significant bit first: second 0 is 0x80 of byte 0.
        self.assertEqual(data[:2], b"\x80\x40")
        self.assertEqual(data[-1], 0x01)
        self.assertEqual(uptime_log.active_seconds(data), 3)

    def test_leaving_a_day_syncs_it(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT - 1)
            with mock.patch("os.fsync") as fsync:
                log.record(self.MIDNIGHT)

        fsync.assert_called_once()

    def test_reopening_keeps_earlier_seconds(self):
        with tempfile.TemporaryDirectory() as tempdir:
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT + 1)
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT + 2)
            data = uptime_log.read_day(Path(tempdir), self.DAY)

        self.assertEqual(data[0], 0x60)

    def test_range_across_midnight(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT + 1, since=self.MIDNIGHT - 2)
            log.sync()
            before = uptime_log.read_day(Path(tempdir), self.DAY - 1)
            after = uptime_log.read_day(Path(tempdir), self.DAY)
            first = uptime_log.first_recorded_day(Path(tempdir))

        self.assertEqual(before[-1], 0x03)
        self.assertEqual(after[0], 0xC0)
        self.assertEqual(first, self.DAY - 1)

    def test_missing_and_short_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            self.assertIsNone(uptime_log.read_day(Path(tempdir), self.DAY))
            self.assertIsNone(uptime_log.first_recorded_day(Path(tempdir)))
            (Path(tempdir) / "2026-10-22.bin").write_bytes(b"\xff")
            (Path(tempdir) / "notes.bin").write_bytes(b"")
            data = uptime_log.read_day(Path(tempdir), self.DAY)
            first = uptime_log.first_recorded_day(Path(tempdir))

        self.assertEqual(len(data), 10_800)
        self.assertEqual(uptime_log.active_seconds(data), 8)
        self.assertEqual(first, self.DAY)

    def test_start_count(self):
        data = bytearray(10_800)
        data[0] = 0b1100_1000  # up at midnight, then a start at second 4
        data[1] = 0b0000_0001  # ... and at second 15
        data[2] = 0b1000_0000  # still up at 16

        self.assertEqual(uptime_log.start_count(bytes(data)), 3)
        self.assertEqual(uptime_log.start_count(bytes(10_800)), 0)

    def test_up_periods(self):
        data = bytearray(10_800)
        data[0] = 0b1100_1000  # up at midnight, then a start at second 4
        data[1] = 0b0000_0001  # ... and at second 15
        data[2] = 0b1000_0000  # still up at 16
        data[-1] = 0b0000_0011  # and up for the day's last two seconds

        periods = uptime_log.up_periods(bytes(data))

        self.assertEqual(
            [(p.start, p.end, p.seconds) for p in periods],
            [(0, 1, 2), (4, 4, 1), (15, 16, 2), (86_398, 86_399, 2)],
        )
        self.assertEqual(len(periods), uptime_log.start_count(bytes(data)))
        self.assertEqual(
            sum(p.seconds for p in periods), uptime_log.active_seconds(bytes(data))
        )
        self.assertEqual(uptime_log.up_periods(bytes(10_800)), [])
        self.assertEqual(
            [(p.start, p.end) for p in uptime_log.up_periods(b"\xff" * 10_800)],
            [(0, 86_399)],
        )

    def test_day_periods_across_midnight(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            # Up from 23:00 the day before until 01:00, then 02:00 to the end
            # of the day, which the next day continues.
            log.record(self.MIDNIGHT + 3599, since=self.MIDNIGHT - 3600)
            log.record(self.MIDNIGHT + 86_400, since=self.MIDNIGHT + 7200)
            now = self.MIDNIGHT + 2 * 86_400
            result = uptime_log.day_periods(Path(tempdir), self.DAY, now)
            before = uptime_log.day_periods(Path(tempdir), self.DAY - 1, now)
            empty_after = uptime_log.day_periods(Path(tempdir), self.DAY + 2, now)

        self.assertEqual(result.date, uptime_log.day_date(self.DAY))
        self.assertEqual(
            [(p.start, p.end) for p in result.periods],
            [(0, 3599), (7200, 86_399)],
        )
        self.assertTrue(result.from_previous_day)
        self.assertTrue(result.into_next_day)
        self.assertFalse(result.ongoing)
        self.assertFalse(result.today)
        # The day before ran to midnight, but didn't begin the day before it.
        self.assertFalse(before.from_previous_day)
        self.assertTrue(before.into_next_day)
        self.assertEqual(empty_after.periods, [])
        self.assertTrue(empty_after.today)

    def test_day_periods_midnight_without_neighbours(self):
        with tempfile.TemporaryDirectory() as tempdir:
            uptime_log.UptimeLog(Path(tempdir)).record(
                self.MIDNIGHT + 86_399, since=self.MIDNIGHT
            )
            result = uptime_log.day_periods(
                Path(tempdir), self.DAY, self.MIDNIGHT + 2 * 86_400
            )

        self.assertEqual([(p.start, p.end) for p in result.periods], [(0, 86_399)])
        self.assertFalse(result.from_previous_day)
        self.assertFalse(result.into_next_day)

    def test_day_periods_ongoing_today(self):
        now = self.MIDNIGHT + 3600.5
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            log.record(self.MIDNIGHT + 100, since=self.MIDNIGHT + 10)
            log.record(now - 5, since=self.MIDNIGHT + 600)
            ongoing = uptime_log.day_periods(Path(tempdir), self.DAY, now)
            stopped = uptime_log.day_periods(Path(tempdir), self.DAY, now + 10)

        self.assertEqual(len(ongoing.periods), 2)
        self.assertTrue(ongoing.today)
        self.assertTrue(ongoing.ongoing)
        self.assertFalse(stopped.ongoing)

    def test_day_periods_no_data(self):
        with tempfile.TemporaryDirectory() as tempdir:
            empty = uptime_log.day_periods(Path(tempdir), self.DAY, self.MIDNIGHT)
            uptime_log.UptimeLog(Path(tempdir)).record(self.MIDNIGHT)
            before = uptime_log.day_periods(Path(tempdir), self.DAY - 1, self.MIDNIGHT)

        self.assertIsNone(empty.periods)
        self.assertIsNone(before.periods)
        self.assertFalse(before.from_previous_day or before.into_next_day)


class UptimeReportCommandTests(SimpleTestCase):
    NOW = 20_748 * 86_400 + 43_199.5  # 2026-10-22T11:59:59.5Z

    def _report(self, env, days=None):
        out = io.StringIO()
        kwargs = {} if days is None else {"days": days}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("time.time", return_value=self.NOW),
        ):
            call_command("uptime_report", stdout=out, **kwargs)
        return out.getvalue()

    def test_report(self):
        with tempfile.TemporaryDirectory() as tempdir:
            log = uptime_log.UptimeLog(Path(tempdir))
            # Half of 2026-10-20, none of the 21st, a quarter of today so far.
            log.record(20_746 * 86_400 + 43_199, since=20_746 * 86_400)
            log.record(self.NOW, since=self.NOW - 10_799.5)
            report = self._report({"LIFETRACKER_UPTIME_DIR": tempdir}, days=4)

        self.assertEqual(
            report.splitlines(),
            [
                "2026-10-19  no data",
                "2026-10-20   50.00%  up 43200 of 86400 s, 1 starts",
                "2026-10-21    0.00%  up 0 of 86400 s, 0 starts",
                "2026-10-22   25.00%  up 10800 of 43200 s, 1 starts  (so far)",
            ],
        )

    def test_errors(self):
        with (
            mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": "off"}),
            self.assertRaisesMessage(CommandError, "Uptime isn't recorded"),
        ):
            call_command("uptime_report")
        with self.assertRaisesMessage(CommandError, "--days must be at least 1"):
            self._report({"LIFETRACKER_UPTIME_DIR": "/nonexistent"}, days=0)


class UptimeReportAdminViewTests(TestCase):
    URL = "/admin/uptime/"
    NOW = UptimeReportCommandTests.NOW

    def setUp(self):
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.directory = Path(tempdir.name)
        for patcher in (
            mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": tempdir.name}),
            mock.patch("time.time", return_value=self.NOW),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rows(self, response):
        return [
            [cell.strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            for row in re.findall(
                r"<tr>(.*?)</tr>",
                response.content.decode().split("<tbody>")[1],
                re.S,
            )
        ]

    def test_url(self):
        self.assertEqual(reverse("admin:uptime_report"), self.URL)

    def test_top_bar_links_to_page_for_superusers_only(self):
        link = f'<a href="{self.URL}">Uptime</a>'
        self.assertContains(self.client.get(reverse("admin:index")), link, html=True)

        staff = User.objects.create_user("staff", "s@example.com", "pass")
        staff.is_staff = True
        staff.save()
        client = Client()
        client.login(username="staff", password="pass")
        self.assertNotContains(client.get(reverse("admin:index")), self.URL)
        self.assertEqual(client.get(self.URL).status_code, 403)

    def test_page_requires_login(self):
        response = Client().get(self.URL)

        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.URL}",
            fetch_redirect_response=False,
        )

    def test_page_shows_report(self):
        log = uptime_log.UptimeLog(self.directory)
        # As in UptimeReportCommandTests.test_report.
        log.record(20_746 * 86_400 + 43_199, since=20_746 * 86_400)
        log.record(self.NOW, since=self.NOW - 10_799.5)

        response = self.client.get(self.URL, {"days": "4"})

        self.assertContains(response, f"<code>{self.directory}</code>")
        self.assertContains(response, 'value="4"')
        rows = self._rows(response)
        # Newest first.
        self.assertEqual(
            [(row[0].split()[0], row[1]) for row in rows],
            [
                ("2026-10-22", "25.00%"),
                ("2026-10-21", "0.00%"),
                ("2026-10-20", "50.00%"),
                ("2026-10-19", "No data"),
            ],
        )
        self.assertIn("(so far)", rows[0][0])
        self.assertEqual(rows[0][3], "3\xa0hours of 12\xa0hours")
        self.assertEqual(rows[0][4], '<a href="/admin/uptime/2026-10-22/?days=4">1</a>')
        self.assertEqual(rows[3][4], "")
        self.assertEqual(rows[2][3], "12\xa0hours of 1\xa0day")
        self.assertIn('<meter min="0" max="100" value="50.00">', rows[2][2])

    def test_default_days(self):
        response = self.client.get(self.URL)

        self.assertEqual(len(self._rows(response)), 7)
        self.assertNotContains(response, "errornote")

    def test_bad_days(self):
        for days in ("0", "367", "x"):
            with self.subTest(days=days):
                response = self.client.get(self.URL, {"days": days})

                self.assertContains(
                    response, "Days must be a whole number from 1 to 366"
                )
                self.assertEqual(len(self._rows(response)), 7)

    def test_not_recorded(self):
        with mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": "off"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "Uptime isn't recorded")
        self.assertNotContains(response, 'lt-uptime"')

    def test_today_start_in_local_time(self):
        response = self.client.get(self.URL)

        self.assertContains(response, "/static/core/local_times.js")
        self.assertContains(
            response,
            "today's UTC day began at <time data-lt-local datetime=\""
            '2026-10-22T00:00:00Z">2026-10-22T00:00:00Z</time>.',
        )


class UptimeDayAdminViewTests(TestCase):
    NOW = UptimeReportCommandTests.NOW  # 2026-10-22T11:59:59.5Z
    URL = "/admin/uptime/2026-10-20/"

    def setUp(self):
        User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client.login(username="admin", password="password")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.directory = Path(tempdir.name)
        for patcher in (
            mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": tempdir.name}),
            mock.patch("time.time", return_value=self.NOW),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rows(self, response):
        """The table's cells, as for the uptime page, but as text: the local
        times' ``<time>`` elements hold the UTC time until ``local_times.js``
        replaces it."""
        return [
            [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip() for cell in row]
            for row in UptimeReportAdminViewTests._rows(self, response)
        ]

    def _record(self):
        log = uptime_log.UptimeLog(self.directory)
        midnight = 20_746 * 86_400  # 2026-10-20
        # From 23:30 the day before to 00:30, 09:05:03 for 2 hours, and the
        # last second of the day, which the next day doesn't continue.
        log.record(midnight + 1799, since=midnight - 1800)
        log.record(midnight + 32_703 + 7199, since=midnight + 32_703)
        log.record(midnight + 86_399)
        log.record(self.NOW, since=self.NOW - 59.5)

    def test_url(self):
        self.assertEqual(reverse("admin:uptime_day", args=["2026-10-20"]), self.URL)

    def test_superusers_only(self):
        staff = User.objects.create_user("staff", "s@example.com", "pass")
        staff.is_staff = True
        staff.save()
        client = Client()
        client.login(username="staff", password="pass")
        self.assertEqual(client.get(self.URL).status_code, 403)
        self.assertRedirects(
            Client().get(self.URL),
            f"{reverse('admin:login')}?next={self.URL}",
            fetch_redirect_response=False,
        )

    def test_report_links_starts(self):
        self._record()

        response = self.client.get("/admin/uptime/", {"days": "5"})

        self.assertContains(
            response, '<a href="/admin/uptime/2026-10-20/?days=5">3</a>', html=True
        )
        self.assertContains(
            response, '<a href="/admin/uptime/2026-10-21/?days=5">0</a>', html=True
        )
        # No data, so no link.
        self.assertContains(response, "No data")
        self.assertNotContains(response, "/admin/uptime/2026-10-18/")

    def test_page_lists_periods(self):
        self._record()

        response = self.client.get(self.URL, {"days": "4"})

        self.assertEqual(
            self._rows(response),
            [
                [
                    "00:00:00 (2026-10-20T00:00:00Z local)",
                    "00:29:59 (2026-10-20T00:29:59Z local)",
                    "00:30:00",
                    "Began the day before.",
                ],
                [
                    "09:05:03 (2026-10-20T09:05:03Z local)",
                    "11:05:02 (2026-10-20T11:05:02Z local)",
                    "02:00:00",
                    "",
                ],
                [
                    "23:59:59 (2026-10-20T23:59:59Z local)",
                    "23:59:59 (2026-10-20T23:59:59Z local)",
                    "00:00:01",
                    "",
                ],
            ],
        )
        self.assertContains(response, "3 periods, 02:30:01 in all")
        self.assertContains(response, "<title>Uptime on 2026-10-20")
        # Links back to the report, and to the days either side, keep its days.
        self.assertContains(response, 'href="/admin/uptime/?days=4"')
        self.assertContains(response, 'href="/admin/uptime/2026-10-19/?days=4"')
        self.assertContains(response, 'href="/admin/uptime/2026-10-21/?days=4"')

    def test_today(self):
        self._record()

        response = self.client.get("/admin/uptime/2026-10-22/")

        self.assertEqual(
            self._rows(response),
            [
                [
                    "11:59:00 (2026-10-22T11:59:00Z local)",
                    "11:59:59 (2026-10-22T11:59:59Z local)",
                    "00:01:00",
                    "Still up.",
                ]
            ],
        )
        self.assertContains(response, "so far")
        # No next day yet, and no days parameter to keep.
        self.assertNotContains(response, "2026-10-23")
        self.assertContains(response, 'href="/admin/uptime/"')

    def test_continues_into_next_day(self):
        uptime_log.UptimeLog(self.directory).record(
            20_747 * 86_400 + 10, since=20_747 * 86_400 - 10
        )

        response = self.client.get("/admin/uptime/2026-10-20/")

        self.assertEqual(
            self._rows(response),
            [
                [
                    "23:59:50 (2026-10-20T23:59:50Z local)",
                    "23:59:59 (2026-10-20T23:59:59Z local)",
                    "00:00:10",
                    "Continued into the next day.",
                ]
            ],
        )

    def test_local_times(self):
        self._record()

        response = self.client.get(self.URL)

        # local_times.js shows them in the browser's time zone, and unhides
        # them and the day's local range, hidden without it.
        self.assertContains(response, "/static/core/local_times.js")
        self.assertContains(
            response,
            '<span data-lt-local-only hidden>(<time data-lt-local="weekday"'
            ' datetime="2026-10-20T09:05:03Z">2026-10-20T09:05:03Z</time> local)'
            "</span>",
            html=True,
        )
        self.assertContains(response, "data-lt-local-only hidden", count=7)
        self.assertContains(
            response,
            '<time data-lt-local datetime="2026-10-20T00:00:00Z">'
            '2026-10-20T00:00:00Z</time> to <time data-lt-local datetime="'
            '2026-10-20T23:59:59Z">2026-10-20T23:59:59Z</time>',
        )

    def test_first_possible_date_local_range(self):
        uptime_log.UptimeLog(self.directory).record(self.NOW)

        response = self.client.get("/admin/uptime/0001-01-01/")

        self.assertEqual(response.context["day_start"], "0001-01-01T00:00:00Z")
        self.assertEqual(response.context["day_end"], "0001-01-01T23:59:59Z")

    def test_no_periods_and_no_data(self):
        uptime_log.UptimeLog(self.directory).record(20_746 * 86_400)

        empty = self.client.get("/admin/uptime/2026-10-21/")
        before = self.client.get("/admin/uptime/2026-10-19/")

        self.assertContains(empty, "The app wasn't up on 2026-10-21.")
        self.assertContains(before, "No data: 2026-10-19 is before the first file")
        self.assertNotContains(before, "<tbody>")

    def test_bad_and_future_dates(self):
        for date in ("2026-10-23", "2026-13-01", "yesterday"):
            with self.subTest(date=date):
                self.assertEqual(
                    self.client.get(f"/admin/uptime/{date}/").status_code, 404
                )

    def test_first_possible_date(self):
        response = self.client.get("/admin/uptime/0001-01-01/")

        self.assertContains(response, "No data: 0001-01-01")
        self.assertContains(response, 'href="/admin/uptime/0001-01-02/"')
        self.assertNotContains(response, "←")

    def test_bad_days_dropped(self):
        response = self.client.get(self.URL, {"days": "0"})

        self.assertContains(response, 'href="/admin/uptime/"')
        self.assertNotContains(response, "days=")

    def test_not_recorded(self):
        with mock.patch.dict(os.environ, {"LIFETRACKER_UPTIME_DIR": "off"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "Uptime isn't recorded")


def _watchdog_status(now, **overrides):
    """A status file's contents, as ``Watchdog.status`` writes them, with times
    relative to ``now`` (a UTC datetime)."""

    def iso(seconds_ago):
        return (now - datetime.timedelta(seconds=seconds_ago)).isoformat()

    status = {
        "mode": "monitor",
        "state": "active",
        "pid": 42,
        "master_pid": 41,
        "started_at": iso(3600),
        "checked_at": iso(20),
        "check_interval_seconds": 30,
        "idle_timeout_seconds": 600,
        "last_request_at": iso(300),
        "idle_since": iso(20),
        "stop_due_at": iso(20 - 600),
        "ports": [
            {"port": 8000, "connections": 0, "last_connection_at": None},
            {"port": 22, "connections": 1, "last_connection_at": iso(20)},
        ],
    }
    status.update(overrides)
    return status


class IdleWatchdogReadTests(SimpleTestCase):
    def test_read_status(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "status.json"
            self.assertIsNone(idle_watchdog.read_status(path))

            idle_watchdog.write_status(path, {"state": "idle"})
            self.assertEqual(idle_watchdog.read_status(path), {"state": "idle"})

            path.write_text("[1, 2]")
            with self.assertRaisesMessage(ValueError, "doesn't hold a JSON object"):
                idle_watchdog.read_status(path)

            path.write_text("{")
            with self.assertRaises(ValueError):
                idle_watchdog.read_status(path)

    def test_system_uptime(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "uptime"
            self.assertIsNone(idle_watchdog.system_uptime(path))

            path.write_text("12345.67 54321.00\n")
            self.assertEqual(idle_watchdog.system_uptime(path), 12345.67)

            path.write_text("")
            self.assertIsNone(idle_watchdog.system_uptime(path))


class IdleWatchdogPageContextTests(SimpleTestCase):
    NOW = datetime.datetime(2027, 1, 15, 8, 0, tzinfo=datetime.UTC)

    def _context(self, status=None, env=None, raw=None, last_request=None):
        with tempfile.TemporaryDirectory() as tempdir:
            status_file = Path(tempdir) / "status.json"
            request_file = Path(tempdir) / "last-request"
            if last_request is not None:
                request_file.touch()
                os.utime(request_file, (last_request, last_request))
            if status is not None:
                idle_watchdog.write_status(status_file, status)
            if raw is not None:
                status_file.write_text(raw)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LIFETRACKER_IDLE_STOP": "monitor",
                        "LIFETRACKER_IDLE_STOP_STATUS_FILE": str(status_file),
                        "LIFETRACKER_IDLE_STOP_REQUEST_FILE": str(request_file),
                        **(env or {}),
                    },
                ),
                mock.patch.object(idle_watchdog, "system_uptime", return_value=90),
            ):
                return idle_watchdog_page_context(now=self.NOW)

    def test_format_duration(self):
        for seconds, expected in [
            (-5, "0 seconds"),
            (1, "1 second"),
            (59.9, "59 seconds"),
            (60, "1\xa0minute"),
            (3 * 3600 + 5 * 60 + 7, "3\xa0hours, 5\xa0minutes"),
        ]:
            with self.subTest(seconds=seconds):
                self.assertEqual(_format_duration(seconds), expected)

    def test_status(self):
        # The request file is newer than the status file's copy of its time
        # (as just after a request, before the next check), and wins.
        context = self._context(
            _watchdog_status(self.NOW),
            last_request=(self.NOW - datetime.timedelta(seconds=7)).timestamp(),
        )

        self.assertIsNone(context["config_error"])
        self.assertIsNone(context["status_error"])
        self.assertEqual(context["config"].mode, "monitor")
        self.assertEqual(context["system_uptime"], "1\xa0minute")
        status = context["status"]
        self.assertEqual(status["checked_ago"], "20 seconds")
        self.assertEqual(status["check_interval"], "30 seconds")
        self.assertEqual(status["idle_timeout"], "10\xa0minutes")
        self.assertEqual(status["uptime"], "1\xa0hour")
        self.assertEqual(
            status["last_request_at"], self.NOW - datetime.timedelta(seconds=7)
        )
        self.assertEqual(status["last_request_at_ago"], "7 seconds")
        self.assertEqual(status["idle_since_ago"], "20 seconds")
        self.assertFalse(status["stale"])
        # Not the status file's stop_due_at: this page's request restarts the
        # idle timer.
        self.assertEqual(status["stop_at"], self.NOW + datetime.timedelta(minutes=10))
        self.assertEqual(
            [
                (port["port"], port["connections"], port["last_connection_ago"])
                for port in status["ports"]
            ],
            [(8000, 0, None), (22, 1, "20 seconds")],
        )

    def test_no_request_file(self):
        # The status file's copy of the time is ignored, even without a file.
        status = self._context(_watchdog_status(self.NOW))["status"]
        self.assertIsNone(status["last_request_at"])
        self.assertIsNone(status["last_request_at_ago"])

    def test_stale(self):
        context = self._context(
            _watchdog_status(
                self.NOW,
                checked_at=(self.NOW - datetime.timedelta(minutes=30)).isoformat(),
            )
        )

        self.assertTrue(context["status"]["stale"])

    def test_before_first_check_judged_by_start_time(self):
        for started_seconds_ago, stale in [(10, False), (600, True)]:
            with self.subTest(started_seconds_ago=started_seconds_ago):
                started_at = self.NOW - datetime.timedelta(seconds=started_seconds_ago)
                context = self._context(
                    _watchdog_status(
                        self.NOW, checked_at=None, started_at=started_at.isoformat()
                    )
                )

                self.assertIsNone(context["status"]["checked_ago"])
                self.assertEqual(context["status"]["stale"], stale)

    def test_no_status_file(self):
        context = self._context()

        self.assertIsNone(context["status"])
        self.assertIsNone(context["status_error"])

    def test_invalid_config(self):
        context = self._context(env={"LIFETRACKER_IDLE_STOP": "sometimes"})

        self.assertIn("LIFETRACKER_IDLE_STOP must be one of", context["config_error"])
        self.assertIsNone(context["config"])

    def test_unreadable_status(self):
        context = self._context(raw="not json")

        self.assertIn("Couldn't read", context["status_error"])
        self.assertIsNone(context["status"])

    def test_unexpected_status_contents(self):
        status = _watchdog_status(self.NOW)
        del status["ports"]

        context = self._context(status)

        self.assertIn("Unexpected contents", context["status_error"])
        self.assertIsNone(context["status"])


class IdleWatchdogAdminViewTests(TestCase):
    URL = "/admin/idle-watchdog/"

    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "password"
        )
        self.client.login(username="admin", password="password")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.status_file = Path(tempdir.name) / "status.json"
        env = mock.patch.dict(
            os.environ,
            {
                "LIFETRACKER_IDLE_STOP": "stop",
                "LIFETRACKER_IDLE_STOP_STATUS_FILE": str(self.status_file),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_url(self):
        self.assertEqual(reverse("admin:idle_watchdog"), self.URL)

    def test_top_bar_links_to_page_for_superusers_only(self):
        link = f'<a href="{self.URL}">Idle Watchdog</a>'
        self.assertContains(self.client.get(reverse("admin:index")), link, html=True)

        staff = User.objects.create_user("staff", "s@example.com", "pass")
        staff.is_staff = True
        staff.save()
        client = Client()
        client.login(username="staff", password="pass")
        self.assertNotContains(client.get(reverse("admin:index")), self.URL)
        self.assertEqual(client.get(self.URL).status_code, 403)

    def test_page_requires_login(self):
        response = Client().get(self.URL)

        self.assertRedirects(
            response,
            f"{reverse('admin:login')}?next={self.URL}",
            fetch_redirect_response=False,
        )

    def test_page_shows_status(self):
        idle_watchdog.write_status(
            self.status_file,
            _watchdog_status(timezone.now(), mode="stop", state="idle"),
        )

        response = self.client.get(self.URL)

        self.assertContains(response, "<code>stop</code>")
        self.assertContains(response, "Idle (no connections open)")
        self.assertContains(response, "Stops At")
        self.assertContains(response, "if nothing else happens (10\xa0minutes after")
        self.assertNotContains(response, "monitor mode")
        self.assertContains(response, "gunicorn master PID 41")
        self.assertContains(response, "<code>22</code>")
        self.assertNotContains(response, "has probably stopped running")

    def test_page_stop_time_by_state_and_mode(self):
        for mode, state, label, expected in [
            ("stop", "active", "Stops At", "Not while connections are open"),
            ("stop", "stopping", "Stops At", "Now: the idle timeout was reached"),
            ("monitor", "idle", "Would Stop At", "(monitor mode: never stops)"),
        ]:
            with self.subTest(mode=mode, state=state):
                idle_watchdog.write_status(
                    self.status_file,
                    _watchdog_status(timezone.now(), mode=mode, state=state),
                )

                response = self.client.get(self.URL)

                self.assertContains(response, label)
                self.assertContains(response, expected)

    def test_page_warns_when_stale(self):
        stale = timezone.now() - datetime.timedelta(hours=1)
        idle_watchdog.write_status(
            self.status_file,
            _watchdog_status(stale, state="stopping"),
        )

        response = self.client.get(self.URL)

        self.assertContains(
            response,
            "hasn't checked for longer than 3 check intervals, so it has probably"
            " stopped running",
        )
        self.assertContains(response, "Stopping gunicorn")
        self.assertContains(response, "Unknown: the watchdog doesn")
        self.assertNotContains(response, "Now: the idle timeout was reached")

    def test_page_without_status(self):
        response = self.client.get(self.URL)

        self.assertContains(response, "No status yet: the watchdog writes it")

    def test_page_when_off(self):
        with mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP": "off"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "No status yet: the watchdog is off.")

    def test_page_with_invalid_config(self):
        with mock.patch.dict(os.environ, {"LIFETRACKER_IDLE_STOP_MINUTES": "x"}):
            response = self.client.get(self.URL)

        self.assertContains(response, "settings are invalid")

    def test_page_with_unreadable_status(self):
        self.status_file.write_text("{")

        response = self.client.get(self.URL)

        self.assertContains(response, "Couldn&#x27;t read")


def _jwks_key(path=None):
    """The public key from the JWKS file, as PyJWT's ``PyJWK``."""
    path = path or settings.LIFETRACKER_JWT_JWKS_FILE
    (key,) = jwt.PyJWKSet.from_json(Path(path).read_text(encoding="utf-8")).keys
    return key


def _decode_jwt(token):
    """``token``'s claims, checked as the front proxy is to check them."""
    key = _jwks_key()
    header = jwt.get_unverified_header(token)
    assert header["kid"] == key.key_id
    return jwt.decode(
        token,
        key,
        algorithms=[jwt_auth.JWT_ALGORITHM],
        audience=jwt_auth.JWT_AUDIENCE,
        issuer=jwt_auth.JWT_ISSUER,
    )


class JWTKeyTests(SimpleTestCase):
    """The JWT signing key and the JWKS written for the front proxy."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name) / "config"
        self.private_path = self.dir / "jwt_private_key.pem"
        self.jwks_path = self.dir / "jwt_jwks.json"
        override = override_settings(
            LIFETRACKER_JWT_PRIVATE_KEY_FILE=self.private_path,
            LIFETRACKER_JWT_JWKS_FILE=self.jwks_path,
        )
        override.enable()
        self.addCleanup(override.disable)

    def _forget_cached_key(self):
        jwt_auth._keys.pop((self.private_path, self.jwks_path), None)

    def test_creates_private_key_and_jwks(self):
        key, kid = jwt_auth.signing_key()
        self.assertEqual(self.private_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.jwks_path.stat().st_mode & 0o777, 0o644)
        (jwk,) = json.loads(self.jwks_path.read_text(encoding="utf-8"))["keys"]
        self.assertEqual(
            {name: jwk[name] for name in ("kty", "crv", "kid", "use", "alg")},
            {"kty": "OKP", "crv": "Ed25519", "kid": kid, "use": "sig", "alg": "EdDSA"},
        )
        self.assertNotIn("d", jwk)
        # No temporary files left behind.
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["jwt_jwks.json", "jwt_private_key.pem"],
        )
        token = jwt.encode({"a": 1}, key, algorithm="EdDSA")
        self.assertEqual(
            jwt.decode(token, _jwks_key(self.jwks_path), algorithms=["EdDSA"]),
            {"a": 1},
        )

    def test_kid_is_rfc_7638_thumbprint(self):
        # RFC 8037 appendix A.3's example key and thumbprint.
        jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
        }
        self.assertEqual(
            jwt_auth._jwk_thumbprint(jwk),
            "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k",
        )

    def test_reuses_existing_key_and_rewrites_jwks(self):
        _, kid = jwt_auth.signing_key()
        pem = self.private_path.read_bytes()
        self.jwks_path.unlink()
        self._forget_cached_key()
        _, kid_again = jwt_auth.signing_key()
        self.assertEqual(kid_again, kid)
        self.assertEqual(self.private_path.read_bytes(), pem)
        self.assertEqual(_jwks_key(self.jwks_path).key_id, kid)

    def test_unchanged_jwks_not_rewritten(self):
        # So a read-only config directory works once both files exist, as with
        # `just docker-run`'s /config.
        _, kid = jwt_auth.signing_key()
        self._forget_cached_key()
        with mock.patch.object(
            jwt_auth.tempfile, "mkstemp", side_effect=AssertionError("wrote")
        ):
            self.assertEqual(jwt_auth.signing_key()[1], kid)

    def test_failed_jwks_write_leaves_no_temporary_file(self):
        self.dir.mkdir()
        with (
            mock.patch.object(jwt_auth.os, "replace", side_effect=OSError("full")),
            self.assertRaisesRegex(OSError, "full"),
        ):
            jwt_auth._write_jwks({"kty": "OKP"}, self.jwks_path)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_key_cached_per_process(self):
        first = jwt_auth.signing_key()
        self.private_path.unlink()
        self.assertEqual(jwt_auth.signing_key(), first)
        self.assertFalse(self.private_path.exists())

    def test_create_keeps_key_another_process_wrote_first(self):
        self.dir.mkdir()
        self.private_path.write_bytes(b"theirs")
        self.assertEqual(
            jwt_auth._create_private_key_file(self.private_path), b"theirs"
        )
        self.assertEqual(self.private_path.read_bytes(), b"theirs")
        self.assertEqual(list(self.dir.iterdir()), [self.private_path])

    def test_rejects_key_of_another_type(self):
        self.dir.mkdir()
        rsa_pem = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.private_path.write_bytes(rsa_pem)
        with self.assertRaisesRegex(ImproperlyConfigured, "isn't an Ed25519"):
            jwt_auth.signing_key()

    def test_ensure_jwt_key_command(self):
        out = io.StringIO()
        call_command("ensure_jwt_key", stdout=out)
        self.assertEqual(out.getvalue(), f"JWKS: {self.jwks_path}\n")
        self.assertTrue(self.private_path.exists())
        self.assertEqual(_jwks_key(self.jwks_path).key_id, jwt_auth.signing_key()[1])


class AuthWhitelistTests(SimpleTestCase):
    """The requests that need no login, shared with the front proxy."""

    def test_matches_exact_paths_and_methods(self):
        login = reverse("admin:login")
        logout = reverse("admin:logout")
        for method, path, expected in [
            ("GET", login, True),
            ("HEAD", login, True),
            ("POST", login, True),
            ("PUT", login, False),
            ("GET", login + "x", False),
            ("GET", login.rstrip("/"), False),
            ("POST", logout, True),
            ("GET", logout, False),
            ("GET", reverse("django_google_sso:oauth_start_login"), True),
            ("GET", reverse("django_google_sso:oauth_callback"), True),
            ("POST", reverse("django_google_sso:oauth_callback"), False),
            ("GET", "/static/admin/css/login.css", True),
            ("POST", "/static/admin/css/login.css", False),
            ("GET", reverse("admin:index"), False),
            ("GET", "/healthz/", True),
            ("HEAD", "/healthz/", True),
            ("POST", "/healthz/", False),
            ("GET", "/manifest.webmanifest", True),
            ("HEAD", "/manifest.webmanifest", True),
            ("POST", "/manifest.webmanifest", False),
            ("GET", "/", False),
        ]:
            with self.subTest(method=method, path=path):
                self.assertIs(is_whitelisted(method, path), expected)

    def test_command_prints_json(self):
        out = io.StringIO()
        call_command("auth_whitelist", stdout=out)
        entries = json.loads(out.getvalue())
        self.assertIn(
            {
                "path": "/admin/login/",
                "methods": ["GET", "HEAD", "POST"],
                "prefix": False,
            },
            entries,
        )
        self.assertIn(
            {"path": "/static/", "methods": ["GET", "HEAD"], "prefix": True}, entries
        )
        self.assertEqual(len(entries), len(auth_whitelist()))


class WebManifestTests(TestCase):
    """The unauthenticated web app manifest, and the admin pages' links to it."""

    url = "/manifest.webmanifest"

    @override_settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Notes")
    def test_manifest(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        self.assertIn("max-age=3600", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "id": "/",
                "name": "Notes",
                "short_name": "Notes",
                "scope": "/",
                "start_url": "/admin/",
                "display": "standalone",
                "background_color": "#417690",
                "theme_color": "#417690",
                "icons": [
                    {
                        "src": "/static/core/app_icon.png",
                        "sizes": "512x512",
                        "type": "image/png",
                    }
                ],
            },
        )

    @override_settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Notes")
    def test_logged_in_still_gets_unauthenticated_name(self):
        user = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url).json()["name"], "Notes")

    def test_head_allowed_post_not(self):
        self.assertEqual(self.client.head(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_icon_is_a_static_file(self):
        icon = finders.find("core/app_icon.png")
        self.assertIsNotNone(icon)
        with open(icon, "rb") as f:
            header = f.read(24)
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        # Width and height, from the IHDR chunk.
        self.assertEqual(header[16:24], (512).to_bytes(4, "big") * 2)

    @override_settings(LIFETRACKER_UNAUTHENTICATED_SITE_NAME="Notes")
    def test_admin_pages_link_it(self):
        response = self.client.get(reverse("admin:login"))
        self.assertContains(
            response, '<link rel="manifest" href="/manifest.webmanifest">', html=True
        )
        self.assertContains(
            response,
            '<link rel="apple-touch-icon" href="/static/core/app_icon.png">',
            html=True,
        )
        self.assertContains(
            response,
            '<meta name="apple-mobile-web-app-title" content="Notes">',
            html=True,
        )
        user = User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)
        response = self.client.get(reverse("admin:index"))
        self.assertContains(
            response, '<link rel="manifest" href="/manifest.webmanifest">', html=True
        )
        self.assertContains(
            response,
            '<meta name="apple-mobile-web-app-title" content="Life Tracker">',
            html=True,
        )


class HealthCheckTests(TestCase):
    """The unauthenticated /healthz/ endpoint CI checks after a deploy."""

    url = "/healthz/"

    @override_settings(LIFETRACKER_COMMIT_ID="abc123")
    def test_healthy(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "commit": "abc123"})
        self.assertIn("no-cache", response["Cache-Control"])
        self.assertNotIn("Set-Cookie", response.headers)

    def test_head_allowed_post_not(self):
        self.assertEqual(self.client.head(self.url).status_code, 200)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    @override_settings(LIFETRACKER_COMMIT_ID="abc123")
    def test_unapplied_migrations(self):
        migration = mock.Mock(app_label="core", name="0099_new")
        migration.name = "0099_new"
        with (
            mock.patch.object(
                MigrationExecutor, "migration_plan", return_value=[(migration, False)]
            ),
            self.assertLogs("lifetracker.core.health", "ERROR") as logs,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"status": "error", "problem": "unapplied migrations", "commit": "abc123"},
        )
        self.assertIn("core.0099_new", logs.output[0])

    def test_database_error(self):
        with (
            mock.patch.object(
                MigrationExecutor,
                "__init__",
                side_effect=DatabaseError("disk I/O error"),
            ),
            self.assertLogs("lifetracker.core.health", "ERROR") as logs,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["problem"], "database error")
        self.assertNotIn("disk I/O", response.content.decode())
        self.assertIn("disk I/O error", logs.output[0])

    def test_other_error(self):
        with (
            mock.patch.object(
                MigrationExecutor, "__init__", side_effect=RuntimeError("boom")
            ),
            self.assertLogs("lifetracker.core.health", "ERROR") as logs,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["problem"], "unhealthy")
        self.assertIn("boom", logs.output[0])


class JWTCookieMiddlewareTests(TestCase):
    """Issuing, re-issuing, clearing and checking the JWT cookie."""

    PASSWORD = "correct horse battery staple"

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "", self.PASSWORD)

    def _login(self):
        response = self.client.post(
            reverse("admin:login"),
            {
                "username": "admin",
                "password": self.PASSWORD,
                "next": reverse("admin:index"),
            },
        )
        self.assertRedirects(response, reverse("admin:index"))
        return response

    def _logged_in_user_id(self):
        return self.client.session.get("_auth_user_id")

    def _token(self):
        return self.client.cookies[jwt_auth.JWT_COOKIE_NAME].value

    def _stored(self):
        return self.client.session[jwt_auth.SESSION_KEY]

    def assertCookieDeleted(self, response):
        cookie = response.cookies[jwt_auth.JWT_COOKIE_NAME]
        self.assertEqual(cookie.value, "")
        self.assertEqual(cookie["max-age"], 0)

    def test_login_issues_cookie(self):
        response = self._login()
        cookie = response.cookies[jwt_auth.JWT_COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/")
        # DEBUG is off in tests, but SESSION_COOKIE_SECURE was set from it at
        # import, when it was on.
        self.assertEqual(bool(cookie["secure"]), settings.SESSION_COOKIE_SECURE)
        session = self.client.session
        self.assertEqual(cookie["max-age"], session.get_expiry_age())
        claims = _decode_jwt(cookie.value)
        self.assertEqual(claims["sub"], str(self.user.pk))
        self.assertAlmostEqual(
            claims["exp"], session.get_expiry_date().timestamp(), delta=5
        )
        self.assertAlmostEqual(claims["iat"], time.time(), delta=5)
        self.assertTrue(claims["jti"])
        self.assertEqual(claims["sid"], jwt_auth.session_id_claim(session.session_key))
        self.assertNotIn(session.session_key, cookie.value)
        self.assertEqual(self._stored(), {"sub": str(self.user.pk)})

    @override_settings(SESSION_COOKIE_SECURE=True)
    def test_cookie_secure_like_session_cookie(self):
        response = self._login()
        self.assertTrue(response.cookies[jwt_auth.JWT_COOKIE_NAME]["secure"])

    @override_settings(SESSION_EXPIRE_AT_BROWSER_CLOSE=True)
    def test_browser_session_cookie_when_session_is(self):
        response = self._login()
        self.assertEqual(response.cookies[jwt_auth.JWT_COOKIE_NAME]["max-age"], "")

    def test_session_without_token_gets_one(self):
        # As for a session from before the middleware existed.
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            _decode_jwt(response.cookies[jwt_auth.JWT_COOKIE_NAME].value)["sub"],
            str(self.user.pk),
        )

    def test_not_reissued_while_session_unchanged(self):
        self._login()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)

    def test_reissued_when_session_saved(self):
        self._login()
        first = self._token()
        with override_settings(SESSION_SAVE_EVERY_REQUEST=True):
            response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        second = response.cookies[jwt_auth.JWT_COOKIE_NAME].value
        self.assertNotEqual(second, first)
        self.assertGreaterEqual(_decode_jwt(second)["exp"], _decode_jwt(first)["exp"])
        self.assertEqual(self._stored(), {"sub": str(self.user.pk)})

    def test_reissued_on_password_change(self):
        # update_session_auth_hash() cycles the session key, so it's saved.
        self._login()
        first = self._token()
        response = self.client.post(
            reverse("admin:password_change"),
            {
                "old_password": self.PASSWORD,
                "new_password1": "another horse battery staple",
                "new_password2": "another horse battery staple",
            },
        )
        self.assertRedirects(response, reverse("admin:password_change_done"))
        self.assertNotEqual(self._token(), first)
        self.assertEqual(
            self.client.get(reverse("admin:index")).status_code,
            200,
        )
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    def test_reissued_when_user_differs(self):
        self._login()
        session = self.client.session
        session[jwt_auth.SESSION_KEY] = {**self._stored(), "sub": "someone else"}
        session.save()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)
        self.assertEqual(self._stored(), {"sub": str(self.user.pk)})

    def test_concurrently_issued_tokens_all_accepted(self):
        # Two overlapping requests that both re-issue: whichever token the
        # browser keeps, the session (which records neither) accepts it.
        self._login()
        session = self.client.session
        tokens = [
            jwt_auth.issue_token(
                self.user, session.session_key, session.get_expiry_date()
            )
            for _ in range(2)
        ]
        self.assertNotEqual(*tokens)
        for token in tokens:
            with self.subTest(token=token):
                self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = token
                self.assertEqual(
                    self.client.get(reverse("admin:index")).status_code, 200
                )

    def test_earlier_token_for_session_still_accepted(self):
        self._login()
        first = self._token()
        with override_settings(SESSION_SAVE_EVERY_REQUEST=True):
            self.client.get(reverse("admin:index"))
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = first
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertEqual(self._logged_in_user_id(), str(self.user.pk))

    def test_logout_clears_cookie(self):
        self._login()
        response = self.client.post(reverse("admin:logout"))
        self.assertEqual(response.status_code, 200)
        self.assertCookieDeleted(response)
        self.assertIsNone(self._logged_in_user_id())

    def test_cookie_without_session_cleared(self):
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = "leftover"
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertCookieDeleted(response)

    def test_no_cookie_set_without_session(self):
        response = self.client.get(reverse("admin:login"))
        self.assertNotIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)

    def test_mismatched_or_missing_cookie_logs_out(self):
        for cookie in ("tampered", "", None):
            with self.subTest(cookie=cookie):
                self._login()
                if cookie is None:
                    del self.client.cookies[jwt_auth.JWT_COOKIE_NAME]
                else:
                    self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = cookie
                with self.assertLogs("lifetracker.core.jwt_auth", "WARNING"):
                    response = self.client.get(reverse("admin:index"))
                self.assertRedirects(
                    response,
                    reverse("admin:login") + "?next=" + reverse("admin:index"),
                )
                self.assertIsNone(self._logged_in_user_id())
                if cookie is not None:
                    self.assertCookieDeleted(response)

    def _assert_rejected(self, token, reason):
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = token
        with self.assertLogs("lifetracker.core.jwt_auth", "WARNING") as logs:
            response = self.client.get(reverse("admin:index"))
        self.assertIn(reason, logs.output[0])
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self._logged_in_user_id())

    def test_expired_token_rejected(self):
        self._login()
        token = jwt_auth.issue_token(
            self.user,
            self.client.session.session_key,
            timezone.now() - datetime.timedelta(seconds=1),
        )
        self._assert_rejected(token, "expired")

    def test_token_for_another_user_rejected(self):
        other = User.objects.create_superuser("other", "", self.PASSWORD)
        self._login()
        token = jwt_auth.issue_token(
            other,
            self.client.session.session_key,
            timezone.now() + datetime.timedelta(hours=1),
        )
        self._assert_rejected(token, "another user")

    def test_token_signed_with_another_key_rejected(self):
        self._login()
        claims = _decode_jwt(self._token())
        token = jwt.encode(claims, Ed25519PrivateKey.generate(), algorithm="EdDSA")
        self._assert_rejected(token, "Signature verification failed")

    def test_token_without_sid_rejected(self):
        self._login()
        claims = _decode_jwt(self._token())
        del claims["sid"]
        token = jwt.encode(claims, jwt_auth.signing_key()[0], algorithm="EdDSA")
        self._assert_rejected(token, "sid")

    def test_new_signing_key_logs_out_once(self):
        # Nothing rotates the key; a new one only appears if the old file goes.
        self._login()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        with override_settings(
            LIFETRACKER_JWT_PRIVATE_KEY_FILE=Path(temp_dir.name) / "key.pem",
            LIFETRACKER_JWT_JWKS_FILE=Path(temp_dir.name) / "jwks.json",
        ):
            self._assert_rejected(self._token(), "Signature verification failed")
            self._login()
            self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_token_from_another_session_rejected(self):
        self._login()
        other_token = self._token()
        self.client.logout()
        self._login()
        self._assert_rejected(other_token, "another session")

    def test_whitelisted_paths_need_no_token(self):
        self.assertEqual(self.client.get(reverse("admin:login")).status_code, 200)
        response = self._login()
        self.assertIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)

    def test_mismatched_cookie_logs_out_on_whitelisted_paths_too(self):
        # Otherwise the admin would send it from the login page to the index,
        # which the front proxy would send back to the login page.
        self._login()
        self.client.cookies[jwt_auth.JWT_COOKIE_NAME] = "tampered"
        with self.assertLogs("lifetracker.core.jwt_auth", "WARNING"):
            response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._logged_in_user_id())
        self.assertCookieDeleted(response)

    def test_not_issued_on_server_error(self):
        # SessionMiddleware doesn't save the session after a 500.
        request = RequestFactory().get("/")
        request.session = SessionStore()
        request.user = self.user
        middleware = jwt_auth.JWTCookieMiddleware(lambda r: HttpResponse(status=500))
        response = middleware(request)
        self.assertNotIn(jwt_auth.JWT_COOKIE_NAME, response.cookies)
        self.assertNotIn(jwt_auth.SESSION_KEY, request.session)
