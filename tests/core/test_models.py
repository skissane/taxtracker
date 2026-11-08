import datetime
import typing

from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from lifetracker.core.models import (
    CLOUD_BACKUP_STATUS_PK,
    CloudBackupStatus,
    DatabaseStorage,
    DBStoredFile,
    FileExtension,
    FileType,
    MimeType,
)


class FileTypeModelTests(TestCase):
    """Tests for FileType, MimeType, and FileExtension models."""

    def setUp(self) -> None:
        self.ft = FileType.objects.create(short_name="PDF", full_name="PDF Document")

    def test_filetype_str(self) -> None:
        self.assertEqual(str(self.ft), "PDF")

    def test_filetype_short_name_unique(self) -> None:
        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF", full_name="Other PDF")

    def test_filetype_full_name_unique(self) -> None:
        with self.assertRaises(IntegrityError):
            FileType.objects.create(short_name="PDF2", full_name="PDF Document")

    # ------------------------------------------------------------------
    # MimeType tests
    # ------------------------------------------------------------------

    def test_mimetype_str(self) -> None:
        mt = MimeType.objects.create(file_type=self.ft, mime_type="application/pdf", is_primary=True)
        self.assertEqual(str(mt), "application/pdf")

    def test_mimetype_valid_formats(self) -> None:
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

    def test_mimetype_invalid_no_slash(self) -> None:
        mt = MimeType(file_type=self.ft, mime_type="applicationpdf")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_invalid_two_slashes(self) -> None:
        mt = MimeType(file_type=self.ft, mime_type="application/pdf/extra")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_invalid_with_space(self) -> None:
        mt = MimeType(file_type=self.ft, mime_type="application /pdf")
        with self.assertRaises(ValidationError):
            mt.clean()

    def test_mimetype_globally_unique(self) -> None:
        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        MimeType.objects.create(file_type=self.ft, mime_type="application/pdf", is_primary=True)
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(file_type=ft2, mime_type="application/pdf", is_primary=False)

    def test_at_most_one_primary_mime_type_per_file_type(self) -> None:
        MimeType.objects.create(file_type=self.ft, mime_type="application/pdf", is_primary=True)
        with self.assertRaises(IntegrityError):
            MimeType.objects.create(file_type=self.ft, mime_type="application/x-pdf", is_primary=True)

    # ------------------------------------------------------------------
    # FileExtension tests
    # ------------------------------------------------------------------

    def test_extension_str(self) -> None:
        ext = FileExtension.objects.create(file_type=self.ft, extension="pdf", is_primary=True)
        self.assertEqual(str(ext), "pdf")

    def test_extension_forced_lowercase_on_save(self) -> None:
        ext = FileExtension(file_type=self.ft, extension="PDF", is_primary=True)
        ext.save()
        ext.refresh_from_db()
        self.assertEqual(ext.extension, "pdf")

    def test_extension_forced_lowercase_in_clean(self) -> None:
        ext = FileExtension(file_type=self.ft, extension="PDF")
        ext.clean()
        self.assertEqual(ext.extension, "pdf")

    def test_extension_invalid_with_dot(self) -> None:
        ext = FileExtension(file_type=self.ft, extension=".pdf")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_invalid_with_slash(self) -> None:
        ext = FileExtension(file_type=self.ft, extension="pd/f")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_invalid_with_space(self) -> None:
        ext = FileExtension(file_type=self.ft, extension="p df")
        with self.assertRaises(ValidationError):
            ext.clean()

    def test_extension_globally_unique(self) -> None:
        ft2 = FileType.objects.create(short_name="PDF2", full_name="PDF 2")
        FileExtension.objects.create(file_type=self.ft, extension="pdf", is_primary=True)
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(file_type=ft2, extension="pdf", is_primary=False)

    def test_at_most_one_primary_extension_per_file_type(self) -> None:
        FileExtension.objects.create(file_type=self.ft, extension="pdf", is_primary=True)
        with self.assertRaises(IntegrityError):
            FileExtension.objects.create(file_type=self.ft, extension="pdff", is_primary=True)


class DatabaseStorageTests(TestCase):
    """Tests for the DatabaseStorage custom storage backend."""

    def setUp(self) -> None:
        self.storage = DatabaseStorage()

    def test_open_annotations_evaluate(self) -> None:
        # ContentFile[bytes] only works at runtime because settings.py calls django_stubs_ext.monkeypatch().
        self.assertEqual(typing.get_type_hints(DatabaseStorage._open)["return"], ContentFile[bytes])

    def test_dbstoredfile_str(self) -> None:
        obj = DBStoredFile.objects.create(filename="report.pdf", content=b"data")
        self.assertEqual(str(obj), "report.pdf")

    def test_save_and_open_round_trip(self) -> None:
        name = self.storage.save("report.pdf", ContentFile(b"hello world"))
        self.assertTrue(name.startswith("db/"))
        self.assertTrue(name.endswith("/report.pdf"))
        with self.storage.open(name) as f:
            self.assertEqual(f.read(), b"hello world")

    def test_exists_true_for_saved_file(self) -> None:
        name = self.storage.save("a.txt", ContentFile(b"x"))
        self.assertTrue(self.storage.exists(name))

    def test_exists_false_for_missing_pk(self) -> None:
        self.assertFalse(self.storage.exists("db/999999/missing.txt"))

    def test_exists_false_for_malformed_name(self) -> None:
        self.assertFalse(self.storage.exists("not-a-db-path"))

    def test_size(self) -> None:
        name = self.storage.save("sized.txt", ContentFile(b"0123456789"))
        self.assertEqual(self.storage.size(name), 10)

    def test_size_missing_pk_raises(self) -> None:
        with self.assertRaises(DBStoredFile.DoesNotExist):
            self.storage.size("db/999999/missing.txt")

    def test_delete_removes_stored_file(self) -> None:
        name = self.storage.save("deleteme.txt", ContentFile(b"bye"))
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))

    def test_delete_malformed_name_is_a_no_op(self) -> None:
        # Should not raise even though the name can't be parsed into a pk.
        self.storage.delete("not-a-db-path")

    def test_delete_missing_pk_is_a_no_op(self) -> None:
        # Valid path shape but no matching row — filter().delete() is a no-op.
        self.storage.delete("db/999999/missing.txt")

    def test_url(self) -> None:
        name = self.storage.save("linked.txt", ContentFile(b"y"))
        pk = DBStoredFile.objects.get().pk
        self.assertEqual(
            self.storage.url(name),
            reverse("admin:core_dbstoredfile_serve_file", args=[pk]),
        )

    def test_get_available_name_short_name_unchanged(self) -> None:
        self.assertEqual(self.storage.get_available_name("short.txt", max_length=100), "short.txt")

    def test_get_available_name_truncates_long_stem(self) -> None:
        # Room is left for "db/<pk>/" with a 19-digit pk, the most a BigAutoField can have.
        result = self.storage.get_available_name("a" * 90 + ".txt", max_length=100)
        self.assertEqual(result, "a" * 73 + ".txt")
        self.assertEqual(len(f"db/{(1 << 63) - 1}/{result}"), 100)

    def test_get_available_name_truncates_hard_when_suffix_too_long(self) -> None:
        # allowed <= 0: suffix alone exceeds the room left, so we hard-truncate.
        name = "a." + "b" * 100
        result = self.storage.get_available_name(name, max_length=100)
        self.assertEqual(result, name[:77])

    def test_get_available_name_rejects_max_length_without_room_for_prefix(self) -> None:
        with self.assertRaises(SuspiciousFileOperation):
            self.storage.get_available_name("short.txt", max_length=23)

    def test_save_name_fits_max_length(self) -> None:
        name = self.storage.save("a" * 200 + ".pdf", ContentFile(b"x"), max_length=100)
        self.assertLessEqual(len(name), 100)
        self.assertTrue(name.endswith(".pdf"))

    def test_name_to_pk_rejects_non_str(self) -> None:
        with self.assertRaises(ValueError):
            self.storage._name_to_pk(123)

    def test_name_to_pk_rejects_missing_prefix(self) -> None:
        with self.assertRaises(ValueError):
            self.storage._name_to_pk("other/1/file.txt")

    def test_name_to_pk_rejects_non_integer_pk(self) -> None:
        with self.assertRaises(ValueError):
            self.storage._name_to_pk("db/notanumber/file.txt")


class CloudBackupStatusModelTests(TestCase):
    def test_str_never_backed_up(self) -> None:
        self.assertEqual(str(CloudBackupStatus()), "Cloud backup status: never backed up")

    def test_str_with_last_backup(self) -> None:
        backup_time = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        status = CloudBackupStatus(last_backup_at=backup_time)
        self.assertEqual(str(status), f"Cloud backup status: last backup at {backup_time}")

    def test_only_singleton_pk_allowed(self) -> None:
        CloudBackupStatus.objects.create(pk=CLOUD_BACKUP_STATUS_PK)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CloudBackupStatus.objects.create(pk=CLOUD_BACKUP_STATUS_PK + 1)


class CloudBackupStatusMoveMigrationTests(TransactionTestCase):
    """taxtracker's 0004 copies the status row into core's table, and back."""

    before = [("core", "0001_initial"), ("taxtracker", "0003_cloudbackupstatus")]
    after = [
        ("core", "0002_cloudbackupstatus"),
        ("taxtracker", "0004_move_cloudbackupstatus_to_core"),
    ]

    def _migrate(self, targets: list[tuple[str, str]]) -> typing.Any:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self) -> None:
        # Leave the schema fully migrated for the tests that follow.
        call_command("migrate", verbosity=0)

    def test_forwards_and_backwards(self) -> None:
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
        self.assertNotIn("taxtracker_cloudbackupstatus", connection.introspection.table_names())

        apps = self._migrate(self.before)
        self.assertEqual(
            list(apps.get_model("taxtracker", "CloudBackupStatus").objects.values(*fields)),
            [row],
        )
        self.assertNotIn("core_cloudbackupstatus", connection.introspection.table_names())
