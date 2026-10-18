from conftest import ALICE, BOLT, DELVER, SOL_C21, SOL_CMM

from franticsearch import cardtypes, ledger, locations
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
