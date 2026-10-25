"""Management command: uptime_report.

Prints, for each of the last few UTC days, what share of it the app was up and
how many times it started, from the files ``uptime_log`` records (see that
module), via ``uptime_log.daily_report``, as the admin's Uptime page does.
"""

import time

from django.core.management.base import BaseCommand, CommandError

from lifetracker.core import uptime_log


class Command(BaseCommand):
    help = "Show what share of each recent UTC day the app was up."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="How many days to show, ending today (default 7).",
        )

    def handle(self, *args, days, **options):
        if days < 1:
            raise CommandError("--days must be at least 1.")
        directory = uptime_log.uptime_dir_from_env()
        if directory is None:
            raise CommandError("Uptime isn't recorded: set LIFETRACKER_UPTIME_DIR or LIFETRACKER_DATA_DIR.")
        for row in uptime_log.daily_report(directory, days, time.time()):
            date = row.date.isoformat()
            if row.up_seconds is None:
                self.stdout.write(f"{date}  no data")
                continue
            self.stdout.write(
                f"{date}  {row.share:7.2%}  up {row.up_seconds} of"
                f" {row.elapsed_seconds} s, {row.starts} starts" + ("  (so far)" if row.today else "")
            )
