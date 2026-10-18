"""Stock + movement ledger: the only code allowed to change physical whereabouts.

Every change to ``stock`` is written together with a ``movements`` row in one
transaction, so ``stock`` always equals the ledger's projection. ``check()``
verifies that.

All public functions open their own transaction (or join the caller's, see
``db.transaction``) and return the batch id that groups the movements they wrote.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Iterable

from .db import transaction

FINISHES = ("nonfoil", "foil", "etched")
CONDITIONS = ("NM", "LP", "MP", "HP", "DMG")
CONDITION_NAMES = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "DMG": "Damaged",
}
LOCATION_TYPES = ("binder", "box", "deck", "lent", "other")
# Scryfall language codes, in the order Scryfall lists them.
LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ja": "Japanese", "ko": "Korean", "ru": "Russian",
    "zhs": "Simplified Chinese", "zht": "Traditional Chinese", "he": "Hebrew",
    "la": "Latin", "grc": "Ancient Greek", "ar": "Arabic", "sa": "Sanskrit",
    "ph": "Phyrexian", "qya": "Quenya",
}


class LedgerError(ValueError):
    """A requested stock change is invalid (bad key, not enough copies, ...)."""


@dataclass(frozen=True)
class StockKey:
    printing_id: str
    finish: str = "nonfoil"
    condition: str = "NM"
    lang: str = "en"

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.printing_id, self.finish, self.condition, self.lang)


def new_batch_id() -> str:
    return uuid.uuid4().hex


def validate_key(conn: sqlite3.Connection, key: StockKey) -> None:
    row = conn.execute(
        "SELECT finishes FROM printings WHERE scryfall_id = ?", (key.printing_id,)
    ).fetchone()
    if row is None:
        raise LedgerError(f"unknown printing {key.printing_id}")
    if key.finish not in FINISHES:
        raise LedgerError(f"unknown finish {key.finish!r}")
    if key.finish not in json.loads(row["finishes"]):
        raise LedgerError(f"this printing does not exist in {key.finish}")
    if key.condition not in CONDITIONS:
        raise LedgerError(f"unknown condition {key.condition!r}")
    if not key.lang or not key.lang.isascii() or not key.lang.islower() or len(key.lang) > 5:
        raise LedgerError(f"bad language code {key.lang!r}")


def _location_type(conn: sqlite3.Connection, location_id: int) -> str:
    row = conn.execute("SELECT type FROM locations WHERE id = ?", (location_id,)).fetchone()
    if row is None:
        raise LedgerError(f"unknown location {location_id}")
    return row["type"]


def qty_at(conn: sqlite3.Connection, location_id: int, key: StockKey) -> int:
    row = conn.execute(
        "SELECT qty FROM stock WHERE location_id = ? AND printing_id = ? AND finish = ?"
        " AND condition = ? AND lang = ?",
        (location_id, *key.as_tuple()),
    ).fetchone()
    return row["qty"] if row else 0


def _change(conn: sqlite3.Connection, location_id: int, key: StockKey, delta: int) -> None:
    have = qty_at(conn, location_id, key)
    new = have + delta
    if new < 0:
        raise LedgerError(f"only {have} cop{'y' if have == 1 else 'ies'} there, cannot take {-delta}")
    if new == 0:
        conn.execute(
            "DELETE FROM stock WHERE location_id = ? AND printing_id = ? AND finish = ?"
            " AND condition = ? AND lang = ?",
            (location_id, *key.as_tuple()),
        )
    else:
        conn.execute(
            "INSERT INTO stock(location_id, printing_id, finish, condition, lang, qty)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(location_id, printing_id, finish, condition, lang)"
            " DO UPDATE SET qty = excluded.qty",
            (location_id, *key.as_tuple(), new),
        )


def _record(
    conn: sqlite3.Connection,
    batch_id: str,
    from_id: int | None,
    to_id: int | None,
    key: StockKey,
    qty: int,
    reason: str,
    note: str,
) -> None:
    """Apply one movement. Caller must hold a transaction."""
    if qty <= 0:
        raise LedgerError("quantity must be positive")
    if from_id is not None and from_id == to_id:
        raise LedgerError("source and destination are the same location")
    validate_key(conn, key)
    if from_id is not None:
        _location_type(conn, from_id)
        _change(conn, from_id, key, -qty)
    if to_id is not None:
        _location_type(conn, to_id)
        if conn.execute("SELECT archived FROM locations WHERE id = ?", (to_id,)).fetchone()[0]:
            raise LedgerError("that location is archived; unarchive it first")
        _change(conn, to_id, key, qty)
    conn.execute(
        "INSERT INTO movements(batch_id, from_location_id, to_location_id, printing_id,"
        " finish, condition, lang, qty, reason, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, from_id, to_id, *key.as_tuple(), qty, reason, note),
    )


def acquire(
    conn: sqlite3.Connection,
    location_id: int,
    key: StockKey,
    qty: int,
    note: str = "",
    reason: str = "acquire",
) -> str:
    """New copies enter the collection (bought, opened, traded for, imported)."""
    if reason not in ("acquire", "import"):
        raise LedgerError(f"bad reason for acquire: {reason}")
    batch = new_batch_id()
    with transaction(conn):
        _record(conn, batch, None, location_id, key, qty, reason, note)
    return batch


def acquire_many(
    conn: sqlite3.Connection,
    location_id: int,
    items: Iterable[tuple[StockKey, int]],
    note: str = "",
    reason: str = "import",
) -> str:
    """Several acquisitions into one location as a single batch (e.g. a file import)."""
    if reason not in ("acquire", "import"):
        raise LedgerError(f"bad reason for acquire: {reason}")
    batch = new_batch_id()
    with transaction(conn):
        for key, qty in items:
            _record(conn, batch, None, location_id, key, qty, reason, note)
    return batch


def move(
    conn: sqlite3.Connection,
    from_id: int,
    to_id: int,
    key: StockKey,
    qty: int,
    note: str = "",
) -> str:
    """Physically move copies. Into a deck is a pull, out of one a return."""
    batch = new_batch_id()
    with transaction(conn):
        to_type = _location_type(conn, to_id)
        from_type = _location_type(conn, from_id)
        if to_type == "deck":
            reason = "pull"
        elif from_type == "deck":
            reason = "return"
        else:
            reason = "move"
        _record(conn, batch, from_id, to_id, key, qty, reason, note)
    return batch


def adjust(
    conn: sqlite3.Connection, location_id: int, key: StockKey, new_qty: int, note: str = ""
) -> str | None:
    """Correct the count at a location to ``new_qty``. Returns None if nothing changed."""
    if new_qty < 0:
        raise LedgerError("quantity cannot be negative")
    batch = new_batch_id()
    with transaction(conn):
        delta = new_qty - qty_at(conn, location_id, key)
        if delta == 0:
            return None
        if delta > 0:
            _record(conn, batch, None, location_id, key, delta, "adjust", note)
        else:
            _record(conn, batch, location_id, None, key, -delta, "adjust", note)
    return batch


def sell(
    conn: sqlite3.Connection, location_id: int, key: StockKey, qty: int, note: str = ""
) -> str:
    """Copies leave the collection (sold, traded away, lost)."""
    batch = new_batch_id()
    with transaction(conn):
        _record(conn, batch, location_id, None, key, qty, "sell", note)
    return batch


_PROJECTION_SQL = """
WITH flows AS (
    SELECT to_location_id AS location_id, printing_id, finish, condition, lang, qty
      FROM movements WHERE to_location_id IS NOT NULL
    UNION ALL
    SELECT from_location_id, printing_id, finish, condition, lang, -qty
      FROM movements WHERE from_location_id IS NOT NULL
),
proj AS (
    SELECT location_id, printing_id, finish, condition, lang, SUM(qty) AS qty
      FROM flows GROUP BY location_id, printing_id, finish, condition, lang
)
SELECT COALESCE(p.location_id, s.location_id) AS location_id,
       COALESCE(p.printing_id, s.printing_id) AS printing_id,
       COALESCE(p.finish, s.finish)           AS finish,
       COALESCE(p.condition, s.condition)     AS condition,
       COALESCE(p.lang, s.lang)               AS lang,
       COALESCE(p.qty, 0)                     AS ledger_qty,
       COALESCE(s.qty, 0)                     AS stock_qty
  FROM proj p
  FULL OUTER JOIN stock s
    ON s.location_id = p.location_id AND s.printing_id = p.printing_id
   AND s.finish = p.finish AND s.condition = p.condition AND s.lang = p.lang
 WHERE COALESCE(p.qty, 0) <> COALESCE(s.qty, 0)
"""


def check(conn: sqlite3.Connection) -> list[dict]:
    """Rows where ``stock`` disagrees with the ledger. Empty list = consistent."""
    return [dict(r) for r in conn.execute(_PROJECTION_SQL)]
