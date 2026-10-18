"""Read-only queries behind the collection browser, card pages, search and history."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

PAGE_SIZE = 100

# Sortable columns: key -> (primary ORDER BY term, tie-breaker). "-key" sorts descending.
COLLECTION_SORTS = {
    "name": ("name COLLATE NOCASE", "card_key"),
    "cost": ("MIN(p.cmc)", "name COLLATE NOCASE"),
    "type": ("MIN(p.type_line) COLLATE NOCASE", "name COLLATE NOCASE"),
    "qty": ("SUM(s.qty)", "name COLLATE NOCASE"),
    "printings": ("COUNT(DISTINCT p.scryfall_id)", "name COLLATE NOCASE"),
}
LOCATION_SORTS = {
    "name": ("p.name COLLATE NOCASE", "p.set_code, p.collector_number"),
    "set": ("p.set_code", "p.collector_number, p.name COLLATE NOCASE"),
    "cost": ("p.cmc", "p.name COLLATE NOCASE"),
    "qty": ("s.qty", "p.name COLLATE NOCASE"),
}


def order_by(sorts: dict[str, tuple[str, str]], sort: str, default: str = "name") -> str:
    """Whitelisted ORDER BY clause for a ``sort`` parameter like "qty" or "-qty"."""
    key = sort.lstrip("-")
    if key not in sorts:
        key, sort = default, default
    primary, tie = sorts[key]
    return f"{primary}{' DESC' if sort.startswith('-') else ''}, {tie}"


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 prefix query on the name column."""
    words = re.findall(r"\w+", text.lower())
    if not words:
        return None
    return " ".join(f'name:"{w}"*' for w in words)


@dataclass
class CollectionFilter:
    q: str = ""
    location_id: int | None = None
    location_type: str = ""
    not_in_deck: bool = False
    set_code: str = ""
    page: int = 1
    sort: str = "name"

    def where(self) -> tuple[str, list]:
        clauses, args = [], []
        if self.q:
            fq = fts_query(self.q)
            if fq:
                clauses.append(
                    "p.rowid IN (SELECT rowid FROM printings_fts WHERE printings_fts MATCH ?)"
                )
                args.append(fq)
        if self.location_id:
            clauses.append("s.location_id = ?")
            args.append(self.location_id)
        if self.location_type:
            clauses.append("l.type = ?")
            args.append(self.location_type)
        if self.not_in_deck:
            clauses.append("l.type <> 'deck'")
        if self.set_code:
            clauses.append("p.set_code = ?")
            args.append(self.set_code.lower())
        return (" AND ".join(clauses) or "1"), args


def collection(conn: sqlite3.Connection, f: CollectionFilter) -> tuple[list[sqlite3.Row], int]:
    """Owned cards grouped by card (oracle id), with totals. Returns (rows, total_cards)."""
    where, args = f.where()
    base = f"""
        FROM stock s
        JOIN printings p ON p.scryfall_id = s.printing_id
        JOIN locations l ON l.id = s.location_id
       WHERE {where}
    """
    total = conn.execute(
        f"SELECT COUNT(DISTINCT COALESCE(p.oracle_id, p.scryfall_id)) {base}", args
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                   MIN(p.name) AS name, MIN(p.type_line) AS type_line,
                   MIN(p.mana_cost) AS mana_cost, MIN(p.image_normal) AS image, MIN(p.cmc) AS cmc,
                   SUM(s.qty) AS qty,
                   COUNT(DISTINCT p.scryfall_id) AS printings,
                   GROUP_CONCAT(DISTINCT l.name) AS locations
            {base}
            GROUP BY card_key
            ORDER BY {order_by(COLLECTION_SORTS, f.sort)}
            LIMIT ? OFFSET ?""",
        [*args, PAGE_SIZE, (max(f.page, 1) - 1) * PAGE_SIZE],
    ).fetchall()
    return rows, total


def totals(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute(
        """SELECT (SELECT COALESCE(SUM(qty), 0) FROM stock) AS copies,
                  (SELECT COUNT(DISTINCT p.oracle_id) FROM stock s
                     JOIN printings p ON p.scryfall_id = s.printing_id) AS cards,
                  (SELECT COUNT(*) FROM printings) AS printings,
                  (SELECT COUNT(*) FROM locations) AS locations,
                  (SELECT COUNT(*) FROM movements) AS movements"""
    ).fetchone()


def card_printings(conn: sqlite3.Connection, card_key: str) -> list[sqlite3.Row]:
    """All printings of a card (by oracle id, or scryfall id for oracle-less objects)."""
    return conn.execute(
        """SELECT * FROM printings
            WHERE oracle_id = ? OR (oracle_id IS NULL AND scryfall_id = ?)
            ORDER BY released_at DESC, set_code, collector_number""",
        (card_key, card_key),
    ).fetchall()


def card_stock(conn: sqlite3.Connection, card_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.*, l.name AS location_name, l.type AS location_type,
                  p.set_code, p.set_name, p.collector_number, p.name, p.image_normal AS image
             FROM stock s
             JOIN printings p ON p.scryfall_id = s.printing_id
             JOIN locations l ON l.id = s.location_id
            WHERE p.oracle_id = ? OR (p.oracle_id IS NULL AND p.scryfall_id = ?)
            ORDER BY p.released_at DESC, p.set_code, p.collector_number, s.finish,
                     l.name COLLATE NOCASE""",
        (card_key, card_key),
    ).fetchall()


def location_stock(conn: sqlite3.Connection, location_id: int, sort: str = "name") -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT s.*, p.name, p.set_code, p.collector_number, p.type_line, p.image_normal AS image,
                   p.mana_cost, p.cmc, COALESCE(p.oracle_id, p.scryfall_id) AS card_key
              FROM stock s JOIN printings p ON p.scryfall_id = s.printing_id
             WHERE s.location_id = ?
             ORDER BY {order_by(LOCATION_SORTS, sort)}""",
        (location_id,),
    ).fetchall()


def search_cards(conn: sqlite3.Connection, text: str, limit: int = 30) -> list[sqlite3.Row]:
    """Distinct cards whose name matches, best (shortest) names first."""
    fq = fts_query(text)
    if not fq:
        return []
    return conn.execute(
        """SELECT COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                  MIN(p.name) AS name, MIN(p.type_line) AS type_line,
                  MIN(p.mana_cost) AS mana_cost, COUNT(*) AS printings,
                  MIN(p.image_normal) AS image, MIN(p.layout) AS layout
             FROM printings p
            WHERE p.rowid IN (SELECT rowid FROM printings_fts WHERE printings_fts MATCH ?)
            GROUP BY card_key
            ORDER BY LENGTH(MIN(p.name)), name COLLATE NOCASE
            LIMIT ?""",
        (fq, limit),
    ).fetchall()


def history(
    conn: sqlite3.Connection,
    location_id: int | None = None,
    card_key: str | None = None,
    page: int = 1,
) -> list[sqlite3.Row]:
    clauses, args = [], []
    if location_id:
        clauses.append("(m.from_location_id = ? OR m.to_location_id = ?)")
        args += [location_id, location_id]
    if card_key:
        clauses.append("(p.oracle_id = ? OR (p.oracle_id IS NULL AND p.scryfall_id = ?))")
        args += [card_key, card_key]
    where = " AND ".join(clauses) or "1"
    return conn.execute(
        f"""SELECT m.*, p.name, p.set_code, p.collector_number, p.image_normal AS image,
                   COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                   fl.name AS from_name, tl.name AS to_name,
                   EXISTS(SELECT 1 FROM movements u WHERE u.undo_of = m.batch_id) AS undone
              FROM movements m
              JOIN printings p ON p.scryfall_id = m.printing_id
              LEFT JOIN locations fl ON fl.id = m.from_location_id
              LEFT JOIN locations tl ON tl.id = m.to_location_id
             WHERE {where}
             ORDER BY m.id DESC
             LIMIT ? OFFSET ?""",
        [*args, PAGE_SIZE, (max(page, 1) - 1) * PAGE_SIZE],
    ).fetchall()
