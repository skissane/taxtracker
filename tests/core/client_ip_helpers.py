"""Helpers shared by the client IP and IP location tests."""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from lifetracker.core import ip_location

# A few lines in the shape of Apple's list, from the documentation ranges.
RELAY_LINES = [
    "203.0.113.0/27,AU,AU-NSW,Sydney,",
    "203.0.113.64/31,AU,AU-VIC,Melbourne,",
    "2001:db8:1::/64,NZ,NZ-AUK,Auckland,",
]


def write_relay_ranges(geoip_dir: Path, lines: list[str] = RELAY_LINES) -> int:
    geoip_dir.mkdir(parents=True, exist_ok=True)
    return ip_location.build_relay_ranges(lines, geoip_dir / ip_location.RELAY_RANGES_FILE)


class TempDirMixin(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.temp = Path(tempdir.name)
