# CloudBackupStatus moved to core, which creates its own table in its 0002;
# this copies the existing row across, then drops taxtracker's table. The old
# taxtracker.cloudbackupstatus content type is left behind, as Django does for
# any deleted model; `manage.py remove_stale_contenttypes` removes it.

from django.db import migrations

_FIELDS = ("id", "last_backup_at", "last_backup_object_key", "last_backup_size")


def _copy(apps, from_label, to_label):
    source = apps.get_model(from_label, "CloudBackupStatus")
    target = apps.get_model(to_label, "CloudBackupStatus")
    target.objects.bulk_create(
        target(**row) for row in source.objects.values(*_FIELDS)
    )


def copy_to_core(apps, schema_editor):
    _copy(apps, "taxtracker", "core")


def copy_from_core(apps, schema_editor):
    _copy(apps, "core", "taxtracker")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_cloudbackupstatus"),
        ("taxtracker", "0003_cloudbackupstatus"),
    ]

    operations = [
        migrations.RunPython(copy_to_core, copy_from_core),
        migrations.DeleteModel(name="CloudBackupStatus"),
    ]
