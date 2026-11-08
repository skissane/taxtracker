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
The admin's Client IPs page (``core.admin_views.client_ips_view``) reads the file.

The client's IP is Fly Proxy's ``Fly-Client-IP`` header, or Envoy's peer
without one (e.g. under ``just docker-run``). ``./run.sh`` runs no Envoy, so
records nothing.

At startup the collector updates the DB-IP Lite databases and Apple's list of
iCloud Private Relay egress ranges (``ip_location.update_geoip``) and fills in any IP
it couldn't look up before. It looks up each new IP's location as it records
it, and checks every IP against the relay ranges each time it's seen.

Like the idle watchdog, it doesn't import Django.
"""

import collections
import dataclasses
import datetime
import ipaddress
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import FrameType
from typing import Any

from lifetracker.core.ip_location import DOWNLOADED, FAILED, GeoIP, Location, Relay, update_geoip
from lifetracker.core.sqlite_helpers import transaction

DEFAULT_REQUEST_LOG_NAME = "lifetracker-envoy-requests.jsonl"
DEFAULT_INTERVAL_SECONDS = 5
# Empty the request log once it's been read and is at least this big.
TRUNCATE_BYTES = 1024 * 1024
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
    geoip_dir: Path | None
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


def _present(value: object) -> bool:
    return value not in (None, "", "-")


def _ip(value: object) -> str | None:
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

    def __init__(self, path: Path, truncate_bytes: int = TRUNCATE_BYTES) -> None:
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
# The SQLite file


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if need be) the collector's database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # So the admin page's reads never wait for the collector.
    connection.execute("PRAGMA journal_mode=WAL")
    with transaction(connection):
        connection.execute(_SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(client_ip)")}
        for column, type_ in _ADDED_COLUMNS.items():
            if column not in columns:
                connection.execute(f"ALTER TABLE client_ip ADD COLUMN {column} {type_}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


def record(connection: sqlite3.Connection, requests_: Iterable[Request], geoip: GeoIP) -> int:
    """Add ``requests_`` to the per-IP totals in one transaction; returns how
    many IPs were updated. A new IP, or one without a location yet, is looked
    up in ``geoip``, and every IP is checked against its relay ranges (unless
    there are none, when it keeps what it had)."""
    by_ip: dict[str, list[Request]] = {}
    for request in requests_:
        by_ip.setdefault(request.ip, []).append(request)
    if not by_ip:
        return 0
    with transaction(connection):
        for ip, ip_requests in by_ip.items():
            ip_requests.sort(key=lambda request: request.time)
            latest = ip_requests[-1]
            logged_in_times = [r.time for r in ip_requests if r.logged_in]
            row = connection.execute("SELECT * FROM client_ip WHERE ip = ?", (ip,)).fetchone()
            row = dict(row) if row is not None else None
            if row is None or (row["country"] is None and row["asn"] is None):
                location = geoip.lookup(ip)
            else:
                location = Location(row["country"], row["country_name"], row["asn"], row["as_org"])
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


def _min(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
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
    with transaction(connection):
        rows = connection.execute("SELECT ip FROM client_ip WHERE country IS NULL AND asn IS NULL").fetchall()
        for row in rows:
            location = geoip.lookup(row["ip"])
            if location:
                connection.execute(
                    "UPDATE client_ip SET country = ?, country_name = ?, asn = ?, as_org = ? WHERE ip = ?",
                    (*dataclasses.astuple(location), row["ip"]),
                )
                found += 1
    return found


def fill_in_relay(connection: sqlite3.Connection, geoip: GeoIP) -> int:
    """Check every IP not checked yet against the relay ranges (e.g. seen
    before there were any, or before this existed); returns how many it
    checked."""
    checked = 0
    with transaction(connection):
        rows = connection.execute("SELECT ip FROM client_ip WHERE private_relay IS NULL").fetchall()
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


def refresh_geoip(config: Config, connection: sqlite3.Connection, geoip: GeoIP) -> None:
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


def recent_ips(connection: sqlite3.Connection, *, logged_in_only: bool = False, limit: int) -> list[sqlite3.Row]:
    """Up to ``limit`` IPs, most recently seen first; only those with a valid
    JWT if ``logged_in_only``."""
    where = "WHERE logged_in > 0" if logged_in_only else ""
    return connection.execute(f"SELECT * FROM client_ip {where} ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()


def ip_count(connection: sqlite3.Connection, *, logged_in_only: bool = False) -> int:
    where = "WHERE logged_in > 0" if logged_in_only else ""
    return int(connection.execute(f"SELECT count(*) FROM client_ip {where}").fetchone()[0])


# The client_ip columns holding POSIX times, which export_rows gives as ISO
# 8601 UTC.
TIME_COLUMNS = frozenset({"first_seen", "last_seen", "first_logged_in", "last_logged_in"})


def _iso_utc(posix_time: float) -> str:
    return (
        datetime.datetime.fromtimestamp(posix_time, datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def export_rows(connection: sqlite3.Connection, *, logged_in_only: bool = False) -> Iterable[dict[str, Any]]:
    """Every IP's row (only those with a valid JWT if ``logged_in_only``),
    first seen first, as a dict in the table's column order (so columns a
    later schema adds come last), with times as ISO 8601 UTC strings and
    missing values ``None``."""
    where = "WHERE logged_in > 0" if logged_in_only else ""
    cursor = connection.execute(f"SELECT * FROM client_ip {where} ORDER BY first_seen, ip")
    columns = [description[0] for description in cursor.description]
    for row in cursor:
        yield {
            column: _iso_utc(value) if column in TIME_COLUMNS and value is not None else value
            for column, value in zip(columns, row, strict=True)
        }


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
    sleep: Callable[[float], None] = time.sleep,
    getppid: Callable[[], int] = os.getppid,
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


def _collect(
    config: Config,
    master_pid: int,
    connection: sqlite3.Connection,
    geoip: GeoIP,
    sleep: Callable[[float], None],
    getppid: Callable[[], int],
) -> int:
    reader = RequestLogReader(config.request_log)
    # Requests read but not yet recorded, as the reader has moved past them
    # (and may have emptied the file): retried until recording works again,
    # keeping only the newest MAX_PENDING_REQUESTS meanwhile.
    pending: collections.deque[Request] = collections.deque(maxlen=MAX_PENDING_REQUESTS)
    failing = False
    while True:
        try:
            pending.extend(request for request in map(parse_line, reader.read()) if request)
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


def spawn(master_pid: int, environ: Mapping[str, str] = os.environ) -> subprocess.Popen[bytes] | None:
    """Start the collector for gunicorn master ``master_pid``, unless there's
    no database to record in. Returns the ``Popen`` or None."""
    if config_from_env(environ).db_path is None:
        return None
    return subprocess.Popen([sys.executable, "-m", __name__, str(master_pid)], env=dict(environ))


def _exit_quietly(signum: int, frame: FrameType | None) -> None:
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
