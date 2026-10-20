import pytest
from conftest import ALICE, BOLT, PASSWORD, SOL_C21, SOL_CMM, SOL_ORACLE, login_as

from franticsearch import ledger, locations
from franticsearch.app import create_app, safe_next
from franticsearch.ledger import StockKey


def test_pages_render(client, conn):
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_C21), 2)
    for url in ["/", "/collection", "/collection?q=sol&notindeck=1", "/add?q=sol",
                f"/card/{SOL_ORACLE}", "/locations", f"/locations/{loc}", "/history",
                "/system", "/system?check=1"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert b"Sol Ring" in client.get("/collection?q=sol").data
    assert b"OK" in client.get("/system?check=1").data
    assert client.get("/card/does-not-exist").status_code == 404


def test_full_manual_flow(client, conn):
    r = client.post("/locations", data={"name": "Binder A", "type": "binder"}, follow_redirects=True)
    assert b"Location created" in r.data
    client.post("/locations", data={"name": "Box", "type": "box"})
    a, b = (r["id"] for r in conn.execute("SELECT id FROM locations ORDER BY id"))

    key = {"printing_id": SOL_CMM, "finish": "foil", "condition": "NM", "lang": "en"}
    r = client.post("/stock/acquire", data={**key, "qty": "3", "location_id": a}, follow_redirects=True)
    assert b"Added 3 copies" in r.data
    r = client.post("/stock/move", data={**key, "qty": "2", "from_id": a, "to_id": b}, follow_redirects=True)
    assert b"Moved 2" in r.data
    client.post("/stock/adjust", data={**key, "qty": "5", "location_id": b})
    client.post("/stock/sell", data={**key, "qty": "1", "location_id": a})
    assert ledger.qty_at(conn, a, StockKey(SOL_CMM, "foil")) == 0
    assert ledger.qty_at(conn, b, StockKey(SOL_CMM, "foil")) == 5
    assert ledger.check(conn) == []

    r = client.post("/stock/move", data={**key, "qty": "9", "from_id": b, "to_id": a}, follow_redirects=True)
    assert b"only 5 copies" in r.data
    r = client.post("/stock/acquire", data={**key, "printing_id": SOL_C21, "qty": "1", "location_id": a},
                    follow_redirects=True)
    assert b"does not exist in foil" in r.data

    hist = client.get("/history").data
    assert hist.count(b"<td>adjust</td>") == 1 and b"<td>sell</td>" in hist


def test_redirect_next_must_be_local(client, conn):
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    r = client.post("/stock/sell", data={"printing_id": "x", "qty": "1", "location_id": loc,
                                         "next": "//evil.example/"})
    assert r.headers["Location"] == "/collection"


@pytest.mark.parametrize("nxt", ["//evil.example/", "/\t/evil.example", "/\n/evil.example",
                                 "/\\evil.example", "https://evil.example/", "evil.example",
                                 "/\x7f/evil.example", " /x"])
def test_safe_next_rejects_offsite(nxt):
    assert safe_next(nxt, "/home") == "/home"


@pytest.mark.parametrize("nxt", ["/", "/collection", "/add?q=sol%20ring&deck=3", "/decks/1#contents",
                                 "/franticsearch/locations/2?sort=-qty"])
def test_safe_next_keeps_local_paths(nxt):
    assert safe_next(nxt, "/home") == nxt


def test_tab_in_next_does_not_redirect_offsite(client, conn):
    # Werkzeug drops the tab when building Location: this used to send "//evil.example"
    r = client.post("/stock/acquire", data={"next": "/\t/evil.example"})
    assert r.headers["Location"] == "/collection"


def test_archive_via_web(client, conn):
    loc = locations.create(conn, "Old binder", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_C21), 1)
    assert b"Archive location" in client.get(f"/locations/{loc}").data
    r = client.post(f"/locations/{loc}/archive", data={"archived": "1"}, follow_redirects=True)
    assert b"move all cards out" in r.data
    ledger.sell(conn, loc, StockKey(SOL_C21), 1)
    r = client.post(f"/locations/{loc}/archive", data={"archived": "1"}, follow_redirects=True)
    assert b"Location archived" in r.data and b"Unarchive" in r.data
    page = client.get("/locations").data
    assert b"Archived locations (1)" in page
    assert b"Old binder" not in client.get(f"/card/{SOL_ORACLE}").data.split(b"Add copies")[1].split(b"Recent history")[0]
    r = client.post(f"/locations/{loc}/archive", data={"archived": "0"}, follow_redirects=True)
    assert b"Location restored" in r.data


def test_import_pages_and_deck_flow(client, conn):
    assert client.get("/import").status_code == 200
    assert client.get("/decks").status_code == 200
    r = client.post("/import/deck", data={"text": "1 Sol Ring\n1 Black Lotus\n\n1 Lightning Bolt",
                                          "name": "Burn", "format": "commander",
                                          "after_blank": "commander"}, follow_redirects=True)
    assert b"Created deck Burn with 2 cards; commander: Lightning Bolt." in r.data
    assert b"no card named" in r.data
    deck_id = conn.execute("SELECT id FROM decks").fetchone()[0]
    page = client.get(f"/decks/{deck_id}").data
    assert b"Commander (1)" in page and b"Lightning Bolt" in page and b"Delete deck" in page
    loc_id = conn.execute("SELECT location_id FROM decks").fetchone()[0]
    r = client.get(f"/locations/{loc_id}")                    # deck locations live on the deck page
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/decks/{deck_id}#contents")

    uid = conn.execute("SELECT id FROM import_unresolved").fetchone()[0]
    assert b"Delver" in client.get(f"/import/line/{uid}?q=delver").data
    page = client.get(f"/import/line/{uid}?card=aaaaaaaa-0000-0000-0000-000000000001").data
    assert b"Use this printing" in page
    r = client.post(f"/import/line/{uid}/fix", data={"printing_id": SOL_CMM, "finish": "foil",
                                                     "condition": "NM", "lang": "en", "qty": "1"},
                    follow_redirects=True)
    assert b"Line fixed" in r.data
    r = client.post(f"/decks/{deck_id}/delete", follow_redirects=True)
    assert b"Deck deleted" in r.data


def test_collection_csv_upload_creates_unsorted(client, conn):
    import io
    csv = (b"Count,Name,Edition,Collector Number,Foil,Condition,Language,Proxy\n"
           b"2,Sol Ring,c21,263,,Near Mint,English,False\n")
    page = client.get("/import").data
    assert b"Unsorted (new box)" in page
    r = client.post("/import/collection", data={"location_id": "new", "file": (io.BytesIO(csv), "c.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 2 copies into Unsorted" in r.data
    r = client.post("/import/collection", data={"location_id": "new", "file": (io.BytesIO(csv), "c.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"already imported" in r.data
    assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 1
    assert ledger.check(conn) == []


def test_card_links_carry_hover_image(client, conn):
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_C21), 1)
    client.post("/import/deck", data={"text": "1 Sol Ring (C21) 263", "name": "D", "format": ""})
    deck_id = conn.execute("SELECT id FROM decks").fetchone()[0]
    peek = b'class="peek" src="https://cards.scryfall.io/normal/x.jpg"'
    for url in ["/collection", "/add?q=sol", f"/locations/{loc}", "/history", f"/decks/{deck_id}",
                f"/card/{SOL_ORACLE}"]:
        assert peek in client.get(url).data, url


def test_behind_proxy_under_a_path_prefix(conn, db_path):
    from franticsearch.app import create_app
    app = create_app({"DATABASE": db_path, "AUTO_SYNC": False, "TESTING": True, "PROXY": True,
                      "CSRF": False})
    c = login_as(app.test_client(), ALICE)
    h = {"X-Forwarded-Prefix": "/franticsearch", "X-Forwarded-Proto": "https",
         "X-Forwarded-Host": "example.org"}
    page = c.get("/locations", headers=h).data
    assert b'href="/franticsearch/collection"' in page and b"/franticsearch/static/style.css" in page
    r = c.post("/locations", data={"name": "Binder A", "type": "binder"}, headers=h)
    assert r.headers["Location"].startswith("/franticsearch/locations/")
    loc = locations.list_all(conn, ALICE)[0]["id"]
    ledger.acquire(conn, loc, StockKey(SOL_C21), 1)
    page = c.get(f"/locations/{loc}", headers=h).data
    assert b'name="next" value="/franticsearch/locations/' in page
    # without the proxy setting, forwarded headers are ignored
    from franticsearch.app import create_app as plain
    p = login_as(plain({"DATABASE": db_path, "AUTO_SYNC": False, "TESTING": True}).test_client(), ALICE)
    assert b"/franticsearch/" not in p.get("/locations", headers=h).data


def test_mana_symbols_are_escaped_images():
    from franticsearch.app import mana_symbols
    html = str(mana_symbols("{2}{W/U}{T}: <b>x</b> {½}"))
    assert 'card-symbols/2.svg' in html and 'card-symbols/WU.svg' in html and 'card-symbols/T.svg' in html
    assert 'card-symbols/HALF.svg' in html and "&lt;b&gt;" in html and "<b>" not in html
    assert str(mana_symbols(None)) == ""


def test_quick_add_nav_search_and_undo(client, conn):
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    page = client.get("/add?q=sol").data
    assert b"Quick add" in page and b"card-symbols/1.svg" in page
    r = client.post("/stock/quick-add", data={"card_key": SOL_ORACLE, "qty": "2", "location_id": loc,
                                               "next": "/add?q=sol"}, follow_redirects=True)
    assert b"Added 2\xc3\x97 Sol Ring (CMM #410) to Binder A." in r.data   # newest paper printing
    assert b"Undo that" in r.data
    assert ledger.qty_at(conn, loc, StockKey(SOL_CMM)) == 2
    # the location is remembered for the next quick add
    assert f'value="{loc}" selected'.encode() in client.get("/add?q=bolt").data
    batch = conn.execute("SELECT batch_id FROM movements").fetchone()[0]
    r = client.post("/undo", data={"batch": batch, "next": "/history"}, follow_redirects=True)
    assert b"Undone." in r.data and b"undone" in r.data
    assert ledger.qty_at(conn, loc, StockKey(SOL_CMM)) == 0
    # nav search jumps straight to a single match, else lists results
    assert client.get("/add?q=lightning&go=1").headers["Location"].startswith("/card/")
    assert client.get("/add?q=zzz&go=1").status_code == 200
    assert client.get("/add?q=delver of secrets&go=1").status_code == 302   # exact front-face name


def test_location_bulk_move_paste_and_sort(client, conn):
    a = locations.create(conn, "Binder A", "binder", owner=ALICE)
    b = locations.create(conn, "Box B", "box", owner=ALICE)
    r = client.post(f"/locations/{a}/paste",
                    data={"text": "2 Sol Ring (C21) 263\nLightning Bolt\n3 Black Lotus\n"},
                    follow_redirects=True)
    assert b"Added 3 copies." in r.data and b"1 line(s) didn&#39;t match" in r.data
    assert b"3 Black Lotus</textarea>" in r.data                    # leftovers prefilled once
    assert b"3 Black Lotus</textarea>" not in client.get(f"/locations/{a}").data
    page = client.get(f"/locations/{a}?sort=-qty").data
    assert page.index(b"Sol Ring") < page.index(b"Lightning Bolt") and b"&#9660;" in page
    assert page.count(b"checked") == 0 and client.get(f"/locations/{a}?all=1").data.count(b" checked") == 2
    rows = [f"{SOL_C21}|nonfoil|NM|en", f"{BOLT}|nonfoil|NM|en"]
    r = client.post(f"/locations/{a}/move-selected", data={"row": rows, "to_id": b}, follow_redirects=True)
    assert b"Moved 3 copies (2 lines) to Box B." in r.data
    assert ledger.qty_at(conn, b, StockKey(SOL_C21)) == 2 and ledger.qty_at(conn, a, StockKey(BOLT)) == 0
    assert ledger.check(conn) == []


def test_collection_sort_and_deck_groups(client, conn):
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_C21), 1)
    ledger.acquire(conn, loc, StockKey(BOLT), 5)
    page = client.get("/collection?sort=-qty").data
    assert page.index(b"Lightning Bolt") < page.index(b"Sol Ring")
    assert client.get("/collection?sort=bogus").status_code == 200       # falls back to name
    client.post("/import/deck", data={"text": "1 Sol Ring\n4 Lightning Bolt\n1 Delver of Secrets",
                                      "name": "D", "format": ""})
    deck_id = conn.execute("SELECT id FROM decks").fetchone()[0]
    page = client.get(f"/decks/{deck_id}").data
    assert b"Main (6)" in page and b"Creatures (1)" in page and b"Instants (4)" in page
    assert b"Artifacts (1)" in page and b"Mana curve" in page
    assert page.index(b"Creatures (1)") < page.index(b"Instants (4)") < page.index(b"Artifacts (1)")


def test_view_switch_text_and_images(client, conn):
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_C21), 3)
    locations.create(conn, "Box B", "box", owner=ALICE)
    client.post("/import/deck", data={"text": "2 Sol Ring (C21) 263\n1 Lightning Bolt", "name": "D", "format": ""})
    deck_id = conn.execute("SELECT id FROM decks").fetchone()[0]
    page = client.get(f"/locations/{loc}").data
    assert b"<b>[Text]</b>" in page and b'class="tile"' not in page and b"Move ticked" in page
    page = client.get(f"/locations/{loc}?view=images").data
    assert b"<b>[Images]</b>" in page and b'class="tile"' in page and b"Move ticked" not in page
    assert b'<span class="qty">3</span>' in page and b"C21 #263" in page
    # remembered for the session, on every card list
    for url in ["/collection", "/add?q=sol", f"/decks/{deck_id}"]:
        assert b'class="tile"' in client.get(url).data, url
    assert b"Instants (1)</h3>" in client.get(f"/decks/{deck_id}").data
    assert b"/stock/quick-add" not in client.get("/add?q=sol").data
    client.get("/collection?view=text")
    assert b'class="tile"' not in client.get(f"/decks/{deck_id}").data
    client.get("/collection?view=bogus")                       # ignored
    assert b"<b>[Text]</b>" in client.get("/collection").data


def test_card_search_filters(client, conn):
    from franticsearch.queries import CardSearch, search
    from werkzeug.datastructures import MultiDict
    loc = locations.create(conn, "Binder A", "binder", owner=ALICE)
    ledger.acquire(conn, loc, StockKey(SOL_CMM, "foil"), 2)
    s = lambda **a: [c["name"] for c in search(conn, CardSearch.from_args(MultiDict(a)), ALICE)[0]]  # noqa: E731
    assert s() == []                                          # empty form: no search
    assert s(q="sol") == ["Sol Ring"]
    assert s(q="ol rin") == ["Sol Ring"]                      # substring fallback
    assert s(type="instant") == ["Lightning Bolt"]
    assert s(type="human insect") == ["Delver of Secrets // Insectile Aberration"]   # back face words
    assert s(text="damage") == ["Lightning Bolt"]
    assert s(c=["R"]) == ["Lightning Bolt"]
    assert s(c=["U"], color_mode="exact") == ["Delver of Secrets // Insectile Aberration"]
    assert s(c=["U", "R"], color_mode="atmost", sort="name") == [   # colourless counts as "at most"
        "Delver of Secrets // Insectile Aberration", "Lightning Bolt", "Sol Ring"]
    assert s(c=["G"], color_mode="atmost") == ["Sol Ring"]
    assert s(c=["C"]) == ["Sol Ring"]
    assert s(mv="1", mv_op=">=", sort="name") == ["Delver of Secrets // Insectile Aberration", "Lightning Bolt", "Sol Ring"]
    assert s(set="m10") == ["Lightning Bolt"]
    assert len(s(format="commander")) == 3 and s(format="modern") == []   # fixture: commander only
    assert s(owned="owned", type="artifact") == ["Sol Ring"] and s(owned="owned", type="instant") == []
    (sol,) = search(conn, CardSearch.from_args(MultiDict({"q": "sol ring"})), ALICE)[0]
    assert (sol["owned"], sol["free"], len(sol["printings"])) == (2, 2, 2)
    assert sol["printings"][0]["scryfall_id"] == SOL_CMM       # owned printing listed first
    page = client.get("/add?q=sol&type=artifact").data
    assert b"1 card." in page and b"2 printings</summary>" in page and b"2 / 2" in page
    assert b"/stock/quick-add" in page
    assert b"No cards match" in client.get("/add?q=zzzz").data


def test_secret_key_first_stored_wins(tmp_path, monkeypatch):
    """Two workers starting on a new DB must end up with the same key."""
    from franticsearch import db
    path = str(tmp_path / "new.db")
    monkeypatch.delenv("FS_SECRET_KEY", raising=False)
    first = create_app({"DATABASE": path, "AUTO_SYNC": False}).secret_key
    real_get_meta = db.get_meta
    calls = []

    def stale_get_meta(conn, key, default=None):   # the 2nd worker read before the 1st stored
        calls.append(key)
        return None if key == "secret_key" and calls.count(key) == 1 else real_get_meta(conn, key, default)

    monkeypatch.setattr(db, "get_meta", stale_get_meta)
    assert create_app({"DATABASE": path, "AUTO_SYNC": False}).secret_key == first
