"""Management command: update_geoip.

Downloads this month's DB-IP Lite country and ASN databases (or last month's,
if this month's aren't out yet) unless they're already there, and Apple's
iCloud Private Relay egress ranges unless they're unchanged, then looks up any
recorded client IP without a location yet, and checks any not yet checked
against the relay ranges, as the client IP collector does at startup (see
``core.client_ips``), recording the outcome in the status file the Client IPs
page shows.
"""

from django.core.management.base import BaseCommand, CommandError

from lifetracker.core import client_ips


class Command(BaseCommand):
    help = (
        "Download the DB-IP Lite databases and iCloud Private Relay ranges, and"
        " look up client IPs in them."
    )

    def handle(self, *args, **options):
        config = client_ips.config_from_env()
        if config.db_path is None or config.geoip_dir is None:
            raise CommandError(
                "Client IPs aren't recorded: set LIFETRACKER_CLIENT_IP_DB or"
                " LIFETRACKER_DATA_DIR."
            )
        try:
            statuses = client_ips.update_geoip(config.geoip_dir)
        except OSError as exc:
            raise CommandError(f"Couldn't update the DB-IP databases: {exc}")
        failed = []
        for name, status in statuses.items():
            if status["outcome"] == client_ips.DOWNLOADED:
                self.stdout.write(f"Downloaded {status['file']}")
            elif status["outcome"] == client_ips.FAILED:
                self.stderr.write(f"Couldn't download {name}: {status['error']}")
                failed.append(name)
        if all(s["outcome"] == client_ips.UP_TO_DATE for s in statuses.values()):
            self.stdout.write("Already up to date")
        connection = client_ips.connect(config.db_path)
        geoip = client_ips.GeoIP(config.geoip_dir)
        try:
            found = client_ips.fill_in_locations(connection, geoip)
            checked = client_ips.fill_in_relay(connection, geoip)
        finally:
            geoip.close()
            connection.close()
        self.stdout.write(f"Looked up {found} IPs without a location")
        self.stdout.write(
            f"Checked {checked} IPs not checked yet against the Private Relay ranges"
        )
        if failed:
            raise CommandError(f"Couldn't download {', '.join(failed)}")
