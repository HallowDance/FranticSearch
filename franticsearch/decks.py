"""Decks and their slots. Each deck owns exactly one location of type 'deck'.

Decks are target lists: slots say which printing (+ finish) and how many copies a
deck wants. Slots match copies by printing + finish only; condition and language
don't matter for decks (slots are stored as NM/en). Derived state and the
pull/return lists are in ``deckplan``.
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


def create(
    conn: sqlite3.Connection, name: str, format_: str = "", notes: str = "", *, owner: int
) -> int:
    """New deck plus its deck location (same name; location names are unique per user)."""
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
                "INSERT INTO locations(owner_id, name, type) VALUES (?, ?, 'deck')", (owner, name)
            ).lastrowid
            return conn.execute(
                "INSERT INTO decks(name, format, location_id, notes) VALUES (?, ?, ?, ?)",
                (name, format_, loc, notes.strip()),
            ).lastrowid
    except sqlite3.IntegrityError:
        raise LedgerError(f"you already have a location or deck named {name!r}") from None


def update(conn: sqlite3.Connection, deck_id: int, name: str, format_: str, notes: str) -> None:
    """Rename / change format and notes. The deck location is renamed with the deck."""
    deck = get(conn, deck_id)
    if deck is None:
        raise LedgerError("unknown deck")
    name = (name or "").strip()
    if not name:
        raise LedgerError("deck name is required")
    if len(name) > 100:
        raise LedgerError("deck name is too long (max 100)")
    if format_ not in FORMATS:
        raise LedgerError(f"unknown format {format_!r}")
    try:
        with transaction(conn):
            conn.execute("UPDATE locations SET name = ? WHERE id = ?", (name, deck["location_id"]))
            conn.execute("UPDATE decks SET name = ?, format = ?, notes = ? WHERE id = ?",
                         (name, format_, (notes or "").strip(), deck_id))
    except sqlite3.IntegrityError:
        raise LedgerError(f"you already have a location or deck named {name!r}") from None


def add_slot(conn: sqlite3.Connection, deck_id: int, key: StockKey, board: str, qty: int,
             proxy: int = 0) -> None:
    """Want ``qty`` more of ``key`` on ``board`` (adds to an existing slot)."""
    if board not in BOARDS:
        raise LedgerError(f"unknown board {board!r}")
    if qty <= 0:
        raise LedgerError("quantity must be positive")
    if not 0 <= proxy <= qty:
        raise LedgerError("proxies must be between 0 and the quantity")
    key = StockKey(key.printing_id, key.finish)  # slots ignore condition/language
    with transaction(conn):
        validate_key(conn, key)
        conn.execute(
            "INSERT INTO deck_slots(deck_id, printing_id, finish, condition, lang, board,"
            " qty_wanted, qty_proxy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(deck_id, printing_id, finish, condition, lang, board)"
            " DO UPDATE SET qty_wanted = qty_wanted + excluded.qty_wanted,"
            "               qty_proxy = qty_proxy + excluded.qty_proxy",
            (deck_id, *key.as_tuple(), board, qty, proxy),
        )


# A slot is addressed in forms as "printing|finish|condition|lang|board".

def slot_id(row) -> str:
    return "|".join((row["printing_id"], row["finish"], row["condition"], row["lang"], row["board"]))


def get_slot(conn: sqlite3.Connection, deck_id: int, sid: str) -> sqlite3.Row | None:
    parts = (sid or "").split("|")
    if len(parts) != 5:
        return None
    return conn.execute(
        "SELECT * FROM deck_slots WHERE deck_id = ? AND printing_id = ? AND finish = ?"
        " AND condition = ? AND lang = ? AND board = ?", (deck_id, *parts),
    ).fetchone()


def _delete_slot(conn: sqlite3.Connection, slot: sqlite3.Row) -> None:
    conn.execute(
        "DELETE FROM deck_slots WHERE deck_id = ? AND printing_id = ? AND finish = ?"
        " AND condition = ? AND lang = ? AND board = ?",
        (slot["deck_id"], slot["printing_id"], slot["finish"], slot["condition"], slot["lang"],
         slot["board"]),
    )


def _check_same_card(conn: sqlite3.Connection, old: str, new: str) -> None:
    if old == new:
        return
    if not conn.execute(
        "SELECT 1 FROM printings a JOIN printings b ON a.oracle_id = b.oracle_id"
        " WHERE a.scryfall_id = ? AND b.scryfall_id = ?", (old, new)).fetchone():
        raise LedgerError("that's a printing of a different card")


def edit_slot(
    conn: sqlite3.Connection, deck_id: int, sid: str, *, qty: int, proxy: int, board: str,
    printing_id: str | None = None, finish: str | None = None,
) -> None:
    """Change a slot's quantity, proxies, board, printing or finish; qty 0 removes it.

    Moving onto a board/printing that already has a slot merges the two. Changing
    the printing leaves the old copies as excess (return list) and makes the new
    printing short (pull list).
    """
    with transaction(conn):
        slot = get_slot(conn, deck_id, sid)
        if slot is None:
            raise LedgerError("that slot doesn't exist any more")
        if qty < 0:
            raise LedgerError("quantity cannot be negative")
        if not 0 <= proxy <= max(qty, 0):
            raise LedgerError("proxies must be between 0 and the quantity")
        _delete_slot(conn, slot)
        if qty:
            key = StockKey(printing_id or slot["printing_id"], finish or slot["finish"])
            _check_same_card(conn, slot["printing_id"], key.printing_id)
            add_slot(conn, deck_id, key, board, qty, proxy)


def step_slot(conn: sqlite3.Connection, deck_id: int, sid: str, delta: int) -> dict | None:
    """Want ``delta`` (+1 / -1) more copies of a slot. At 0 the slot is removed: returns
    it as it was (for "put it back"), else None. Proxies never exceed the quantity."""
    if delta not in (1, -1):
        raise LedgerError("change one copy at a time")
    with transaction(conn):
        slot = get_slot(conn, deck_id, sid)
        if slot is None:
            raise LedgerError("that card isn't in the deck any more")
        qty = slot["qty_wanted"] + delta
        edit_slot(conn, deck_id, sid, qty=qty, proxy=min(slot["qty_proxy"], qty), board=slot["board"])
    return None if qty else dict(slot)


def remove_slot(conn: sqlite3.Connection, deck_id: int, sid: str) -> dict:
    """Take a card off the deck list (its copies in the deck become excess, i.e. the
    return list). Returns the slot as it was, for "put it back"."""
    with transaction(conn):
        slot = get_slot(conn, deck_id, sid)
        if slot is None:
            raise LedgerError("that card isn't in the deck any more")
        _delete_slot(conn, slot)
    return dict(slot)


def retarget(conn: sqlite3.Connection, deck_id: int, sid: str, key: StockKey, qty: int | None = None) -> None:
    """Switch ``qty`` copies (default all) of a slot to another printing/finish of the card."""
    with transaction(conn):
        slot = get_slot(conn, deck_id, sid)
        if slot is None:
            raise LedgerError("that slot doesn't exist any more")
        _check_same_card(conn, slot["printing_id"], key.printing_id)
        n = slot["qty_wanted"] if qty is None else min(qty, slot["qty_wanted"])
        if n <= 0:
            raise LedgerError("quantity must be positive")
        rest = slot["qty_wanted"] - n
        edit_slot(conn, deck_id, sid, qty=rest, proxy=min(slot["qty_proxy"], rest), board=slot["board"])
        moved_proxy = max(0, slot["qty_proxy"] - rest)
        add_slot(conn, deck_id, key, slot["board"], n, min(moved_proxy, n))


def commander_names(conn: sqlite3.Connection, deck_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT p.name FROM deck_slots ds JOIN printings p ON p.scryfall_id = ds.printing_id"
        " WHERE ds.deck_id = ? AND ds.board = 'commander' ORDER BY p.name", (deck_id,))]


def get(conn: sqlite3.Connection, deck_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()


# Card-type groups for the deck page, in display order. A card goes into the
# first group whose type appears in its (front face) type line.
TYPE_GROUPS = (
    ("Creature", "Creatures"), ("Planeswalker", "Planeswalkers"), ("Battle", "Battles"),
    ("Land", "Lands"), ("Instant", "Instants"), ("Sorcery", "Sorceries"),
    ("Artifact", "Artifacts"), ("Enchantment", "Enchantments"),
)
_GROUP_ORDER = ("Creatures", "Planeswalkers", "Battles", "Instants", "Sorceries",
                "Artifacts", "Enchantments", "Lands", "Other")


def type_group(type_line: str | None) -> str:
    front = (type_line or "").split(" // ")[0]
    return next((group for t, group in TYPE_GROUPS if t in front), "Other")


def grouped(slots: list, sort_key=None) -> list[tuple[str, list]]:
    """[(group name, slots)] in display order; each group sorted by ``sort_key``."""
    groups: dict[str, list] = {}
    for s in slots:
        groups.setdefault(type_group(s["type_line"]), []).append(s)
    return [(g, sorted(groups[g], key=sort_key) if sort_key else groups[g])
            for g in _GROUP_ORDER if g in groups]


UNTAGGED = "Untagged"


def grouped_by_tag(slots: list, sort_key=None) -> list[tuple[str, list]]:
    """[(tag, slots)] by tag name, "Untagged" last. A card with several tags is in
    each of their groups. Slots need a ``tags`` list (``with_tags``)."""
    groups: dict[str, list] = {}
    for s in slots:
        for t in s["tags"] or [UNTAGGED]:
            groups.setdefault(t, []).append(s)
    names = sorted((t for t in groups if t != UNTAGGED), key=str.lower)
    names += [UNTAGGED] if UNTAGGED in groups else []
    return [(t, sorted(groups[t], key=sort_key) if sort_key else groups[t]) for t in names]


# ---- tags: per card (``card_key``) within a deck, any number, case-insensitive

MAX_TAGS = 20
MAX_TAG_LEN = 40


def parse_tags(text: str) -> list[str]:
    """Comma-separated tags → cleaned list (spaces collapsed, duplicates dropped)."""
    out: list[str] = []
    for t in (text or "").split(","):
        t = " ".join(t.split())
        if not t or t.lower() in (o.lower() for o in out):
            continue
        if len(t) > MAX_TAG_LEN:
            raise LedgerError(f"tag {t[:20]!r}... is too long (max {MAX_TAG_LEN})")
        if t.lower() == UNTAGGED.lower():
            raise LedgerError(f"{UNTAGGED!r} can't be used as a tag")
        out.append(t)
    if len(out) > MAX_TAGS:
        raise LedgerError(f"at most {MAX_TAGS} tags per card")
    return out


def card_key(conn: sqlite3.Connection, printing_id: str) -> str:
    """The key tags are stored under: the card's oracle id, else the printing id."""
    row = conn.execute("SELECT COALESCE(oracle_id, scryfall_id) FROM printings WHERE scryfall_id = ?",
                       (printing_id,)).fetchone()
    return row[0] if row else printing_id


def tags(conn: sqlite3.Connection, deck_id: int) -> dict[str, list[str]]:
    """{card_key: [tags, sorted case-insensitively]} for a deck (includes cards no longer on the list)."""
    out: dict[str, list[str]] = {}
    for key, tag in conn.execute(
            "SELECT card_key, tag FROM deck_tags WHERE deck_id = ? ORDER BY tag", (deck_id,)):
        out.setdefault(key, []).append(tag)
    return out


def tag_names(conn: sqlite3.Connection, deck_id: int) -> list[str]:
    """The tags used on cards that are on the deck's list."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT t.tag FROM deck_tags t WHERE t.deck_id = ? AND EXISTS ("
        " SELECT 1 FROM deck_slots ds JOIN printings p ON p.scryfall_id = ds.printing_id"
        " WHERE ds.deck_id = t.deck_id AND COALESCE(p.oracle_id, p.scryfall_id) = t.card_key)"
        " ORDER BY t.tag", (deck_id,))]


def set_tags(conn: sqlite3.Connection, deck_id: int, card_key: str, text: str) -> list[str]:
    """Replace a card's tags in a deck with the comma-separated ``text``. A tag the
    deck already uses in another spelling ("Ramp" vs "ramp") takes that spelling."""
    new = parse_tags(text)
    with transaction(conn):
        spelling = {r[0].lower(): r[0] for r in conn.execute(
            "SELECT DISTINCT tag FROM deck_tags WHERE deck_id = ? AND card_key <> ?", (deck_id, card_key))}
        new = [spelling.get(t.lower(), t) for t in new]
        conn.execute("DELETE FROM deck_tags WHERE deck_id = ? AND card_key = ?", (deck_id, card_key))
        conn.executemany("INSERT INTO deck_tags(deck_id, card_key, tag) VALUES (?, ?, ?)",
                         [(deck_id, card_key, t) for t in new])
    return sorted(new, key=str.lower)


def with_tags(conn: sqlite3.Connection, deck_id: int, slots: list[dict]) -> list[dict]:
    """Set ``s["tags"]`` on each slot dict (sorted list, empty if none)."""
    by_card = tags(conn, deck_id)
    for s in slots:
        s["tags"] = by_card.get(s["card_key"], [])
    return slots


def mana_curve(slots: list) -> list[tuple[str, int]]:
    """Copies per mana value (0..6, "7+") over non-land cards."""
    counts = [0] * 8
    for s in slots:
        if type_group(s["type_line"]) != "Lands":
            counts[min(int(s["cmc"] or 0), 7)] += s["qty_wanted"]
    return [(str(i) if i < 7 else "7+", n) for i, n in enumerate(counts)]


def by_location(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM decks WHERE location_id = ?", (location_id,)).fetchone()


def list_all(conn: sqlite3.Connection, owner: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT d.*, l.visibility,
                  (SELECT COALESCE(SUM(qty_wanted), 0) FROM deck_slots
                    WHERE deck_id = d.id AND board <> 'maybe') AS cards,
                  (SELECT COUNT(*) FROM import_unresolved u JOIN imports i ON i.id = u.import_id
                    WHERE i.deck_id = d.id AND u.status = 'open') AS unresolved
             FROM decks d JOIN locations l ON l.id = d.location_id
            WHERE l.owner_id = ? ORDER BY d.name COLLATE NOCASE""",
        (owner,),
    ).fetchall()

# Per-slot copy counts for ``slots()`` (correlated subqueries: the slow part).

_SLOT_COUNTS = """,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s
                    WHERE s.location_id = d.location_id AND s.printing_id = ds.printing_id
                      AND s.finish = ds.finish
                  ) AS in_deck,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s JOIN locations l ON l.id = s.location_id
                    WHERE l.type <> 'deck' AND l.owner_id = dl.owner_id AND s.printing_id = ds.printing_id
                      AND s.finish = ds.finish
                  ) AS free,
                  (SELECT GROUP_CONCAT(l.name, ', ') FROM stock s JOIN locations l ON l.id = s.location_id
                    WHERE l.type <> 'deck' AND l.owner_id = dl.owner_id AND s.printing_id = ds.printing_id
                      AND s.finish = ds.finish
                  ) AS free_where,
                  (SELECT COALESCE(SUM(s.qty), 0) FROM stock s JOIN printings p2 ON p2.scryfall_id = s.printing_id
                     JOIN locations l ON l.id = s.location_id
                    WHERE p2.oracle_id = p.oracle_id AND l.owner_id = dl.owner_id
                      AND l.id <> d.location_id) AS owned_any"""


def slots(conn: sqlite3.Connection, deck_id: int, *, counts: bool = True) -> list[sqlite3.Row]:
    """Slots with printing info and where matching copies are.

    ``in_deck`` = copies of the printing + finish at the deck's location (shared by
    slots of the same printing on several boards; ``deckplan.state`` splits it);
    ``free`` = the deck owner's copies of the printing + finish outside any deck;
    ``owned_any`` = the owner's copies of any printing of the card outside this deck.

    ``counts=False`` leaves out those four columns (subqueries per slot): the pull
    list, buy list and Decks page compute state for every deck and don't need them.
    """
    return conn.execute(
        f"""SELECT ds.*, p.name, p.set_code, p.set_name, p.collector_number, p.type_line, p.mana_cost, p.cmc,
                  p.image_normal AS image, p.oracle_id, p.colors, p.color_identity,
                  p.legalities, p.oracle_text,
                  COALESCE(p.oracle_id, p.scryfall_id) AS card_key
                  {_SLOT_COUNTS if counts else ""}
             FROM deck_slots ds
             JOIN decks d ON d.id = ds.deck_id
             JOIN locations dl ON dl.id = d.location_id
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
