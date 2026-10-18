"""Location CRUD and sharing. Locations of type 'deck' belong to decks and are managed there.

Every location has an owner; names are unique per owner. Visibility:
``private`` (owner only) < ``users`` (any logged-in user; listed on the People
page) < ``link`` (``users`` plus a read-only share URL that needs no login).
"""

from __future__ import annotations

import secrets
import sqlite3

from .db import transaction
from .ledger import LOCATION_TYPES, LedgerError

# Types a user can create/edit directly; 'deck' locations come with decks (phase 4).
USER_TYPES = tuple(t for t in LOCATION_TYPES if t != "deck")
VISIBILITIES = ("private", "users", "link")
VISIBILITY_NAMES = {"private": "Private", "users": "Shared with users",
                    "link": "Shared with users + anyone with the link"}


def _clean(name: str, type_: str) -> str:
    name = (name or "").strip()
    if not name:
        raise LedgerError("name is required")
    if len(name) > 100:
        raise LedgerError("name is too long (max 100)")
    if type_ not in USER_TYPES:
        raise LedgerError(f"type must be one of: {', '.join(USER_TYPES)}")
    return name


def list_all(
    conn: sqlite3.Connection, owner: int, archived: bool | None = False
) -> list[sqlite3.Row]:
    """A user's locations with copy counts. ``archived``: False = active only,
    True = archived only, None = both."""
    where = "" if archived is None else f"AND l.archived = {int(archived)}"
    return conn.execute(
        f"""SELECT l.*, COALESCE(SUM(s.qty), 0) AS cards, COUNT(s.printing_id) AS rows_
              FROM locations l LEFT JOIN stock s ON s.location_id = l.id
             WHERE l.owner_id = ? {where}
             GROUP BY l.id ORDER BY l.type = 'deck', l.name COLLATE NOCASE""",
        (owner,),
    ).fetchall()


def shared(conn: sqlite3.Connection, viewer: int | None = None) -> list[sqlite3.Row]:
    """Other users' shared (users/link) locations, for the People page."""
    return conn.execute(
        """SELECT l.*, u.username AS owner_name, d.id AS deck_id,
                  COALESCE(SUM(s.qty), 0) AS cards
             FROM locations l JOIN users u ON u.id = l.owner_id
             LEFT JOIN decks d ON d.location_id = l.id
             LEFT JOIN stock s ON s.location_id = l.id
            WHERE l.visibility IN ('users', 'link') AND l.archived = 0
              AND (? IS NULL OR l.owner_id <> ?)
            GROUP BY l.id ORDER BY u.username COLLATE NOCASE, l.type = 'deck', l.name COLLATE NOCASE""",
        (viewer, viewer),
    ).fetchall()


def can_view(loc: sqlite3.Row | None, user_id: int | None) -> bool:
    """May ``user_id`` (None = anonymous) see this location's contents?"""
    if loc is None:
        return False
    if loc["owner_id"] == user_id and user_id is not None:
        return True
    return user_id is not None and loc["visibility"] in ("users", "link")


def by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM locations WHERE share_token = ? AND visibility = 'link'", (token,)
    ).fetchone()


def set_visibility(conn: sqlite3.Connection, location_id: int, visibility: str,
                   new_link: bool = False) -> None:
    """Change who can see a location; 'link' gets a share token (``new_link`` replaces it)."""
    if visibility not in VISIBILITIES:
        raise LedgerError(f"visibility must be one of: {', '.join(VISIBILITIES)}")
    with transaction(conn):
        loc = get(conn, location_id)
        if loc is None:
            raise LedgerError("unknown location")
        token = loc["share_token"]
        if visibility == "link" and (new_link or not token):
            token = secrets.token_urlsafe(18)
        conn.execute("UPDATE locations SET visibility = ?, share_token = ? WHERE id = ?",
                     (visibility, token, location_id))


def get(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()


def create(conn: sqlite3.Connection, name: str, type_: str, notes: str = "", *, owner: int) -> int:
    name = _clean(name, type_)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO locations(owner_id, name, type, notes) VALUES (?, ?, ?, ?)",
                (owner, name, type_, notes.strip()),
            )
    except sqlite3.IntegrityError:
        raise LedgerError(f"you already have a location named {name!r}") from None
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
        raise LedgerError(f"you already have a location named {name!r}") from None


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
