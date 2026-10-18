"""Decks and their slots. Each deck owns exactly one location of type 'deck'.

Phase 2 creates decks through deck import and shows them read-only; the editor,
derived deck state and pull/return lists come in phase 4.
"""

from __future__ import annotations

import sqlite3

from .db import transaction
from .ledger import LedgerError, StockKey, validate_key
from .locations import has_history

BOARDS = ("commander", "main", "side", "maybe")
BOARD_NAMES = {"commander": "Commander", "main": "Main", "side": "Sideboard", "maybe": "Maybeboard"}
# Scryfall legality keys offered as deck formats ('' = unspecified).
FORMATS = (
    "", "commander", "standard", "pioneer", "modern", "legacy", "vintage", "pauper",
    "paupercommander", "oathbreaker", "brawl", "standardbrawl", "premodern", "oldschool",
    "predh", "duel",
)
# Formats played with a commander (the deck import looks for one).
COMMANDER_FORMATS = ("commander", "paupercommander", "oathbreaker", "brawl", "standardbrawl",
                     "predh", "duel")


def create(conn: sqlite3.Connection, name: str, format_: str = "", notes: str = "") -> int:
    """New deck plus its deck location (same name; location names are unique)."""
    name = (name or "").strip()
    if not name:
        raise LedgerError("deck name is required")
    if len(name) > 100:
        raise LedgerError("deck name is too long (max 100)")
    if format_ not in FORMATS:
        raise LedgerError(f"unknown format {format_!r}")
    try:
        with transaction(conn):
            loc = conn.execute(
                "INSERT INTO locations(name, type) VALUES (?, 'deck')", (name,)
            ).lastrowid
            return conn.execute(
                "INSERT INTO decks(name, format, location_id, notes) VALUES (?, ?, ?, ?)",
                (name, format_, loc, notes.strip()),
            ).lastrowid
    except sqlite3.IntegrityError:
        raise LedgerError(f"a location or deck named {name!r} already exists") from None


def add_slot(conn: sqlite3.Connection, deck_id: int, key: StockKey, board: str, qty: int) -> None:
    """Want ``qty`` more of ``key`` on ``board`` (adds to an existing slot)."""
    if board not in BOARDS:
        raise LedgerError(f"unknown board {board!r}")
    if qty <= 0:
        raise LedgerError("quantity must be positive")
    with transaction(conn):
        validate_key(conn, key)
        conn.execute(
            "INSERT INTO deck_slots(deck_id, printing_id, finish, condition, lang, board, qty_wanted)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(deck_id, printing_id, finish, condition, lang, board)"
            " DO UPDATE SET qty_wanted = qty_wanted + excluded.qty_wanted",
            (deck_id, *key.as_tuple(), board, qty),
        )


def commander_names(conn: sqlite3.Connection, deck_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT p.name FROM deck_slots ds JOIN printings p ON p.scryfall_id = ds.printing_id"
        " WHERE ds.deck_id = ? AND ds.board = 'commander' ORDER BY p.name", (deck_id,))]


def get(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()


def by_location(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM decks WHERE location_id = ?", (location_id,)).fetchone()


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.*,
                  (SELECT COALESCE(SUM(qty_wanted), 0) FROM deck_slots
                    WHERE deck_id = d.id AND board <> 'maybe') AS cards,
                  (SELECT COUNT(*) FROM import_unresolved u JOIN imports i ON i.id = u.import_id
                    WHERE i.deck_id = d.id AND u.status = 'open') AS unresolved
             FROM decks d ORDER BY d.name COLLATE NOCASE"""
    ).fetchall()


def slots(conn: sqlite3.Connection, deck_id: int) -> list[sqlite3.Row]:
    """Slots with printing info and where matching copies are (read-only view).

    ``in_deck`` = copies at the deck's location; ``free`` = copies of the exact
    key outside any deck; ``owned_any`` = copies of any printing of the card.
    """
    return conn.execute(
        """SELECT ds.*, p.name, p.set_code, p.collector_number, p.type_line, p.mana_cost, p.cmc,
                  COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s
                    WHERE s.location_id = d.location_id AND s.printing_id = ds.printing_id
                      AND s.finish = ds.finish AND s.condition = ds.condition AND s.lang = ds.lang
                  ) AS in_deck,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s JOIN locations l ON l.id = s.location_id
                    WHERE l.type <> 'deck' AND s.printing_id = ds.printing_id
                      AND s.finish = ds.finish AND s.condition = ds.condition AND s.lang = ds.lang
                  ) AS free,
                  (SELECT GROUP_CONCAT(l.name, ', ') FROM stock s JOIN locations l ON l.id = s.location_id
                    WHERE l.type <> 'deck' AND s.printing_id = ds.printing_id
                      AND s.finish = ds.finish AND s.condition = ds.condition AND s.lang = ds.lang
                  ) AS free_where,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s JOIN printings p2 ON p2.scryfall_id = s.printing_id
                    WHERE p2.oracle_id = p.oracle_id) AS owned_any
             FROM deck_slots ds
             JOIN decks d ON d.id = ds.deck_id
             JOIN printings p ON p.scryfall_id = ds.printing_id
            WHERE ds.deck_id = ?
            ORDER BY CASE ds.board WHEN 'commander' THEN 0 WHEN 'main' THEN 1
                                   WHEN 'side' THEN 2 ELSE 3 END,
                     p.name COLLATE NOCASE""",
        (deck_id,),
    ).fetchall()


def delete(conn: sqlite3.Connection, deck_id: int) -> None:
    """Delete a deck, its slots, its import record and its (never used) location."""
    deck = get(conn, deck_id)
    if deck is None:
        raise LedgerError("unknown deck")
    with transaction(conn):
        if has_history(conn, deck["location_id"]):
            raise LedgerError("cards have been moved into this deck; it cannot be deleted")
        conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))  # cascades slots + import
        conn.execute("DELETE FROM locations WHERE id = ?", (deck["location_id"],))
