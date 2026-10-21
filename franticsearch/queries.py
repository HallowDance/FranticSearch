"""Read-only queries behind the collection browser, card pages, search and history."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, fields

from . import cardtypes
from .decks import FORMATS

PAGE_SIZE = 100

# Sortable columns: key -> (primary ORDER BY term, tie-breaker). "-key" sorts descending.
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


def totals(conn: sqlite3.Connection, owner: int | None) -> sqlite3.Row:
    """Counts for one user's collection (``owner`` None = the whole instance)."""
    # Separate SQL per case: "? IS NULL OR owner_id = ?" would keep SQLite off the indexes.
    def mine(alias: str) -> str:
        return f"{alias}.owner_id = :o" if owner is not None else "1"
    return conn.execute(
        f"""SELECT (SELECT COALESCE(SUM(s.qty), 0) FROM stock s JOIN locations l ON l.id = s.location_id
                     WHERE {mine('l')}) AS copies,
                   (SELECT COUNT(DISTINCT p.oracle_id) FROM stock s
                      JOIN printings p ON p.scryfall_id = s.printing_id
                      JOIN locations l ON l.id = s.location_id
                     WHERE {mine('l')}) AS cards,
                   (SELECT COUNT(*) FROM printings) AS printings,
                   (SELECT COUNT(*) FROM locations l WHERE {mine('l')} AND l.type <> 'deck'
                       AND l.archived = 0) AS locations,
                   (SELECT COUNT(*) FROM movements m WHERE {mine('m')}) AS movements""",
        {"o": owner},
    ).fetchone()


def card_printings(conn: sqlite3.Connection, card_key: str) -> list[sqlite3.Row]:
    """All printings of a card (by oracle id, or scryfall id for oracle-less objects)."""
    return conn.execute(
        """SELECT * FROM printings
            WHERE oracle_id = ? OR (oracle_id IS NULL AND scryfall_id = ?)
            ORDER BY released_at DESC, set_code, collector_number""",
        (card_key, card_key),
    ).fetchall()


def shared_stock(conn: sqlite3.Connection, card_key: str, viewer: int) -> list[sqlite3.Row]:
    """Copies of a card in other users' shared locations."""
    return conn.execute(
        """SELECT s.*, l.name AS location_name, l.type AS location_type, u.username AS owner_name,
                  p.set_code, p.collector_number, p.name, p.image_normal AS image
             FROM stock s
             JOIN printings p ON p.scryfall_id = s.printing_id
             JOIN locations l ON l.id = s.location_id
             JOIN users u ON u.id = l.owner_id
            WHERE (p.oracle_id = ? OR (p.oracle_id IS NULL AND p.scryfall_id = ?))
              AND l.visibility IN ('users', 'link') AND l.archived = 0 AND l.owner_id <> ?
              AND u.disabled = 0
            ORDER BY u.username COLLATE NOCASE, l.name COLLATE NOCASE""",
        (card_key, card_key, viewer),
    ).fetchall()


def card_stock(conn: sqlite3.Connection, card_key: str, owner: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.*, l.name AS location_name, l.type AS location_type,
                  p.set_code, p.set_name, p.collector_number, p.name, p.image_normal AS image
             FROM stock s
             JOIN printings p ON p.scryfall_id = s.printing_id
             JOIN locations l ON l.id = s.location_id
            WHERE (p.oracle_id = ? OR (p.oracle_id IS NULL AND p.scryfall_id = ?)) AND l.owner_id = ?
            ORDER BY p.released_at DESC, p.set_code, p.collector_number, s.finish,
                     l.name COLLATE NOCASE""",
        (card_key, card_key, owner),
    ).fetchall()


def location_stock(conn: sqlite3.Connection, location_id: int, sort: str = "name") -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT s.*, p.name, p.set_code, p.set_name, p.collector_number, p.type_line,
                   p.image_normal AS image, p.mana_cost, p.cmc, p.colors,
                   COALESCE(p.oracle_id, p.scryfall_id) AS card_key
              FROM stock s JOIN printings p ON p.scryfall_id = s.printing_id
             WHERE s.location_id = ?
             ORDER BY {order_by(LOCATION_SORTS, sort)}""",
        (location_id,),
    ).fetchall()


COLORS = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green",
          "C": "Colourless", "M": "Multicolour"}
MANA_VALUES = ("0", "1", "2", "3", "4", "5", "6", "7+")


@dataclass
class StockFilter:
    """Filters for a location's card list (applied in Python: a location is small)."""
    q: str = ""          # name contains
    type: str = ""       # card type or supertype (Creature, Legendary, ...)
    subtype: str = ""    # Elf, Equipment, Time Lord, ...
    color: str = ""      # W U B R G, C = colourless, M = multicolour
    mv: str = ""         # "0".."6", "7+"
    set: str = ""        # set code
    finish: str = ""
    min_qty: int = 0

    @classmethod
    def from_args(cls, args) -> "StockFilter":
        """From request args: the text fields by name, plus ``min_qty``. Subclasses read
        their other fields themselves."""
        f = cls(**{fl.name: (args.get(fl.name) or "").strip() for fl in fields(cls) if fl.type == "str"})
        try:
            f.min_qty = max(0, int(args.get("min_qty") or 0))
        except ValueError:
            f.min_qty = 0
        return f

    def active(self) -> bool:
        return any((self.q, self.type, self.subtype, self.color, self.mv, self.set, self.finish,
                    self.min_qty > 1))

    def matches(self, row) -> bool:
        return self.matches_card(row) and row["qty"] >= self.min_qty

    def matches_card(self, row) -> bool:
        """Everything but the quantity: name, types, colour, MV, set, finish."""
        if self.q and self.q.lower() not in row["name"].lower():
            return False
        if self.type or self.subtype:
            supers, types, subs = cardtypes.parse(row["type_line"])
            if self.type and self.type not in supers | types:
                return False
            if self.subtype and self.subtype not in subs:
                return False
        if self.color:
            colors = json.loads(row["colors"] or "[]")
            if self.color == "C" and colors or self.color == "M" and len(colors) < 2:
                return False
            if self.color in "WUBRG" and self.color not in colors:
                return False
        if self.mv:
            mv = int(row["cmc"] or 0)
            if (self.mv == "7+" and mv < 7) or (self.mv != "7+" and str(mv) != self.mv):
                return False
        if self.set and row["set_code"] != self.set.lower():
            return False
        if self.finish and row["finish"] != self.finish:
            return False
        return True


def stock_facets(rows) -> dict:
    """Choices for the filter dropdowns: only what occurs in ``rows``."""
    supers, types, subs, sets, finishes = set(), set(), set(), {}, set()
    for r in rows:
        sp, ty, sb = cardtypes.parse(r["type_line"])
        supers |= sp
        types |= ty
        subs |= sb
        sets[r["set_code"]] = r["set_name"]
        finishes.add(r["finish"])
    return {
        "types": sorted(types), "supertypes": sorted(supers), "subtypes": sorted(subs),
        "sets": sorted(sets.items(), key=lambda kv: kv[1].lower()), "finishes": sorted(finishes),
    }


@dataclass
class CollectionFilter(StockFilter):
    """The collection page: the location filters, per copy, plus where the copies are.
    A card is listed if any of its copies match; ``min_qty`` applies to its matching
    copies in total. Param names: ``location``, ``ltype``, ``notindeck``, ``page``, ``sort``."""
    location_id: int | None = None
    location_type: str = ""
    not_in_deck: bool = False
    page: int = 1
    sort: str = "name"

    @classmethod
    def from_args(cls, args) -> "CollectionFilter":
        f = super().from_args(args)
        f.location_type = (args.get("ltype") or "").strip()
        f.not_in_deck = args.get("notindeck") == "1"
        f.sort = args.get("sort") or "name"
        for name, attr, minimum in (("location", "location_id", None), ("page", "page", 1)):
            try:
                value = int(args.get(name) or "")
            except ValueError:
                continue
            setattr(f, attr, value if minimum is None else max(minimum, value))
        return f

    def active(self) -> bool:
        return super().active() or bool(self.location_id or self.location_type or self.not_in_deck)

    def matches(self, row) -> bool:   # one copy (stock line); min_qty is per card, see collection()
        if self.location_id and row["location_id"] != self.location_id:
            return False
        if self.location_type and row["location_type"] != self.location_type:
            return False
        if self.not_in_deck and row["location_type"] == "deck":
            return False
        return self.matches_card(row)


COLLECTION_KEYS = {   # sort key -> (primary, tie-breaker) over grouped cards
    "name": (lambda c: c["name"].lower(), lambda c: c["card_key"]),
    "cost": (lambda c: -1 if c["cmc"] is None else c["cmc"], lambda c: c["name"].lower()),
    "type": (lambda c: (c["type_line"] or "").lower(), lambda c: c["name"].lower()),
    "qty": (lambda c: c["qty"], lambda c: c["name"].lower()),
    "printings": (lambda c: c["printings"], lambda c: c["name"].lower()),
}


def collection_lines(conn: sqlite3.Connection, owner: int) -> list[sqlite3.Row]:
    """All of a user's stock lines with card and location fields, newest printings first."""
    return conn.execute(
        """SELECT s.*, p.name, p.set_code, p.set_name, p.collector_number, p.type_line, p.mana_cost,
                  p.cmc, p.colors, p.image_normal AS image, COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                  l.name AS location_name, l.type AS location_type
             FROM stock s JOIN printings p ON p.scryfall_id = s.printing_id
             JOIN locations l ON l.id = s.location_id
            WHERE l.owner_id = ?
            ORDER BY p.released_at DESC""",
        (owner,),
    ).fetchall()


def collection(
    conn: sqlite3.Connection, f: CollectionFilter, owner: int
) -> tuple[list[dict], int, list[sqlite3.Row]]:
    """A user's cards grouped by card (oracle id), filtered and sorted, one page.

    Filtering happens in Python, like a location's (a collection is thousands of lines,
    not millions), so types and subtypes match exactly and the filter dropdowns can list
    what occurs. Returns (page of cards, matching cards, all lines for the facets)."""
    lines = collection_lines(conn, owner)
    cards: dict[str, dict] = {}
    for r in lines:
        if not f.matches(r):
            continue
        c = cards.get(r["card_key"])
        if c is None:   # card fields are the same for every printing; the image is the newest one
            c = cards[r["card_key"]] = {"card_key": r["card_key"], "name": r["name"], "type_line": r["type_line"],
                                        "mana_cost": r["mana_cost"], "cmc": r["cmc"], "image": r["image"],
                                        "qty": 0, "_printings": set(), "_where": set()}
        c["qty"] += r["qty"]
        c["_printings"].add(r["printing_id"])
        c["_where"].add(("Decks / " if r["location_type"] == "deck" else "") + r["location_name"])
    rows = []
    for c in cards.values():
        if c["qty"] < max(f.min_qty, 1):
            continue
        c["printings"] = len(c.pop("_printings"))
        c["locations"] = sorted(c.pop("_where"), key=str.lower)
        rows.append(c)
    key = f.sort.lstrip("-") if f.sort.lstrip("-") in COLLECTION_KEYS else "name"
    primary, tie = COLLECTION_KEYS[key]
    rows.sort(key=tie)
    rows.sort(key=primary, reverse=f.sort.startswith("-") and key == f.sort.lstrip("-"))
    start = (max(f.page, 1) - 1) * PAGE_SIZE
    return rows[start:start + PAGE_SIZE], len(rows), lines


DECK_STATUSES = {"short": "short", "proxied": "with proxies", "complete": "complete"}


@dataclass
class DeckFilter(StockFilter):
    """A deck's card list: the location filters (no qty) plus board and status. Stats
    and warnings on the deck page still describe the whole deck."""
    board: str = ""
    status: str = ""   # DECK_STATUSES; owner views only (it reveals what's in the deck)

    def active(self) -> bool:
        return super().active() or bool(self.board or self.status)

    def matches(self, slot) -> bool:
        if self.board and slot["board"] != self.board:
            return False
        counts = slot["board"] != "maybe"
        if self.status == "short" and not slot["short"]:
            return False
        if self.status == "proxied" and not slot["qty_proxy"]:
            return False
        if self.status == "complete" and (not counts or slot["short"]):
            return False
        return self.matches_card(slot)


# --------------------------------------------------------------------------- card search (phase 3)

SEARCH_PAGE = 50
COLOR_FIELDS = {"colors": "colour", "identity": "colour identity"}
COLOR_MODES = {"include": "including", "exact": "exactly", "atmost": "at most"}
MV_OPS = {"=": "=", "<=": "≤", ">=": "≥"}
SEARCH_SORTS = {
    "name": ("MIN(p.name) COLLATE NOCASE", "card_key"),
    "cost": ("MIN(p.cmc)", "MIN(p.name) COLLATE NOCASE"),
    "owned": ("COALESCE(SUM(o.owned), 0)", "MIN(p.name) COLLATE NOCASE"),
}


def _fts_words(column: str, text: str) -> list[str]:
    return [f'{column}:"{w}"*' for w in re.findall(r"\w+", text.lower())]


@dataclass
class CardSearch:
    """Structured search over ``printings`` (PLAN.md § 5). Empty = no search."""
    q: str = ""                 # name words (prefix); falls back to substring
    type: str = ""              # words that must all appear in the type line
    text: str = ""              # words that must all appear in the rules text
    colors: str = ""            # letters from WUBRG, or "C" = colourless
    color_mode: str = "include"
    color_field: str = "colors"
    mv_op: str = "="
    mv: str = ""
    set: str = ""
    format: str = ""            # legal (or restricted) in this format
    owned: str = ""             # "" | "owned" | "free" (owned outside decks)
    sort: str = ""
    page: int = 1

    @classmethod
    def from_args(cls, args) -> "CardSearch":
        f = cls()
        for fl in fields(cls):
            if fl.name == "page":
                try:
                    f.page = max(1, int(args.get("page") or 1))
                except ValueError:
                    pass
            elif fl.name == "colors":
                f.colors = "".join(c for c in "WUBRGC" if c in args.getlist("c"))
            elif args.get(fl.name):
                setattr(f, fl.name, args.get(fl.name).strip())
        return f

    def active(self) -> bool:
        return any((self.q, self.type, self.text, self.colors, self.mv, self.set, self.format,
                    self.owned))

    def where(self, substring_names: bool = False) -> tuple[str, list]:
        clauses = ["p.digital = 0", "p.layout <> 'art_series'"]
        args: list = []
        if "token" not in self.type.lower():
            clauses.append("p.layout NOT IN ('token', 'double_faced_token', 'emblem')")
        match = _fts_words("oracle_text", self.text)
        if self.q and not substring_names:
            match += _fts_words("name", self.q)
        elif self.q:
            clauses.append("p.name LIKE ?")
            args.append(f"%{self.q}%")
        if match:
            clauses.append("p.rowid IN (SELECT rowid FROM printings_fts WHERE printings_fts MATCH ?)")
            args.append(" AND ".join(match))
        for word in self.type.split():
            clauses.append("p.type_line LIKE ?")
            args.append(f"%{word}%")
        col = "p.color_identity" if self.color_field == "identity" else "p.colors"
        if "C" in self.colors:
            clauses.append(f"{col} = '[]'")
        elif self.colors:
            for c in "WUBRG":
                if c in self.colors and self.color_mode in ("include", "exact"):
                    clauses.append(f"{col} LIKE '%\"{c}\"%'")
                if c not in self.colors and self.color_mode in ("exact", "atmost"):
                    clauses.append(f"{col} NOT LIKE '%\"{c}\"%'")
        if self.mv:
            try:
                mv = float(self.mv)
                clauses.append(f"COALESCE(p.cmc, 0) {self.mv_op if self.mv_op in MV_OPS else '='} ?")
                args.append(mv)
            except ValueError:
                pass
        if self.set:
            clauses.append("p.set_code = ?")
            args.append(self.set.lower())
        if self.format in FORMATS and self.format:
            clauses.append(f"json_extract(p.legalities, '$.{self.format}') IN ('legal', 'restricted')")
        if self.owned == "owned":
            clauses.append("o.owned > 0")
        elif self.owned == "free":
            clauses.append("o.free > 0")
        return " AND ".join(clauses), args


_OWNED_SQL = """LEFT JOIN (
    SELECT s.printing_id, SUM(s.qty) AS owned,
           SUM(CASE WHEN l.type <> 'deck' THEN s.qty ELSE 0 END) AS free
      FROM stock s JOIN locations l ON l.id = s.location_id
     WHERE l.owner_id = ? GROUP BY s.printing_id
) o ON o.printing_id = p.scryfall_id"""


def search(conn: sqlite3.Connection, f: CardSearch, owner: int) -> tuple[list[dict], int]:
    """Cards matching ``f`` (one page), each with its matching printings and the
    user's owned/free counts. Returns (cards, total matching cards)."""
    if not f.active():
        return [], 0

    def run(substring: bool):
        where, args = f.where(substring)
        base = f"FROM printings p {_OWNED_SQL} WHERE {where}"
        total = conn.execute(
            f"SELECT COUNT(DISTINCT COALESCE(p.oracle_id, p.scryfall_id)) {base}", [owner, *args]
        ).fetchone()[0]
        return base, args, total

    base, args, total = run(False)
    if not total and f.q:                     # "fuzzy": no prefix match, try a substring
        base, args, total = run(True)
    if not total:
        return [], 0
    order = (order_by(SEARCH_SORTS, f.sort) if f.sort.lstrip("-") in SEARCH_SORTS
             else "LENGTH(MIN(p.name)), MIN(p.name) COLLATE NOCASE" if f.q
             else order_by(SEARCH_SORTS, "name"))
    cards = [dict(r) for r in conn.execute(
        f"""SELECT COALESCE(p.oracle_id, p.scryfall_id) AS card_key, MIN(p.name) AS name,
                   MIN(p.type_line) AS type_line, MIN(p.mana_cost) AS mana_cost, MIN(p.cmc) AS cmc,
                   MIN(p.image_normal) AS image, COUNT(*) AS n_printings,
                   COALESCE(SUM(o.owned), 0) AS owned, COALESCE(SUM(o.free), 0) AS free
            {base} GROUP BY card_key ORDER BY {order} LIMIT ? OFFSET ?""",
        [owner, *args, SEARCH_PAGE, (f.page - 1) * SEARCH_PAGE],
    )]
    keys = [c["card_key"] for c in cards]
    by_key: dict[str, list] = {k: [] for k in keys}
    for r in conn.execute(
        f"""SELECT p.*, COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                   COALESCE(o.owned, 0) AS owned, COALESCE(o.free, 0) AS free
            {base} AND COALESCE(p.oracle_id, p.scryfall_id) IN ({','.join('?' * len(keys))})
            ORDER BY o.owned IS NULL, p.released_at DESC, p.set_code, p.collector_number""",
        [owner, *args, *keys],
    ):
        by_key[r["card_key"]].append(r)
    for c in cards:
        c["printings"] = by_key[c["card_key"]]
    return cards, total


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
    owner: int,
    location_id: int | None = None,
    card_key: str | None = None,
    page: int = 1,
) -> list[sqlite3.Row]:
    """A user's movements (both ends of a move are always in the same collection)."""
    clauses = ["m.owner_id = ?"]
    args: list = [owner]
    if location_id:
        clauses.append("(m.from_location_id = ? OR m.to_location_id = ?)")
        args += [location_id, location_id]
    if card_key:
        clauses.append("(p.oracle_id = ? OR (p.oracle_id IS NULL AND p.scryfall_id = ?))")
        args += [card_key, card_key]
    where = " AND ".join(clauses)
    return conn.execute(
        f"""SELECT m.*, p.name, p.set_code, p.collector_number, p.image_normal AS image,
                   COALESCE(p.oracle_id, p.scryfall_id) AS card_key,
                   fl.name AS from_name, tl.name AS to_name, fl.type AS from_type, tl.type AS to_type,
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
