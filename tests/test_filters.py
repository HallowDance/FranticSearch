import pytest
from conftest import ALICE, BOLT, DELVER, SOL_C21, SOL_CMM

from franticsearch import cardtypes, deckplan, decks, ledger, locations, queries, users
from franticsearch.ledger import StockKey


def test_parse_type_lines():
    assert cardtypes.parse("Legendary Creature — Human Wizard // Creature — Human Insect") == (
        {"Legendary"}, {"Creature"}, {"Human", "Wizard", "Insect"})
    assert cardtypes.parse("Legendary Creature — Time Lord Doctor") == (
        {"Legendary"}, {"Creature"}, {"Time Lord", "Doctor"})
    assert cardtypes.parse("Kindred Enchantment — Treefolk Aura") == (
        set(), {"Kindred", "Enchantment"}, {"Treefolk", "Aura"})
    assert cardtypes.parse("Basic Snow Land — Island") == ({"Basic", "Snow"}, {"Land"}, {"Island"})
    assert cardtypes.parse(None) == (set(), set(), set())


def _binder(conn):
    loc = locations.create(conn, "Trade binder", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_C21), 1)
    ledger.acquire(conn, loc, StockKey(SOL_CMM, "foil"), 3)
    ledger.acquire(conn, loc, StockKey(BOLT), 4)
    ledger.acquire(conn, loc, StockKey(DELVER), 1)
    return loc


def _names(page: bytes) -> list[str]:
    """Card names in the contents list (not the history table further down)."""
    page = page.split(b"<h2>Recent history")[0]
    return [n for n in ("Sol Ring", "Lightning Bolt", "Delver of Secrets") if f">{n}".encode() in page]


def test_location_filters(client, conn):
    loc = _binder(conn)
    url = f"/locations/{loc}"
    page = client.get(url).data
    assert b"<legend>Filter</legend>" in page
    assert b">Creature</option>" in page and b">Insect</option>" in page   # facets from contents
    assert b">Equipment</option>" not in page
    assert _names(client.get(url + "?type=Creature").data) == ["Delver of Secrets"]
    assert _names(client.get(url + "?subtype=Insect").data) == ["Delver of Secrets"]  # back face counts
    assert _names(client.get(url + "?type=Instant").data) == ["Lightning Bolt"]
    assert _names(client.get(url + "?color=R").data) == ["Lightning Bolt"]
    assert _names(client.get(url + "?color=C").data) == ["Sol Ring"]
    assert _names(client.get(url + "?q=sol").data) == ["Sol Ring"]
    page = client.get(url + "?finish=foil").data.split(b"<h2>Recent history")[0]
    assert _names(page) == ["Sol Ring"] and b"CMM #410" in page and b"C21 #263" not in page
    assert _names(client.get(url + "?set=c21").data) == ["Sol Ring"]
    assert _names(client.get(url + "?min_qty=3").data) == ["Sol Ring", "Lightning Bolt"]
    page = client.get(url + "?type=Instant&mv=1").data
    assert _names(page) == ["Lightning Bolt"] and b"showing 1 of 4 lines" in page
    assert b"No cards match the filter" in client.get(url + "?type=Creature&color=R").data
    assert client.get(url + "?min_qty=abc&mv=99").status_code == 200        # junk is harmless


def test_filters_keep_sort_and_tick_only_shown_rows(client, conn):
    loc = _binder(conn)
    page = client.get(f"/locations/{loc}?finish=foil&all=1&sort=-qty").data.decode()
    assert page.count('name="row"') == 1 and page.count(" checked ") + page.count(" checked>") == 1
    assert 'name="sort" value="-qty"' in page                   # the filter form keeps the sort
    assert "finish=foil" in page[page.index('class="sort"') - 200:page.index('class="sort"')]  # sorting keeps the filter


def test_shared_view_can_filter(client, app, conn):
    loc = _binder(conn)
    client.post(f"/locations/{loc}/visibility", data={"visibility": "link"})
    token = locations.get(conn, loc)["share_token"]
    page = app.test_client().get(f"/s/{token}?type=Creature").data
    assert _names(page) == ["Delver of Secrets"] and b"showing 1 of 4 lines" in page


# --------------------------------------------------------------------------- collection

@pytest.fixture
def spread(conn):
    """The trade binder, plus a box with a Bolt and a deck holding another Bolt."""
    binder = _binder(conn)
    box = locations.create(conn, "Box", "box", owner=ALICE)
    ledger.acquire(conn, box, StockKey(BOLT), 2)
    deck = decks.create(conn, "Burn", "modern", owner=ALICE)
    decks.add_slot(conn, deck, StockKey(BOLT), "main", 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[deck])   # most copies first: one Bolt from the binder
    return {"binder": binder, "box": box, "deck": deck}


def test_collection_card_filters(client, spread):
    page = client.get("/collection").data
    assert b"<legend>Filter</legend>" in page and b">Insect</option>" in page and b">Equipment</option>" not in page
    assert _names(page) == ["Sol Ring", "Lightning Bolt", "Delver of Secrets"]
    for query, names in [("type=Creature", ["Delver of Secrets"]), ("subtype=Insect", ["Delver of Secrets"]),
                         ("type=Instant", ["Lightning Bolt"]), ("color=R", ["Lightning Bolt"]),
                         ("color=C", ["Sol Ring"]), ("set=cmm", ["Sol Ring"]), ("finish=foil", ["Sol Ring"]),
                         ("q=ring", ["Sol Ring"]), ("mv=7%2B", [])]:
        assert _names(client.get(f"/collection?{query}").data) == names, query
    page = client.get("/collection?type=Instant").data
    assert b"showing 1 of 3 cards" in page and b"3 cards." in client.get("/collection").data
    assert client.get("/collection?location=abc&page=x&min_qty=y").status_code == 200    # junk is harmless


def test_collection_location_filters_and_card_totals(client, spread):
    def rows(query):   # (name, copies) per card row
        import re
        page = client.get(f"/collection?{query}").data.decode()
        return re.findall(r'class="cardlink"[^>]*>([^<]+)(?:<img[^>]*>)?</a></td>\s*<td class="cost">.*?</td>\s*'
                          r'<td>.*?</td>\s*<td class="num">(\d+)</td>', page, re.S)
    assert rows("q=bolt") == [("Lightning Bolt", "6")]                  # 3 binder + 2 box + 1 deck
    assert rows(f"location={spread['box']}") == [("Lightning Bolt", "2")]
    assert rows("ltype=deck") == [("Lightning Bolt", "1")]
    assert rows("q=bolt&notindeck=1") == [("Lightning Bolt", "5")]
    assert rows("finish=foil") == [("Sol Ring", "3")]                    # only the matching copies count
    assert rows("min_qty=4") == [("Lightning Bolt", "6"), ("Sol Ring", "4")]   # per card, over its copies
    assert rows("min_qty=6&notindeck=1") == []
    assert [n for n, _ in rows("sort=-qty")] == ["Lightning Bolt", "Sol Ring", "Delver of Secrets // Insectile Aberration"]
    page = client.get("/collection?q=bolt").data.decode()
    assert "Box, Decks / Burn, Trade binder" in page


def test_collection_pages_keep_the_filter(client, spread, monkeypatch):
    monkeypatch.setattr(queries, "PAGE_SIZE", 1)
    assert "Page:" not in client.get("/collection?color=C").data.decode()   # one card, one page
    page = client.get("/collection?mv=1&sort=name").data.decode()
    assert _names(page.encode()) == ["Delver of Secrets"] and "Page:" in page
    assert "mv=1" in page[page.index("Page:"):] and "page=2" in page[page.index("Page:"):]
    assert _names(client.get("/collection?mv=1&sort=name&page=3").data) == ["Sol Ring"]


# --------------------------------------------------------------------------- decks

@pytest.fixture
def burn(conn):
    """Bolt x4 (3 in the deck + 1 proxy: complete), Delver (short), Sol Ring on the side
    (short) and in the maybeboard."""
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    ledger.acquire(conn, binder, StockKey(BOLT), 3)
    deck = decks.create(conn, "Burn", "modern", owner=ALICE)
    decks.add_slot(conn, deck, StockKey(BOLT), "main", 4, proxy=1)
    decks.add_slot(conn, deck, StockKey(DELVER), "main", 1)
    decks.add_slot(conn, deck, StockKey(SOL_C21), "side", 1)
    decks.add_slot(conn, deck, StockKey(SOL_CMM), "maybe", 1)
    deckplan.confirm_pulls(conn, ALICE, deck_ids=[deck])
    return deck


def _deck_names(page: bytes) -> list[str]:
    """Card names in the deck's list (not the format warnings above it or the stats below)."""
    return _names(page.split(b'<p class="viewswitch">', 1)[1].split(b"<h2>Stats")[0])


def test_deck_filters(client, burn):
    url = f"/decks/{burn}"
    page = client.get(url).data
    assert b"<legend>Filter</legend>" in page and b'name="status"' in page and b'name="min_qty"' not in page
    assert _deck_names(page) == ["Sol Ring", "Lightning Bolt", "Delver of Secrets"]
    for query, names in [("board=side", ["Sol Ring"]), ("board=main", ["Lightning Bolt", "Delver of Secrets"]),
                         ("status=short", ["Sol Ring", "Delver of Secrets"]),
                         ("status=proxied", ["Lightning Bolt"]), ("status=complete", ["Lightning Bolt"]),
                         ("type=Creature", ["Delver of Secrets"]), ("color=C&board=maybe", ["Sol Ring"]),
                         ("type=Creature&color=R", [])]:
        assert _deck_names(client.get(f"{url}?{query}").data) == names, query
    page = client.get(f"{url}?board=main").data
    assert b"showing 5 of 7 cards" in page
    assert b"No cards match the filter" in client.get(f"{url}?type=Creature&color=R").data


def test_deck_stats_ignore_the_filter(client, burn):
    def stats(page: bytes) -> bytes:
        import re
        part = page.split(b"<h2>Stats")[1].split(b"<h2")[0]
        return re.sub(rb"\s+", b" ", part.replace(b'<p class="muted">For the whole deck, not just the filtered cards.</p>', b""))
    url = f"/decks/{burn}"
    full, filtered = client.get(url).data, client.get(f"{url}?type=Creature").data
    assert stats(full) == stats(filtered) and b"For the whole deck" in filtered
    assert full.split(b'<p class="status">')[1][:60] == filtered.split(b'<p class="status">')[1][:60]


def test_shared_deck_filters_without_status(client, app, conn, burn):
    bob = users.create(conn, "bob", "bob's password")
    client.post(f"/locations/{decks.get(conn, burn)['location_id']}/visibility", data={"visibility": "users"})
    from conftest import login_as
    bob_client = login_as(app.test_client(), bob)
    page = bob_client.get(f"/decks/{burn}?status=short").data
    assert b'name="status"' not in page and b'name="board"' in page
    assert _deck_names(page) == ["Sol Ring", "Lightning Bolt", "Delver of Secrets"]   # status ignored
    assert _deck_names(bob_client.get(f"/decks/{burn}?type=Creature").data) == ["Delver of Secrets"]
