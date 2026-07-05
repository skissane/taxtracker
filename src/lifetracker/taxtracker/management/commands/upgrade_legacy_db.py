"""One-time, idempotent fixup for databases created under the pre-rename
"tracker" app label.

Before the lifetracker/core split, all tables lived under a single app
label "tracker" (project package taxtracker.tracker). This command
renames the 8 legacy tracker_* tables to their new core_*/taxtracker_*
names, relabels the corresponding django_content_type rows (preserving
their primary keys, so auth_permission and django_admin_log rows -- which
reference content types by FK -- keep working), and replaces the 5
legacy tracker migration records with the two new squashed initial
migrations.

Safe to run unconditionally before `migrate`: it is a no-op on a fresh
database (no legacy tables) and a no-op on an already-upgraded database
(same reason -- no legacy tables). It aborts without making any changes
if it finds a partially-upgraded or otherwise unrecognised state; in that
case, restore db.sqlite3 from a backup and investigate before retrying.

Note: SQLite's ALTER TABLE ... RENAME TO has no equivalent for indexes,
so the renamed tables keep their old tracker_-prefixed index names (e.g.
tracker_item_parent_id_d6e76dee) instead of matching what a fresh install
would generate. This is purely cosmetic -- Django never looks up indexes
by name at runtime -- so it is left as-is rather than trying to recreate
the indexes with hash-matched names.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder

TABLE_RENAMES = {
    "tracker_filetype": "core_filetype",
    "tracker_mimetype": "core_mimetype",
    "tracker_fileextension": "core_fileextension",
    "tracker_dbstoredfile": "core_dbstoredfile",
    "tracker_financialyear": "taxtracker_financialyear",
    "tracker_financialyearstatushistory": "taxtracker_financialyearstatushistory",
    "tracker_item": "taxtracker_item",
    "tracker_attachment": "taxtracker_attachment",
}

CORE_MODELS = ["filetype", "mimetype", "fileextension", "dbstoredfile"]
TAXTRACKER_MODELS = [
    "financialyear",
    "financialyearstatushistory",
    "item",
    "attachment",
]

LEGACY_MIGRATIONS = {
    "0001_initial",
    "0002_filetype_model",
    "0003_dbstoredfile_alter_attachment_file",
    "0004_attachment_date",
    "0005_financialyear_status_financialyearstatushistory",
}


class Command(BaseCommand):
    help = (
        "One-time fixup: rename tables and re-label content types for "
        "databases created under the pre-rename 'tracker' app label. "
        "Idempotent -- safe to run before every `migrate`."
    )

    def handle(self, *args, **options):
        existing_tables = set(connection.introspection.table_names())
        legacy_tables_present = set(TABLE_RENAMES) & existing_tables

        if not legacy_tables_present:
            self.stdout.write("No legacy tracker_* tables found; nothing to do.")
            return

        if legacy_tables_present != set(TABLE_RENAMES):
            missing = sorted(set(TABLE_RENAMES) - legacy_tables_present)
            raise CommandError(
                f"Found some but not all legacy tracker_* tables (missing: "
                f"{missing}). Refusing to guess -- restore db.sqlite3 from a "
                "backup and investigate."
            )

        target_tables_present = sorted(set(TABLE_RENAMES.values()) & existing_tables)
        if target_tables_present:
            raise CommandError(
                "Legacy tracker_* tables are present AND some of the target "
                f"tables already exist ({target_tables_present}). This looks "
                "like a partially-migrated database -- restore db.sqlite3 from "
                "a backup and investigate before retrying."
            )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT app FROM django_migrations "
                "WHERE app IN ('tracker', 'core', 'taxtracker')"
            )
            apps_present = {row[0] for row in cursor.fetchall()}
        if apps_present - {"tracker"}:
            raise CommandError(
                "django_migrations already has rows for 'core' or 'taxtracker' "
                "alongside legacy 'tracker' tables. Refusing to guess -- "
                "restore db.sqlite3 from a backup and investigate."
            )

        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM django_migrations WHERE app = 'tracker'")
            legacy_migration_names = {row[0] for row in cursor.fetchall()}
        if legacy_migration_names != LEGACY_MIGRATIONS:
            raise CommandError(
                "django_migrations rows for app='tracker' do not match the "
                f"expected legacy migration set (found: "
                f"{sorted(legacy_migration_names)}, expected: "
                f"{sorted(LEGACY_MIGRATIONS)}). Refusing to guess -- restore "
                "db.sqlite3 from a backup and investigate."
            )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT app_label FROM django_content_type "
                "WHERE app_label IN ('core', 'taxtracker')"
            )
            if cursor.fetchall():
                raise CommandError(
                    "django_content_type already has rows for 'core' or "
                    "'taxtracker'. Refusing to guess -- restore db.sqlite3 from "
                    "a backup and investigate."
                )

        with transaction.atomic():
            with connection.cursor() as cursor:
                for old_name, new_name in TABLE_RENAMES.items():
                    cursor.execute(
                        f"ALTER TABLE {connection.ops.quote_name(old_name)} "
                        f"RENAME TO {connection.ops.quote_name(new_name)}"
                    )

                # Defensive: SQLite >= 3.25 already updates sqlite_sequence rows
                # as part of ALTER TABLE ... RENAME TO, but guard against older
                # versions/edge cases with a no-op-safe UPDATE.
                cursor.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='sqlite_sequence'"
                )
                if cursor.fetchone():
                    for old_name, new_name in TABLE_RENAMES.items():
                        cursor.execute(
                            "UPDATE sqlite_sequence SET name = %s WHERE name = %s",
                            [new_name, old_name],
                        )

                core_placeholders = ", ".join(["%s"] * len(CORE_MODELS))
                cursor.execute(
                    "UPDATE django_content_type SET app_label = 'core' "
                    f"WHERE app_label = 'tracker' AND model IN ({core_placeholders})",
                    CORE_MODELS,
                )
                taxtracker_placeholders = ", ".join(["%s"] * len(TAXTRACKER_MODELS))
                cursor.execute(
                    "UPDATE django_content_type SET app_label = 'taxtracker' "
                    "WHERE app_label = 'tracker' AND model IN "
                    f"({taxtracker_placeholders})",
                    TAXTRACKER_MODELS,
                )

                cursor.execute(
                    "SELECT COUNT(*) FROM django_content_type "
                    "WHERE app_label = 'tracker'"
                )
                remaining = cursor.fetchone()[0]
                if remaining:
                    raise CommandError(
                        f"{remaining} django_content_type row(s) still have "
                        "app_label='tracker' after relabeling; aborting."
                    )

                cursor.execute("DELETE FROM django_migrations WHERE app = 'tracker'")

                recorder = MigrationRecorder(connection)
                recorder.record_applied("core", "0001_initial")
                recorder.record_applied("taxtracker", "0001_initial")

                cursor.execute("PRAGMA foreign_key_check")
                violations = cursor.fetchall()
                if violations:
                    raise CommandError(
                        f"Foreign key check failed after upgrade: {violations}"
                    )

        self.stdout.write(
            self.style.SUCCESS(
                "Upgraded legacy 'tracker' schema: renamed "
                f"{len(TABLE_RENAMES)} tables, relabelled content types, and "
                "replaced migration history with core/0001_initial and "
                "taxtracker/0001_initial."
            )
        )
