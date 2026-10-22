"""Management command: uptime_report.

Prints, for each of the last few UTC days, what share of it the app was up and
how many times it started, from the files ``uptime_log`` records (see that
module). Today's share is of the day so far. A day with no file counts as never
up, unless it's before the first file.
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
            raise CommandError(
                "Uptime isn't recorded: set LIFETRACKER_UPTIME_DIR or"
                " LIFETRACKER_DATA_DIR."
            )
        now = time.time()
        today = uptime_log.day_of(now)
        first_recorded = uptime_log.first_recorded_day(directory)
        for day in range(today - days + 1, today + 1):
            date = uptime_log.day_date(day).isoformat()
            if first_recorded is None or day < first_recorded:
                self.stdout.write(f"{date}  no data")
                continue
            data = uptime_log.read_day(directory, day) or bytes(
                uptime_log.DAY_FILE_SIZE
            )
            up = uptime_log.active_seconds(data)
            elapsed = uptime_log.SECONDS_PER_DAY
            if day == today:
                elapsed = int(now) % uptime_log.SECONDS_PER_DAY + 1
            self.stdout.write(
                f"{date}  {up / elapsed:7.2%}  up {up} of {elapsed} s,"
                f" {uptime_log.start_count(data)} starts"
                + ("  (so far)" if day == today else "")
            )
