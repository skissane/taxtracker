"""Management command: purge_orphaned_files.

Finds the stored files (``DBStoredFile`` rows) that no attachment refers to any
more, via ``orphaned_stored_file_pks``, and prints how many there are and their
total size. Rows deleted before ``delete_stored_files`` was connected (by a
cascade or a ``QuerySet.delete()``) left these behind. A dry run unless given
``--delete``, which deletes them; run a cloud backup first, and run it while
nothing else is using the database.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Length

from lifetracker.core.attachments import orphaned_stored_file_pks
from lifetracker.core.models import DBStoredFile

# Keeps each pk__in query under SQLite's limit on query parameters.
_BATCH_SIZE = 500


def _batches(pks):
    for start in range(0, len(pks), _BATCH_SIZE):
        yield pks[start : start + _BATCH_SIZE]


class Command(BaseCommand):
    help = "Report stored files no attachment refers to, and delete them with --delete."

    def add_arguments(self, parser):
        parser.add_argument(
            "--delete",
            action="store_true",
            help="Delete the orphaned files (default: only report them).",
        )

    def handle(self, *args, delete=False, **options):
        # One transaction, so the count reported matches what's deleted. It
        # doesn't lock out writers until its first delete, so an upload saved
        # while it runs (file first, then the row referring to it) could lose
        # its file: run it while nothing else is using the database.
        with transaction.atomic():
            pks = orphaned_stored_file_pks()
            size = sum(
                DBStoredFile.objects.filter(pk__in=batch).aggregate(size=Sum(Length("content")))["size"] or 0
                for batch in _batches(pks)
            )
            summary = f"{len(pks)} orphaned stored file(s), {size} bytes"
            if not delete:
                self.stdout.write(f"Found {summary}. Run with --delete to delete.")
                return
            for batch in _batches(pks):
                DBStoredFile.objects.filter(pk__in=batch).delete()
        self.stdout.write(f"Deleted {summary}.")
