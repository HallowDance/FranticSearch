from conftest import SOL_C21, SOL_CMM, SOL_ORACLE

from franticsearch import ledger, locations
from franticsearch.ledger import StockKey


def test_pages_render(client, conn):
    loc = locations.create(conn, "Binder A", "binder")
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
    r = client.post("/stock/sell", data={"printing_id": "x", "qty": "1", "location_id": "1",
                                         "next": "//evil.example/"})
    assert r.headers["Location"] == "/collection"


def test_archive_via_web(client, conn):
    loc = locations.create(conn, "Old binder", "binder")
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
    assert b"This is the location of deck" in client.get(f"/locations/{loc_id}").data

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
    loc = locations.create(conn, "Binder A", "binder")
    ledger.acquire(conn, loc, StockKey(SOL_C21), 1)
    client.post("/import/deck", data={"text": "1 Sol Ring (C21) 263", "name": "D", "format": ""})
    deck_id = conn.execute("SELECT id FROM decks").fetchone()[0]
    peek = b'class="peek" src="https://cards.scryfall.io/normal/x.jpg"'
    for url in ["/collection", "/add?q=sol", f"/locations/{loc}", "/history", f"/decks/{deck_id}",
                f"/card/{SOL_ORACLE}"]:
        assert peek in client.get(url).data, url
