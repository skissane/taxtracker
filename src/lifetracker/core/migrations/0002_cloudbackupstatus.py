# Moved from taxtracker (whose migration 0004 copies the existing row across),
# since backups cover the whole database rather than one module.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="CloudBackupStatus",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("last_backup_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_backup_object_key",
                    models.CharField(blank=True, max_length=1024),
                ),
                (
                    "last_backup_size",
                    models.PositiveBigIntegerField(
                        blank=True,
                        help_text="Size of the last backup, in bytes.",
                        null=True,
                    ),
                ),
            ],
            options={
                "verbose_name": "Cloud Backup Status",
                "verbose_name_plural": "Backups",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("pk", 1)),
                        name="cloudbackupstatus_singleton",
                    )
                ],
            },
        ),
    ]
