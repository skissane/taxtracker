"""Where an IP is, for the client IP record (``core/client_ips.py``): its
country and network (ASN) from DB-IP's free Lite databases, and whether it's an
iCloud Private Relay egress address, from Apple's list of their ranges.

``update_geoip`` (run by the collector at startup, and by the ``update_geoip``
command) downloads this month's DB-IP Lite country and ASN databases into the
GeoIP directory if it doesn't have them yet, falling back to last month's early
in a month before the new ones are out. It also downloads Apple's list, an RFC
8805 geofeed of a few hundred thousand prefixes, into a SQLite file of ranges in
the same directory, unless it hasn't changed (by its ETag). Each attempt's
outcome, failures' tracebacks included, goes in ``status.json`` in the GeoIP
directory, which the Client IPs page shows. ``GeoIP`` looks IPs up in them.
DB-IP Lite is CC BY 4.0: anywhere showing its data links to db-ip.com.

Like the collector, it doesn't import Django.
"""

import dataclasses
import datetime
import gzip
import ipaddress
import os
import shutil
import sqlite3
import tempfile
import time
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import maxminddb
import requests

from lifetracker.core import idle_watchdog

# The DB-IP Lite databases, by the name in their download URLs.
GEOIP_DATABASES = ("dbip-country-lite", "dbip-asn-lite")
GEOIP_URL = "https://download.db-ip.com/free/{name}-{month}.mmdb.gz"
GEOIP_TIMEOUT_SECONDS = 60
# Apple's iCloud Private Relay egress ranges, by its name in ``status.json``,
# and the SQLite file of ranges built from it in the GeoIP directory.
RELAY_RANGES = "icloud-private-relay"
RELAY_RANGES_URL = "https://mask-api.icloud.com/egress-ip-ranges.csv"
RELAY_RANGES_FILE = f"{RELAY_RANGES}.sqlite3"
# Everything ``update_geoip`` downloads, in the order the Client IPs page shows.
GEOIP_SOURCES = (*GEOIP_DATABASES, RELAY_RANGES)
# ``update_geoip``'s record of its last attempt, beside the databases.
GEOIP_STATUS_NAME = "status.json"
# Its outcomes for each database.
DOWNLOADED = "downloaded"
UP_TO_DATE = "up to date"
FAILED = "failed"


# ---------------------------------------------------------------------------
# DB-IP Lite


@dataclasses.dataclass(frozen=True)
class Location:
    country: str | None = None
    country_name: str | None = None
    asn: int | None = None
    as_org: str | None = None

    def __bool__(self) -> bool:
        return any(dataclasses.astuple(self))


@dataclasses.dataclass(frozen=True)
class Relay:
    """Whether an IP is an iCloud Private Relay egress address (None if there
    are no ranges to check against), and if so, the rough location of its
    users that Apple's list gives."""

    private_relay: bool | None = None
    relay_country: str | None = None
    relay_region: str | None = None
    relay_city: str | None = None


def _month(date: datetime.date) -> str:
    return f"{date.year:04d}-{date.month:02d}"


def _months(today: datetime.date) -> tuple[str, str]:
    """This month and last month, as DB-IP names them."""
    return _month(today), _month(today.replace(day=1) - datetime.timedelta(days=1))


def geoip_files(geoip_dir: Path, name: str) -> list[Path]:
    """``name``'s downloaded files in ``geoip_dir``, newest month first."""
    return sorted(geoip_dir.glob(f"{name}-????-??.mmdb"), reverse=True)


def _download(url: str, destination: Path) -> bool:
    """Download and unzip ``url`` to ``destination``; False if there's no such
    file (404). Checks it's a database before putting it in place."""
    with requests.get(url, stream=True, timeout=GEOIP_TIMEOUT_SECONDS) as response:
        if response.status_code == 404:
            return False
        response.raise_for_status()
        fd, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
        try:
            with os.fdopen(fd, "wb") as temp_file, gzip.GzipFile(fileobj=response.raw) as unzipped:
                shutil.copyfileobj(unzipped, temp_file)
            maxminddb.open_database(temp_name).close()
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    return True


def _update_one(
    geoip_dir: Path, name: str, months: tuple[str, str], download: Callable[[str, Path], bool], tried: list[str]
) -> dict[str, Any]:
    """``update_geoip`` for one database: its outcome, and whether it fell back
    to last month's. Each URL is added to ``tried`` before it's downloaded."""
    this_month, last_month = months
    current = geoip_dir / f"{name}-{this_month}.mmdb"
    if current.exists():
        return {"outcome": UP_TO_DATE, "fell_back": False}
    tried.append(GEOIP_URL.format(name=name, month=this_month))
    if download(tried[-1], current):
        return {"outcome": DOWNLOADED, "fell_back": False}
    previous = geoip_dir / f"{name}-{last_month}.mmdb"
    if previous.exists():
        return {"outcome": UP_TO_DATE, "fell_back": True}
    tried.append(GEOIP_URL.format(name=name, month=last_month))
    if not download(tried[-1], previous):
        raise OSError(f"Neither {this_month}'s nor {last_month}'s {name} is available")
    return {"outcome": DOWNLOADED, "fell_back": True}


def update_geoip(
    geoip_dir: Path,
    *,
    now: float | None = None,
    download: Callable[[str, Path], bool] = _download,
    download_relay_ranges: Callable[[str, Path, str | None], str | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Make sure ``geoip_dir`` has this month's DB-IP Lite databases, or last
    month's if this month's aren't out yet, downloading them if not, and
    delete older ones; and download the iCloud Private Relay ranges, unless
    they haven't changed since last time (see ``_download_relay_ranges``).

    Each database is tried even if another fails. Returns each one's status,
    which it also writes to ``status.json`` in ``geoip_dir`` (see
    ``read_geoip_status``): ``attempted`` (POSIX time), ``url`` (the last URL
    tried, or None if it had this month's), ``outcome`` (``DOWNLOADED``,
    ``UP_TO_DATE`` or ``FAILED``), ``fell_back`` (this month's wasn't out, so
    it has last month's), ``file`` (the file it has now, or None),
    ``succeeded`` (the last attempt that didn't fail, kept from the previous
    status file when this one did, or None), and ``error`` and ``traceback``
    (the exception, or None). The relay ranges' (``RELAY_RANGES``) also has
    ``etag``, the ETag of the list it has, or None.

    Raises ``OSError`` only if it can't create ``geoip_dir`` or write the
    status file.
    """
    now = time.time() if now is None else now
    months = _months(datetime.datetime.fromtimestamp(now, datetime.UTC).date())
    geoip_dir.mkdir(parents=True, exist_ok=True)
    try:
        previous = read_geoip_status(geoip_dir) or {}
    except OSError, ValueError:
        previous = {}
    statuses = {}
    for name in GEOIP_DATABASES:
        tried: list[str] = []
        status: dict[str, Any] = {"attempted": now, "error": None, "traceback": None}
        try:
            status.update(_update_one(geoip_dir, name, months, download, tried))
        # Any exception: a cut-off download alone can raise EOFError or
        # zlib.error as well as requests' and maxminddb's own.
        except Exception as exc:
            status.update(
                outcome=FAILED,
                fell_back=False,
                succeeded=previous.get(name, {}).get("succeeded"),
                error=repr(exc),
                traceback=traceback.format_exc(),
            )
        else:
            status["succeeded"] = now
        # Keep only the newest.
        files = geoip_files(geoip_dir, name)
        for old in files[1:]:
            old.unlink(missing_ok=True)
        status["url"] = tried[-1] if tried else None
        status["file"] = files[0].name if files else None
        statuses[name] = status
    statuses[RELAY_RANGES] = _update_relay_ranges(
        geoip_dir,
        now,
        previous.get(RELAY_RANGES, {}),
        download_relay_ranges or _download_relay_ranges,
    )
    idle_watchdog.write_status(geoip_dir / GEOIP_STATUS_NAME, {"databases": statuses})
    return statuses


def _update_relay_ranges(
    geoip_dir: Path, now: float, previous: dict[str, Any], download: Callable[[str, Path, str | None], str | None]
) -> dict[str, Any]:
    """``update_geoip``'s status for the relay ranges."""
    destination = geoip_dir / RELAY_RANGES_FILE
    # Its ETag is only any use while the file it came with is still there.
    etag = previous.get("etag") if destination.exists() else None
    if not isinstance(etag, str):
        etag = None
    status = {
        "attempted": now,
        "url": RELAY_RANGES_URL,
        "fell_back": False,
        "error": None,
        "traceback": None,
    }
    try:
        new_etag = download(RELAY_RANGES_URL, destination, etag)
    except Exception as exc:
        status.update(
            outcome=FAILED,
            succeeded=previous.get("succeeded"),
            error=repr(exc),
            traceback=traceback.format_exc(),
        )
    else:
        if new_etag is None:
            status.update(outcome=UP_TO_DATE, succeeded=now)
        else:
            status.update(outcome=DOWNLOADED, succeeded=now, etag=new_etag or None)
    status.setdefault("etag", etag)
    status["file"] = RELAY_RANGES_FILE if destination.exists() else None
    if status["file"] is None:
        status["etag"] = None
    return status


def read_geoip_status(geoip_dir: Path) -> dict[str, dict[str, Any]] | None:
    """Each database's status as ``update_geoip`` last wrote it, or None if it
    hasn't yet. Raises ValueError if the file holds something else, and
    OSError if it can't be read."""
    path = geoip_dir / GEOIP_STATUS_NAME
    status = idle_watchdog.read_status(path)
    if status is None:
        return None
    databases = status.get("databases")
    if not isinstance(databases, dict) or not all(isinstance(entry, dict) for entry in databases.values()):
        raise ValueError("it doesn't hold each database's status")
    return databases


class GeoIP:
    """Looks IPs up in the newest DB-IP Lite databases in a directory, opening
    them again when newer ones appear."""

    def __init__(self, geoip_dir: Path | None) -> None:
        self.geoip_dir = geoip_dir
        # Each database's name: the file open and its reader.
        self._readers: dict[str, tuple[Path, maxminddb.Reader]] = {}
        self._relay_ranges = RelayRanges(geoip_dir)

    def _reader(self, name: str) -> maxminddb.Reader | None:
        if self.geoip_dir is None:
            return None
        files = geoip_files(self.geoip_dir, name)
        opened = self._readers.get(name)
        if opened is not None and files and opened[0] == files[0]:
            return opened[1]
        if opened is not None:
            opened[1].close()
            del self._readers[name]
        if not files:
            return None
        try:
            reader = maxminddb.open_database(files[0])
        except OSError, ValueError, maxminddb.InvalidDatabaseError:
            return None
        self._readers[name] = (files[0], reader)
        return reader

    def lookup(self, ip: str) -> Location:
        country = country_name = asn = as_org = None
        reader = self._reader("dbip-country-lite")
        if reader is not None:
            record = cast(dict[str, Any], reader.get(ip) or {})
            country_record = record.get("country") or {}
            country = country_record.get("iso_code")
            country_name = (country_record.get("names") or {}).get("en")
        reader = self._reader("dbip-asn-lite")
        if reader is not None:
            record = cast(dict[str, Any], reader.get(ip) or {})
            asn = record.get("autonomous_system_number")
            as_org = record.get("autonomous_system_organization")
        return Location(country, country_name, asn, as_org)

    def relay(self, ip: str) -> Relay:
        return self._relay_ranges.lookup(ip)

    def close(self) -> None:
        for _, reader in self._readers.values():
            reader.close()
        self._readers.clear()
        self._relay_ranges.close()


# ---------------------------------------------------------------------------
# iCloud Private Relay


def _range_key(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bytes:
    """``address`` as 16 bytes that sort as addresses do: IPv4 addresses as
    IPv4-mapped IPv6 ones (``::ffff:a.b.c.d``)."""
    if address.version == 4:
        return b"\0" * 10 + b"\xff\xff" + address.packed
    return address.packed


def _parse_range(line: str) -> tuple[bytes, bytes, str | None, str | None, str | None] | None:
    """One line of Apple's list (``prefix,country,region,city,``) as a row of
    the ranges table, or None if it's blank, a comment or not a prefix."""
    fields = [field.strip() for field in line.split(",")]
    if not fields[0] or fields[0].startswith("#"):
        return None
    try:
        network = ipaddress.ip_network(fields[0], strict=False)
    except ValueError:
        return None
    country, region, city = (fields[1:4] + ["", "", ""])[:3]
    return (
        _range_key(network.network_address),
        _range_key(network.broadcast_address),
        country or None,
        region or None,
        city or None,
    )


def build_relay_ranges(lines: Iterable[str], destination: Path) -> int:
    """Write the ranges in ``lines`` (Apple's list) to a new SQLite file,
    replacing ``destination`` with it; returns how many there were. Lines that
    aren't ranges are skipped; ValueError if none are."""
    fd, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
    os.close(fd)
    try:
        connection = sqlite3.connect(temp_name, isolation_level=None)
        try:
            connection.execute(
                "CREATE TABLE relay_range (start BLOB PRIMARY KEY, end BLOB NOT NULL,"
                " country TEXT, region TEXT, city TEXT) WITHOUT ROWID"
            )
            # One transaction, as row-by-row commits would be slow; on failure,
            # the file is deleted, so there's nothing to roll back.
            connection.execute("BEGIN")
            # Streamed, so the whole list is never in memory at once.
            connection.executemany(
                "INSERT OR REPLACE INTO relay_range VALUES (?, ?, ?, ?, ?)",
                filter(None, map(_parse_range, lines)),
            )
            connection.execute("COMMIT")
            count = int(connection.execute("SELECT count(*) FROM relay_range").fetchone()[0])
        finally:
            connection.close()
        if not count:
            raise ValueError("no ranges in the list")
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, destination)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return count


def _download_relay_ranges(url: str, destination: Path, etag: str | None) -> str | None:
    """Download Apple's list from ``url`` into ``destination`` (see
    ``build_relay_ranges``), unless it's unchanged since the one with ``etag``.
    Returns the new list's ETag ("" if it has none), or None if unchanged."""
    headers = {"If-None-Match": etag} if etag else {}
    with requests.get(url, headers=headers, stream=True, timeout=GEOIP_TIMEOUT_SECONDS) as response:
        if response.status_code == 304:
            return None
        response.raise_for_status()
        lines = (line.decode("utf-8", errors="replace") for line in response.iter_lines())
        build_relay_ranges(lines, destination)
        return response.headers.get("ETag", "")


class RelayRanges:
    """Looks IPs up in the ranges file in a directory, opening it again when
    it's replaced."""

    def __init__(self, geoip_dir: Path | None) -> None:
        self.path = geoip_dir / RELAY_RANGES_FILE if geoip_dir is not None else None
        self._connection: sqlite3.Connection | None = None
        self._stat: tuple[int, int] | None = None

    def _open(self) -> sqlite3.Connection | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            stat = None
        identity = stat and (stat.st_ino, stat.st_mtime_ns)
        if self._connection is not None and identity == self._stat:
            return self._connection
        self.close()
        if identity is None:
            return None
        connection = None
        try:
            connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("SELECT 1 FROM relay_range LIMIT 1").fetchall()
        except sqlite3.Error:
            if connection is not None:
                connection.close()
            return None
        self._connection, self._stat = connection, identity
        return connection

    def lookup(self, ip: str) -> Relay:
        connection = self._open()
        if connection is None:
            return Relay()
        key = _range_key(ipaddress.ip_address(ip))
        try:
            row = connection.execute(
                "SELECT * FROM relay_range WHERE start <= ? ORDER BY start DESC LIMIT 1",
                (key,),
            ).fetchone()
        except sqlite3.Error:
            return Relay()
        if row is None or row["end"] < key:
            return Relay(False)
        return Relay(True, row["country"], row["region"], row["city"])

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = self._stat = None
