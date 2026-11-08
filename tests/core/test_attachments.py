import datetime
import io
from typing import override
from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from lifetracker.core.attachments import extract_date_from_filename, file_type_for_filename, orphaned_stored_file_pks
from lifetracker.core.models import DBStoredFile, FileExtension, FileType, database_storage
from lifetracker.tasktracker.models import Task, TaskAttachment
from lifetracker.taxtracker.models import Attachment, FinancialYear, Item


class ExtractDateFromFilenameTests(TestCase):
    def test_extract_iso_date(self) -> None:
        self.assertEqual(
            extract_date_from_filename("statement-2026-01-15.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_compact_date_no_separator(self) -> None:
        self.assertEqual(
            extract_date_from_filename("report20260115.pdf"),
            datetime.date(2026, 1, 15),
        )

    def test_extract_date_followed_by_digits(self) -> None:
        """DD may be followed by digits – the match is still valid."""
        self.assertEqual(
            extract_date_from_filename("20260101123.pdf"),
            datetime.date(2026, 1, 1),
        )

    def test_year_preceded_by_non_digit_allowed(self) -> None:
        """Year preceded by a non-digit character (e.g. '-') is valid."""
        self.assertEqual(
            extract_date_from_filename("123-2026-01-01.pdf"),
            datetime.date(2026, 1, 1),
        )

    def test_year_preceded_by_digit_rejected(self) -> None:
        """Year directly preceded by another digit must not be matched."""
        self.assertIsNone(extract_date_from_filename("1232026-01-01.pdf"))

    def test_invalid_calendar_date_skipped(self) -> None:
        """A pattern that is not a real date (e.g. month 13) must be ignored."""
        self.assertIsNone(extract_date_from_filename("2026-13-45.pdf"))

    def test_returns_first_valid_date(self) -> None:
        """When multiple date patterns exist, the first valid one is returned."""
        self.assertEqual(
            extract_date_from_filename("2026-13-45_2025-06-30.txt"),
            datetime.date(2025, 6, 30),
        )

    def test_no_date_in_filename(self) -> None:
        self.assertIsNone(extract_date_from_filename("nodatehere.pdf"))


class FileTypeForFilenameTests(TestCase):
    @override
    def setUp(self) -> None:
        self.pdf = FileType.objects.create(short_name="PDF", full_name="PDF Document")
        FileExtension.objects.create(file_type=self.pdf, extension="pdf")

    def test_matches_extension_case_insensitively(self) -> None:
        self.assertEqual(file_type_for_filename("Statement.PDF"), self.pdf)

    def test_unknown_extension(self) -> None:
        self.assertIsNone(file_type_for_filename("notes.xyz"))

    def test_no_extension(self) -> None:
        self.assertIsNone(file_type_for_filename("README"))


class PurgeOrphanedFilesTests(TestCase):
    """Tests for orphaned_stored_file_pks and the purge_orphaned_files command.

    They use taxtracker's and tasktracker's attachment models to check that
    file fields are found across apps.
    """

    @override
    def setUp(self) -> None:
        item = Item.objects.create(year=FinancialYear.objects.create(year=2024), title="Item", order=1)
        self.attachment = Attachment.objects.create(item=item, file=ContentFile(b"kept", name="kept.pdf"))
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

    def call(self, *args: str) -> str:
        """Call purge_orphaned_files and return captured stdout."""
        out = io.StringIO()
        call_command("purge_orphaned_files", *args, stdout=out)
        return out.getvalue()

    def referenced_pks(self) -> set[int | None]:
        return {database_storage._name_to_pk(att.file.name) for att in (self.attachment, self.task_attachment)}

    def test_finds_only_unreferenced_files(self) -> None:
        self.assertEqual(orphaned_stored_file_pks(), self.orphan_pks)

    def test_ignores_rows_without_a_file_or_with_an_unparseable_name(self) -> None:
        # Neither refers to a stored file, so neither keeps one.
        Attachment.objects.filter(pk=self.attachment.pk).update(file="")
        TaskAttachment.objects.filter(pk=self.task_attachment.pk).update(file="not-a-db-path.pdf")
        self.assertEqual(
            orphaned_stored_file_pks(),
            sorted(self.orphan_pks + list(self.referenced_pks())),
        )

    def test_dry_run_reports_without_deleting(self) -> None:
        output = self.call()
        self.assertEqual(
            output,
            "Found 2 orphaned stored file(s), 8 bytes. Run with --delete to delete.\n",
        )
        self.assertEqual(DBStoredFile.objects.count(), 4)

    def test_delete_removes_only_orphans(self) -> None:
        output = self.call("--delete")
        self.assertEqual(output, "Deleted 2 orphaned stored file(s), 8 bytes.\n")
        self.assertEqual(
            set(DBStoredFile.objects.values_list("pk", flat=True)),
            self.referenced_pks(),
        )
        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.file.read(), b"kept")

    def test_nothing_orphaned(self) -> None:
        DBStoredFile.objects.filter(pk__in=self.orphan_pks).delete()
        self.assertEqual(self.call("--delete"), "Deleted 0 orphaned stored file(s), 0 bytes.\n")
        self.assertEqual(DBStoredFile.objects.count(), 2)

    @mock.patch("lifetracker.core.management.commands.purge_orphaned_files._BATCH_SIZE", 1)
    def test_deletes_in_batches(self) -> None:
        self.assertEqual(self.call("--delete"), "Deleted 2 orphaned stored file(s), 8 bytes.\n")
        self.assertFalse(DBStoredFile.objects.filter(pk__in=self.orphan_pks).exists())
