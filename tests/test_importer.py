import io
import json

import pytest
from conftest import ALICE, BOLT, DELVER, SOL_C21, SOL_CMM, SOL_ORACLE

from franticsearch import decks, importer, ledger, locations, scryfall
from franticsearch.importer import ImportFailed, Line
from franticsearch.ledger import StockKey

DIGITAL_SOL = "00000000-0000-0000-0000-0000000000d1"
FUTURE_SOL = "00000000-0000-0000-0000-0000000000f1"


def _add_printings(conn, *cards):
    base = {"object": "card", "lang": "en", "layout": "normal", "finishes": ["nonfoil"],
            "type_line": "Artifact", "set_name": "Extra"}
    scryfall.import_stream(conn, io.BytesIO(json.dumps([{**base, **c} for c in cards]).encode()))


@pytest.fixture
def more_sols(conn):
    """Newer Sol Rings that must never be picked as a default: digital-only, unreleased."""
    _add_printings(
        conn,
        {"id": DIGITAL_SOL, "oracle_id": SOL_ORACLE, "name": "Sol Ring", "set": "ymid",
         "collector_number": "1", "released_at": "2025-01-01", "digital": True},
        {"id": FUTURE_SOL, "oracle_id": SOL_ORACLE, "name": "Sol Ring", "set": "zzz",
         "collector_number": "1", "released_at": "2999-01-01"},
    )


# --------------------------------------------------------------------------- deck text parsing

def test_parse_moxfield_plain_export_puts_last_block_on_commander():
    text = "1 Sol Ring\n11 Island\n\n1 Millicent, Restless Revenant\n"
    lines = importer.parse_deck_text(text)
    assert [(l.qty, l.name, l.board) for l in lines] == [
        (1, "Sol Ring", "main"), (11, "Island", "main"),
        (1, "Millicent, Restless Revenant", "commander"),
    ]
    assert importer.parse_deck_text(text, after_blank="side")[-1].board == "side"


def test_parse_set_number_and_finish_flags():
    (a, b, c) = importer.parse_deck_text(
        "1 Sol Ring (C21) 263\n2x Sol Ring (CMM) 410 *F*\n1 Fire // Ice (MH2) 290 *E*"
    )
    assert (a.set_code, a.collector_number, a.finish) == ("c21", "263", None)
    assert (b.qty, b.set_code, b.collector_number, b.finish) == (2, "cmm", "410", "foil")
    assert (c.name, c.finish) == ("Fire // Ice", "etched")


def test_parse_section_headers_win_over_blank_lines():
    text = "Commander\n1 Sol Ring\n\nDeck\n1 Island\n\n1 Plains\nSIDEBOARD:\n1 Bolt\n//Maybeboard\n1 Ice"
    assert [l.board for l in importer.parse_deck_text(text)] == [
        "commander", "main", "main", "side", "maybe"]


def test_parse_moxfield_export_with_sets_lists_commander_first():
    text = "1 Millicent, Restless Revenant (VOC) 1 *F*\n1 Sol Ring (VOC) 168\n11 Island (VOW) 399\n"
    boards = [l.board for l in importer.parse_deck_text(text, first_is_commander=True)]
    assert boards == ["commander", "main", "main"]
    assert {l.board for l in importer.parse_deck_text(text)} == {"main"}  # not a commander deck
    # a blank line or headers mean the list says where the commander is
    blank = "1 Sol Ring\n\n1 Millicent, Restless Revenant"
    assert [l.board for l in importer.parse_deck_text(blank, first_is_commander=True)] == [
        "main", "commander"]
    headed = "Deck\n1 Sol Ring\n1 Island"
    assert {l.board for l in importer.parse_deck_text(headed, first_is_commander=True)} == {"main"}


def test_parse_line_without_count_means_one():
    (line,) = importer.parse_deck_text("Sol Ring (C21) 263")
    assert (line.qty, line.name, line.set_code, line.problem) == (1, "Sol Ring", "c21", "")


# --------------------------------------------------------------------------- CSV parsing

CSV_HEADER = ('"Count","Tradelist Count","Name","Edition","Condition","Language","Foil",'
              '"Tags","Last Modified","Collector Number","Alter","Proxy","Purchase Price"\n')


def _csv(*rows):
    return (CSV_HEADER + "".join(rows)).encode()


def test_parse_moxfield_csv():
    data = _csv(
        '"2","0","Sol Ring","cmm","Near Mint","English","foil","","2024-01-01","410","False","False",""\n',
        '"1","0","Lightning Bolt","m10","Lightly Played","Japanese","","","","146","False","False",""\n',
        '"1","0","Sol Ring","c21","Mint","English","","","","263","False","True",""\n',
    )
    a, b, c = importer.parse_collection_csv(b"\xef\xbb\xbf" + data)  # BOM is fine
    assert (a.qty, a.name, a.set_code, a.collector_number, a.finish, a.condition, a.lang) == (
        2, "Sol Ring", "cmm", "410", "foil", "NM", "en")
    assert (b.finish, b.condition, b.lang) == ("nonfoil", "LP", "ja")
    assert c.proxy and not a.proxy and not a.problem


def test_parse_csv_reports_unknown_values():
    (line,) = importer.parse_collection_csv(
        _csv('"1","0","Sol Ring","c21","Gem Mint","Klingon","shiny","","","263","False","False",""\n'))
    assert "condition" in line.problem and "language" in line.problem and "foil" in line.problem


def test_parse_csv_rejects_other_files():
    with pytest.raises(ImportFailed, match="Count and Name"):
        importer.parse_collection_csv(b"a,b,c\n1,2,3\n")


# --------------------------------------------------------------------------- resolution

def test_resolve_by_set_and_number_then_name_and_set(conn):
    key, _ = importer.resolve(conn, Line(1, "", name="x", set_code="cmm", collector_number="410",
                                         finish="etched"), allow_default=False)
    assert key == StockKey(SOL_CMM, "etched")
    key, _ = importer.resolve(conn, Line(1, "", name="sol ring", set_code="c21"), allow_default=False)
    assert key == StockKey(SOL_C21)
    key, problem = importer.resolve(conn, Line(1, "", name="Sol Ring", set_code="c21", finish="foil"),
                                    allow_default=False)
    assert key is None and "no foil version" in problem
    assert importer.resolve(conn, Line(1, "", name="Sol Ring"), allow_default=False) == (None, "no set given")
    assert "no card named" in importer.resolve(conn, Line(1, "", name="Nope"), allow_default=True)[1]


def test_resolve_front_face_name(conn):
    key, _ = importer.resolve(conn, Line(1, "", name="Delver of Secrets"), allow_default=True)
    assert key == StockKey(DELVER)


def test_default_printing_is_newest_released_paper(conn, more_sols):
    key, _ = importer.resolve(conn, Line(1, "", name="Sol Ring"), allow_default=True)
    assert key == StockKey(SOL_CMM)
    key, _ = importer.resolve(conn, Line(1, "", name="Sol Ring", finish="foil"), allow_default=True)
    assert key == StockKey(SOL_CMM, "foil")


def test_default_printing_prefers_free_owned_copy(conn, more_sols):
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    ledger.acquire(conn, binder, StockKey(SOL_C21, condition="LP"), 1)
    key, _ = importer.resolve(conn, Line(1, "", name="Sol Ring"), allow_default=True, owner=ALICE)
    assert key == StockKey(SOL_C21, condition="LP")


# --------------------------------------------------------------------------- collection import

def test_import_collection(conn):
    loc = locations.create(conn, "Unsorted", "box", owner=ALICE)
    data = _csv(
        '"2","0","Sol Ring","cmm","Near Mint","English","foil","","","410","False","False",""\n',
        '"1","0","Sol Ring","cmm","Near Mint","English","foil","","","410","False","False",""\n',
        '"1","0","Lightning Bolt","m10","Near Mint","English","","","","146","False","False",""\n',
        '"3","0","Sol Ring","c21","Near Mint","English","","","","263","False","True",""\n',
        '"1","0","Black Lotus","lea","Near Mint","English","","","","232","False","False",""\n',
    )
    iid = importer.import_collection(conn, data, loc, filename="moxfield.csv")
    imp = importer.get_import(conn, iid)
    assert (imp["lines"], imp["imported"], imp["skipped"], imp["open"]) == (5, 4, 3, 1)
    assert ledger.qty_at(conn, loc, StockKey(SOL_CMM, "foil")) == 3
    assert ledger.qty_at(conn, loc, StockKey(BOLT)) == 1
    assert ledger.qty_at(conn, loc, StockKey(SOL_C21)) == 0  # proxy
    reasons = {r[0] for r in conn.execute("SELECT DISTINCT reason FROM movements")}
    assert reasons == {"import"}
    assert ledger.check(conn) == []

    with pytest.raises(ImportFailed, match="already imported"):
        importer.import_collection(conn, data, loc)

    (u,) = importer.unresolved(conn, iid)
    assert "no card named 'Black Lotus'" in u["problem"]
    importer.fix(conn, u["id"], StockKey(BOLT), qty=2)  # "close enough", for the test
    assert ledger.qty_at(conn, loc, StockKey(BOLT)) == 3
    assert importer.get_import(conn, iid)["open"] == 0
    with pytest.raises(ledger.LedgerError, match="not open"):
        importer.dismiss(conn, u["id"])
    assert ledger.check(conn) == []


def test_collection_import_refuses_deck_locations(conn):
    iid = importer.import_deck(conn, "1 Sol Ring", "Deck A", owner=ALICE)
    deck_loc = decks.get(conn, importer.get_import(conn, iid)["deck_id"])["location_id"]
    with pytest.raises(ImportFailed, match="binder or box"):
        importer.import_collection(conn, _csv(), deck_loc)


# --------------------------------------------------------------------------- deck import

def test_import_deck_creates_deck_location_and_slots(conn):
    text = "1 Sol Ring (C21) 263\n1 Sol Ring (C21) 263\n2 Lightning Bolt\n1 Black Lotus\n\n1 Delver of Secrets"
    iid = importer.import_deck(conn, text, "Delver Tribal", "commander", filename="d.txt", owner=ALICE)
    imp = importer.get_import(conn, iid)
    assert (imp["lines"], imp["imported"], imp["open"]) == (5, 5, 1)
    deck = decks.get(conn, imp["deck_id"])
    assert (deck["name"], deck["format"]) == ("Delver Tribal", "commander")
    assert locations.get(conn, deck["location_id"])["type"] == "deck"
    slots = {(s["name"], s["board"]): s["qty_wanted"] for s in decks.slots(conn, deck["id"])}
    assert slots == {("Sol Ring", "main"): 2, ("Lightning Bolt", "main"): 2,
                     ("Delver of Secrets // Insectile Aberration", "commander"): 1}
    assert conn.execute("SELECT COUNT(*) FROM movements").fetchone()[0] == 0  # nothing moved

    (u,) = importer.unresolved(conn, iid)
    importer.fix(conn, u["id"], StockKey(SOL_CMM, "etched"))
    assert any(s["printing_id"] == SOL_CMM and s["finish"] == "etched"
               for s in decks.slots(conn, deck["id"]))
    assert decks.list_all(conn, ALICE)[0]["unresolved"] == 0


def test_commander_format_takes_first_line_as_commander(conn):
    iid = importer.import_deck(conn, "1 Delver of Secrets (ISD) 51 *F*\n1 Sol Ring (C21) 263", "A",
                               "commander", owner=ALICE)
    deck_id = importer.get_import(conn, iid)["deck_id"]
    assert decks.commander_names(conn, deck_id) == ["Delver of Secrets // Insectile Aberration"]
    iid = importer.import_deck(conn, "1 Delver of Secrets (ISD) 51\n1 Sol Ring (C21) 263", "B", "modern", owner=ALICE)
    assert decks.commander_names(conn, importer.get_import(conn, iid)["deck_id"]) == []


def test_deck_import_duplicates_and_delete(conn):
    text = "1 Sol Ring\n"
    iid = importer.import_deck(conn, text, "A", owner=ALICE)
    with pytest.raises(ImportFailed, match="already imported"):
        importer.import_deck(conn, text + "\r\n", "B", owner=ALICE)  # same list, other line endings
    with pytest.raises(ledger.LedgerError, match="already have a location or deck"):
        importer.import_deck(conn, "1 Island", "a", owner=ALICE)
    assert conn.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 1  # failed import left nothing

    decks.delete(conn, importer.get_import(conn, iid)["deck_id"])
    assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0
    importer.import_deck(conn, text, "A", owner=ALICE)  # deleting the deck allows importing it again


def test_deck_with_moved_cards_cannot_be_deleted(conn):
    iid = importer.import_deck(conn, "1 Sol Ring (C21) 263", "A", owner=ALICE)
    deck = decks.get(conn, importer.get_import(conn, iid)["deck_id"])
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 1)
    ledger.move(conn, binder, deck["location_id"], StockKey(SOL_C21), 1)
    (slot,) = decks.slots(conn, deck["id"])
    assert (slot["in_deck"], slot["free"], slot["owned_any"]) == (1, 0, 1)
    with pytest.raises(ledger.LedgerError, match="cannot be deleted"):
        decks.delete(conn, deck["id"])
