"""Phase 5 (buy list, exports, proxies) and phase 6 (backups, restore, ledger views)."""

import gzip
import sqlite3

import pytest
from conftest import ALICE, BOLT, DELVER, SOL_C21, SOL_CMM, SOL_ORACLE

from franticsearch import backup, db, deckplan, decks, exports, importer, ledger, locations
from franticsearch.__main__ import main as cli
from franticsearch.ledger import StockKey


@pytest.fixture
def two_decks(conn):
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    a = decks.create(conn, "Alpha", "commander", owner=ALICE)
    b = decks.create(conn, "Beta", owner=ALICE)
    decks.add_slot(conn, a, StockKey(SOL_C21), "main", 1)
    decks.add_slot(conn, b, StockKey(SOL_C21), "main", 2)
    decks.add_slot(conn, b, StockKey(BOLT, "foil"), "side", 3, proxy=1)
    decks.add_slot(conn, a, StockKey(DELVER), "commander", 1)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 1)          # one deck gets it
    ledger.acquire(conn, binder, StockKey(SOL_CMM), 1)          # another printing, free
    return {"binder": binder, "a": a, "b": b}


ITEMS = [
    {"qty": 1, "name": "Sol Ring", "set_code": "c21", "collector_number": "263", "finish": "nonfoil", "board": "main"},
    {"qty": 2, "name": "Lightning Bolt", "set_code": "m10", "collector_number": "146", "finish": "foil", "board": "side"},
    {"qty": 1, "name": "Delver of Secrets // Insectile Aberration", "set_code": "isd", "collector_number": "51",
     "finish": "nonfoil", "board": "commander"},
]


def test_export_formats():
    assert exports.render(ITEMS[:2], "text") == "1 Sol Ring\n2 Lightning Bolt\n"
    assert exports.render(ITEMS[:2], "moxfield") == "1 Sol Ring (C21) 263\n2 Lightning Bolt (M10) 146 *F*\n"
    assert exports.render(ITEMS[:2], "tcgplayer") == "1 Sol Ring [C21]\n2 Lightning Bolt [M10]\n"
    assert exports.render(ITEMS[:2], "csv").splitlines() == [
        "Count,Name,Edition,Collector Number,Foil", "1,Sol Ring,c21,263,", "2,Lightning Bolt,m10,146,foil"]
    deck = exports.render(ITEMS, "moxfield", boards=True)
    assert deck.startswith("Commander\n1 Delver") and "\n\nDeck\n1 Sol Ring" in deck and "\n\nSideboard\n" in deck
    with pytest.raises(ValueError):
        exports.render(ITEMS, "pdf")


def test_deck_export_round_trips_through_import(client, conn, two_decks):
    text = client.get(f"/decks/{two_decks['a']}/export?format=moxfield").data.decode()
    assert text == "Commander\n1 Delver of Secrets // Insectile Aberration (ISD) 51\n\nDeck\n1 Sol Ring (C21) 263\n"
    iid = importer.import_deck(conn, text, "Copy", owner=ALICE)
    copy = importer.get_import(conn, iid)
    assert copy["open"] == 0
    assert sorted((s["board"], s["printing_id"]) for s in decks.slots(conn, copy["deck_id"])) \
        == sorted((s["board"], s["printing_id"]) for s in decks.slots(conn, two_decks["a"]))
    r = client.get(f"/decks/{two_decks['b']}/export?format=csv&download=1")
    assert r.mimetype == "text/csv" and 'filename="Beta.csv"' in r.headers["Content-Disposition"]
    assert client.get(f"/decks/{two_decks['b']}/export?what=proxies&format=text").data == b"1 Lightning Bolt\n"
    assert client.get(f"/decks/{two_decks['b']}/export?format=nope").status_code == 404


def test_buy_list(client, conn, two_decks):
    lines = {(l["printing_id"], l["finish"]): l for l in deckplan.buy_list(conn, ALICE)}
    sol, bolt = lines[(SOL_C21, "nonfoil")], lines[(BOLT, "foil")]
    assert sol["qty"] == 2 and sol["alt_free"] == 1                   # 3 wanted, 1 free; CMM is free
    assert bolt["qty"] == 2 and bolt["alt_free"] == 0                 # 3 wanted, 1 proxy
    assert (DELVER, "nonfoil") in lines
    page = client.get("/buy").data.decode()
    assert "Lightning Bolt" in page and "2 Sol Ring (C21) 263" in page
    hidden = client.get("/buy/export?format=text&hide_alt=1").data.decode()
    assert "Sol Ring" not in hidden and "2 Lightning Bolt" in hidden
    page = client.get("/buy?format=tcgplayer&hide_alt=1").data.decode()
    assert "<b>[TCGplayer mass entry]</b>" in page and "2 Lightning Bolt [M10]" in page
    assert "format=text&amp;hide_alt=1" in page or "hide_alt=1&amp;format=text" in page   # links keep the filter
    assert page.index("<textarea") < page.index('<table class="grid">')                  # list above the table


def test_proxy_all_short(client, conn, two_decks):
    r = client.post(f"/decks/{two_decks['b']}/proxy-short", follow_redirects=True)
    assert b"Marked 3 short cards as proxies" in r.data or b"Marked 4 short cards as proxies" in r.data
    st = deckplan.state(conn, decks.get(conn, two_decks["b"]))
    assert st["short"] == 0 and all(s["qty_proxy"] + s["in_deck"] == s["qty_wanted"] for s in st["slots"])
    r = client.post(f"/decks/{two_decks['b']}/proxy-short", follow_redirects=True)
    assert b"Nothing is short" in r.data


def test_backup_and_restore(tmp_path, conn, db_path, two_decks):
    out = backup.backup(db_path, tmp_path / "bk", keep=2)
    assert out.name.startswith("franticsearch-") and out.suffix == ".gz"
    with gzip.open(out) as f:
        assert f.read(16) == b"SQLite format 3\x00"
    ledger.acquire(conn, two_decks["binder"], StockKey(BOLT), 5)        # after the backup
    import os
    for day in ("01", "02"):
        old = tmp_path / "bk" / f"franticsearch-200001{day}-000000.db.gz"
        old.write_bytes(b"old")
        os.utime(old, (946684800, 946684800))
    backup.backup(db_path, tmp_path / "bk", keep=2)
    kept = backup.list_backups(tmp_path / "bk")
    assert len(kept) == 2 and all("2000" not in p.name for p in kept) and out in kept
    conn.close()
    r = backup.restore(db_path, out, port=1)
    assert r["saved"].exists() and r["problems"] == []
    c = db.connect(db_path)
    assert ledger.qty_at(c, two_decks["binder"], StockKey(BOLT)) == 0     # back to the backup's state
    c.close()
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    with pytest.raises(backup.BackupError, match="usable"):
        backup.restore(db_path, bad, port=1)
    empty = tmp_path / "other.db"
    sqlite3.connect(empty).execute("CREATE TABLE x(y)").connection.close()
    with pytest.raises(backup.BackupError, match="FranticSearch"):
        backup.restore(db_path, empty, port=1)


def test_restore_refuses_while_server_port_is_open(tmp_path, db_path, conn):
    import socket
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        out = backup.backup(db_path, tmp_path)
        assert cli(["--db", db_path, "restore", str(out), "--port", str(port)]) == 1
    assert cli(["--db", db_path, "backup", "--dir", str(tmp_path / "x")]) == 0


def test_history_by_card_and_system_backups(client, conn, two_decks, db_path):
    page = client.get(f"/history?card={SOL_ORACLE}").data.decode()
    assert "History of" in page and "Sol Ring" in page and "Lightning Bolt" not in page
    assert "Full history of this card" in client.get(f"/card/{SOL_ORACLE}").data.decode()
    assert "No backups yet" in client.get("/system").data.decode()
    backup.backup(db_path)
    assert "franticsearch-" in client.get("/system").data.decode()
