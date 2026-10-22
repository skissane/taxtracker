"""Record, one bit per second, when the app was running, so it's easy to work
out what share of each day it was up (on Fly.io the Machine stops when idle).

There's one file per UTC day, ``<YYYY-MM-DD>.bin`` in the uptime directory, of
exactly 10,800 bytes: one bit for each of the day's 86,400 seconds (POSIX
seconds, so leap seconds don't exist and every day has that many). Second ``s``
of the day is bit ``7 - s % 8`` of byte ``s // 8``, most significant bit first,
so read as one big-endian integer the day's first second is the top bit, and
``xxd -b`` shows the seconds left to right. A set bit means the app was running
during that second. A day it never ran has no file.

The idle watchdog's process (``idle_watchdog.run``) calls ``UptimeLog.record``
every second. Each call writes the changed byte straight to the file with
``os.pwrite``, without holding the file open in between: once that returns the
byte is in the kernel's page cache, so it survives the process being killed,
and ``sync`` (called every watchdog check) bounds what a crash of the whole
Machine can lose.

Doesn't import Django, as the watchdog doesn't.
"""

import datetime
import os
from collections.abc import Mapping
from pathlib import Path

SECONDS_PER_DAY = 86_400
DAY_FILE_SIZE = SECONDS_PER_DAY // 8
# A gap of up to this many seconds since the previous call (e.g. a throttled
# CPU oversleeping past a whole second) is filled in, as the process was
# evidently running throughout.
MAX_FILLED_GAP_SECONDS = 5
# At startup, the seconds since the system booted are recorded too, so a
# Machine's boot and migrations count as up, unless it has been up longer than
# this: then it isn't a Machine that just booted for the app (e.g. gunicorn was
# restarted, or it's local Docker, whose /proc/uptime is the host's).
MAX_BOOT_BACKFILL_SECONDS = 300
_EPOCH = datetime.date(1970, 1, 1)


def uptime_dir_from_env(environ: Mapping[str, str] = os.environ) -> Path | None:
    """The uptime directory: ``LIFETRACKER_UPTIME_DIR`` if set, else
    ``$LIFETRACKER_DATA_DIR/uptime`` if that's set (as in the Docker image),
    else None (not recorded). ``LIFETRACKER_UPTIME_DIR=off`` turns it off."""
    raw = environ.get("LIFETRACKER_UPTIME_DIR", "").strip()
    if raw.lower() == "off":
        return None
    if raw:
        return Path(raw)
    data_dir = environ.get("LIFETRACKER_DATA_DIR", "").strip()
    return Path(data_dir) / "uptime" if data_dir else None


def day_of(timestamp: float) -> int:
    """The UTC day (days since 1970-01-01) a POSIX timestamp falls on."""
    return int(timestamp // SECONDS_PER_DAY)


def day_date(day: int) -> datetime.date:
    return _EPOCH + datetime.timedelta(days=day)


def day_file(directory: Path, day: int) -> Path:
    return directory / f"{day_date(day).isoformat()}.bin"


def first_recorded_day(directory: Path) -> int | None:
    """The earliest day with a file, or None if there are none."""
    days = []
    for path in directory.glob("*.bin"):
        try:
            days.append((datetime.date.fromisoformat(path.stem) - _EPOCH).days)
        except ValueError:
            continue
    return min(days, default=None)


def read_day(directory: Path, day: int) -> bytes | None:
    """The day's file, or None if there's none (the app never ran that day).
    A short file is padded with zeros."""
    try:
        data = day_file(directory, day).read_bytes()
    except FileNotFoundError:
        return None
    return data[:DAY_FILE_SIZE].ljust(DAY_FILE_SIZE, b"\0")


def active_seconds(data: bytes) -> int:
    """How many seconds a day's file marks as up."""
    return int.from_bytes(data, "big").bit_count()


def start_count(data: bytes) -> int:
    """How many times the app started during the day: the up seconds whose
    previous second was down. Running at midnight counts as a start."""
    bits = int.from_bytes(data, "big")
    # Shifting right moves each second's bit onto the next second's.
    return (bits & ~(bits >> 1)).bit_count()


class UptimeLog:
    """Marks seconds as up in the day files in ``directory``. Only one should
    write to a directory at a time, as it keeps the current day in memory."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.day: int | None = None
        self.data = bytearray(DAY_FILE_SIZE)
        self.last_second: int | None = None
        # The first second a failed call didn't record, for the next to.
        self.unrecorded_from: int | None = None

    def record(self, now: float, *, since: float | None = None) -> None:
        """Mark the second ``now`` falls in as up, along with those since the
        previous call if that was at most ``MAX_FILLED_GAP_SECONDS`` earlier,
        or those since ``since`` if given. Raises OSError if a file can't be
        read or written; the next call then records those seconds too."""
        second = int(now // 1)
        if since is not None:
            first = min(int(since // 1), second)
        elif (
            self.last_second is not None
            and 0 < second - self.last_second <= MAX_FILLED_GAP_SECONDS
        ):
            first = self.last_second + 1
        else:
            first = second
        if self.unrecorded_from is not None:
            first = min(first, self.unrecorded_from)
        self.unrecorded_from = first
        for day in range(day_of(first), day_of(second) + 1):
            day_start = day * SECONDS_PER_DAY
            self._mark(
                day,
                max(first, day_start) - day_start,
                min(second, day_start + SECONDS_PER_DAY - 1) - day_start,
            )
        self.unrecorded_from = None
        self.last_second = second

    def _mark(self, day: int, start: int, end: int) -> None:
        """Mark seconds ``start`` to ``end`` (inclusive) of ``day``."""
        if day != self.day:
            self.data = bytearray(read_day(self.directory, day) or bytes(DAY_FILE_SIZE))
            self.day = day
        for second in range(start, end + 1):
            self.data[second >> 3] |= 0x80 >> (second & 7)
        first_byte, last_byte = start >> 3, end >> 3
        self._write(bytes(self.data[first_byte : last_byte + 1]), first_byte)

    def _write(self, data: bytes, offset: int, *, sync: bool = False) -> None:
        assert self.day is not None
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            day_file(self.directory, self.day), os.O_WRONLY | os.O_CREAT, 0o644
        )
        try:
            if os.fstat(fd).st_size != DAY_FILE_SIZE:
                # A new file: write the whole day, so the earlier bytes are
                # zeros on disk too, not a hole a reader might see as short.
                data, offset = bytes(self.data), 0
            os.pwrite(fd, data, offset)
            if sync:
                os.fsync(fd)
        finally:
            os.close(fd)

    def sync(self) -> None:
        """Flush the current day's file to disk (nothing if none yet)."""
        if self.day is not None:
            self._write(b"", 0, sync=True)
