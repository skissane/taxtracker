"""Management command: auth_whitelist.

Prints ``core.auth_whitelist.auth_whitelist()``, the requests that need no
login, as JSON, for generating the front proxy's config from.
"""

import dataclasses
import json

from django.core.management.base import BaseCommand

from lifetracker.core.auth_whitelist import auth_whitelist


class Command(BaseCommand):
    help = "Print the requests that need no login, as JSON."

    def handle(self, *args, **options):
        entries = [dataclasses.asdict(entry) for entry in auth_whitelist()]
        self.stdout.write(json.dumps(entries, indent=2))
