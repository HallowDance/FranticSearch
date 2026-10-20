"""Derived deck state, pull and return lists, "mark as built", buy list, deck checks.

PLAN.md § Data model ("Derived deck state"), § Core workflows 1–3, 5, § Phase 4
as built, § Phases 5 & 6 as built. Nothing
here is stored: it's all computed from slots + stock. The confirm functions hand
the moves to the ledger as one batch, recomputing the plan first, so a stale
page can't move more than the plan allows.

Slots match copies by printing + finish (condition and language don't matter).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import decks, ledger, locations
from .db import transaction
from .ledger import LedgerError, StockKey

# Commander first: if copies are short, the commander gets them before the main deck.
COUNTING = ("commander", "main", "side")   # maybe-board slots never count
# Cards a deck must have (main + commander) in formats with a fixed size.
DECK_SIZE = {"commander": 100, "duel": 100, "predh": 100, "paupercommander": 100, "brawl": 100,
             "standardbrawl": 60, "oathbreaker": 60}
COLOR_NAMES = {"W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green", "C": "Colourless"}


def _pf(row) -> tuple[str, str]:
    return (row["printing_id"], row["finish"])


def _key(row) -> StockKey:
    return StockKey(row["printing_id"], row["finish"], row["condition"], row["lang"])


def deck_stock(conn: sqlite3.Connection, location_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.*, p.name, p.set_code, p.collector_number, p.image_normal AS image,
                  COALESCE(p.oracle_id, p.scryfall_id) AS card_key
             FROM stock s JOIN printings p ON p.scryfall_id = s.printing_id
            WHERE s.location_id = ?
            ORDER BY p.name COLLATE NOCASE, s.condition = 'NM', s.lang = 'en'""",
        (location_id,),
    ).fetchall()


def state(conn: sqlite3.Connection, deck: sqlite3.Row) -> dict:
    """Per slot ``in_deck`` / ``short``, plus the deck's excess stock lines.

    Copies of a printing + finish at the deck location are shared out over its
    slots in board order (commander, main, side). ``short`` = wanted − in deck −
    proxies. Excess = copies nobody wants (including maybe-board cards).
    """
    slots = [dict(r) for r in decks.slots(conn, deck["id"])]
    stock = deck_stock(conn, deck["location_id"])
    left: dict[tuple[str, str], int] = {}
    for r in stock:
        left[_pf(r)] = left.get(_pf(r), 0) + r["qty"]
    for s in sorted(slots, key=lambda s: COUNTING.index(s["board"]) if s["board"] in COUNTING else 9):
        s["sid"] = decks.slot_id(s)
        if s["board"] not in COUNTING:
            s["in_deck"] = s["short"] = 0
            continue
        n = min(left.get(_pf(s), 0), s["qty_wanted"])
        left[_pf(s)] = left.get(_pf(s), 0) - n
        s["in_deck"] = n
        s["short"] = max(0, s["qty_wanted"] - n - s["qty_proxy"])
    excess = []
    for r in stock:   # NM/English copies come last, so worn/foreign copies go back first
        n = min(left.get(_pf(r), 0), r["qty"])
        if n > 0:
            excess.append({**dict(r), "qty": n, "key": _key(r)})
            left[_pf(r)] -= n
    counting = [s for s in slots if s["board"] in COUNTING]
    return dict(
        slots=slots, excess=excess,
        short=sum(s["short"] for s in counting),
        excess_total=sum(e["qty"] for e in excess),
        proxies=sum(s["qty_proxy"] for s in counting),
        in_deck=sum(s["in_deck"] for s in counting),
        wanted=sum(s["qty_wanted"] for s in counting),
    )


def status(st: dict) -> str:
    parts = [f"{st['short']} short"] if st["short"] else []
    if st["excess_total"]:
        parts.append(f"{st['excess_total']} excess")
    return ", ".join(parts) or "complete"


# --------------------------------------------------------------------------- pull list

@dataclass
class PullLine:
    deck_id: int
    deck_name: str
    to_id: int           # the deck's location
    from_id: int
    from_name: str
    key: StockKey
    qty: int
    name: str
    set_code: str
    collector_number: str
    image: str | None
    card_key: str

    @property
    def id(self) -> str:
        return f"{self.deck_id}:{self.from_id}:" + "|".join(self.key.as_tuple())


@dataclass
class Shortfall:
    """Copies of a slot that no free copy of the exact printing + finish can fill."""
    deck_id: int
    deck_name: str
    slot: dict
    qty: int
    alternatives: list[dict] = field(default_factory=list)   # other free printings of the card
    in_other_decks: list[str] = field(default_factory=list)


def _free_pool(conn: sqlite3.Connection, owner: int) -> list[dict]:
    """The owner's copies outside decks, most copies (per location) first."""
    return [dict(r) for r in conn.execute(
        """SELECT s.*, l.name AS location_name, p.oracle_id, p.set_code, p.collector_number
             FROM stock s JOIN locations l ON l.id = s.location_id
             JOIN printings p ON p.scryfall_id = s.printing_id
            WHERE l.owner_id = ? AND l.type <> 'deck'
            ORDER BY s.qty DESC, l.name COLLATE NOCASE, s.condition = 'NM' DESC""",
        (owner,))]


def _owner_decks(conn: sqlite3.Connection, owner: int, deck_ids) -> list[sqlite3.Row]:
    rows = decks.list_all(conn, owner)
    return [d for d in rows if d["id"] in deck_ids] if deck_ids is not None else rows


def pull_plan(
    conn: sqlite3.Connection, owner: int, deck_ids=None
) -> tuple[list[PullLine], list[Shortfall]]:
    """Moves that fill short slots from the owner's non-deck locations.

    ``deck_ids`` None = all the owner's decks; a free copy is planned for one deck
    only. What can't be filled becomes a ``Shortfall``, with the other printings of
    the card that are free (for "retarget slot to this printing").
    """
    pool = _free_pool(conn, owner)
    by_pf: dict[tuple[str, str], list[dict]] = {}
    for p in pool:
        by_pf.setdefault(_pf(p), []).append(p)
    lines: dict[str, PullLine] = {}
    shortfalls: list[Shortfall] = []
    for deck in _owner_decks(conn, owner, deck_ids):
        for s in state(conn, deck)["slots"]:
            need = s["short"]
            for src in by_pf.get(_pf(s), []) if need else []:
                n = min(need, src["qty"])
                if not n:
                    continue
                src["qty"] -= n
                need -= n
                line = PullLine(deck["id"], deck["name"], deck["location_id"], src["location_id"],
                                src["location_name"], _key(src), n, s["name"], s["set_code"],
                                s["collector_number"], s["image"], s["card_key"])
                if line.id in lines:
                    lines[line.id].qty += n
                else:
                    lines[line.id] = line
                if not need:
                    break
            if need:
                shortfalls.append(Shortfall(deck["id"], deck["name"], s, need))
    for sf in shortfalls:   # what's still free after the exact pulls
        alts: dict[tuple[str, str], dict] = {}
        for p in pool:
            if p["qty"] and p["oracle_id"] == sf.slot["oracle_id"] and _pf(p) != _pf(sf.slot):
                a = alts.setdefault(_pf(p), {"printing_id": p["printing_id"], "finish": p["finish"],
                                             "set_code": p["set_code"], "collector_number": p["collector_number"],
                                             "qty": 0, "where": []})
                a["qty"] += p["qty"]
                if p["location_name"] not in a["where"]:
                    a["where"].append(p["location_name"])
        sf.alternatives = sorted(alts.values(), key=lambda a: -a["qty"])
        sf.in_other_decks = [r[0] for r in conn.execute(
            """SELECT DISTINCT l.name FROM stock s JOIN locations l ON l.id = s.location_id
                WHERE l.owner_id = ? AND l.type = 'deck' AND l.id <> ?
                  AND s.printing_id = ? AND s.finish = ? ORDER BY l.name""",
            (owner, _deck_location(conn, sf.deck_id),
             sf.slot["printing_id"], sf.slot["finish"]))]
    return list(lines.values()), shortfalls


def _deck_location(conn: sqlite3.Connection, deck_id: int) -> int:
    return conn.execute("SELECT location_id FROM decks WHERE id = ?", (deck_id,)).fetchone()[0]


def confirm_pulls(
    conn: sqlite3.Connection, owner: int, selected=None, deck_ids=None, note: str = "pull list"
) -> tuple[str, int]:
    """Do the planned pulls whose ids are in ``selected`` (None = all) as one batch."""
    with transaction(conn):
        lines, _ = pull_plan(conn, owner, deck_ids)
        chosen = [l for l in lines if selected is None or l.id in selected]
        if not chosen:
            raise LedgerError("nothing to pull (the list may have changed; reload it)")
        batch = ledger.transfer_many(conn, [(l.from_id, l.to_id, l.key, l.qty) for l in chosen], note)
    return batch, sum(l.qty for l in chosen)


# --------------------------------------------------------------------------- return list

@dataclass
class ReturnLine:
    key: StockKey
    qty: int
    name: str
    set_code: str
    collector_number: str
    image: str | None
    card_key: str
    default_to: int | None   # where it was last pulled from; None = Unsorted

    @property
    def id(self) -> str:
        return "|".join(self.key.as_tuple())


def _last_source(conn: sqlite3.Connection, deck_location: int, key: StockKey) -> int | None:
    """The non-deck location this printing last came into the deck from (still usable)."""
    row = conn.execute(
        """SELECT m.from_location_id FROM movements m JOIN locations l ON l.id = m.from_location_id
            WHERE m.to_location_id = ? AND m.printing_id = ? AND m.finish = ?
              AND l.type <> 'deck' AND l.archived = 0
            ORDER BY (m.condition = ? AND m.lang = ?) DESC, m.id DESC LIMIT 1""",
        (deck_location, key.printing_id, key.finish, key.condition, key.lang),
    ).fetchone()
    return row[0] if row else None


def return_plan(conn: sqlite3.Connection, deck: sqlite3.Row) -> list[ReturnLine]:
    return [ReturnLine(e["key"], e["qty"], e["name"], e["set_code"], e["collector_number"],
                       e["image"], e["card_key"], _last_source(conn, deck["location_id"], e["key"]))
            for e in state(conn, deck)["excess"]]


def confirm_returns(
    conn: sqlite3.Connection, deck: sqlite3.Row, choices: dict, *, owner: int,
    note: str = "return list",
) -> tuple[str, int]:
    """Return excess lines: ``choices`` maps line id → destination location id or
    "unsorted". Lines not in the current plan are ignored."""
    with transaction(conn):
        items = []
        for line in return_plan(conn, deck):
            if line.id not in choices:
                continue
            dest = choices[line.id]
            if dest == "unsorted":
                dest = locations.unsorted(conn, owner=owner)
            loc = locations.get(conn, dest) if isinstance(dest, int) else None
            if loc is None or loc["owner_id"] != owner or loc["type"] == "deck":
                raise LedgerError("choose one of your (non-deck) locations")
            items.append((deck["location_id"], dest, line.key, line.qty))
        if not items:
            raise LedgerError("nothing to return (the list may have changed; reload it)")
        batch = ledger.transfer_many(conn, items, note)
    return batch, sum(i[3] for i in items)


# --------------------------------------------------------------------------- mark as built

def mark_built(conn: sqlite3.Connection, deck: sqlite3.Row) -> tuple[str | None, int]:
    """"I own this deck": add every card it's still short to the collection as new
    copies, directly in the deck location (one ``acquire`` batch, undoable).

    Copies already in the deck count; proxied copies and the maybeboard are not
    acquired. Free copies elsewhere are left alone (the pull list moves those).
    Returns (batch or None, copies added).
    """
    with transaction(conn):
        wanted: dict[StockKey, int] = {}
        for s in state(conn, deck)["slots"]:
            if s["short"]:
                key = StockKey(s["printing_id"], s["finish"])
                wanted[key] = wanted.get(key, 0) + s["short"]
        if not wanted:
            return None, 0
        batch = ledger.acquire_many(conn, deck["location_id"], wanted.items(), note="mark as built",
                                    reason="acquire")
    return batch, sum(wanted.values())


# --------------------------------------------------------------------------- stats & checks

_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10}
_UP_TO = re.compile(r"a deck can have up to (\w+) cards named", re.I)


def _copy_limit(s: dict, singleton: bool) -> int | None:
    """Max copies of a card in a deck; None = no limit (basic lands, Relentless Rats...)."""
    if "Basic" in (s["type_line"] or "").split(" // ")[0]:
        return None
    text = s["oracle_text"] or ""
    if "deck can have any number of cards named" in text.lower():
        return None
    m = _UP_TO.search(text)
    if m:
        return _NUMBERS.get(m[1].lower(), int(m[1]) if m[1].isdigit() else None)
    return 1 if singleton else 4


def analysis(st: dict, deck: sqlite3.Row) -> dict:
    """Counts per board, colour breakdown and format warnings (legality, copies, size,
    commander colour identity)."""
    slots = st["slots"]
    counts = {b: sum(s["qty_wanted"] for s in slots if s["board"] == b) for b in decks.BOARDS}
    colours = {c: 0 for c in COLOR_NAMES}
    for s in slots:
        if s["board"] in ("main", "commander") and decks.type_group(s["type_line"]) != "Lands":
            cs = json.loads(s["colors"] or "[]") or ["C"]
            for c in cs:
                if c in colours:
                    colours[c] += s["qty_wanted"]
    warnings: list[str] = []
    fmt = deck["format"]
    counting = [s for s in slots if s["board"] in COUNTING]
    if fmt:
        per_card: dict[str, dict] = {}
        for s in counting:
            c = per_card.setdefault(s["card_key"], {"slot": s, "qty": 0})
            c["qty"] += s["qty_wanted"]
        singleton = fmt in decks.COMMANDER_FORMATS
        for c in sorted(per_card.values(), key=lambda c: c["slot"]["name"]):
            s = c["slot"]
            legal = json.loads(s["legalities"] or "{}").get(fmt, "not_legal")
            if legal in ("banned", "not_legal"):
                warnings.append(f"{s['name']} is {legal.replace('_', ' ')} in {fmt}.")
            limit = 1 if legal == "restricted" else _copy_limit(s, singleton)
            if limit is not None and c["qty"] > limit:
                warnings.append(f"{c['qty']} copies of {s['name']} (at most {limit}).")
        size = DECK_SIZE.get(fmt)
        total = counts["main"] + counts["commander"]
        if size and total != size:
            warnings.append(f"{total} cards in main + commander; {fmt} decks have {size}.")
        if not size and counts["main"] < 60:
            warnings.append(f"Main deck has {counts['main']} cards (at least 60).")
        if not size and counts["side"] > 15:
            warnings.append(f"Sideboard has {counts['side']} cards (at most 15).")
        if singleton:
            commanders = [s for s in slots if s["board"] == "commander"]
            if not commanders:
                warnings.append("No commander.")
            else:
                identity = set().union(*(json.loads(s["color_identity"] or "[]") for s in commanders))
                shown = "".join(c for c in "WUBRG" if c in identity) or "colourless"
                for s in counting:
                    if s["board"] != "commander" and not set(json.loads(s["color_identity"] or "[]")) <= identity:
                        warnings.append(f"{s['name']} is outside the commander's colour identity ({shown}).")
    return dict(counts=counts, colours=colours, warnings=warnings)


# --------------------------------------------------------------------------- shortfalls (phase 5)

def buy_list(conn: sqlite3.Connection, owner: int) -> list[dict]:
    """What all decks are still short after free copies and proxies, per printing + finish.

    Each line: qty, card fields, ``decks`` [(deck id, name, qty)], and ``alt_free`` =
    free copies of other printings of the card (a retarget might do instead of buying).
    """
    _, shortfalls = pull_plan(conn, owner)
    lines: dict[tuple[str, str], dict] = {}
    for sf in shortfalls:
        s = sf.slot
        line = lines.setdefault(_pf(s), {
            "qty": 0, "name": s["name"], "set_code": s["set_code"], "collector_number": s["collector_number"],
            "finish": s["finish"], "printing_id": s["printing_id"], "card_key": s["card_key"],
            "image": s["image"], "decks": [],
            # the same free pool for every shortfall, so one card's alternatives count once
            "alt_free": sum(a["qty"] for a in sf.alternatives),
        })
        line["qty"] += sf.qty
        line["decks"].append((sf.deck_id, sf.deck_name, sf.qty))
    return sorted(lines.values(), key=lambda l: (l["name"].lower(), l["set_code"]))


def proxy_short(conn: sqlite3.Connection, deck: sqlite3.Row) -> int:
    """Cover everything the deck is still short with proxies. Returns proxies added."""
    added = 0
    with transaction(conn):
        for s in state(conn, deck)["slots"]:
            if s["short"]:
                conn.execute(
                    "UPDATE deck_slots SET qty_proxy = qty_proxy + ? WHERE deck_id = ? AND printing_id = ?"
                    " AND finish = ? AND condition = ? AND lang = ? AND board = ?",
                    (s["short"], deck["id"], s["printing_id"], s["finish"], s["condition"], s["lang"], s["board"]),
                )
                added += s["short"]
    return added
