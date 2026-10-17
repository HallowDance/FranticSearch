import pytest
from conftest import BOLT, SOL_C21, SOL_CMM

from franticsearch import ledger, locations
from franticsearch.ledger import LedgerError, StockKey


@pytest.fixture
def locs(conn):
    return {
        "binder": locations.create(conn, "Binder A", "binder"),
        "box": locations.create(conn, "Bulk box", "box"),
    }


def total(conn):
    return conn.execute("SELECT COALESCE(SUM(qty), 0) FROM stock").fetchone()[0]


def test_acquire_move_adjust_sell_keep_ledger_consistent(conn, locs):
    sol = StockKey(SOL_C21)
    ledger.acquire(conn, locs["binder"], sol, 3)
    ledger.move(conn, locs["binder"], locs["box"], sol, 2)
    assert ledger.qty_at(conn, locs["binder"], sol) == 1
    assert ledger.qty_at(conn, locs["box"], sol) == 2
    ledger.adjust(conn, locs["box"], sol, 4)
    ledger.adjust(conn, locs["box"], sol, 1)
    assert ledger.adjust(conn, locs["box"], sol, 1) is None
    ledger.sell(conn, locs["binder"], sol, 1)
    assert ledger.qty_at(conn, locs["binder"], sol) == 0
    assert conn.execute("SELECT COUNT(*) FROM stock WHERE location_id = ?", (locs["binder"],)).fetchone()[0] == 0
    assert total(conn) == 1
    assert ledger.check(conn) == []
    reasons = [r[0] for r in conn.execute("SELECT reason FROM movements ORDER BY id")]
    assert reasons == ["acquire", "move", "adjust", "adjust", "sell"]


def test_printing_key_dimensions_are_separate(conn, locs):
    ledger.acquire(conn, locs["binder"], StockKey(SOL_CMM, "foil"), 1)
    ledger.acquire(conn, locs["binder"], StockKey(SOL_CMM, "nonfoil", "LP", "ja"), 2)
    ledger.acquire(conn, locs["binder"], StockKey(SOL_CMM), 3)
    rows = conn.execute("SELECT COUNT(*) FROM stock").fetchone()[0]
    assert rows == 3 and total(conn) == 6


@pytest.mark.parametrize(
    "key, message",
    [
        (StockKey("nope"), "unknown printing"),
        (StockKey(SOL_C21, "foil"), "does not exist in foil"),
        (StockKey(BOLT, "shiny"), "unknown finish"),
        (StockKey(BOLT, "nonfoil", "MINT"), "unknown condition"),
        (StockKey(BOLT, "nonfoil", "NM", "EN"), "bad language"),
    ],
)
def test_invalid_keys_rejected(conn, locs, key, message):
    with pytest.raises(LedgerError, match=message):
        ledger.acquire(conn, locs["binder"], key, 1)
    assert total(conn) == 0 and conn.execute("SELECT COUNT(*) FROM movements").fetchone()[0] == 0


def test_cannot_take_more_than_present(conn, locs):
    ledger.acquire(conn, locs["binder"], StockKey(BOLT), 1)
    with pytest.raises(LedgerError, match="only 1 copy"):
        ledger.move(conn, locs["binder"], locs["box"], StockKey(BOLT), 2)
    with pytest.raises(LedgerError):
        ledger.sell(conn, locs["box"], StockKey(BOLT), 1)
    with pytest.raises(LedgerError, match="same location"):
        ledger.move(conn, locs["binder"], locs["binder"], StockKey(BOLT), 1)
    with pytest.raises(LedgerError, match="positive"):
        ledger.acquire(conn, locs["binder"], StockKey(BOLT), 0)
    # failed operations leave nothing behind
    assert ledger.qty_at(conn, locs["binder"], StockKey(BOLT)) == 1
    assert conn.execute("SELECT COUNT(*) FROM movements").fetchone()[0] == 1


def test_moves_into_and_out_of_decks_are_pulls_and_returns(conn, locs):
    deck = conn.execute("INSERT INTO locations(name, type) VALUES ('Deck: Atraxa', 'deck')").lastrowid
    ledger.acquire(conn, locs["binder"], StockKey(BOLT), 2)
    ledger.move(conn, locs["binder"], deck, StockKey(BOLT), 1)
    ledger.move(conn, deck, locs["box"], StockKey(BOLT), 1)
    reasons = [r[0] for r in conn.execute("SELECT reason FROM movements ORDER BY id")]
    assert reasons == ["acquire", "pull", "return"]


def test_check_detects_tampering(conn, locs):
    ledger.acquire(conn, locs["binder"], StockKey(BOLT), 2)
    conn.execute("UPDATE stock SET qty = 5")
    (problem,) = ledger.check(conn)
    assert problem["ledger_qty"] == 2 and problem["stock_qty"] == 5
    conn.execute("DELETE FROM stock")
    (problem,) = ledger.check(conn)
    assert problem["ledger_qty"] == 2 and problem["stock_qty"] == 0


def test_schema_rejects_inconsistent_movements(conn, locs):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO movements(batch_id, from_location_id, to_location_id, printing_id,"
            " finish, condition, lang, qty, reason) VALUES ('b', ?, NULL, ?, 'nonfoil', 'NM', 'en', 1, 'acquire')",
            (locs["binder"], BOLT),
        )


def test_locations_crud(conn, locs):
    with pytest.raises(LedgerError, match="already exists"):
        locations.create(conn, "binder a", "binder")
    with pytest.raises(LedgerError, match="type must be"):
        locations.create(conn, "X", "deck")
    locations.update(conn, locs["box"], "Bulk box 1", "box", "under the bed")
    assert locations.get(conn, locs["box"])["notes"] == "under the bed"
    ledger.acquire(conn, locs["box"], StockKey(BOLT), 1)
    ledger.sell(conn, locs["box"], StockKey(BOLT), 1)
    with pytest.raises(LedgerError, match="history"):
        locations.delete(conn, locs["box"])
    locations.delete(conn, locs["binder"])
    assert locations.get(conn, locs["binder"]) is None
