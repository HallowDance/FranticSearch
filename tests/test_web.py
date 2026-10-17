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
