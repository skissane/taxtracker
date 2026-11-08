import contextlib
import datetime
import io
import json
import sqlite3
import tempfile
import typing
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.db.utils import ConnectionHandler
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from lifetracker.core.backup import (
    CLOUD_BACKUP_S3_CONFIG,
    backup_database,
    check_backup_file,
    database_changed,
    database_signature,
    restore_database,
    run_cloud_backup,
)
from lifetracker.core.models import CLOUD_BACKUP_STATUS_PK, CloudBackupStatus


@contextlib.contextmanager
def _non_sqlite_database() -> Iterator[None]:
    """Override ``DATABASES`` with a non-SQLite engine.

    Only enabling the override warns, so ``pytest.warns`` covers just that, not the code under test.
    """
    override = override_settings(
        DATABASES={"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "irrelevant"}}
    )
    try:
        with pytest.warns(UserWarning, match="Overriding setting DATABASES"):
            override.enable()
        yield
    finally:
        override.disable()


def _record_uploads(mock_client: mock.MagicMock) -> list[dict[str, typing.Any]]:
    """Make *mock_client*'s ``upload_fileobj`` read each file it's given.

    Returns a list it appends each upload to, as a dict of its arguments with
    the file's content as ``Body``, since the file is closed after the upload.
    """
    uploads: list[dict[str, typing.Any]] = []

    def upload_fileobj(
        fileobj: typing.BinaryIO, bucket: str, key: str, ExtraArgs: dict[str, typing.Any] | None = None
    ) -> None:
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

    def setUp(self) -> None:
        self.user = User.objects.create_superuser("admin", "admin@example.com", "password")
        self.client = Client()
        self.client.login(username="admin", password="password")

    def test_admin_index_links_to_backups_for_superusers(self) -> None:
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, reverse("admin:core_cloudbackupstatus_changelist"))

    def test_admin_index_hides_backups_from_non_superusers(self) -> None:
        non_super = User.objects.create_user("regular0", "r0@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular0", **{"password": "pass"})
        response = client.get(reverse("admin:index"))
        self.assertNotContains(response, reverse("admin:core_cloudbackupstatus_changelist"))

    def test_backups_page_links_to_db_download(self) -> None:
        response = self.client.get(reverse("admin:core_cloudbackupstatus_changelist"))
        self.assertContains(response, reverse("admin:core_cloudbackupstatus_download_db_backup"))

    def test_download_db_backup_view(self) -> None:
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

    def test_download_db_backup_requires_superuser(self) -> None:
        non_super = User.objects.create_user("regular", "r@example.com", "pass")
        non_super.is_staff = True
        non_super.save()
        client = Client()
        client.login(username="regular", password="pass")
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        response = client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_download_db_backup_rejects_non_sqlite_engine(self) -> None:
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        with _non_sqlite_database():
            response = self.client.get(url, follow=True)
        self.assertRedirects(response, reverse("admin:core_cloudbackupstatus_changelist"))
        self.assertContains(response, "DB backup is only available for SQLite databases.")

    def test_download_db_backup_failure_shows_error(self) -> None:
        url = reverse("admin:core_cloudbackupstatus_download_db_backup")
        with mock.patch(
            "lifetracker.core.admin.backup_database",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            response = self.client.get(url, follow=True)
        self.assertRedirects(response, reverse("admin:core_cloudbackupstatus_changelist"))
        self.assertContains(response, "DB backup failed: database or disk is full")

    @override_settings(
        LIFETRACKER_BACKUP_S3_ENDPOINT_URL="https://example.invalid",
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_REGION_NAME="auto",
        LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
    )
    def test_cloud_backup_status_view(self) -> None:
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://example.invalid")
        self.assertContains(response, "test-bucket")
        self.assertContains(response, "auto")
        self.assertContains(response, "<code>test-host.local</code>", html=True)
        self.assertContains(response, "Never")

    def test_cloud_backup_status_view_shows_time_since_last_backup(self) -> None:
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

    def test_cloud_backup_status_requires_superuser(self) -> None:
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
    def test_cloud_backup_status_view_run_backup_updates_status(self, mock_client: mock.MagicMock) -> None:
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
    def test_cloud_backup_post_with_missing_settings_shows_error(self, mock_client: mock.MagicMock) -> None:
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
    def test_run_cloud_backup_rejects_missing_settings(self) -> None:
        with self.assertRaisesMessage(RuntimeError, "Missing: LIFETRACKER_BACKUP_S3_BUCKET_NAME."):
            run_cloud_backup()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    def test_run_cloud_backup_checks_engine_before_settings(self) -> None:
        with _non_sqlite_database(), self.assertRaisesMessage(RuntimeError, "only available for SQLite databases"):
            run_cloud_backup()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_rejects_non_sqlite_engine(self, mock_client: mock.MagicMock) -> None:
        url = reverse("admin:core_cloudbackupstatus_changelist")
        with _non_sqlite_database():
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
    def test_cloud_backup_invalid_endpoint_shows_error(self) -> None:
        # Uses the real boto3.client, which raises ValueError for this endpoint.
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.post(url)
        self.assertContains(
            response,
            "Cloud backup failed: Invalid S3 configuration: Invalid endpoint: not a url",
        )
        self.assertFalse(CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).exists())

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_upload_error_shows_error(self, mock_client: mock.MagicMock) -> None:
        mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.post(url)
        self.assertContains(response, "Cloud backup failed:")
        self.assertContains(response, "AccessDenied")
        self.assertFalse(CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).exists())

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_status_write_failure_shows_warning(self, mock_client: mock.MagicMock) -> None:
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
            f"Cloud backup uploaded successfully as {key}, but failed to record backup status.",
        )

    def test_cloud_backup_status_links_to_list(self) -> None:
        url = reverse("admin:core_cloudbackupstatus_changelist")
        response = self.client.get(url)
        self.assertContains(response, reverse("admin:core_cloudbackupstatus_list"))

    def _mock_bucket_listing(
        self, mock_client: mock.MagicMock, folders: list[str], objects: list[dict[str, typing.Any]]
    ) -> mock.MagicMock:
        """Make the mocked S3 client's paginator return *folders* for the
        delimited top-level listing and *objects* (split across two pages)
        for the prefixed one."""

        def paginate(**kwargs: typing.Any) -> list[dict[str, typing.Any]]:
            if kwargs.get("Delimiter") == "/":
                return [{"CommonPrefixes": [{"Prefix": f} for f in folders]}, {}]
            matching = [o for o in objects if o["Key"].startswith(kwargs["Prefix"])]
            return [{"Contents": matching[:1]}, {"Contents": matching[1:]}]

        paginator: mock.MagicMock = mock_client.return_value.get_paginator.return_value
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
    def test_cloud_backup_list_defaults_to_own_hostname(self, mock_client: mock.MagicMock) -> None:
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
        paginator.paginate.assert_any_call(Bucket="test-bucket", Prefix="test-host.local/")
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
    def test_cloud_backup_list_all_prefixes(self, mock_client: mock.MagicMock) -> None:
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
        self.assertEqual(response.context["folders"], ["other-host/", "test-host.local/"])
        self.assertContains(response, "(this instance)")

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_list_with_missing_settings(self, mock_client: mock.MagicMock) -> None:
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Backups can't be listed until")
        self.assertContains(response, "<code>LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID</code>", html=True)
        self.assertIsNone(response.context["objects"])
        mock_client.assert_not_called()

    @override_settings(
        LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
        LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
        LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    )
    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_cloud_backup_list_s3_error_shows_message(self, mock_client: mock.MagicMock) -> None:
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
    def test_cloud_backup_list_invalid_endpoint_shows_message(self) -> None:
        # Uses the real boto3.client, which raises ValueError for this endpoint.
        url = reverse("admin:core_cloudbackupstatus_list")
        response = self.client.get(url)
        self.assertContains(
            response,
            "Failed to list cloud backups: Invalid S3 configuration: Invalid endpoint: not a url",
        )

    def test_cloud_backup_list_requires_superuser(self) -> None:
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

    def call(self) -> str:
        """Call cloud_backup and return captured stdout."""
        out = io.StringIO()
        call_command("cloud_backup", stdout=out)
        return out.getvalue()

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_uploads_and_records_status(self, mock_client: mock.MagicMock) -> None:
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
    def test_missing_settings_fail(self, mock_client: mock.MagicMock) -> None:
        with self.assertRaisesMessage(
            CommandError,
            "Cloud backup failed: Cloud backup settings are incomplete. Missing: "
            "LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID, "
            "LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY.",
        ):
            self.call()
        mock_client.assert_not_called()

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_upload_error_fails(self, mock_client: mock.MagicMock) -> None:
        mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        with self.assertRaisesMessage(CommandError, "Cloud backup failed: "):
            self.call()
        self.assertFalse(CloudBackupStatus.objects.exists())

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_upload_error_closes_backup_file(self, mock_client: mock.MagicMock) -> None:
        mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        backup_files = []

        def backup_database_spy() -> typing.IO[bytes]:
            backup_files.append(backup_database())
            return backup_files[-1]

        with mock.patch("lifetracker.core.backup.backup_database", side_effect=backup_database_spy):
            with self.assertRaises(CommandError):
                self.call()
        [backup_file] = backup_files
        self.assertTrue(backup_file.closed)

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_backup_copy_error_fails(self, mock_client: mock.MagicMock) -> None:
        with mock.patch(
            "lifetracker.core.backup.backup_database",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            with self.assertRaisesMessage(CommandError, "Cloud backup failed: database or disk is full"):
                self.call()
        mock_client.return_value.upload_fileobj.assert_not_called()
        self.assertFalse(CloudBackupStatus.objects.exists())

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_status_update_error_fails_naming_uploaded_key(self, mock_client: mock.MagicMock) -> None:
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
            f"Cloud backup uploaded successfully as {key}, but failed to record backup status.",
        )
        assert cm.exception.__cause__ is not None
        self.assertIsInstance(cm.exception.__cause__.__cause__, IntegrityError)

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_if_changed_skips_unchanged_database(self, mock_client: mock.MagicMock) -> None:
        out = io.StringIO()
        with mock.patch(
            "lifetracker.core.management.commands.cloud_backup.database_changed",
            return_value=False,
        ):
            call_command("cloud_backup", "--if-changed", stdout=out)
        self.assertEqual(
            out.getvalue(),
            "The database hasn't changed since the last cloud backup; skipping.\n",
        )
        mock_client.assert_not_called()

    @mock.patch("lifetracker.core.backup.boto3.client")
    def test_if_changed_backs_up_changed_database(self, mock_client: mock.MagicMock) -> None:
        uploads = _record_uploads(mock_client)
        out = io.StringIO()
        # The tests' in-memory database always counts as changed.
        call_command("cloud_backup", "--if-changed", stdout=out)
        [upload] = uploads
        self.assertIn(f"uploaded successfully as {upload['Key']}", out.getvalue())


@override_settings(
    LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
    LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
    LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
)
class BackupStateTests(TestCase):
    """database_changed() after run_cloud_backup(), backing up a file-backed
    database (the tests' own is in memory)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "db.sqlite3"
        handler = ConnectionHandler({"default": {**settings.DATABASES["default"], "NAME": self.path}})
        self.addCleanup(handler.close_all)
        self.connection = handler["default"]
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t (v TEXT)")
            cursor.execute("INSERT INTO t VALUES ('old')")
        patcher = mock.patch("lifetracker.core.backup.connections", {"default": self.connection})
        patcher.start()
        self.addCleanup(patcher.stop)
        client_patcher = mock.patch("lifetracker.core.backup.boto3.client")
        self.mock_client = client_patcher.start()
        self.addCleanup(client_patcher.stop)

    def other_connection(self) -> sqlite3.Connection:
        """A connection like another process's (e.g. a web request's)."""
        conn = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def test_changed_without_a_backup(self) -> None:
        self.assertTrue(database_changed())

    def test_unchanged_after_backup(self) -> None:
        run_cloud_backup()

        self.assertFalse(database_changed())
        state = json.loads(settings.LIFETRACKER_BACKUP_STATE_FILE.read_text())
        key = self.mock_client.return_value.upload_fileobj.call_args.args[2]
        self.assertEqual(state["object_key"], key)

    def test_reads_dont_count(self) -> None:
        run_cloud_backup()
        other = self.other_connection()
        other.execute("SELECT * FROM t").fetchall()
        other.close()

        self.assertFalse(database_changed())

    def test_commit_counts_in_log_and_after_checkpoint(self) -> None:
        run_cloud_backup()
        other = self.other_connection()
        other.execute("INSERT INTO t VALUES ('new')")

        self.assertTrue(database_changed())
        # Copied into the database file, and the log emptied.
        other.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertEqual(Path(f"{self.path}-wal").stat().st_size, 0)
        self.assertTrue(database_changed())

    def test_commit_during_backup_records_nothing(self) -> None:
        run_cloud_backup()

        def upload_fileobj(*args: typing.Any, **kwargs: typing.Any) -> None:
            # e.g. a web request, while the upload runs.
            self.other_connection().execute("INSERT INTO t VALUES ('new')")

        self.mock_client.return_value.upload_fileobj.side_effect = upload_fileobj
        run_cloud_backup()

        self.assertFalse(settings.LIFETRACKER_BACKUP_STATE_FILE.exists())
        self.assertTrue(database_changed())

    def test_new_destination_counts(self) -> None:
        run_cloud_backup()

        with override_settings(LIFETRACKER_BACKUP_S3_BUCKET_NAME="other-bucket"):
            self.assertTrue(database_changed())
        with override_settings(LIFETRACKER_BACKUP_HOSTNAME="other-host"):
            self.assertTrue(database_changed())

    def test_unreadable_state_counts_as_changed(self) -> None:
        run_cloud_backup()
        settings.LIFETRACKER_BACKUP_STATE_FILE.write_text("[]")

        self.assertTrue(database_changed())

    def test_state_write_failure_is_logged(self) -> None:
        with (
            override_settings(LIFETRACKER_BACKUP_STATE_FILE=Path("/nonexistent/state.json")),
            self.assertLogs("lifetracker.core.backup", "WARNING") as logs,
        ):
            run_cloud_backup()

        self.assertIn("Couldn't record the backup state", logs.output[0])

    def test_signature(self) -> None:
        # setUp's commits are still in the log.
        self.assertIsNone(database_signature())
        with self.connection.cursor() as cursor:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        signature = database_signature()
        stat = self.path.stat()

        self.assertEqual(signature, [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
        self.other_connection().execute("INSERT INTO t VALUES ('new')")
        self.assertIsNone(database_signature())

    def test_signature_without_log(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        # The last connection to close deletes the log.
        self.connection.close()
        self.assertFalse(Path(f"{self.path}-wal").exists())

        self.assertIsNotNone(database_signature())

    def test_unremovable_state_is_logged(self) -> None:
        # A directory where the state file should be can't be replaced or
        # removed.
        settings.LIFETRACKER_BACKUP_STATE_FILE.mkdir()

        with self.assertLogs("lifetracker.core.backup", "WARNING") as logs:
            run_cloud_backup()

        self.assertIn("Couldn't record the backup state", logs.output[0])
        self.assertTrue(database_changed())


class InMemoryBackupStateTests(SimpleTestCase):
    def test_in_memory_database_has_no_signature(self) -> None:
        self.assertIsNone(database_signature())


class RestoreDatabaseTests(TestCase):
    """restore_database() and check_backup_file(), on file-backed databases."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        handler = ConnectionHandler(
            {
                "default": {
                    **settings.DATABASES["default"],
                    "NAME": self.tmp / "db.sqlite3",
                }
            }
        )
        self.addCleanup(handler.close_all)
        self.connection = handler["default"]
        with self.connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t (v TEXT)")
            cursor.execute("INSERT INTO t VALUES ('old')")
        patcher = mock.patch("lifetracker.core.backup.connections", {"default": self.connection})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_backup(self, *statements: str, name: str = "backup.sqlite3") -> Path:
        path = self.tmp / name
        conn = sqlite3.connect(path)
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        conn.close()
        return path

    def live_rows(self) -> list[tuple[typing.Any, ...]]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT v FROM t")
            rows: list[tuple[typing.Any, ...]] = cursor.fetchall()
            return rows

    def test_restore_replaces_contents_and_keeps_wal(self) -> None:
        path = self.make_backup(
            "CREATE TABLE t (v TEXT)",
            "INSERT INTO t VALUES ('restored')",
            "CREATE TABLE django_migrations (id INTEGER)",
        )
        # Another connection, as a web request would have, holding a read
        # snapshot: it sees the old contents until it ends, then the new.
        reader = sqlite3.connect(self.tmp / "db.sqlite3", isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        self.assertEqual(reader.execute("SELECT v FROM t").fetchall(), [("old",)])
        restore_database(path)
        self.assertEqual(reader.execute("SELECT v FROM t").fetchall(), [("old",)])
        reader.execute("COMMIT")
        self.assertEqual(reader.execute("SELECT v FROM t").fetchall(), [("restored",)])
        self.assertEqual(self.live_rows(), [("restored",)])
        with self.connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode")
            self.assertEqual(cursor.fetchone(), ("wal",))

    def test_restore_refuses_different_page_size(self) -> None:
        path = self.make_backup("PRAGMA page_size = 8192", "CREATE TABLE t (v TEXT)")
        with self.assertRaisesMessage(RuntimeError, "page size (8192 bytes)"):
            restore_database(path)
        self.assertEqual(self.live_rows(), [("old",)])

    def test_restore_refuses_inside_transaction(self) -> None:
        path = self.make_backup("CREATE TABLE t (v TEXT)")
        with mock.patch.object(self.connection, "in_atomic_block", True):
            with self.assertRaisesMessage(RuntimeError, "Can't restore the database inside a transaction."):
                restore_database(path)
        self.assertEqual(self.live_rows(), [("old",)])

    def test_check_accepts_django_database(self) -> None:
        check_backup_file(self.make_backup("CREATE TABLE django_migrations (id)"))

    def test_check_rejects_database_without_migrations(self) -> None:
        with self.assertRaisesMessage(RuntimeError, "The backup has no django_migrations table."):
            check_backup_file(self.make_backup("CREATE TABLE t (v TEXT)"))

    def test_check_rejects_corrupt_database(self) -> None:
        path = self.make_backup(
            "CREATE TABLE django_migrations (id)",
            "CREATE TABLE t (v TEXT)",
            *["INSERT INTO t VALUES ('" + "x" * 100 + "')"] * 500,
        )
        data = bytearray(path.read_bytes())
        # Point page 4's (a leaf of t's) first freeblock past the page's end.
        offset = 4096 * 3 + 1
        data[offset : offset + 2] = b"\x0f\xf0"
        path.write_bytes(data)
        with self.assertRaisesMessage(RuntimeError, "The backup failed SQLite's integrity check: "):
            check_backup_file(path)

    def test_check_rejects_non_database(self) -> None:
        path = self.tmp / "junk.sqlite3"
        path.write_bytes(b"not a database" * 100)
        with self.assertRaisesMessage(RuntimeError, "The backup isn't a valid database"):
            check_backup_file(path)


@override_settings(
    LIFETRACKER_BACKUP_S3_ENDPOINT_URL="https://example.invalid",
    LIFETRACKER_BACKUP_S3_BUCKET_NAME="test-bucket",
    LIFETRACKER_BACKUP_S3_REGION_NAME="auto",
    LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID="access-key",
    LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="secret-key",
    LIFETRACKER_BACKUP_HOSTNAME="test-host.local",
)
class RestoreCloudBackupCommandTests(TransactionTestCase):
    # Not TestCase: backup_database() refuses to run inside a transaction.

    """Tests for the restore_cloud_backup management command.

    ``restore_database`` is replaced, as copying over the in-memory test
    database would destroy it; ``RestoreDatabaseTests`` tests the real one.
    """

    KEY = "other-host/db-backup.2026-10-01.00.00.00.sqlite3"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "backup.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE django_migrations (id INTEGER)")
        conn.commit()
        conn.close()
        self.backup_bytes = path.read_bytes()
        client_patcher = mock.patch("lifetracker.core.backup.boto3.client")
        self.mock_client = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.uploads = _record_uploads(self.mock_client)
        self.downloads: list[tuple[str, str]] = []

        def download_fileobj(bucket: str, key: str, fileobj: typing.BinaryIO) -> None:
            self.downloads.append((bucket, key))
            fileobj.write(self.backup_bytes)

        self.mock_client.return_value.download_fileobj.side_effect = download_fileobj
        self.restored: list[bytes] = []

        def fake_restore(path: str | Path) -> None:
            self.restored.append(Path(path).read_bytes())
            # As restoring would, replace the status row with the backup's.
            CloudBackupStatus.objects.update_or_create(
                pk=CLOUD_BACKUP_STATUS_PK,
                defaults={
                    "last_backup_at": timezone.now(),
                    "last_backup_object_key": "from-the-backup",
                    "last_backup_size": 1,
                },
            )

        restore_patcher = mock.patch(
            "lifetracker.core.management.commands.restore_cloud_backup.restore_database",
            side_effect=fake_restore,
        )
        restore_patcher.start()
        self.addCleanup(restore_patcher.stop)
        migrate_patcher = mock.patch("lifetracker.core.management.commands.restore_cloud_backup.call_command")
        self.mock_migrate = migrate_patcher.start()
        self.addCleanup(migrate_patcher.stop)

    def call(self, *args: str) -> str:
        out = io.StringIO()
        call_command("restore_cloud_backup", self.KEY, *args, stdout=out)
        return out.getvalue()

    def test_backs_up_then_restores(self) -> None:
        output = self.call("--no-input")
        self.assertEqual(self.downloads, [("test-bucket", self.KEY)])
        [upload] = self.uploads
        current_key = upload["Key"]
        self.assertRegex(current_key, r"^test-host\.local/db-backup\.")
        self.assertEqual(self.restored, [self.backup_bytes])
        # The status describes the backup just made, not the restored one's.
        status = CloudBackupStatus.objects.get(pk=CLOUD_BACKUP_STATUS_PK)
        self.assertEqual(status.last_backup_object_key, current_key)
        self.mock_migrate.assert_called_once_with("migrate", interactive=False, stdout=mock.ANY)
        self.assertIn(f"Backed up the current database as {current_key}.", output)
        self.assertIn(
            f"Restored {self.KEY}. To undo, restore {current_key}, which holds",
            output,
        )

    def test_confirmed(self) -> None:
        with mock.patch("builtins.input", return_value="yes") as mock_input:
            self.call()
        mock_input.assert_called_once()
        self.assertEqual(len(self.restored), 1)

    def test_cancelled(self) -> None:
        cases: list[dict[str, typing.Any]] = [{"return_value": "no"}, {"side_effect": EOFError}]
        for kwargs in cases:
            with self.subTest(**kwargs):
                with mock.patch("builtins.input", **kwargs):
                    with self.assertRaisesMessage(CommandError, "Restore cancelled."):
                        self.call()
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.restored, [])

    def test_download_error_changes_nothing(self) -> None:
        self.mock_client.return_value.download_fileobj.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        with self.assertRaisesMessage(CommandError, f"Downloading {self.KEY} failed: "):
            self.call("--no-input")
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.restored, [])

    @override_settings(LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY="")
    def test_missing_settings_fail(self) -> None:
        with self.assertRaisesMessage(
            CommandError,
            f"Downloading {self.KEY} failed: Cloud backup settings are incomplete. "
            "Missing: LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY.",
        ):
            self.call("--no-input")
        self.mock_client.assert_not_called()

    def test_invalid_backup_changes_nothing(self) -> None:
        self.backup_bytes = b"not a database" * 100
        with self.assertRaisesMessage(CommandError, f"Not restoring {self.KEY}: The backup isn't a valid"):
            self.call("--no-input")
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.restored, [])

    def test_backup_error_changes_nothing(self) -> None:
        self.mock_client.return_value.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "PutObject",
        )
        with self.assertRaisesMessage(CommandError, "Not restoring: backing up the current database failed: "):
            self.call("--no-input")
        self.assertEqual(self.restored, [])

    def test_restores_when_only_recording_backup_status_fails(self) -> None:
        with (
            mock.patch.object(
                CloudBackupStatus.objects,
                "update_or_create",
                side_effect=IntegrityError("boom"),
            ),
            mock.patch(
                "lifetracker.core.management.commands.restore_cloud_backup.restore_database",
                side_effect=lambda path: self.restored.append(Path(path).read_bytes()),
            ),
        ):
            output = self.call("--no-input")
        [upload] = self.uploads
        self.assertIn(f"Backed up the current database as {upload['Key']}.", output)
        self.assertEqual(self.restored, [self.backup_bytes])

    def test_restore_error_reported(self) -> None:
        with mock.patch(
            "lifetracker.core.management.commands.restore_cloud_backup.restore_database",
            side_effect=RuntimeError("page sizes differ"),
        ):
            with self.assertRaisesMessage(
                CommandError,
                f"Restoring {self.KEY} failed, so the database should be unchanged: page sizes differ",
            ):
                self.call("--no-input")
        self.mock_migrate.assert_not_called()
