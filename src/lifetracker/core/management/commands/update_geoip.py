"""Management command: update_geoip.

Downloads this month's DB-IP Lite country and ASN databases (or last month's,
if this month's aren't out yet) unless they're already there, then looks up
any recorded client IP without a location yet, as the client IP collector does
at startup (see ``core.client_ips``).
"""

import maxminddb
import requests
from django.core.management.base import BaseCommand, CommandError

from lifetracker.core import client_ips


class Command(BaseCommand):
    help = "Download the DB-IP Lite databases and look up client IPs in them."

    def handle(self, *args, **options):
        config = client_ips.config_from_env()
        if config.db_path is None or config.geoip_dir is None:
            raise CommandError(
                "Client IPs aren't recorded: set LIFETRACKER_CLIENT_IP_DB or"
                " LIFETRACKER_DATA_DIR."
            )
        try:
            downloaded = client_ips.update_geoip(config.geoip_dir)
        except (
            OSError,
            requests.RequestException,
            maxminddb.InvalidDatabaseError,
        ) as exc:
            raise CommandError(f"Couldn't download the DB-IP databases: {exc}")
        for name in downloaded:
            self.stdout.write(f"Downloaded {name}")
        if not downloaded:
            self.stdout.write("Already up to date")
        connection = client_ips.connect(config.db_path)
        geoip = client_ips.GeoIP(config.geoip_dir)
        try:
            found = client_ips.fill_in_locations(connection, geoip)
        finally:
            geoip.close()
            connection.close()
        self.stdout.write(f"Looked up {found} IPs without a location")
