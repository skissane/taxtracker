"""Management command: client_ips_export.

Writes the client IP record (see ``core.client_ips``) to stdout as JSON Lines,
one object per IP, e.g. ``./flyssh.sh python manage.py client_ips_export >
client-ips.jsonl``. Times are ISO 8601 UTC, and missing values ``null``.
"""

import json
import sqlite3

from django.core.management.base import BaseCommand, CommandError

from lifetracker.core import client_ips


class Command(BaseCommand):
    help = "Write the client IP record to stdout as JSON Lines."

    def add_arguments(self, parser):
        parser.add_argument(
            "--logged-in",
            action="store_true",
            help="Only IPs with requests that had a valid JWT.",
        )

    def handle(self, *args, **options):
        config = client_ips.config_from_env()
        if config.db_path is None:
            raise CommandError("Client IPs aren't recorded: set LIFETRACKER_CLIENT_IP_DB or LIFETRACKER_DATA_DIR.")
        try:
            connection = client_ips.connect_read_only(config.db_path)
        except sqlite3.OperationalError as exc:
            raise CommandError(f"Couldn't open {config.db_path}: {exc}")
        try:
            for row in client_ips.export_rows(connection, logged_in_only=options["logged_in"]):
                self.stdout.write(json.dumps(row))
        finally:
            connection.close()
