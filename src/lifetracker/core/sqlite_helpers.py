"""SQLite helpers shared by the modules that keep their own SQLite files
outside Django's database (``client_ips`` and ``ip_location``).

Like them, it doesn't import Django.
"""

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open ``path`` read-only, with rows as ``sqlite3.Row``
    (``sqlite3.OperationalError`` if it doesn't exist)."""
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Run the block in one transaction, on a connection opened with
    ``isolation_level=None``, rolling it back if the block raises."""
    # IMMEDIATE, so a read then a write never fails with "database is locked".
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
