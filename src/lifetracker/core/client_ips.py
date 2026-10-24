"""Record where requests come from: each client IP, when it was first and last
seen, how many of its requests Envoy turned away or had a valid JWT, and its
country and network (ASN) from DB-IP's free Lite databases (issue #201), and
whether it's an iCloud Private Relay egress address.

Envoy sees every request, including the ones it rejects for want of a JWT,
which never reach gunicorn, so it writes one JSON line per request to a file
(``envoy/envoy.yaml``; the ``envoy_config`` command fills in the path from
``config_from_env``). A collector process, which gunicorn's ``when_ready``
hook starts as it does the idle watchdog (``spawn``), reads the new lines every
few seconds and adds them up per IP in a SQLite file of its own (by default
``$LIFETRACKER_DATA_DIR/client_ips.sqlite3``, beside the main database rather
than in it). Once it has read the whole file, and the file has grown past
``TRUNCATE_BYTES``, it empties it; Envoy appends, so carries on at the start.
The admin's Client IPs page (``core.admin.client_ips_view``) reads the file.

The client's IP is Fly Proxy's ``Fly-Client-IP`` header, or Envoy's peer
without one (e.g. under ``just docker-run``). ``./run.sh`` runs no Envoy, so
records nothing.

At startup the collector downloads this month's DB-IP Lite country and ASN
databases into the GeoIP directory if it doesn't have them yet
(``update_geoip``; the ``update_geoip`` command does the same), falling back
to last month's early in a month before the new ones are out, and fills in
any IP it couldn't look up before; it looks up each new IP as it records it.
It also downloads Apple's list of iCloud Private Relay egress ranges, an RFC
8805 geofeed of a few hundred thousand prefixes, into a SQLite file of ranges
in the same directory, unless it hasn't changed (by its ETag), and checks each
IP against it every time it's seen, and any IP not checked yet (e.g. seen
before this existed) once it's there.
Each attempt's outcome, failures' tracebacks included, goes in ``status.json``
in the GeoIP directory, which the Client IPs page shows. DB-IP Lite is CC BY
4.0: anywhere showing its data links to db-ip.com.

Like the idle watchdog, it doesn't import Django.
"""

import collections
import contextlib
import dataclasses
import datetime
import gzip
import ipaddress
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterable, Mapping
from pathlib import Path

import maxminddb
import requests

from lifetracker.core import idle_watchdog

DEFAULT_REQUEST_LOG_NAME = "lifetracker-envoy-requests.jsonl"
DEFAULT_INTERVAL_SECONDS = 5
# Empty the request log once it's been read and is at least this big.
TRUNCATE_BYTES = 1024 * 1024
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
# The most requests kept while recording them fails (see ``_collect``).
MAX_PENDING_REQUESTS = 100_000
# The longest request line and user agent kept (the rest is cut off).
MAX_TEXT_LENGTH = 500
SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_ip (
    ip TEXT PRIMARY KEY,
    -- POSIX times.
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    requests INTEGER NOT NULL,
    -- Turned away by Envoy for want of a valid JWT.
    rejected INTEGER NOT NULL,
    -- With a valid JWT, i.e. from a logged-in browser (or a future API client).
    logged_in INTEGER NOT NULL,
    first_logged_in REAL,
    last_logged_in REAL,
    -- The latest request's method and path, response status and user agent.
    last_request TEXT NOT NULL,
    last_status INTEGER,
    last_user_agent TEXT NOT NULL,
    -- From DB-IP Lite when first looked up; NULL if it couldn't be.
    country TEXT,
    country_name TEXT,
    asn INTEGER,
    as_org TEXT,
    -- 1 if an iCloud Private Relay egress address when last seen, 0 if not,
    -- NULL if not checked yet; and the user's rough location, from Apple's list.
    private_relay INTEGER,
    relay_country TEXT,
    relay_region TEXT,
    relay_city TEXT
)
"""
# Columns added since version 1, which ``connect`` adds to an older table.
_ADDED_COLUMNS = {
    "private_relay": "INTEGER",
    "relay_country": "TEXT",
    "relay_region": "TEXT",
    "relay_city": "TEXT",
}


@dataclasses.dataclass(frozen=True)
class Config:
    # The SQLite file, or None not to record.
    db_path: Path | None
    request_log: Path
    geoip_dir: Path | None
    download: bool = True
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS


def config_from_env(environ: Mapping[str, str] = os.environ) -> Config:
    """The collector's settings.

    - ``LIFETRACKER_CLIENT_IP_DB``: the SQLite file (default
      ``$LIFETRACKER_DATA_DIR/client_ips.sqlite3`` if that's set, as in the
      Docker image, else nothing is recorded); ``off`` turns recording off.
    - ``LIFETRACKER_CLIENT_IP_REQUEST_LOG``: the file Envoy writes (default
      ``lifetracker-envoy-requests.jsonl`` in ``$TMPDIR``, else ``/tmp``).
    - ``LIFETRACKER_GEOIP_DIR``: where the DB-IP databases are (default
      ``geoip`` beside the SQLite file).
    - ``LIFETRACKER_GEOIP_DOWNLOAD``: ``0`` never to download them.
    """
    raw = environ.get("LIFETRACKER_CLIENT_IP_DB", "").strip()
    data_dir = environ.get("LIFETRACKER_DATA_DIR", "").strip()
    if raw.lower() == "off":
        db_path = None
    elif raw:
        db_path = Path(raw)
    elif data_dir:
        db_path = Path(data_dir) / "client_ips.sqlite3"
    else:
        db_path = None
    request_log = Path(
        environ.get("LIFETRACKER_CLIENT_IP_REQUEST_LOG", "").strip()
        or Path(environ.get("TMPDIR", "").strip() or "/tmp") / DEFAULT_REQUEST_LOG_NAME
    )
    raw_geoip = environ.get("LIFETRACKER_GEOIP_DIR", "").strip()
    if raw_geoip:
        geoip_dir = Path(raw_geoip)
    else:
        geoip_dir = db_path.parent / "geoip" if db_path is not None else None
    return Config(
        db_path=db_path,
        request_log=request_log,
        geoip_dir=geoip_dir,
        download=environ.get("LIFETRACKER_GEOIP_DOWNLOAD", "").strip() != "0",
    )


def log(message: str) -> None:
    print(f"client-ips: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Envoy's request log


@dataclasses.dataclass(frozen=True)
class Request:
    ip: str
    time: float
    request: str
    status: int | None
    user_agent: str
    rejected: bool
    logged_in: bool


def _present(value) -> bool:
    return value not in (None, "", "-")


def _ip(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def parse_line(line: str) -> Request | None:
    """One line of Envoy's request log, or None if it isn't one (e.g. cut off,
    or with no usable client IP)."""
    try:
        entry = json.loads(line)
        when = float(entry["time"])
    except ValueError, TypeError, KeyError:
        return None
    # Only a JSON object got this far: indexing anything else with "time"
    # raises TypeError.
    ip = _ip(entry.get("fly_client_ip")) or _ip(entry.get("peer"))
    if ip is None:
        return None
    try:
        status = int(entry.get("status"))
    except ValueError, TypeError:
        status = None
    method = entry.get("method") if _present(entry.get("method")) else "-"
    path = entry.get("path") if _present(entry.get("path")) else "-"
    user_agent = entry.get("user_agent")
    return Request(
        ip=ip,
        time=when,
        request=f"{method} {path}"[:MAX_TEXT_LENGTH],
        status=status,
        user_agent=(str(user_agent) if _present(user_agent) else "")[:MAX_TEXT_LENGTH],
        rejected=_present(entry.get("jwt_failure")),
        logged_in=_present(entry.get("jwt_sub")),
    )


class RequestLogReader:
    """Reads the lines Envoy has added to its request log since last time."""

    def __init__(self, path: Path, truncate_bytes: int = TRUNCATE_BYTES):
        self.path = path
        self.truncate_bytes = truncate_bytes
        self.offset = 0

    def read(self) -> list[str]:
        """The complete lines added since the last call (a line Envoy is part
        way through writing waits for the next)."""
        try:
            with self.path.open("rb") as file:
                size = os.fstat(file.fileno()).st_size
                # Emptied or replaced since: start again.
                if size < self.offset:
                    self.offset = 0
                file.seek(self.offset)
                data = file.read(size - self.offset)
        except FileNotFoundError:
            self.offset = 0
            return []
        end = data.rfind(b"\n") + 1
        self.offset += end
        if self.offset >= self.truncate_bytes and self.offset == size:
            # A line Envoy writes between the read and this is lost, which
            # isn't worth guarding against for a record like this.
            os.truncate(self.path, 0)
            self.offset = 0
        return data[:end].decode("utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------------------
# DB-IP Lite


@dataclasses.dataclass(frozen=True)
class Location:
    country: str | None = None
    country_name: str | None = None
    asn: int | None = None
    as_org: str | None = None

    def __bool__(self):
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
        fd, temp_name = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}."
        )
        try:
            with os.fdopen(fd, "wb") as temp_file:
                with gzip.GzipFile(fileobj=response.raw) as unzipped:
                    shutil.copyfileobj(unzipped, temp_file)
            maxminddb.open_database(temp_name).close()
            os.chmod(temp_name, 0o644)
            os.replace(temp_name, destination)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    return True


def _update_one(
    geoip_dir: Path, name: str, months: tuple[str, str], download, tried: list[str]
) -> dict:
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
    download=_download,
    download_relay_ranges=None,
) -> dict[str, dict]:
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
        status = {"attempted": now, "error": None, "traceback": None}
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


def _update_relay_ranges(geoip_dir: Path, now: float, previous: dict, download):
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


def read_geoip_status(geoip_dir: Path) -> dict[str, dict] | None:
    """Each database's status as ``update_geoip`` last wrote it, or None if it
    hasn't yet. Raises ValueError if the file holds something else, and
    OSError if it can't be read."""
    path = geoip_dir / GEOIP_STATUS_NAME
    status = idle_watchdog.read_status(path)
    if status is None:
        return None
    databases = status.get("databases")
    if not isinstance(databases, dict) or not all(
        isinstance(entry, dict) for entry in databases.values()
    ):
        raise ValueError("it doesn't hold each database's status")
    return databases


class GeoIP:
    """Looks IPs up in the newest DB-IP Lite databases in a directory, opening
    them again when newer ones appear."""

    def __init__(self, geoip_dir: Path | None):
        self.geoip_dir = geoip_dir
        # Each database's name: the file open and its reader.
        self._readers: dict[str, tuple[Path, maxminddb.Reader]] = {}
        self._relay_ranges = RelayRanges(geoip_dir)

    def _reader(self, name: str):
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
            record = reader.get(ip) or {}
            country_record = record.get("country") or {}
            country = country_record.get("iso_code")
            country_name = (country_record.get("names") or {}).get("en")
        reader = self._reader("dbip-asn-lite")
        if reader is not None:
            record = reader.get(ip) or {}
            asn = record.get("autonomous_system_number")
            as_org = record.get("autonomous_system_organization")
        return Location(country, country_name, asn, as_org)

    def relay(self, ip: str) -> Relay:
        return self._relay_ranges.lookup(ip)

    def close(self):
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


def _parse_range(line: str) -> tuple | None:
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
    fd, temp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}."
    )
    os.close(fd)
    try:
        connection = sqlite3.connect(temp_name, isolation_level=None)
        try:
            connection.execute(
                "CREATE TABLE relay_range (start BLOB PRIMARY KEY, end BLOB NOT NULL,"
                " country TEXT, region TEXT, city TEXT) WITHOUT ROWID"
            )
            with _transaction(connection):
                # Streamed, so the whole list is never in memory at once.
                connection.executemany(
                    "INSERT OR REPLACE INTO relay_range VALUES (?, ?, ?, ?, ?)",
                    filter(None, map(_parse_range, lines)),
                )
            count = connection.execute("SELECT count(*) FROM relay_range").fetchone()[0]
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


def _download_relay_ranges(url: str, destination: Path, etag: str | None):
    """Download Apple's list from ``url`` into ``destination`` (see
    ``build_relay_ranges``), unless it's unchanged since the one with ``etag``.
    Returns the new list's ETag ("" if it has none), or None if unchanged."""
    headers = {"If-None-Match": etag} if etag else {}
    with requests.get(
        url, headers=headers, stream=True, timeout=GEOIP_TIMEOUT_SECONDS
    ) as response:
        if response.status_code == 304:
            return None
        response.raise_for_status()
        lines = (
            line.decode("utf-8", errors="replace") for line in response.iter_lines()
        )
        build_relay_ranges(lines, destination)
        return response.headers.get("ETag", "")


class RelayRanges:
    """Looks IPs up in the ranges file in a directory, opening it again when
    it's replaced."""

    def __init__(self, geoip_dir: Path | None):
        self.path = geoip_dir / RELAY_RANGES_FILE if geoip_dir is not None else None
        self._connection = None
        self._stat = None

    def _open(self):
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
        try:
            connection = connect_read_only(self.path)
        except sqlite3.Error:
            return None
        try:
            connection.execute("SELECT 1 FROM relay_range LIMIT 1").fetchall()
        except sqlite3.Error:
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
                "SELECT * FROM relay_range WHERE start <= ?"
                " ORDER BY start DESC LIMIT 1",
                (key,),
            ).fetchone()
        except sqlite3.Error:
            return Relay()
        if row is None or row["end"] < key:
            return Relay(False)
        return Relay(True, row["country"], row["region"], row["city"])

    def close(self):
        if self._connection is not None:
            self._connection.close()
        self._connection = self._stat = None


# ---------------------------------------------------------------------------
# The SQLite file


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if need be) the collector's database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # So the admin page's reads never wait for the collector.
    connection.execute("PRAGMA journal_mode=WAL")
    with _transaction(connection):
        connection.execute(_SCHEMA)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(client_ip)")
        }
        for column, type_ in _ADDED_COLUMNS.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE client_ip ADD COLUMN {column} {type_}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open the collector's database read-only (``sqlite3.OperationalError``
    if it doesn't exist)."""
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30
    )
    connection.row_factory = sqlite3.Row
    return connection


@contextlib.contextmanager
def _transaction(connection: sqlite3.Connection):
    # IMMEDIATE, so a read then a write never fails with "database is locked".
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def record(
    connection: sqlite3.Connection, requests_: Iterable[Request], geoip: GeoIP
) -> int:
    """Add ``requests_`` to the per-IP totals in one transaction; returns how
    many IPs were updated. A new IP, or one without a location yet, is looked
    up in ``geoip``, and every IP is checked against its relay ranges (unless
    there are none, when it keeps what it had)."""
    by_ip: dict[str, list[Request]] = {}
    for request in requests_:
        by_ip.setdefault(request.ip, []).append(request)
    if not by_ip:
        return 0
    with _transaction(connection):
        for ip, ip_requests in by_ip.items():
            ip_requests.sort(key=lambda request: request.time)
            latest = ip_requests[-1]
            logged_in_times = [r.time for r in ip_requests if r.logged_in]
            row = connection.execute(
                "SELECT * FROM client_ip WHERE ip = ?", (ip,)
            ).fetchone()
            row = dict(row) if row is not None else None
            if row is None or (row["country"] is None and row["asn"] is None):
                location = geoip.lookup(ip)
            else:
                location = Location(
                    row["country"], row["country_name"], row["asn"], row["as_org"]
                )
            relay = geoip.relay(ip)
            if relay.private_relay is None and row is not None:
                relay = Relay(*(row[field.name] for field in dataclasses.fields(Relay)))
            new = {
                "ip": ip,
                "first_seen": ip_requests[0].time,
                "last_seen": latest.time,
                "requests": len(ip_requests),
                "rejected": sum(r.rejected for r in ip_requests),
                "logged_in": len(logged_in_times),
                "first_logged_in": min(logged_in_times, default=None),
                "last_logged_in": max(logged_in_times, default=None),
                "last_request": latest.request,
                "last_status": latest.status,
                "last_user_agent": latest.user_agent,
                **dataclasses.asdict(location),
                **dataclasses.asdict(relay),
            }
            if row is not None:
                new = _merge(row, new)
            columns = ", ".join(new)
            placeholders = ", ".join(f":{column}" for column in new)
            connection.execute(
                f"INSERT OR REPLACE INTO client_ip ({columns}) VALUES ({placeholders})",
                new,
            )
    return len(by_ip)


def _min(a, b):
    return b if a is None else a if b is None else min(a, b)


def _max(a, b):
    return b if a is None else a if b is None else max(a, b)


def _merge(old: dict, new: dict) -> dict:
    merged = {
        **new,
        "first_seen": _min(old["first_seen"], new["first_seen"]),
        "last_seen": _max(old["last_seen"], new["last_seen"]),
        "first_logged_in": _min(old["first_logged_in"], new["first_logged_in"]),
        "last_logged_in": _max(old["last_logged_in"], new["last_logged_in"]),
    }
    for column in ("requests", "rejected", "logged_in"):
        merged[column] = old[column] + new[column]
    if old["last_seen"] > new["last_seen"]:
        for column in ("last_request", "last_status", "last_user_agent"):
            merged[column] = old[column]
    return merged


def fill_in_locations(connection: sqlite3.Connection, geoip: GeoIP) -> int:
    """Look up every IP without a location yet (e.g. seen before the databases
    were downloaded); returns how many it found."""
    found = 0
    with _transaction(connection):
        rows = connection.execute(
            "SELECT ip FROM client_ip WHERE country IS NULL AND asn IS NULL"
        ).fetchall()
        for row in rows:
            location = geoip.lookup(row["ip"])
            if location:
                connection.execute(
                    "UPDATE client_ip SET country = ?, country_name = ?, asn = ?,"
                    " as_org = ? WHERE ip = ?",
                    (*dataclasses.astuple(location), row["ip"]),
                )
                found += 1
    return found


def fill_in_relay(connection: sqlite3.Connection, geoip: GeoIP) -> int:
    """Check every IP not checked yet against the relay ranges (e.g. seen
    before there were any, or before this existed); returns how many it
    checked."""
    checked = 0
    with _transaction(connection):
        rows = connection.execute(
            "SELECT ip FROM client_ip WHERE private_relay IS NULL"
        ).fetchall()
        for row in rows:
            relay = geoip.relay(row["ip"])
            if relay.private_relay is None:
                break
            connection.execute(
                "UPDATE client_ip SET private_relay = ?, relay_country = ?,"
                " relay_region = ?, relay_city = ? WHERE ip = ?",
                (*dataclasses.astuple(relay), row["ip"]),
            )
            checked += 1
    return checked


def refresh_geoip(config: Config, connection: sqlite3.Connection, geoip: GeoIP):
    """``update_geoip``, then ``fill_in_locations`` and ``fill_in_relay``; logs
    rather than raises, as nothing would restart the collector if it exited."""
    if config.geoip_dir is None:
        return
    try:
        statuses = update_geoip(config.geoip_dir)
    # It catches each download's exceptions itself, but nothing would restart
    # the collector if anything got through.
    except Exception as exc:
        log(f"couldn't update the DB-IP databases: {exc!r}")
        statuses = {}
    for name, status in statuses.items():
        if status["outcome"] == DOWNLOADED:
            log(f"downloaded {status['file']}")
        elif status["outcome"] == FAILED:
            log(f"couldn't download {name}: {status['error']}")
    try:
        found = fill_in_locations(connection, geoip)
        checked = fill_in_relay(connection, geoip)
    except sqlite3.Error as exc:
        log(f"couldn't look up IPs seen earlier: {exc}")
        return
    if found:
        log(f"looked up {found} IPs seen earlier")
    if checked:
        log(f"checked {checked} IPs seen earlier against the Private Relay ranges")


def recent_ips(
    connection: sqlite3.Connection, *, logged_in_only: bool = False, limit: int
) -> list[sqlite3.Row]:
    """Up to ``limit`` IPs, most recently seen first; only those with a valid
    JWT if ``logged_in_only``."""
    where = "WHERE logged_in > 0" if logged_in_only else ""
    return connection.execute(
        f"SELECT * FROM client_ip {where} ORDER BY last_seen DESC LIMIT ?", (limit,)
    ).fetchall()


def ip_count(connection: sqlite3.Connection, *, logged_in_only: bool = False) -> int:
    where = "WHERE logged_in > 0" if logged_in_only else ""
    return connection.execute(f"SELECT count(*) FROM client_ip {where}").fetchone()[0]


def logged_in_networks(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Each network (ASN) any request with a valid JWT came from, with its
    IPs' count and first and last such request, most recent first."""
    return connection.execute(
        """
        SELECT asn, max(as_org) AS as_org, count(*) AS ips,
            group_concat(DISTINCT country) AS countries,
            sum(logged_in) AS logged_in,
            sum(private_relay) AS relay_ips,
            min(first_logged_in) AS first_logged_in,
            max(last_logged_in) AS last_logged_in
        FROM client_ip WHERE logged_in > 0
        GROUP BY asn ORDER BY max(last_logged_in) DESC
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# The collector process


def run(
    config: Config,
    master_pid: int,
    *,
    sleep=time.sleep,
    getppid=os.getppid,
) -> int:
    """Record Envoy's requests every ``config.interval_seconds`` until
    gunicorn's master has gone; returns the exit status."""
    if config.db_path is None:
        return 0
    connection = connect(config.db_path)
    geoip = GeoIP(config.geoip_dir)
    try:
        log(f"recording client IPs from {config.request_log} in {config.db_path}")
        if config.download:
            refresh_geoip(config, connection, geoip)
        return _collect(config, master_pid, connection, geoip, sleep, getppid)
    finally:
        geoip.close()
        connection.close()


def _collect(config, master_pid, connection, geoip, sleep, getppid) -> int:
    reader = RequestLogReader(config.request_log)
    # Requests read but not yet recorded, as the reader has moved past them
    # (and may have emptied the file): retried until recording works again,
    # keeping only the newest MAX_PENDING_REQUESTS meanwhile.
    pending: collections.deque[Request] = collections.deque(maxlen=MAX_PENDING_REQUESTS)
    failing = False
    while True:
        try:
            pending.extend(
                request for request in map(parse_line, reader.read()) if request
            )
            record(connection, pending, geoip)
            pending.clear()
        except (OSError, sqlite3.Error) as exc:
            # Logged once per failing stretch.
            if not failing:
                log(f"couldn't record client IPs: {exc}")
            failing = True
        else:
            if failing:
                log("recording client IPs again")
            failing = False
        # As the idle watchdog does, exit with the master.
        if getppid() != master_pid:
            log(f"gunicorn master {master_pid} has exited, exiting")
            return 0
        sleep(config.interval_seconds)


def spawn(master_pid: int, environ: Mapping[str, str] = os.environ):
    """Start the collector for gunicorn master ``master_pid``, unless there's
    no database to record in. Returns the ``Popen`` or None."""
    if config_from_env(environ).db_path is None:
        return None
    return subprocess.Popen(
        [sys.executable, "-m", __name__, str(master_pid)], env=dict(environ)
    )


def _exit_quietly(signum, frame):
    sys.exit(0)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].isdigit():
        log("usage: python -m lifetracker.core.client_ips <gunicorn master PID>")
        return 2
    signal.signal(signal.SIGINT, _exit_quietly)
    signal.signal(signal.SIGTERM, _exit_quietly)
    return run(config_from_env(), int(argv[0]))


if __name__ == "__main__":
    sys.exit(main())
