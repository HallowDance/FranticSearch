"""Location CRUD. Locations of type 'deck' belong to decks and are managed there."""

from __future__ import annotations

import sqlite3

from .db import transaction
from .ledger import LOCATION_TYPES, LedgerError

# Types a user can create/edit directly; 'deck' locations come with decks (phase 4).
USER_TYPES = tuple(t for t in LOCATION_TYPES if t != "deck")


def _clean(name: str, type_: str) -> str:
    name = (name or "").strip()
    if not name:
        raise LedgerError("name is required")
    if len(name) > 100:
        raise LedgerError("name is too long (max 100)")
    if type_ not in USER_TYPES:
        raise LedgerError(f"type must be one of: {', '.join(USER_TYPES)}")
    return name


def list_all(conn: sqlite3.Connection, archived: bool | None = False) -> list[sqlite3.Row]:
    """Locations with copy counts. ``archived``: False = active only, True = archived only,
    None = both."""
    where = "" if archived is None else f"WHERE l.archived = {int(archived)}"
    return conn.execute(
        f"""SELECT l.*, COALESCE(SUM(s.qty), 0) AS cards, COUNT(s.printing_id) AS rows_
              FROM locations l LEFT JOIN stock s ON s.location_id = l.id
              {where}
             GROUP BY l.id ORDER BY l.type = 'deck', l.name COLLATE NOCASE"""
    ).fetchall()


def get(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()


def create(conn: sqlite3.Connection, name: str, type_: str, notes: str = "") -> int:
    name = _clean(name, type_)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO locations(name, type, notes) VALUES (?, ?, ?)",
                (name, type_, notes.strip()),
            )
    except sqlite3.IntegrityError:
        raise LedgerError(f"a location named {name!r} already exists") from None
    return cur.lastrowid


def update(conn: sqlite3.Connection, location_id: int, name: str, type_: str, notes: str) -> None:
    loc = get(conn, location_id)
    if loc is None:
        raise LedgerError("unknown location")
    if loc["type"] == "deck":
        raise LedgerError("deck locations are edited through their deck")
    name = _clean(name, type_)
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE locations SET name = ?, type = ?, notes = ? WHERE id = ?",
                (name, type_, notes.strip(), location_id),
            )
    except sqlite3.IntegrityError:
        raise LedgerError(f"a location named {name!r} already exists") from None


def has_history(conn: sqlite3.Connection, location_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM movements WHERE from_location_id = ? OR to_location_id = ? LIMIT 1",
        (location_id, location_id),
    ).fetchone() is not None


def set_archived(conn: sqlite3.Connection, location_id: int, archived: bool) -> None:
    """Archive (hide) an empty location, or bring it back. History is untouched."""
    loc = get(conn, location_id)
    if loc is None:
        raise LedgerError("unknown location")
    if loc["type"] == "deck":
        raise LedgerError("deck locations are archived through their deck")
    with transaction(conn):
        if archived and conn.execute(
            "SELECT 1 FROM stock WHERE location_id = ? LIMIT 1", (location_id,)
        ).fetchone():
            raise LedgerError("move all cards out before archiving")
        conn.execute(
            "UPDATE locations SET archived = ? WHERE id = ?", (int(archived), location_id)
        )


def delete(conn: sqlite3.Connection, location_id: int) -> None:
    """Only locations that never held anything can be deleted (the ledger refers to them);
    used locations are archived instead."""
    loc = get(conn, location_id)
    if loc is None:
        raise LedgerError("unknown location")
    if loc["type"] == "deck":
        raise LedgerError("deck locations are deleted through their deck")
    with transaction(conn):
        if has_history(conn, location_id):
            raise LedgerError(
                "this location has history in the ledger and cannot be deleted; "
                "move its cards out and archive it instead"
            )
        conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
