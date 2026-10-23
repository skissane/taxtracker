"""Record where requests come from: each client IP, when it was first and last
seen, how many of its requests Envoy turned away or had a valid JWT, and its
country and network (ASN) from DB-IP's free Lite databases (issue #201).

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
any IP it couldn't look up before; it looks up each new IP as it records it. DB-IP
Lite is CC BY 4.0: anywhere showing its data links to db-ip.com.

Like the idle watchdog, it doesn't import Django.
"""

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
from collections.abc import Iterable, Mapping
from pathlib import Path

import maxminddb
import requests

DEFAULT_REQUEST_LOG_NAME = "lifetracker-envoy-requests.jsonl"
DEFAULT_INTERVAL_SECONDS = 5
# Empty the request log once it's been read and is at least this big.
TRUNCATE_BYTES = 1024 * 1024
# The DB-IP Lite databases, by the name in their download URLs.
GEOIP_DATABASES = ("dbip-country-lite", "dbip-asn-lite")
GEOIP_URL = "https://download.db-ip.com/free/{name}-{month}.mmdb.gz"
GEOIP_TIMEOUT_SECONDS = 60
# The longest request line and user agent kept (the rest is cut off).
MAX_TEXT_LENGTH = 500
SCHEMA_VERSION = 1
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
    as_org TEXT
)
"""


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
    if not isinstance(entry, dict):
        return None
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


def update_geoip(
    geoip_dir: Path, *, today: datetime.date | None = None, download=_download
) -> list[str]:
    """Make sure ``geoip_dir`` has this month's DB-IP Lite databases, or last
    month's if this month's aren't out yet, downloading them if not, and
    delete older ones. Returns the names of the files it downloaded.

    Raises ``OSError`` or ``requests.RequestException`` if a download fails
    (other than for want of this month's file) or ``maxminddb``'s error if one
    isn't a database.
    """
    this_month, last_month = _months(
        today or datetime.datetime.now(datetime.UTC).date()
    )
    geoip_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for name in GEOIP_DATABASES:
        current = geoip_dir / f"{name}-{this_month}.mmdb"
        if not current.exists():
            url = GEOIP_URL.format(name=name, month=this_month)
            if download(url, current):
                downloaded.append(current.name)
            else:
                previous = geoip_dir / f"{name}-{last_month}.mmdb"
                if not previous.exists():
                    url = GEOIP_URL.format(name=name, month=last_month)
                    if not download(url, previous):
                        raise OSError(
                            f"Neither {this_month}'s nor {last_month}'s {name}"
                            " is available"
                        )
                    downloaded.append(previous.name)
        # Keep only the newest.
        for old in geoip_files(geoip_dir, name)[1:]:
            old.unlink(missing_ok=True)
    return downloaded


class GeoIP:
    """Looks IPs up in the newest DB-IP Lite databases in a directory, opening
    them again when newer ones appear."""

    def __init__(self, geoip_dir: Path | None):
        self.geoip_dir = geoip_dir
        # Each database's name: the file open and its reader.
        self._readers: dict[str, tuple[Path, maxminddb.Reader]] = {}

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

    def close(self):
        for _, reader in self._readers.values():
            reader.close()
        self._readers.clear()


# ---------------------------------------------------------------------------
# The SQLite file


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if need be) the collector's database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # So the admin page's reads never wait for the collector.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(_SCHEMA)
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
    up in ``geoip``."""
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


def refresh_geoip(config: Config, connection: sqlite3.Connection, geoip: GeoIP):
    """``update_geoip``, then ``fill_in_locations``; logs rather than raises."""
    if config.geoip_dir is None:
        return
    try:
        downloaded = update_geoip(config.geoip_dir)
    except (OSError, requests.RequestException, maxminddb.InvalidDatabaseError) as exc:
        log(f"couldn't download the DB-IP databases: {exc}")
        downloaded = []
    for name in downloaded:
        log(f"downloaded {name}")
    found = fill_in_locations(connection, geoip)
    if found:
        log(f"looked up {found} IPs seen earlier")


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
    failing = False
    while True:
        try:
            parsed = (parse_line(line) for line in reader.read())
            record(connection, (request for request in parsed if request), geoip)
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
