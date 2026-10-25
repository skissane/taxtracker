"""Management command: restore_cloud_backup.

Replaces the whole database with a cloud backup, given its object key (as the
admin's cloud backup list shows it):

1. Downloads the backup to a temporary file and checks it's an intact SQLite
   database with Django's migrations table.
2. Asks for confirmation, unless given ``--no-input``.
3. Uploads a cloud backup of the current database, as ``cloud_backup`` does, so
   the restore can be undone by restoring that.
4. Copies the downloaded database into the live one (``restore_database``,
   which is safe while the web app is running; see its docstring), keeping
   ``CloudBackupStatus`` as it was just before, i.e. the backup from step 3,
   rather than whatever the restored database had recorded.
5. Runs ``migrate``, in case the backup is from before a migration.

Fails (exit status 1) before changing anything if the download, the check or
the backup of the current database fails.
"""

import os
import tempfile

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from lifetracker.core.backup import (
    CLOUD_BACKUP_ERRORS,
    CloudBackupStatusUpdateError,
    check_backup_file,
    download_cloud_backup,
    restore_database,
    run_cloud_backup,
)
from lifetracker.core.models import CLOUD_BACKUP_STATUS_PK, CloudBackupStatus


class Command(BaseCommand):
    help = "Replace the database with a cloud backup, after uploading a cloud backup of the current one."

    def add_arguments(self, parser):
        parser.add_argument(
            "object_key",
            help="The backup's object key, e.g. host.example/db-backup.2026-10-24.01.02.03.sqlite3.",
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            help="Don't ask for confirmation.",
        )

    def handle(self, *args, object_key, interactive, **options):
        with tempfile.TemporaryDirectory(prefix="lifetracker-restore-") as tmp_dir:
            path = os.path.join(tmp_dir, "restore.sqlite3")
            try:
                download_cloud_backup(object_key, path)
            except CLOUD_BACKUP_ERRORS as exc:
                raise CommandError(f"Downloading {object_key} failed: {exc}") from exc
            size = os.path.getsize(path)
            self.stdout.write(f"Downloaded {object_key} ({size:,} bytes).")
            try:
                check_backup_file(path)
            except RuntimeError as exc:
                raise CommandError(f"Not restoring {object_key}: {exc}") from exc

            if interactive and not self._confirm(object_key):
                raise CommandError("Restore cancelled.")

            try:
                current_key = run_cloud_backup()
            except CloudBackupStatusUpdateError as exc:
                # The backup is safely uploaded; only its status row is
                # missing, and restoring would overwrite that anyway.
                current_key = exc.object_key
            except CLOUD_BACKUP_ERRORS as exc:
                raise CommandError(f"Not restoring: backing up the current database failed: {exc}") from exc
            self.stdout.write(f"Backed up the current database as {current_key}.")

            # Nothing else should record a backup in the meantime, as the
            # command is to be run while nobody's using the app.
            status = CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).first()
            try:
                restore_database(path)
            except CLOUD_BACKUP_ERRORS as exc:
                raise CommandError(
                    f"Restoring {object_key} failed, so the database should be unchanged: {exc}"
                ) from exc
        # Printed before anything else can fail.
        self.stdout.write(
            f"Restored {object_key}. To undo, restore {current_key}, which holds the database as it was before."
        )

        with transaction.atomic():
            CloudBackupStatus.objects.filter(pk=CLOUD_BACKUP_STATUS_PK).delete()
            if status is not None:
                status.save(force_insert=True)
        call_command("migrate", interactive=False, stdout=self.stdout)

    def _confirm(self, object_key):
        self.stdout.write(
            f"This replaces the whole database with {object_key}, after uploading "
            "a cloud backup of the current one. Changes made between that backup "
            "and the restore are lost, and anyone logged in may be logged out."
        )
        try:
            answer = input("Type 'yes' to continue: ")
        except EOFError:
            return False
        return answer.strip().lower() == "yes"
