"""Wishlist: per-user list of wanted cards, separate from decks and the buy list."""

import pytest
from conftest import ALICE, SOL_C21, SOL_CMM, SOL_ORACLE, BOLT, login_as

from franticsearch import exports, ledger, locations, users, wishlist
from franticsearch.ledger import LedgerError, StockKey

BOLT_ORACLE = "aaaaaaaa-0000-0000-0000-000000000004"


@pytest.fixture
def bob(conn):
    return users.create(conn, "bob", "bob's password")


def test_add_merges_identical_lines(conn):
    a = wishlist.add(conn, SOL_ORACLE, 1, owner=ALICE)
    assert wishlist.add(conn, SOL_ORACLE, 2, note="for EDH", owner=ALICE) == a
    b = wishlist.add(conn, SOL_ORACLE, 1, SOL_CMM, "foil", owner=ALICE)
    assert a != b
    lines = wishlist.lines(conn, ALICE)
    assert [(l["qty"], l["set_code"], l["finish"], l["note"]) for l in lines] == [
        (3, None, None, "for EDH"), (1, "cmm", "foil", "")]
    assert {l["name"] for l in lines} == {"Sol Ring"}


def test_add_validates(conn):
    with pytest.raises(LedgerError):
        wishlist.add(conn, "nope", 1, owner=ALICE)
    with pytest.raises(LedgerError):
        wishlist.add(conn, SOL_ORACLE, 1, BOLT, owner=ALICE)       # printing of another card
    with pytest.raises(LedgerError):
        wishlist.add(conn, SOL_ORACLE, 1, SOL_C21, "foil", owner=ALICE)  # C21 has no foil
    with pytest.raises(LedgerError):
        wishlist.add(conn, SOL_ORACLE, 0, owner=ALICE)
    wishlist.add(conn, SOL_ORACLE, 1, finish="etched", owner=ALICE)  # some printing is etched


def test_owned_counts_matching_copies(conn):
    box = locations.create(conn, "Box", "box", owner=ALICE)
    ledger.acquire(conn, box, StockKey(SOL_C21), 2)
    wishlist.add(conn, SOL_ORACLE, 4, owner=ALICE)
    wishlist.add(conn, SOL_ORACLE, 1, SOL_CMM, owner=ALICE)
    assert [l["owned"] for l in wishlist.lines(conn, ALICE)] == [2, 0]


def test_got_it_acquires_and_reduces(conn):
    box = locations.create(conn, "Box", "box", owner=ALICE)
    w = wishlist.add(conn, SOL_ORACLE, 3, owner=ALICE)
    wishlist.got(conn, w, box, StockKey(SOL_CMM, "foil"), 2)
    assert ledger.qty_at(conn, box, StockKey(SOL_CMM, "foil")) == 2
    assert wishlist.get(conn, w)["qty"] == 1
    batch = wishlist.got(conn, w, box, StockKey(SOL_C21), 1)
    assert wishlist.get(conn, w) is None
    ledger.undo(conn, batch)  # removes the copy, the line stays gone
    assert ledger.qty_at(conn, box, StockKey(SOL_C21)) == 0
    assert ledger.check(conn) == []


def test_got_it_rejects_other_cards_and_rolls_back(conn):
    box = locations.create(conn, "Box", "box", owner=ALICE)
    w = wishlist.add(conn, SOL_ORACLE, 1, owner=ALICE)
    with pytest.raises(LedgerError):
        wishlist.got(conn, w, box, StockKey(BOLT), 1)
    with pytest.raises(LedgerError):
        wishlist.got(conn, w, box, StockKey(SOL_C21, "foil"), 1)  # no such finish
    assert wishlist.get(conn, w)["qty"] == 1
    assert ledger.qty_at(conn, box, StockKey(SOL_C21)) == 0


def test_update_and_merge(conn):
    a = wishlist.add(conn, SOL_ORACLE, 1, owner=ALICE)
    b = wishlist.add(conn, SOL_ORACLE, 2, SOL_CMM, owner=ALICE)
    wishlist.update(conn, b, 2, None, None, "")       # becomes identical to a: merged
    assert wishlist.get(conn, b) is None and wishlist.get(conn, a)["qty"] == 3
    wishlist.update(conn, a, 0, None, None, "")
    assert wishlist.lines(conn, ALICE) == []


def test_paste(conn):
    added, left = wishlist.paste(conn, "2 Sol Ring\n1 Sol Ring (CMM) 410 *F*\n1 Black Lotus\n1 Lightning Bolt",
                                 owner=ALICE)
    assert added == 4 and [p for _, p in left] == ["no card named 'Black Lotus'"]
    got = {(l["name"], l["set_code"], l["finish"], l["qty"]) for l in wishlist.lines(conn, ALICE)}
    assert got == {("Sol Ring", None, None, 2), ("Sol Ring", "cmm", "foil", 1), ("Lightning Bolt", None, None, 1)}


def test_exports_any_printing(conn):
    wishlist.add(conn, SOL_ORACLE, 2, owner=ALICE)
    wishlist.add(conn, SOL_ORACLE, 1, SOL_CMM, "foil", owner=ALICE)
    lines = wishlist.lines(conn, ALICE)
    assert exports.render(lines, "moxfield") == "2 Sol Ring\n1 Sol Ring (CMM) 410 *F*\n"
    assert exports.render(lines, "tcgplayer") == "2 Sol Ring\n1 Sol Ring [CMM]\n"
    assert exports.render(lines, "csv").splitlines()[1:] == ["2,Sol Ring,,,", "1,Sol Ring,cmm,410,foil"]


def test_wishlists_are_per_user_and_sharing(conn, bob):
    wishlist.add(conn, SOL_ORACLE, 1, owner=ALICE)
    assert wishlist.lines(conn, bob) == []
    assert not wishlist.can_view(conn, ALICE, bob) and wishlist.shared(conn, bob) == []
    assert wishlist.wanted_by(conn, SOL_ORACLE, bob) == []
    wishlist.set_visibility(conn, ALICE, "users")
    assert wishlist.can_view(conn, ALICE, bob) and not wishlist.can_view(conn, ALICE, None)
    assert [r["cards"] for r in wishlist.shared(conn, bob)] == [1]
    assert [r["owner_name"] for r in wishlist.wanted_by(conn, SOL_ORACLE, bob)] == ["alice"]
    assert wishlist.by_token(conn, "") is None and wishlist.settings(conn, ALICE)["share_token"] is None
    wishlist.set_visibility(conn, ALICE, "link")
    token = wishlist.settings(conn, ALICE)["share_token"]
    assert wishlist.by_token(conn, token) == ALICE
    wishlist.set_visibility(conn, ALICE, "link", new_link=True)
    assert wishlist.by_token(conn, token) is None
    wishlist.set_visibility(conn, ALICE, "private")
    assert not wishlist.can_view(conn, ALICE, bob)


# --------------------------------------------------------------------------- web

def test_wishlist_pages(client, conn):
    r = client.post("/wishlist/add", data={"card_key": SOL_ORACLE, "qty": "2", "printing_id": "", "finish": "",
                                          "note": "spare", "next": f"/card/{SOL_ORACLE}"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/card/{SOL_ORACLE}")
    page = client.get(f"/card/{SOL_ORACLE}").get_data(as_text=True)
    assert "On your" in page and "2&times; any printing" in page
    page = client.get("/wishlist").get_data(as_text=True)
    assert "Sol Ring" in page and "spare" in page and "2 Sol Ring" in page
    assert "Sol Ring" in client.get("/wishlist?view=images").get_data(as_text=True)
    assert client.get("/wishlist/export?format=text").get_data(as_text=True) == "2 Sol Ring\n"
    w = wishlist.lines(conn, ALICE)[0]["id"]
    assert b"Got it" in client.get(f"/wishlist/{w}").data and b"Remove from wishlist" in client.get(f"/wishlist/{w}").data
    # got it with no location goes to Unsorted
    r = client.post(f"/wishlist/{w}/got", data={"printing_id": SOL_C21, "finish": "nonfoil", "qty": "1",
                                               "location_id": "", "condition": "NM", "lang": "en"})
    assert r.status_code == 302
    unsorted = locations.unsorted(conn, owner=ALICE)
    assert ledger.qty_at(conn, unsorted, StockKey(SOL_C21)) == 1 and wishlist.get(conn, w)["qty"] == 1
    client.post(f"/wishlist/{w}/edit", data={"qty": "5", "printing_id": SOL_CMM, "finish": "etched", "note": ""})
    assert (wishlist.get(conn, w)["qty"], wishlist.get(conn, w)["finish"]) == (5, "etched")
    client.post("/wishlist/paste", data={"text": "1 Lightning Bolt\nnonsense card"})
    assert len(wishlist.lines(conn, ALICE)) == 2
    client.post(f"/wishlist/{w}/remove")
    assert wishlist.get(conn, w) is None


def test_shared_wishlist_web(app, client, conn, bob):
    wishlist.add(conn, SOL_ORACLE, 1, note="please", owner=ALICE)
    bob_client = login_as(app.test_client(), bob)
    assert bob_client.get(f"/people/{ALICE}/wishlist").status_code == 404
    client.post("/wishlist/visibility", data={"visibility": "link"})
    page = bob_client.get(f"/people/{ALICE}/wishlist").get_data(as_text=True)
    assert "alice's wishlist" in page and "please" in page and "Got it" not in page
    assert "alice's wishlist" in bob_client.get("/people").get_data(as_text=True)
    assert "Wanted by" in bob_client.get(f"/card/{SOL_ORACLE}").get_data(as_text=True)
    token = wishlist.settings(conn, ALICE)["share_token"]
    anon = app.test_client().get(f"/w/{token}").get_data(as_text=True)
    assert "Sol Ring" in anon and "/card/" not in anon
    assert app.test_client().get("/w/wrong").status_code == 404


def test_other_users_wishes_are_not_found(app, conn, bob):
    w = wishlist.add(conn, SOL_ORACLE, 1, owner=ALICE)
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    bob_client = login_as(app.test_client(), bob)
    assert bob_client.get(f"/wishlist/{w}").status_code == 404
    for url, data in [(f"/wishlist/{w}/edit", {"qty": "3"}), (f"/wishlist/{w}/remove", {}),
                      (f"/wishlist/{w}/got", {"printing_id": SOL_C21, "finish": "nonfoil", "qty": "1"})]:
        assert bob_client.post(url, data=data).status_code == 404, url
    # bob's own line can't be "got" into alice's location
    mine = wishlist.add(conn, SOL_ORACLE, 1, owner=bob)
    assert bob_client.post(f"/wishlist/{mine}/got", data={"printing_id": SOL_C21, "finish": "nonfoil", "qty": "1",
                                                          "location_id": binder}).status_code == 404
    assert wishlist.get(conn, w)["qty"] == 1 and wishlist.get(conn, mine)["qty"] == 1
    assert ledger.qty_at(conn, binder, StockKey(SOL_C21)) == 0
