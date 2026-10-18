"""Management command: cloud_backup.

Uploads the database to the cloud backup bucket, as the **Run Backup Now** button
on the admin's Backups page does, via the same ``run_cloud_backup``. Prints the
uploaded object's key on success. Fails (exit status 1) if the settings are
incomplete or the upload fails, and also if the upload succeeded but recording
it in ``CloudBackupStatus`` didn't.
"""

from django.core.management.base import BaseCommand, CommandError

from lifetracker.core.backup import (
    CLOUD_BACKUP_ERRORS,
    CloudBackupStatusUpdateError,
    run_cloud_backup,
)


class Command(BaseCommand):
    help = "Upload a backup of the database to the cloud backup bucket."

    def handle(self, *args, **options):
        try:
            object_key = run_cloud_backup()
        except CloudBackupStatusUpdateError as exc:
            # Chained, so --traceback shows why recording the status failed.
            raise CommandError(
                f"Cloud backup uploaded successfully as {exc.object_key}, "
                "but failed to record backup status."
            ) from exc
        except CLOUD_BACKUP_ERRORS as exc:
            raise CommandError(f"Cloud backup failed: {exc}") from exc
        self.stdout.write(f"Cloud backup uploaded successfully as {object_key}.")
