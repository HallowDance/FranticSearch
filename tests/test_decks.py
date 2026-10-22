"""Phase 4: deck editor, derived deck state, pull / return lists, mark as built, checks."""

import pytest
from conftest import ALICE, BOLT, DELVER, SOL_C21, SOL_CMM, SOL_ORACLE

from franticsearch import deckplan, decks, importer, ledger, locations
from franticsearch.ledger import LedgerError, StockKey


@pytest.fixture
def setup(conn):
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    box = locations.create(conn, "Box", "box", owner=ALICE)
    deck_id = decks.create(conn, "Burn", "modern", owner=ALICE)
    return {"binder": binder, "box": box, "deck": deck_id, "deck_row": decks.get(conn, deck_id),
            "loc": decks.get(conn, deck_id)["location_id"]}


def sid(printing, board="main", finish="nonfoil"):
    return f"{printing}|{finish}|NM|en|{board}"


def test_state_short_excess_and_proxies(conn, setup):
    d = setup["deck_row"]
    decks.add_slot(conn, d["id"], StockKey(BOLT), "main", 4, proxy=1)
    decks.add_slot(conn, d["id"], StockKey(BOLT), "side", 1)
    decks.add_slot(conn, d["id"], StockKey(DELVER), "maybe", 1)
    st = deckplan.state(conn, d)
    assert (st["short"], st["excess_total"], deckplan.status(st)) == (4, 0, "4 short")
    # 3 bolts (one LP) + a Delver (maybe) + a Sol Ring in the deck
    ledger.acquire(conn, setup["loc"], StockKey(BOLT), 2)
    ledger.acquire(conn, setup["loc"], StockKey(BOLT, condition="LP"), 1)
    ledger.acquire(conn, setup["loc"], StockKey(DELVER), 1)
    ledger.acquire(conn, setup["loc"], StockKey(SOL_C21), 1)
    st = deckplan.state(conn, d)
    main, side = [s for s in st["slots"] if s["printing_id"] == BOLT]
    assert (main["board"], main["in_deck"], main["short"]) == ("main", 3, 0)   # LP counts; 1 proxy
    assert (side["in_deck"], side["short"]) == (0, 1)
    assert sorted((e["printing_id"], e["qty"]) for e in st["excess"]) == [(SOL_C21, 1), (DELVER, 1)]
    assert deckplan.status(st) == "1 short, 2 excess"


def test_pull_plan_and_confirm(conn, setup):
    d = setup["deck_row"]
    decks.add_slot(conn, d["id"], StockKey(BOLT), "main", 4)
    ledger.acquire(conn, setup["binder"], StockKey(BOLT), 1)
    ledger.acquire(conn, setup["box"], StockKey(BOLT, condition="LP"), 2)
    ledger.acquire(conn, setup["box"], StockKey(BOLT, finish="foil"), 1)   # other finish: not this slot
    lines, shortfalls = deckplan.pull_plan(conn, ALICE, [d["id"]])
    assert sorted((l.from_name, l.key.condition, l.qty) for l in lines) == [("Binder", "NM", 1), ("Box", "LP", 2)]
    assert [(sf.qty, [(a["finish"], a["qty"]) for a in sf.alternatives]) for sf in shortfalls] == [(1, [("foil", 1)])]
    one = next(l for l in lines if l.from_name == "Binder")
    batch, n = deckplan.confirm_pulls(conn, ALICE, {one.id}, [d["id"]])
    assert n == 1 and ledger.qty_at(conn, setup["loc"], StockKey(BOLT)) == 1
    assert {r["reason"] for r in conn.execute("SELECT reason FROM movements WHERE batch_id = ?", (batch,))} == {"pull"}
    with pytest.raises(LedgerError, match="nothing to pull"):
        deckplan.confirm_pulls(conn, ALICE, {one.id}, [d["id"]])          # stale line
    deckplan.confirm_pulls(conn, ALICE, None, [d["id"]])
    assert deckplan.state(conn, d)["short"] == 1 and ledger.check(conn) == []


def test_pull_plan_shares_free_copies_between_decks(conn, setup):
    other = decks.create(conn, "Other", owner=ALICE)
    for deck_id in (setup["deck"], other):
        decks.add_slot(conn, deck_id, StockKey(SOL_C21), "main", 1)
    ledger.acquire(conn, setup["binder"], StockKey(SOL_C21), 1)
    lines, shortfalls = deckplan.pull_plan(conn, ALICE)
    assert len(lines) == 1 and len(shortfalls) == 1                      # one copy, one deck
    deckplan.confirm_pulls(conn, ALICE)
    _, shortfalls = deckplan.pull_plan(conn, ALICE)
    assert shortfalls[0].in_other_decks in (["Burn"], ["Other"])


def test_return_list_defaults_to_pull_source(conn, setup):
    d = setup["deck_row"]
    decks.add_slot(conn, d["id"], StockKey(SOL_C21), "main", 1)
    ledger.acquire(conn, setup["box"], StockKey(SOL_C21), 1)
    deckplan.confirm_pulls(conn, ALICE, None, [d["id"]])
    ledger.acquire(conn, setup["loc"], StockKey(BOLT), 1)                  # no history: Unsorted
    # switching the printing: the old copy becomes excess, the new one short
    decks.edit_slot(conn, d["id"], sid(SOL_C21), qty=1, proxy=0, board="main", printing_id=SOL_CMM)
    st = deckplan.state(conn, d)
    assert (st["short"], st["excess_total"]) == (1, 2)
    plan = {l.key.printing_id: l for l in deckplan.return_plan(conn, d)}
    assert plan[SOL_C21].default_to == setup["box"] and plan[BOLT].default_to is None
    batch, n = deckplan.confirm_returns(conn, d, {plan[SOL_C21].id: setup["box"], plan[BOLT].id: "unsorted"},
                                        owner=ALICE)
    assert n == 2 and ledger.qty_at(conn, setup["box"], StockKey(SOL_C21)) == 1
    unsorted = locations.unsorted(conn, owner=ALICE)
    assert ledger.qty_at(conn, unsorted, StockKey(BOLT)) == 1
    assert {r[0] for r in conn.execute("SELECT reason FROM movements WHERE batch_id = ?", (batch,))} == {"return"}
    with pytest.raises(LedgerError, match="non-deck"):
        ledger.acquire(conn, setup["loc"], StockKey(BOLT), 1)
        plan = deckplan.return_plan(conn, d)
        deckplan.confirm_returns(conn, d, {plan[0].id: setup["loc"]}, owner=ALICE)


def test_edit_slot_merge_remove_and_checks(conn, setup):
    d = setup["deck"]
    decks.add_slot(conn, d, StockKey(BOLT), "main", 3)
    decks.add_slot(conn, d, StockKey(BOLT), "side", 1)
    decks.edit_slot(conn, d, sid(BOLT, "side"), qty=1, proxy=0, board="main")          # merges
    assert [(r["board"], r["qty_wanted"]) for r in decks.slots(conn, d)] == [("main", 4)]
    with pytest.raises(LedgerError, match="proxies"):
        decks.edit_slot(conn, d, sid(BOLT), qty=2, proxy=3, board="main")
    with pytest.raises(LedgerError, match="different card"):
        decks.edit_slot(conn, d, sid(BOLT), qty=2, proxy=0, board="main", printing_id=SOL_C21)
    with pytest.raises(LedgerError, match="etched"):
        decks.edit_slot(conn, d, sid(BOLT), qty=2, proxy=0, board="main", printing_id=None, finish="etched")
    decks.edit_slot(conn, d, sid(BOLT), qty=0, proxy=0, board="main")
    assert decks.slots(conn, d) == []


def test_mark_as_built_adds_new_copies_in_the_deck(conn, setup):
    iid = importer.import_deck(conn, "1 Sol Ring (C21) 263\n2 Lightning Bolt\nMaybeboard\n1 Delver of Secrets",
                               "Imported", owner=ALICE)
    deck = decks.get(conn, importer.get_import(conn, iid)["deck_id"])
    bolt = next(s for s in decks.slots(conn, deck["id"]) if s["printing_id"] == BOLT)
    decks.edit_slot(conn, deck["id"], decks.slot_id(bolt), qty=2, proxy=1, board="main")
    ledger.acquire(conn, setup["binder"], StockKey(SOL_C21), 1)          # free elsewhere: left alone
    batch, n = deckplan.mark_built(conn, deck)
    assert n == 2                                                          # Sol Ring + 1 bolt (1 proxy)
    st = deckplan.state(conn, deck)
    assert (st["short"], st["excess_total"]) == (0, 0)
    assert ledger.qty_at(conn, setup["binder"], StockKey(SOL_C21)) == 1
    assert ledger.qty_at(conn, deck["location_id"], StockKey(DELVER)) == 0   # maybeboard
    assert {r[0] for r in conn.execute("SELECT reason FROM movements WHERE batch_id = ?", (batch,))} == {"acquire"}
    assert deckplan.mark_built(conn, deck) == (None, 0)
    ledger.undo(conn, batch)                                               # undoable, unlike imports
    assert deckplan.state(conn, deck)["short"] == 2 and ledger.check(conn) == []


def test_analysis_warnings(conn, setup):
    cmd = decks.create(conn, "EDH", "commander", owner=ALICE)
    decks.add_slot(conn, cmd, StockKey(DELVER), "commander", 1)
    decks.add_slot(conn, cmd, StockKey(BOLT), "main", 2)
    decks.add_slot(conn, cmd, StockKey(SOL_C21), "main", 1)
    deck = decks.get(conn, cmd)
    w = deckplan.analysis(deckplan.state(conn, deck), deck)["warnings"]
    assert any("2 copies of Lightning Bolt (at most 1)" in x for x in w)
    assert any("Lightning Bolt is outside the commander's colour identity (U)" in x for x in w)
    assert any("4 cards in main + commander; commander decks have 100" in x for x in w)
    assert not any("Sol Ring is outside" in x for x in w)
    modern = setup["deck_row"]
    decks.add_slot(conn, modern["id"], StockKey(BOLT), "main", 5)
    w = deckplan.analysis(deckplan.state(conn, modern), modern)["warnings"]
    assert any("5 copies of Lightning Bolt (at most 4)" in x for x in w)
    assert any("Main deck has 5 cards" in x for x in w)


def test_paste_into_deck(conn, setup):
    added, left = importer.paste_into_deck(conn, setup["deck"], "2 Lightning Bolt\n1 Nope Card", "side",
                                           owner=ALICE)
    assert added == 2 and left[0][0] == "1 Nope Card"
    assert [(r["board"], r["qty_wanted"]) for r in decks.slots(conn, setup["deck"])] == [("side", 2)]
    importer.paste_into_deck(conn, setup["deck"], "Commander\n1 Delver of Secrets", "side", owner=ALICE)
    assert {r["board"] for r in decks.slots(conn, setup["deck"])} == {"side", "commander"}


# --------------------------------------------------------------------------- web

def test_deck_editor_pages(client, conn, setup):
    d = setup["deck"]
    r = client.post("/decks", data={"name": "Fresh", "format": "modern"}, follow_redirects=True)
    assert b"Deck created" in r.data
    r = client.post("/decks/add-card", data={"deck_id": d, "card_key": SOL_ORACLE, "qty": "1", "board": "main"},
                    follow_redirects=True)
    assert b"Added 1\xc3\x97 Sol Ring" in r.data
    page = client.get(f"/decks/{d}").data
    assert b"1 short" in page and b"Pull list" in page and b"Sol Ring is not legal in modern" in page
    assert client.get(f"/add?deck={d}&q=bolt").status_code == 200
    s = decks.slots(conn, d)[0]
    url = f"/decks/{d}/slot?slot={decks.slot_id(s)}"
    assert b"Proxies" in client.get(url).data
    client.post(f"/decks/{d}/slot", data={"slot": decks.slot_id(s), "qty": "2", "proxy": "1", "board": "main",
                                          "printing_id": SOL_CMM, "finish": "etched"})
    assert [(r["printing_id"], r["finish"], r["qty_wanted"], r["qty_proxy"]) for r in decks.slots(conn, d)] \
        == [(SOL_CMM, "etched", 2, 1)]
    ledger.acquire(conn, setup["binder"], StockKey(SOL_CMM, "etched"), 1)
    assert b"Binder" in client.get(f"/decks/{d}/pull").data
    r = client.post(f"/decks/{d}/pull", data={"all": "1"}, follow_redirects=True)
    assert b"Pulled 1 card" in r.data and b"Undo that" in r.data
    assert b"complete" in client.get("/decks").data
    client.post(f"/decks/{d}/edit", data={"name": "Burn 2", "format": "modern", "notes": "n"})
    assert locations.get(conn, setup["loc"])["name"] == "Burn 2"
    # remove the card: its copy becomes excess, return it to where it came from
    client.post(f"/decks/{d}/slot", data={"slot": sid(SOL_CMM, finish="etched"), "remove": "1"})
    page = client.get(f"/decks/{d}/return").data.decode()
    assert "Binder</option>" in page
    line = f"{SOL_CMM}|etched|NM|en"
    r = client.post(f"/decks/{d}/return", data={"line": line, f"to:{line}": setup["binder"]}, follow_redirects=True)
    assert b"Returned 1 card" in r.data
    assert ledger.qty_at(conn, setup["binder"], StockKey(SOL_CMM, "etched")) == 1


def test_retarget_and_build_routes(client, conn, setup):
    iid = importer.import_deck(conn, "2 Lightning Bolt (M10) 146", "Imp", owner=ALICE)
    deck_id = importer.get_import(conn, iid)["deck_id"]
    page = client.get(f"/decks/{deck_id}").data
    assert b"Mark as built" in page
    ledger.acquire(conn, setup["binder"], StockKey(BOLT, "foil"), 1)
    page = client.get(f"/decks/{deck_id}/pull").data
    assert b"use this printing" in page
    client.post(f"/decks/{deck_id}/retarget", data={"slot": sid(BOLT), "printing_id": BOLT, "finish": "foil",
                                                   "qty": "1"})
    assert sorted((r["finish"], r["qty_wanted"]) for r in decks.slots(conn, deck_id)) == [("foil", 1), ("nonfoil", 1)]
    r = client.post(f"/decks/{deck_id}/build", follow_redirects=True)
    assert b"Added 2 cards to your collection, in Imp." in r.data and b"Undo that" in r.data
    assert ledger.qty_at(conn, setup["binder"], StockKey(BOLT, "foil")) == 1   # not pulled
    r = client.post("/import/deck", data={"text": "1 Sol Ring (C21) 263", "name": "Built", "built": "1"},
                    follow_redirects=True)
    assert b"marked as built: 1 added" in r.data
    built = decks.get(conn, conn.execute("SELECT id FROM decks WHERE name = 'Built'").fetchone()[0])
    assert ledger.qty_at(conn, built["location_id"], StockKey(SOL_C21)) == 1


def test_card_page_add_to_deck(client, conn, setup):
    page = client.get(f"/card/{SOL_ORACLE}").data
    assert b"Add to a deck" in page
    client.post("/decks/add-card", data={"deck_id": setup["deck"], "printing_id": SOL_CMM, "finish": "foil",
                                         "qty": "1", "board": "side"})
    assert [(r["printing_id"], r["finish"], r["board"]) for r in decks.slots(conn, setup["deck"])] \
        == [(SOL_CMM, "foil", "side")]


def test_deck_locations_show_as_decks_slash_name(client, conn, setup):
    decks.add_slot(conn, setup["deck"], StockKey(SOL_C21), "main", 1)
    ledger.acquire(conn, setup["binder"], StockKey(SOL_C21), 1)
    deckplan.confirm_pulls(conn, ALICE)
    for url in ["/collection", f"/card/{SOL_ORACLE}", "/history"]:
        assert "Decks / Burn" in client.get(url).data.decode(), url
    page = client.get("/locations").data.decode()
    assert ">Burn</a>" not in page and ">Decks</a>" in page
    assert "Decks</a></td><td>decks</td><td class=\"num\">1</td>" in page
    page = client.get(f"/decks/{setup['deck']}").data.decode()
    assert "Physically in the deck (1)" in page and "Recent moves in and out" in page


def test_where_columns_only_while_short(client, conn, setup):
    """Free / Where (free) say where to fetch copies: only shown for short cards."""
    d, binder = setup["deck_row"], setup["binder"]
    decks.add_slot(conn, d["id"], StockKey(SOL_C21), "main", 1)
    decks.add_slot(conn, d["id"], StockKey(BOLT), "main", 1)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 1)
    ledger.acquire(conn, binder, StockKey(SOL_CMM), 1)        # another printing of Sol Ring
    page = client.get(f"/decks/{d['id']}").data.decode()
    assert "Where (free)" in page and "Binder" in page          # both short, Sol Ring free in the binder

    deckplan.confirm_pulls(conn, ALICE, deck_ids=[d["id"]])    # Sol Ring C21 into the deck
    page = client.get(f"/decks/{d['id']}").data.decode()
    assert "Where (free)" in page                               # Bolt still short ...
    sol_row = next(r for r in page.split("<tr>") if "Sol Ring" in r)
    assert "Binder" not in sol_row and "other printings" not in sol_row   # ... but Sol Ring isn't

    ledger.acquire(conn, binder, StockKey(BOLT), 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[d["id"]])
    page = client.get(f"/decks/{d['id']}").data.decode()
    assert "Where (free)" not in page and "<th>Free</th>" not in page   # complete: no fetch columns
    assert "<th>Status</th>" in page and "<b>1 short</b>" not in page


def test_owned_elsewhere_hint_ignores_this_deck(client, conn, setup):
    d, binder = setup["deck_row"], setup["binder"]
    decks.add_slot(conn, d["id"], StockKey(SOL_C21), "main", 2)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[d["id"]])    # 1 in the deck, 1 short, none free
    assert "other printings or other decks" not in client.get(f"/decks/{d['id']}").data.decode()
    ledger.acquire(conn, binder, StockKey(SOL_CMM), 1)
    assert "1 in other printings or other decks" in client.get(f"/decks/{d['id']}").data.decode()


def test_slot_page_lists_your_copies(client, conn, setup):
    d, binder = setup["deck_row"], setup["binder"]
    decks.add_slot(conn, d["id"], StockKey(SOL_C21), "main", 1)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 2)
    ledger.acquire(conn, binder, StockKey(SOL_CMM, "foil"), 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[d["id"]])
    page = client.get(f"/decks/{d['id']}/slot?slot={sid(SOL_C21)}").data.decode()
    assert "Your copies of Sol Ring" in page
    assert "Decks / Burn</a> <span class=\"muted\">(this deck)" in page
    assert page.count("(free)") == 2 and "foil" in page



def test_status_column(client, conn, setup):
    """In deck / proxies / short in one cell."""
    import re
    d, binder = setup["deck_row"], setup["binder"]
    decks.add_slot(conn, d["id"], StockKey(SOL_C21), "main", 1)              # in the deck
    decks.add_slot(conn, d["id"], StockKey(BOLT), "main", 3, proxy=1)        # 1 in, 1 proxy, 1 short
    decks.add_slot(conn, d["id"], StockKey(DELVER), "main", 1, proxy=1)      # proxied
    decks.add_slot(conn, d["id"], StockKey(SOL_CMM), "maybe", 1)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 1)
    ledger.acquire(conn, binder, StockKey(BOLT), 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[d["id"]])
    page = client.get(f"/decks/{d['id']}").data.decode()

    def status(name: str) -> list[str]:   # the Status cell of each row for that card
        return [c.strip() for c in re.findall(
            re.escape(name) + r'(?:<img [^>]*>)?</a></td>\s*<td class="cost">.*?</td>\s*<td>.*?</td>\s*<td>(.*?)</td>', page, re.S)]

    assert status("Sol Ring") == ["&#10003;", '<span class="muted">doesn\'t count</span>']
    assert status("Lightning Bolt") == ["1 in deck, 1 proxy, <b>1 short</b>"]
    assert status("Delver of Secrets // Insectile Aberration") == ["&#10003; 1 proxy"]


def test_manage_page_holds_settings_sharing_export_delete(client, conn, setup):
    d = setup["deck"]
    page = client.get(f"/decks/{d}").data.decode()
    assert "Manage deck" in page
    for moved in ("<h2>Deck settings</h2>", "<h2>Export</h2>", "Delete deck", 'name="visibility"'):
        assert moved not in page, moved
    page = client.get(f"/decks/{d}/manage").data.decode()
    for there in ("<h2>Deck settings</h2>", "<h2>Export</h2>", "Delete deck", 'name="visibility"',
                  f'href="/decks/{d}"'):
        assert there in page, there
    r = client.post(f"/decks/{d}/edit", data={"name": "Burn 3", "format": "modern", "notes": ""})
    assert r.headers["Location"].endswith(f"/decks/{d}/manage")
    r = client.post(f"/locations/{setup['loc']}/visibility", data={"visibility": "users"})
    assert r.headers["Location"].endswith(f"/decks/{d}/manage")
    decks.add_slot(conn, d, StockKey(BOLT), "main", 1)
    ledger.acquire(conn, setup["binder"], StockKey(BOLT), 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[d])
    page = client.get(f"/decks/{d}/manage").data.decode()
    assert "Delete deck" not in page and "can't be deleted" in page
