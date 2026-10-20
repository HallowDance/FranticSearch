"""User accounts (admin-created, no sign-up). See PLAN.md "Users & sharing".

Each user owns their locations; stock, movements and decks belong to a user
through those. Card data (``printings``) is shared.
"""

from __future__ import annotations

import re
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from .db import transaction
from .ledger import LedgerError

MIN_PASSWORD = 8
_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
# Compared against when the username doesn't exist, so a login takes the same time either way.
_DUMMY_HASH = generate_password_hash("not a real password")


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise LedgerError(f"password must be at least {MIN_PASSWORD} characters")


def create(conn: sqlite3.Connection, username: str, password: str, is_admin: bool = False) -> int:
    """New account. The first admin adopts data from before users existed."""
    username = (username or "").strip()
    if not _USERNAME.match(username):
        raise LedgerError("username: 2-32 letters, digits, '_', '.' or '-'")
    _check_password(password)
    try:
        with transaction(conn):
            uid = conn.execute(
                "INSERT INTO users(username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), int(is_admin)),
            ).lastrowid
            if is_admin and not conn.execute(
                "SELECT 1 FROM users WHERE is_admin = 1 AND id <> ?", (uid,)
            ).fetchone():
                conn.execute("UPDATE locations SET owner_id = ? WHERE owner_id IS NULL", (uid,))
                conn.execute("UPDATE imports SET owner_id = ? WHERE owner_id IS NULL", (uid,))
                conn.execute("UPDATE movements SET owner_id = ? WHERE owner_id IS NULL", (uid,))
    except sqlite3.IntegrityError:
        raise LedgerError(f"user {username!r} already exists") from None
    return uid


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
    ).fetchone()
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password or "")
    return row if row and ok else None


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    _check_password(password)
    with transaction(conn):
        n = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        ).rowcount
    if not n:
        raise LedgerError("unknown user")


def get(conn: sqlite3.Connection, user_id: int | None) -> sqlite3.Row | None:
    if user_id is None:
        return None
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def by_name(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT u.*,
                  (SELECT COUNT(*) FROM locations l WHERE l.owner_id = u.id) AS locations,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s JOIN locations l ON l.id = s.location_id
                    WHERE l.owner_id = u.id) AS copies
             FROM users u ORDER BY u.username COLLATE NOCASE"""
    ).fetchall()


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
