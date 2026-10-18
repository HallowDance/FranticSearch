"""Moxfield import: collection CSV and deck text, plus the unresolved queue.

Parsing is pure (text in, ``Line`` objects out). Resolution maps a line to an
exact printing key: (set, collector number) first, then (name, set), and for
deck lines without a set the default printing (a free owned printing, else the
newest paper printing). Anything that doesn't resolve goes to the unresolved
queue, where it can be fixed by hand or dismissed.

Each imported file / pasted deck is recorded with its sha256; the same content
can never be imported twice.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from . import decks, ledger
from .db import transaction
from .ledger import CONDITIONS, LANGUAGES, LedgerError, StockKey


class ImportFailed(LedgerError):
    """The whole file/text was rejected (wrong format, duplicate, ...)."""


@dataclass
class Line:
    line_no: int
    raw: str
    qty: int = 1
    name: str = ""
    set_code: str = ""
    collector_number: str = ""
    finish: str | None = None   # None = not given
    condition: str = "NM"
    lang: str = "en"
    board: str | None = None
    proxy: bool = False
    problem: str = ""           # set when the line itself couldn't be read


# --------------------------------------------------------------------------- deck text

# "1 Sol Ring", "1x Sol Ring (C21) 263", "1 Sol Ring (C21) 263 *F*"
_DECK_LINE = re.compile(
    r"^(?P<qty>\d+)x?\s+(?P<name>.+?)"
    r"(?:\s+\((?P<set>[A-Za-z0-9]+)\)(?:\s+(?P<cn>[^\s*]+))?)?"
    r"(?:\s+\*(?P<flag>[FE])\*)?\s*$"
)
_SECTION = re.compile(r"^(?://\s*)?(?P<head>[A-Za-z ]+?)\s*:?\s*(?:\(\d+\))?$")
_SECTIONS = {
    "commander": "commander", "commanders": "commander",
    "deck": "main", "main": "main", "mainboard": "main", "main deck": "main",
    "sideboard": "side", "side": "side", "companion": "side",
    "maybeboard": "maybe", "maybe": "maybe", "considering": "maybe",
}


def parse_deck_text(text: str, after_blank: str = "commander") -> list[Line]:
    """Parse a Moxfield/MTGA-style deck list.

    Section headers ("Commander", "Sideboard:", "//Maybeboard" ...) set the board.
    Without any headers, cards before the first blank line are the main deck and
    cards after it go to ``after_blank`` (Moxfield puts the commander there).
    """
    if after_blank not in decks.BOARDS:
        raise ImportFailed(f"unknown board {after_blank!r}")
    rows = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    has_headers = any(_section(r) for r in rows)
    board, lines, seen_card = "main", [], False
    for no, raw in enumerate(rows, start=1):
        s = raw.strip()
        if not s:
            if not has_headers and seen_card:
                board = after_blank
            continue
        sec = _section(s)
        if sec:
            board = sec
            continue
        m = _DECK_LINE.match(s)
        if not m:
            lines.append(Line(no, s, name=s, board=board, problem="unreadable line"))
            continue
        seen_card = True
        flag = m["flag"]
        lines.append(Line(
            no, s, qty=int(m["qty"]), name=m["name"].strip(),
            set_code=(m["set"] or "").lower(), collector_number=m["cn"] or "",
            finish={"F": "foil", "E": "etched"}.get(flag) if flag else None,
            board=board,
        ))
    return [l for l in lines if l.qty > 0]


def _section(s: str) -> str | None:
    m = _SECTION.match(s.strip())
    return _SECTIONS.get(m["head"].strip().lower()) if m else None


# --------------------------------------------------------------------------- collection CSV

# Moxfield's documented header: Count, Tradelist Count, Name, Edition, Condition,
# Language, Foil, Tags, Last Modified, Collector Number, Alter, Proxy, Purchase Price.
_CSV_ALIASES = {
    "count": "count", "quantity": "count", "qty": "count",
    "name": "name", "card name": "name",
    "edition": "set", "set": "set", "set code": "set",
    "collector number": "cn", "collector_number": "cn", "number": "cn",
    "foil": "foil", "finish": "foil",
    "condition": "condition",
    "language": "lang", "lang": "lang",
    "proxy": "proxy",
}
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"", "false", "no", "n", "0"}
_CONDITION_WORDS = {
    "m": "NM", "mint": "NM", "nm": "NM", "near mint": "NM",
    "lp": "LP", "lightly played": "LP", "ex": "LP", "excellent": "LP", "good": "LP",
    "good (lightly played)": "LP", "sp": "LP", "slightly played": "LP",
    "mp": "MP", "moderately played": "MP", "pl": "MP", "played": "MP",
    "hp": "HP", "heavily played": "HP",
    "d": "DMG", "dmg": "DMG", "damaged": "DMG", "poor": "DMG",
}
_LANG_WORDS = {name.lower(): code for code, name in LANGUAGES.items()} | {
    code: code for code in LANGUAGES
} | {
    "chinese simplified": "zhs", "chinese traditional": "zht",
    "simplified chinese": "zhs", "traditional chinese": "zht",
    "jp": "ja", "kr": "ko", "cn": "zhs", "tw": "zht", "sp": "es",
}


def parse_collection_csv(data: bytes) -> list[Line]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFailed("the file is not UTF-8 text") from None
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None) or []
    cols = {_CSV_ALIASES.get(h.strip().lower()): i for i, h in enumerate(header)}
    cols.pop(None, None)
    if "count" not in cols or "name" not in cols:
        raise ImportFailed("not a collection CSV: it needs at least Count and Name columns")
    lines = []
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        get = lambda k: row[cols[k]].strip() if k in cols and cols[k] < len(row) else ""  # noqa: E731
        raw = ",".join(row)
        line = Line(reader.line_num, raw, name=get("name"), set_code=get("set").lower(),
                    collector_number=get("cn"))
        problems = []
        try:
            line.qty = int(get("count") or "0")
        except ValueError:
            line.qty, problems = 1, ["unreadable count"]
        foil = get("foil").lower()
        line.finish = {"foil": "foil", "etched": "etched", "nonfoil": "nonfoil",
                       "normal": "nonfoil"}.get(foil) or (
            "foil" if foil in _TRUE else "nonfoil" if foil in _FALSE else None)
        if line.finish is None:
            problems.append(f"unknown foil value {get('foil')!r}")
        cond = get("condition").lower()
        if cond:
            line.condition = _CONDITION_WORDS.get(cond) or cond.upper()
            if line.condition not in CONDITIONS:
                problems.append(f"unknown condition {get('condition')!r}")
                line.condition = "NM"
        lang = get("lang").lower()
        if lang:
            if lang not in _LANG_WORDS:
                problems.append(f"unknown language {get('lang')!r}")
            line.lang = _LANG_WORDS.get(lang, "en")
        line.proxy = get("proxy").lower() in _TRUE
        if not line.name and not (line.set_code and line.collector_number):
            problems.append("no card name")
        line.problem = "; ".join(problems)
        if line.qty > 0:
            lines.append(line)
    return lines


# --------------------------------------------------------------------------- resolution

# Never chosen as a default printing unless nothing else exists.
_ODD_LAYOUTS = ("token", "double_faced_token", "emblem", "art_series", "vanguard", "planar", "scheme")
_ODD_SET_TYPES = ("token", "memorabilia", "minigame", "alchemy", "treasure_chest")
# Real paper cards, but rarely what a deck list means (Secret Lair, joke sets, The List).
_SPECIAL_SET_TYPES = ("box", "funny", "promo")
_SPECIAL_SETS = ("plst", "sld")


def printings_named(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Printings whose full name or front-face name matches (case-insensitive)."""
    names = {name.strip(), re.sub(r"\s+/\s+", " // ", name.strip())}
    rows: dict[str, sqlite3.Row] = {}
    for n in names:
        for r in conn.execute(
            "SELECT * FROM printings WHERE name = ? COLLATE NOCASE"
            " UNION SELECT * FROM printings WHERE face_name = ? COLLATE NOCASE",
            (n, n),
        ):
            rows[r["scryfall_id"]] = r
    return list(rows.values())


def _preference(p: sqlite3.Row, today: str) -> tuple:
    """Sort key: higher = better default printing (released paper, regular set, newest)."""
    return (
        p["layout"] not in _ODD_LAYOUTS,
        (p["set_type"] or "") not in _ODD_SET_TYPES,
        not p["digital"],
        (p["released_at"] or "") <= today,   # previews of unreleased sets are in the bulk file
        p["lang"] == "en",
        (p["set_type"] or "") not in _SPECIAL_SET_TYPES and p["set_code"] not in _SPECIAL_SETS,
        not p["promo"],
        p["released_at"] or "",
    )


def _default_finish(p: sqlite3.Row) -> str:
    finishes = json.loads(p["finishes"])
    return "nonfoil" if "nonfoil" in finishes or not finishes else finishes[0]


def default_key(
    conn: sqlite3.Connection, candidates: list[sqlite3.Row], finish: str | None = None
) -> StockKey | None:
    """A free owned printing (most copies outside decks), else the best printing.

    With ``finish``, only printings available in that finish are considered.
    """
    if finish:
        candidates = [p for p in candidates if finish in json.loads(p["finishes"])]
    if not candidates:
        return None
    ids = [p["scryfall_id"] for p in candidates]
    owned = conn.execute(
        f"""SELECT s.printing_id, s.finish, s.condition, s.lang, SUM(s.qty) AS n
              FROM stock s JOIN locations l ON l.id = s.location_id
             WHERE l.type <> 'deck' AND s.printing_id IN ({','.join('?' * len(ids))})
               AND (? IS NULL OR s.finish = ?)
             GROUP BY s.printing_id, s.finish, s.condition, s.lang
             ORDER BY n DESC LIMIT 1""",
        [*ids, finish, finish],
    ).fetchone()
    if owned:
        return StockKey(owned["printing_id"], owned["finish"], owned["condition"], owned["lang"])
    today = date.today().isoformat()
    best = max(candidates, key=lambda p: _preference(p, today))
    return StockKey(best["scryfall_id"], finish or _default_finish(best))


def resolve(conn: sqlite3.Connection, line: Line, allow_default: bool) -> tuple[StockKey | None, str]:
    """Map a line to a printing key. Returns (key, "") or (None, problem)."""
    if line.problem:
        return None, line.problem
    p = None
    if line.set_code and line.collector_number:
        p = conn.execute(
            "SELECT * FROM printings WHERE set_code = ? AND collector_number = ? COLLATE NOCASE",
            (line.set_code, line.collector_number),
        ).fetchone()
    if p is None and line.name:
        named = printings_named(conn, line.name)
        if not named:
            return None, f"no card named {line.name!r}"
        if line.set_code:
            in_set = [r for r in named if r["set_code"] == line.set_code]
            if not in_set:
                return None, f"no {line.name!r} in set {line.set_code.upper()}"
            if len(in_set) > 1:
                return None, f"{len(in_set)} printings of {line.name!r} in {line.set_code.upper()}"
            p = in_set[0]
        elif allow_default:
            key = default_key(conn, named, line.finish)
            return (key, "") if key else (None, f"no {line.finish} printing of {line.name!r}")
        else:
            return None, "no set given"
    if p is None:
        return None, f"no printing {line.set_code.upper()} #{line.collector_number}"
    finish = line.finish or _default_finish(p)
    if finish not in json.loads(p["finishes"]):
        return None, f"{p['name']} ({p['set_code'].upper()} #{p['collector_number']}) has no {finish} version"
    return StockKey(p["scryfall_id"], finish, line.condition, line.lang), ""


# --------------------------------------------------------------------------- imports

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_new(conn: sqlite3.Connection, sha: str) -> None:
    prev = conn.execute("SELECT id, ts FROM imports WHERE sha256 = ?", (sha,)).fetchone()
    if prev:
        raise ImportFailed(f"this was already imported on {prev['ts'][:16].replace('T', ' ')} UTC"
                           f" (import #{prev['id']})")


def _queue(conn: sqlite3.Connection, import_id: int, line: Line, problem: str) -> None:
    conn.execute(
        "INSERT INTO import_unresolved(import_id, line_no, raw, qty, name, set_code,"
        " collector_number, finish, condition, lang, board, problem)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (import_id, line.line_no, line.raw, line.qty, line.name, line.set_code,
         line.collector_number, line.finish, line.condition, line.lang, line.board, problem),
    )


def import_collection(
    conn: sqlite3.Connection, data: bytes, location_id: int, filename: str = ""
) -> int:
    """Import a collection CSV into one location. Returns the import id."""
    sha = _sha(data)
    _check_new(conn, sha)
    lines = parse_collection_csv(data)
    loc = conn.execute("SELECT type FROM locations WHERE id = ?", (location_id,)).fetchone()
    if loc is None:
        raise ImportFailed("unknown location")
    if loc["type"] == "deck":
        raise ImportFailed("import into a binder or box; decks are filled by 'mark as built'")
    resolved: dict[StockKey, int] = {}
    unresolved, skipped = [], 0
    for line in lines:
        if line.proxy:
            skipped += line.qty
            continue
        key, problem = resolve(conn, line, allow_default=False)
        if key:
            resolved[key] = resolved.get(key, 0) + line.qty
        else:
            unresolved.append((line, problem))
    with transaction(conn):
        import_id = conn.execute(
            "INSERT INTO imports(kind, filename, sha256, location_id, lines, imported, skipped)"
            " VALUES ('collection', ?, ?, ?, ?, ?, ?)",
            (filename, sha, location_id, len(lines), sum(resolved.values()), skipped),
        ).lastrowid
        if resolved:
            batch = ledger.acquire_many(
                conn, location_id, resolved.items(), note=_note(import_id, filename)
            )
            conn.execute("UPDATE imports SET batch_id = ? WHERE id = ?", (batch, import_id))
        for line, problem in unresolved:
            _queue(conn, import_id, line, problem)
    return import_id


def import_deck(
    conn: sqlite3.Connection,
    text: str,
    name: str,
    format_: str = "",
    after_blank: str = "commander",
    filename: str = "",
) -> int:
    """Create a deck (and its location) from a deck list. Returns the import id."""
    normalized = "\n".join(l.rstrip() for l in text.replace("\r\n", "\n").strip().split("\n"))
    sha = _sha(normalized.encode())
    _check_new(conn, sha)
    lines = parse_deck_text(normalized, after_blank)
    if not lines:
        raise ImportFailed("no cards found in the deck list")
    with transaction(conn):
        deck_id = decks.create(conn, name, format_)
        import_id = conn.execute(
            "INSERT INTO imports(kind, filename, sha256, deck_id, lines) VALUES ('deck', ?, ?, ?, ?)",
            (filename, sha, deck_id, len(lines)),
        ).lastrowid
        imported = 0
        for line in lines:
            key, problem = resolve(conn, line, allow_default=True)
            if key:
                try:
                    decks.add_slot(conn, deck_id, key, line.board, line.qty)
                    imported += line.qty
                    continue
                except LedgerError as e:
                    problem = str(e)
            _queue(conn, import_id, line, problem)
        conn.execute("UPDATE imports SET imported = ? WHERE id = ?", (imported, import_id))
    return import_id


def _note(import_id: int, filename: str) -> str:
    return f"import #{import_id}" + (f" ({filename})" if filename else "")


# --------------------------------------------------------------------------- queue

def get_import(conn: sqlite3.Connection, import_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT i.*, l.name AS location_name, d.name AS deck_name,
                  (SELECT COUNT(*) FROM import_unresolved u
                    WHERE u.import_id = i.id AND u.status = 'open') AS open
             FROM imports i LEFT JOIN locations l ON l.id = i.location_id
             LEFT JOIN decks d ON d.id = i.deck_id WHERE i.id = ?""",
        (import_id,),
    ).fetchone()


def list_imports(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT i.*, l.name AS location_name, d.name AS deck_name,
                  (SELECT COUNT(*) FROM import_unresolved u
                    WHERE u.import_id = i.id AND u.status = 'open') AS open
             FROM imports i LEFT JOIN locations l ON l.id = i.location_id
             LEFT JOIN decks d ON d.id = i.deck_id ORDER BY i.id DESC"""
    ).fetchall()


def unresolved(conn: sqlite3.Connection, import_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM import_unresolved WHERE import_id = ?"
        " ORDER BY status = 'open' DESC, line_no",
        (import_id,),
    ).fetchall()


def get_unresolved(conn: sqlite3.Connection, uid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM import_unresolved WHERE id = ?", (uid,)).fetchone()


def fix(conn: sqlite3.Connection, uid: int, key: StockKey, qty: int | None = None) -> None:
    """Resolve a queued line to ``key``: add the copies (collection) or the slot (deck)."""
    u = get_unresolved(conn, uid)
    if u is None or u["status"] != "open":
        raise LedgerError("this line is not open any more")
    qty = u["qty"] if qty is None else qty
    imp = get_import(conn, u["import_id"])
    with transaction(conn):
        if imp["kind"] == "collection":
            ledger.acquire(conn, imp["location_id"], key, qty, note=_note(imp["id"], imp["filename"]),
                           reason="import")
        else:
            decks.add_slot(conn, imp["deck_id"], key, u["board"] or "main", qty)
        conn.execute("UPDATE imports SET imported = imported + ? WHERE id = ?", (qty, imp["id"]))
        conn.execute("UPDATE import_unresolved SET status = 'fixed' WHERE id = ?", (uid,))


def dismiss(conn: sqlite3.Connection, uid: int) -> None:
    with transaction(conn):
        n = conn.execute(
            "UPDATE import_unresolved SET status = 'dismissed' WHERE id = ? AND status = 'open'", (uid,)
        ).rowcount
    if not n:
        raise LedgerError("this line is not open any more")
