"""The wishlist: cards a user wants, separate from decks and their buy list (PLAN.md § Wishlist).

One list per user. A line wants ``qty`` of a card in any printing (``printing_id``
NULL) or in a specific printing; ``finish`` NULL means any finish. Lines are only
a list: nothing is moved until "got it", which adds the copies to a location
(``ledger.acquire``) and takes them off the line in one transaction.

Sharing works as for locations: ``private`` < ``users`` (on other users' People
page) < ``link`` (plus a read-only URL that needs no login).
"""

from __future__ import annotations

import json
import secrets
import sqlite3

from . import importer, ledger
from .db import transaction
from .ledger import FINISHES, LedgerError, StockKey
from .locations import VISIBILITIES

def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# A printing to show for "any printing" lines, in the order of ``importer._preference``:
# a regular, released, English paper printing, newest first.
_SHOWN = f"""COALESCE(w.printing_id, (
    SELECT p2.scryfall_id FROM printings p2
     WHERE p2.oracle_id = w.card_key OR p2.scryfall_id = w.card_key
     ORDER BY p2.layout IN {_in(importer._ODD_LAYOUTS)},
              COALESCE(p2.set_type, '') IN {_in(importer._ODD_SET_TYPES)}, COALESCE(p2.digital, 0),
              COALESCE(p2.released_at, '') > date('now'), p2.lang <> 'en',
              COALESCE(p2.set_type, '') IN {_in(importer._SPECIAL_SET_TYPES)}
                  OR p2.set_code IN {_in(importer._SPECIAL_SETS)},
              COALESCE(p2.promo, 0), p2.released_at DESC
     LIMIT 1))"""

# Copies the owner has that would satisfy the line (any location, decks included).
_OWNED = """(SELECT COALESCE(SUM(s.qty), 0)
       FROM printings p3 JOIN stock s ON s.printing_id = p3.scryfall_id
       JOIN locations l ON l.id = s.location_id
      WHERE (p3.oracle_id = w.card_key OR p3.scryfall_id = w.card_key) AND l.owner_id = w.owner_id
        AND (w.printing_id IS NULL OR s.printing_id = w.printing_id)
        AND (w.finish IS NULL OR s.finish = w.finish))"""


def _printings(conn: sqlite3.Connection, card_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM printings WHERE oracle_id = ? OR (oracle_id IS NULL AND scryfall_id = ?)",
        (card_key, card_key),
    ).fetchall()


def _check(conn: sqlite3.Connection, card_key: str, printing_id: str | None, finish: str | None) -> None:
    """The card exists, the printing is one of it, and the finish exists for it."""
    printings = _printings(conn, card_key)
    if not printings:
        raise LedgerError("unknown card")
    if printing_id:
        printings = [p for p in printings if p["scryfall_id"] == printing_id]
        if not printings:
            raise LedgerError("that printing is not of this card")
    if finish:
        if finish not in FINISHES:
            raise LedgerError(f"unknown finish {finish!r}")
        if not any(finish in json.loads(p["finishes"]) for p in printings):
            raise LedgerError(f"no {finish} version of {'this printing' if printing_id else 'this card'}")


def _qty(qty: int, allow_zero: bool = False) -> int:
    if qty < (0 if allow_zero else 1):
        raise LedgerError("quantity must be at least 1" if not allow_zero else "quantity can't be negative")
    if qty > 9999:
        raise LedgerError("quantity is too large")
    return qty


def add(conn: sqlite3.Connection, card_key: str, qty: int, printing_id: str | None = None,
        finish: str | None = None, note: str = "", *, owner: int) -> int:
    """Want ``qty`` more of a card. An identical line (card, printing, finish) gets the
    quantity added (and the note, if one is given). Returns the line id."""
    printing_id, finish, note = printing_id or None, finish or None, (note or "").strip()[:200]
    _qty(qty)
    with transaction(conn):
        _check(conn, card_key, printing_id, finish)
        row = conn.execute(
            """SELECT id FROM wishes WHERE owner_id = ? AND card_key = ?
                 AND COALESCE(printing_id, '') = COALESCE(?, '') AND COALESCE(finish, '') = COALESCE(?, '')""",
            (owner, card_key, printing_id, finish),
        ).fetchone()
        if row:
            conn.execute("UPDATE wishes SET qty = MIN(qty + ?, 9999), note = CASE WHEN ? = '' THEN note ELSE ? END "
                         "WHERE id = ?", (qty, note, note, row["id"]))
            return row["id"]
        return conn.execute(
            "INSERT INTO wishes(owner_id, card_key, printing_id, finish, qty, note) VALUES (?, ?, ?, ?, ?, ?)",
            (owner, card_key, printing_id, finish, qty, note),
        ).lastrowid


def lines(conn: sqlite3.Connection, owner: int, wish_id: int | None = None) -> list[sqlite3.Row]:
    """The owner's wishlist by card name, with what to show for each line and how many
    matching copies the owner already has (``owned``)."""
    return conn.execute(
        f"""SELECT w.*, p.name, p.image_normal AS image, p.mana_cost, p.type_line,
                   CASE WHEN w.printing_id IS NULL THEN NULL ELSE p.set_code END AS set_code,
                   CASE WHEN w.printing_id IS NULL THEN NULL ELSE p.set_name END AS set_name,
                   CASE WHEN w.printing_id IS NULL THEN NULL ELSE p.collector_number END AS collector_number,
                   {_OWNED} AS owned
              FROM wishes w JOIN printings p ON p.scryfall_id = {_SHOWN}
             WHERE w.owner_id = ? AND (? IS NULL OR w.id = ?)
             ORDER BY p.name COLLATE NOCASE, w.printing_id IS NOT NULL, w.id""",
        (owner, wish_id, wish_id),
    ).fetchall()


def get(conn: sqlite3.Connection, wish_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM wishes WHERE id = ?", (wish_id,)).fetchone()


def update(conn: sqlite3.Connection, wish_id: int, qty: int, printing_id: str | None,
           finish: str | None, note: str) -> None:
    """Change a line; quantity 0 removes it. Becoming identical to another line merges them."""
    printing_id, finish, note = printing_id or None, finish or None, (note or "").strip()[:200]
    _qty(qty, allow_zero=True)
    with transaction(conn):
        w = get(conn, wish_id)
        if w is None:
            raise LedgerError("unknown wishlist line")
        if qty == 0:
            conn.execute("DELETE FROM wishes WHERE id = ?", (wish_id,))
            return
        _check(conn, w["card_key"], printing_id, finish)
        other = conn.execute(
            """SELECT id FROM wishes WHERE owner_id = ? AND card_key = ? AND id <> ?
                 AND COALESCE(printing_id, '') = COALESCE(?, '') AND COALESCE(finish, '') = COALESCE(?, '')""",
            (w["owner_id"], w["card_key"], wish_id, printing_id, finish),
        ).fetchone()
        if other:
            conn.execute("DELETE FROM wishes WHERE id = ?", (wish_id,))
            conn.execute("UPDATE wishes SET qty = MIN(qty + ?, 9999), note = CASE WHEN ? = '' THEN note ELSE ? END "
                         "WHERE id = ?", (qty, note, note, other["id"]))
        else:
            conn.execute("UPDATE wishes SET qty = ?, printing_id = ?, finish = ?, note = ? WHERE id = ?",
                         (qty, printing_id, finish, note, wish_id))


def remove(conn: sqlite3.Connection, wish_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM wishes WHERE id = ?", (wish_id,))


def got(conn: sqlite3.Connection, wish_id: int, location_id: int, key: StockKey, qty: int) -> str:
    """"Got it": add ``qty`` new copies (``key``, any printing of the card) to a location
    and take them off the line (removed when it reaches 0). Returns the ledger batch;
    undoing it removes the copies but doesn't put the line back."""
    _qty(qty)
    with transaction(conn):
        w = get(conn, wish_id)
        if w is None:
            raise LedgerError("unknown wishlist line")
        if key.printing_id not in {p["scryfall_id"] for p in _printings(conn, w["card_key"])}:
            raise LedgerError("that printing is not of this card")
        batch = ledger.acquire(conn, location_id, key, qty, note="wishlist")
        if qty >= w["qty"]:
            conn.execute("DELETE FROM wishes WHERE id = ?", (wish_id,))
        else:
            conn.execute("UPDATE wishes SET qty = qty - ? WHERE id = ?", (qty, wish_id))
    return batch


def paste(conn: sqlite3.Connection, text: str, *, owner: int) -> tuple[int, list[tuple[str, str]]]:
    """Add a pasted list: "2 Sol Ring" wants any printing, "1 Sol Ring (C21) 263 *F*" that
    printing (and finish, if flagged). Returns (cards added, [(line, problem)])."""
    added, left = 0, []
    with transaction(conn):
        for line in importer.parse_deck_text(text):
            if line.problem:
                left.append((line.raw, line.problem))
                continue
            try:
                if line.set_code:
                    key, problem = importer.resolve(conn, line, allow_default=False)
                    if key is None:
                        left.append((line.raw, problem))
                        continue
                    p = conn.execute("SELECT COALESCE(oracle_id, scryfall_id) AS k FROM printings "
                                     "WHERE scryfall_id = ?", (key.printing_id,)).fetchone()
                    add(conn, p["k"], line.qty, key.printing_id, line.finish, owner=owner)
                else:
                    # The card of the name's best printing (not e.g. its art-series card).
                    best = importer.default_key(conn, importer.printings_named(conn, line.name), line.finish)
                    if best is None:
                        left.append((line.raw, f"no {line.finish + ' ' if line.finish else ''}card named "
                                               f"{line.name!r}"))
                        continue
                    p = conn.execute("SELECT COALESCE(oracle_id, scryfall_id) AS k FROM printings "
                                     "WHERE scryfall_id = ?", (best.printing_id,)).fetchone()
                    add(conn, p["k"], line.qty, finish=line.finish, owner=owner)
            except LedgerError as e:
                left.append((line.raw, str(e)))
                continue
            added += line.qty
    return added, left


# --------------------------------------------------------------------------- sharing

def settings(conn: sqlite3.Connection, owner: int) -> dict:
    row = conn.execute("SELECT visibility, share_token FROM wishlists WHERE owner_id = ?", (owner,)).fetchone()
    return dict(row) if row else {"visibility": "private", "share_token": None}


def set_visibility(conn: sqlite3.Connection, owner: int, visibility: str, new_link: bool = False) -> None:
    """Who can see the wishlist; 'link' gets a share token (``new_link`` replaces it)."""
    if visibility not in VISIBILITIES:
        raise LedgerError(f"visibility must be one of: {', '.join(VISIBILITIES)}")
    with transaction(conn):
        token = settings(conn, owner)["share_token"]
        if visibility == "link" and (new_link or not token):
            token = secrets.token_urlsafe(18)
        conn.execute(
            """INSERT INTO wishlists(owner_id, visibility, share_token) VALUES (?, ?, ?)
               ON CONFLICT(owner_id) DO UPDATE SET visibility = excluded.visibility,
                                                   share_token = excluded.share_token""",
            (owner, visibility, token),
        )


def can_view(conn: sqlite3.Connection, owner: int, viewer: int | None) -> bool:
    """May ``viewer`` (None = anonymous) see ``owner``'s wishlist? Anonymous viewers come
    through ``by_token`` instead."""
    if viewer is not None and viewer == owner:
        return True
    row = conn.execute(
        """SELECT 1 FROM wishlists w JOIN users u ON u.id = w.owner_id
            WHERE w.owner_id = ? AND w.visibility IN ('users', 'link') AND u.disabled = 0""",
        (owner,),
    ).fetchone()
    return viewer is not None and row is not None


def by_token(conn: sqlite3.Connection, token: str) -> int | None:
    """The owner of the wishlist behind a share link (None if it's not shared by link)."""
    if not token:
        return None
    row = conn.execute(
        """SELECT w.owner_id FROM wishlists w JOIN users u ON u.id = w.owner_id
            WHERE w.share_token = ? AND w.visibility = 'link' AND u.disabled = 0""",
        (token,),
    ).fetchone()
    return row["owner_id"] if row else None


def shared(conn: sqlite3.Connection, viewer: int) -> list[sqlite3.Row]:
    """Other users' shared wishlists, for the People page."""
    return conn.execute(
        """SELECT u.id AS owner_id, u.username AS owner_name,
                  (SELECT COALESCE(SUM(qty), 0) FROM wishes x WHERE x.owner_id = u.id) AS cards
             FROM wishlists w JOIN users u ON u.id = w.owner_id
            WHERE w.visibility IN ('users', 'link') AND u.disabled = 0 AND u.id <> ?
            ORDER BY u.username COLLATE NOCASE""",
        (viewer,),
    ).fetchall()


def wanted_by(conn: sqlite3.Connection, card_key: str, viewer: int) -> list[sqlite3.Row]:
    """Other users who want this card on a wishlist shared with the viewer (card page)."""
    return conn.execute(
        """SELECT w.*, u.username AS owner_name, p.set_code, p.collector_number
             FROM wishes w JOIN users u ON u.id = w.owner_id
             JOIN wishlists l ON l.owner_id = w.owner_id
             LEFT JOIN printings p ON p.scryfall_id = w.printing_id
            WHERE w.card_key = ? AND w.owner_id <> ? AND l.visibility IN ('users', 'link') AND u.disabled = 0
            ORDER BY u.username COLLATE NOCASE""",
        (card_key, viewer),
    ).fetchall()
