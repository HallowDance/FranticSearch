"""SQLite connection handling and the migration runner.

Migrations are plain SQL files in ``migrations/`` named ``NNN_description.sql``.
Each runs once, in order, inside a transaction; the applied version is kept in
``PRAGMA user_version``.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with the settings every part of the app relies on."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception.

    Re-entrant: inside an open transaction it just joins it, so domain functions
    can be combined into one atomic unit (the outermost block commits).
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrations() -> list[tuple[int, str, str]]:
    found = []
    for entry in resources.files("franticsearch").joinpath("migrations").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    for expected, (version, name, _) in enumerate(found, start=1):
        if version != expected:
            raise RuntimeError(f"migration numbering gap at {name} (expected {expected:03d})")
    return found


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations. Returns the names of those applied."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = []
    for version, name, sql in _migrations():
        if version <= current:
            continue
        # executescript() commits implicitly, so wrap the script in its own transaction.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
