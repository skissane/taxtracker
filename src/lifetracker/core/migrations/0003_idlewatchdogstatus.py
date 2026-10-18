# Unmanaged (no table): only gives the idle watchdog's admin page an index entry.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_cloudbackupstatus"),
    ]

    operations = [
        migrations.CreateModel(
            name="IdleWatchdogStatus",
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
            ],
            options={
                "verbose_name": "Idle Watchdog",
                "verbose_name_plural": "Idle Watchdog",
                "managed": False,
                "default_permissions": (),
            },
        ),
    ]
