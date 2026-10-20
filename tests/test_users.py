"""Phase 2b: accounts, login, CSRF, separate collections, sharing."""

import re
import subprocess
import sys

import pytest
from conftest import ALICE, PASSWORD, SOL_C21, SOL_ORACLE, login_as

from franticsearch import importer, ledger, locations, users
from franticsearch.app import create_app
from franticsearch.ledger import LedgerError, StockKey


@pytest.fixture
def bob(conn):
    return users.create(conn, "bob", "bob's password")


@pytest.fixture
def bob_client(app, bob):
    return login_as(app.test_client(), bob)


@pytest.fixture
def alices(conn):
    """Alice's things: a binder with a card, a deck with an unresolved line, a batch."""
    binder = locations.create(conn, "Binder", "binder", owner=ALICE)
    batch = ledger.acquire(conn, binder, StockKey(SOL_C21), 2)
    iid = importer.import_deck(conn, "1 Sol Ring (C21) 263\n1 Black Lotus", "Alice deck", owner=ALICE)
    imp = importer.get_import(conn, iid)
    uid = importer.unresolved(conn, iid)[0]["id"]
    deck_loc = conn.execute("SELECT location_id FROM decks WHERE id = ?", (imp["deck_id"],)).fetchone()[0]
    return {"binder": binder, "batch": batch, "import": iid, "deck": imp["deck_id"],
            "deck_loc": deck_loc, "unresolved": uid}


# --------------------------------------------------------------------------- accounts & login

def test_login_required_and_login_flow(app, conn):
    c = app.test_client()
    r = c.get("/collection")
    assert r.status_code == 302 and "/login?next=/collection" in r.headers["Location"]
    assert c.get("/static/style.css").status_code == 200
    r = c.post("/login", data={"username": "alice", "password": "nope"}, follow_redirects=True)
    assert b"Wrong username or password" in r.data
    r = c.post("/login", data={"username": "ALICE", "password": PASSWORD, "next": "/collection"})
    assert r.headers["Location"] == "/collection"        # usernames are case-insensitive
    assert b"alice</a>" in c.get("/collection").data
    r = c.post("/login", data={"username": "alice", "password": PASSWORD, "next": "//evil.example"})
    assert r.headers["Location"] == "/"
    c.post("/logout")
    assert c.get("/collection").status_code == 302


def test_login_page_explains_first_admin(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "empty.db"), "AUTO_SYNC": False, "TESTING": True})
    assert b"user add NAME --admin" in app.test_client().get("/login").data


def test_csrf_token_required_on_every_post(conn, db_path):
    app = create_app({"DATABASE": db_path, "AUTO_SYNC": False, "TESTING": True})  # CSRF on
    c = app.test_client()
    page = c.get("/login").data.decode()
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert c.post("/login", data={"username": "alice", "password": PASSWORD}).status_code == 400
    r = c.post("/login", data={"username": "alice", "password": PASSWORD, "csrf": token})
    assert r.status_code == 302
    # logging in starts a fresh session: the old token is gone, pages carry a new one
    page = c.get("/locations").data.decode()
    new = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert new != token
    assert c.post("/locations", data={"name": "X", "type": "box", "csrf": token}).status_code == 400
    assert c.post("/locations", data={"name": "X", "type": "box", "csrf": new}).status_code == 302
    # every POST form on the main pages carries the token
    for url in ["/", "/locations", "/import", "/add?q=sol", f"/card/{SOL_ORACLE}", "/account",
                "/admin/users", "/system", "/decks"]:
        html = c.get(url).data.decode()
        forms = re.findall(r'<form\b[^>]*method="post"[^>]*>(.{0,120})', html)
        assert forms and all('name="csrf"' in f for f in forms), url


def test_account_password_change(client, conn):
    r = client.post("/account", data={"current": "wrong", "new": "newpassword", "repeat": "newpassword"},
                    follow_redirects=True)
    assert b"current password is wrong" in r.data
    r = client.post("/account", data={"current": PASSWORD, "new": "newpassword", "repeat": "newpassword"},
                    follow_redirects=True)
    assert b"Password changed" in r.data
    assert users.authenticate(conn, "alice", "newpassword")


def test_admin_pages(client, bob_client, conn):
    assert bob_client.get("/system").status_code == 403
    assert bob_client.get("/admin/users").status_code == 403
    assert bob_client.post("/admin/users", data={"username": "eve", "password": "12345678"}).status_code == 403
    assert b"Users</a>" not in bob_client.get("/").data and b"Users</a>" in client.get("/").data
    r = client.post("/admin/users", data={"username": "carol", "password": "carols password"},
                    follow_redirects=True)
    assert b"User carol created" in r.data
    r = client.post("/admin/users", data={"username": "carol", "password": "carols password"},
                    follow_redirects=True)
    assert b"already exists" in r.data
    assert b"at least 8" in client.post("/admin/users", data={"username": "dan", "password": "short"},
                                        follow_redirects=True).data
    carol = users.by_name(conn, "carol")["id"]
    client.post(f"/admin/users/{carol}/password", data={"password": "another password"})
    assert users.authenticate(conn, "carol", "another password")


def test_first_admin_adopts_data_from_before_users(tmp_path):
    from franticsearch import db
    c = db.connect(tmp_path / "old.db")
    db.migrate(c)
    c.execute("INSERT INTO locations(name, type) VALUES ('Old binder', 'binder')")
    first = users.create(c, "someone", "password1")                   # not admin: adopts nothing
    admin = users.create(c, "admin", "password2", is_admin=True)
    later = users.create(c, "admin2", "password3", is_admin=True)
    owners = [r[0] for r in c.execute("SELECT owner_id FROM locations")]
    assert owners == [admin] and first != admin != later


def test_cli_user_commands(db_path, conn):
    run = lambda *a, stdin="": subprocess.run(  # noqa: E731
        [sys.executable, "-m", "franticsearch", "--db", db_path, "user", *a],
        input=stdin, capture_output=True, text=True)
    r = run("add", "dave", "--password-stdin", stdin="daves password\n")
    assert r.returncode == 0 and "created dave" in r.stdout
    assert run("add", "dave", "--password-stdin", stdin="daves password\n").returncode == 1
    assert run("passwd", "dave", "--password-stdin", stdin="new password!\n").returncode == 0
    assert users.authenticate(conn, "dave", "new password!")
    assert "dave" in run("list").stdout


# --------------------------------------------------------------------------- separate collections

def test_other_users_ids_are_not_found(bob_client, alices):
    a = alices
    key = {"printing_id": SOL_C21, "finish": "nonfoil", "condition": "NM", "lang": "en", "qty": "1"}
    sid = f"{SOL_C21}|nonfoil|NM|en|main"
    gets = [f"/locations/{a['binder']}", f"/decks/{a['deck']}", f"/locations/{a['deck_loc']}",
            f"/import/{a['import']}", f"/import/line/{a['unresolved']}",
            f"/decks/{a['deck']}/slot?slot={sid}", f"/decks/{a['deck']}/pull",
            f"/decks/{a['deck']}/return", f"/add?deck={a['deck']}&q=sol",
            f"/decks/{a['deck']}/export?format=moxfield", f"/decks/{a['deck']}/export?what=proxies"]
    for url in gets:
        assert bob_client.get(url).status_code == 404, url
    posts = [
        ("/stock/acquire", {**key, "location_id": a["binder"]}),
        ("/stock/move", {**key, "from_id": a["binder"], "to_id": a["binder"]}),
        ("/stock/adjust", {**key, "location_id": a["binder"]}),
        ("/stock/sell", {**key, "location_id": a["binder"]}),
        ("/stock/quick-add", {"card_key": SOL_ORACLE, "qty": "1", "location_id": a["binder"]}),
        ("/undo", {"batch": a["batch"]}),
        (f"/locations/{a['binder']}/move-selected", {"row": f"{SOL_C21}|nonfoil|NM|en", "to_id": a["binder"]}),
        (f"/locations/{a['binder']}/paste", {"text": "1 Sol Ring"}),
        (f"/locations/{a['binder']}/edit", {"name": "Mine now", "type": "box"}),
        (f"/locations/{a['binder']}/archive", {"archived": "1"}),
        (f"/locations/{a['binder']}/delete", {}),
        (f"/locations/{a['binder']}/visibility", {"visibility": "link"}),
        (f"/decks/{a['deck']}/delete", {}),
        (f"/import/line/{a['unresolved']}/fix", {**key}),
        (f"/import/line/{a['unresolved']}/dismiss", {}),
        ("/import/collection", {"location_id": a["binder"]}),
        (f"/decks/{a['deck']}/edit", {"name": "Mine now"}),
        (f"/decks/{a['deck']}/slot", {"slot": sid, "qty": "4", "proxy": "0", "board": "main"}),
        ("/decks/add-card", {"deck_id": a["deck"], "card_key": SOL_ORACLE, "qty": "1", "board": "main"}),
        (f"/decks/{a['deck']}/paste", {"text": "1 Sol Ring"}),
        (f"/decks/{a['deck']}/pull", {"all": "1"}),
        (f"/decks/{a['deck']}/retarget", {"slot": sid, "printing_id": SOL_C21, "qty": "1"}),
        (f"/decks/{a['deck']}/return", {"all": "1"}),
        (f"/decks/{a['deck']}/build", {}),
        (f"/decks/{a['deck']}/proxy-short", {}),
    ]
    for url, data in posts:
        assert bob_client.post(url, data=data).status_code == 404, url


def test_alice_data_untouched_after_bobs_attempts(bob_client, conn, alices):
    test_other_users_ids_are_not_found(bob_client, alices)
    assert ledger.qty_at(conn, alices["binder"], StockKey(SOL_C21)) == 2
    assert locations.get(conn, alices["binder"])["name"] == "Binder"
    assert conn.execute("SELECT name FROM decks WHERE id = ?", (alices["deck"],)).fetchone()[0] == "Alice deck"
    assert [tuple(r) for r in conn.execute("SELECT printing_id, qty_wanted FROM deck_slots")] == [(SOL_C21, 1)]
    assert ledger.qty_at(conn, alices["deck_loc"], StockKey(SOL_C21)) == 0
    assert ledger.check(conn) == []


def test_lists_and_totals_are_per_user(bob_client, conn, alices, bob):
    for url in ["/", "/collection", "/locations", "/decks", "/import", "/history", f"/card/{SOL_ORACLE}",
                "/buy", "/buy/export?format=text", "/pull"]:
        page = bob_client.get(url).data
        assert b"Alice deck" not in page and b"Binder</a>" not in page, url
    assert b"Sol Ring" not in bob_client.get("/collection").data
    assert b"You don't own any copies" in bob_client.get(f"/card/{SOL_ORACLE}").data


def test_same_names_and_same_deck_list_per_user(conn, alices, bob):
    locations.create(conn, "Binder", "binder", owner=bob)                 # name taken only for alice
    importer.import_deck(conn, "1 Sol Ring (C21) 263\n1 Black Lotus", "Alice deck", owner=bob)
    with pytest.raises(LedgerError, match="already imported"):
        importer.import_deck(conn, "1 Sol Ring (C21) 263\n1 Black Lotus", "Again", owner=ALICE)


def test_ledger_refuses_moves_between_collections(conn, alices, bob):
    bobs = locations.create(conn, "Bob box", "box", owner=bob)
    with pytest.raises(LedgerError, match="different users"):
        ledger.move(conn, alices["binder"], bobs, StockKey(SOL_C21), 1)


def test_default_printing_prefers_only_your_own_copies(conn, alices, bob):
    from franticsearch.importer import Line
    from conftest import SOL_CMM
    key, _ = importer.resolve(conn, Line(1, "", name="Sol Ring"), allow_default=True, owner=bob)
    assert key == StockKey(SOL_CMM)              # not alice's free C21 copy
    key, _ = importer.resolve(conn, Line(1, "", name="Sol Ring"), allow_default=True, owner=ALICE)
    assert key == StockKey(SOL_C21)


# --------------------------------------------------------------------------- sharing

def test_share_with_users(client, bob_client, conn, alices):
    r = client.post(f"/locations/{alices['binder']}/visibility", data={"visibility": "users"},
                    follow_redirects=True)
    assert b"Sharing updated" in r.data and b"Other users can see this" in r.data
    page = bob_client.get(f"/locations/{alices['binder']}").data
    assert b"alice's binder, shared read-only" in page and b"Sol Ring" in page
    assert b"Move" not in page and b"Set count" not in page and b"/paste" not in page
    assert b"Binder</a>" in bob_client.get("/people").data
    card = bob_client.get(f"/card/{SOL_ORACLE}").data
    assert b"Shared by others" in card and b"alice" in card
    assert bob_client.get(f"/decks/{alices['deck']}").status_code == 404     # deck still private


def test_shared_deck_hides_owners_other_locations(client, bob_client, conn, alices):
    client.post(f"/locations/{alices['deck_loc']}/visibility", data={"visibility": "users"})
    page = bob_client.get(f"/decks/{alices['deck']}").data
    assert b"alice's deck, shared read-only" in page and b"Sol Ring" in page
    assert b"Where (free)" not in page and b"Binder" not in page and b"Delete deck" not in page
    assert b"Where (free)" in client.get(f"/decks/{alices['deck']}").data


def test_share_link_works_without_login_and_can_be_revoked(client, app, conn, alices):
    client.post(f"/locations/{alices['binder']}/visibility", data={"visibility": "link"})
    token = locations.get(conn, alices["binder"])["share_token"]
    assert token and len(token) >= 20
    anon = app.test_client()
    page = anon.get(f"/s/{token}").data
    assert b"Sol Ring" in page and b"shared read-only" in page
    assert b"Collection</a>" not in page and b'href="/card/' not in page    # no nav, no logged-in links
    assert anon.get("/s/not-a-real-token").status_code == 404
    client.post(f"/locations/{alices['binder']}/visibility", data={"visibility": "link", "new_link": "1"})
    assert anon.get(f"/s/{token}").status_code == 404
    new = locations.get(conn, alices["binder"])["share_token"]
    assert anon.get(f"/s/{new}").status_code == 200
    client.post(f"/locations/{alices['binder']}/visibility", data={"visibility": "private"})
    assert anon.get(f"/s/{new}").status_code == 404
    # a shared deck link renders the deck page, read-only
    client.post(f"/locations/{alices['deck_loc']}/visibility", data={"visibility": "link"})
    dtoken = locations.get(conn, alices["deck_loc"])["share_token"]
    page = anon.get(f"/s/{dtoken}").data
    assert b"Alice deck" in page and b"Where (free)" not in page


# --------------------------------------------------------------------------- admin: generated passwords, disabling

def test_admin_generated_passwords(client, conn):
    r = client.post("/admin/users", data={"username": "carol", "password": ""})
    assert r.status_code == 200      # rendered straight away, not via a (cookie) flash
    page = r.data.decode()
    pw = re.search(r"Password for <b>carol</b>: <code>([^<]+)</code>", page).group(1)
    assert len(pw) == users.GENERATED_LENGTH and users.authenticate(conn, "carol", pw)
    assert pw not in client.get("/admin/users").data.decode()       # shown once
    with client.session_transaction() as sess:
        assert pw not in str(dict(sess))

    carol = users.by_name(conn, "carol")["id"]
    page = client.post(f"/admin/users/{carol}/password", data={"password": ""}).data.decode()
    new = re.search(r"<code>([^<]+)</code>", page).group(1)
    assert new != pw and users.authenticate(conn, "carol", new) and not users.authenticate(conn, "carol", pw)
    assert client.post("/admin/users/999/password", data={"password": ""}).status_code == 404


def test_disabled_account_cannot_log_in_and_its_sharing_is_hidden(app, client, bob, bob_client, conn):
    binder = locations.create(conn, "Bob's binder", "binder", owner=bob)
    ledger.acquire(conn, binder, StockKey(SOL_C21), 1)
    locations.set_visibility(conn, binder, "link")
    token = locations.get(conn, binder)["share_token"]
    anon = app.test_client()

    def visible() -> list[bool]:
        return [b"Bob&#39;s binder" in client.get("/people").data,
                b"bob" in client.get(f"/card/{SOL_ORACLE}").data,
                client.get(f"/locations/{binder}").status_code == 200,
                anon.get(f"/s/{token}").status_code == 200]

    assert visible() == [True] * 4
    r = client.post(f"/admin/users/{bob}/disabled", data={"disabled": "1"}, follow_redirects=True)
    assert b"bob disabled" in r.data and b"disabled</td>" in r.data
    assert visible() == [False] * 4
    assert bob_client.get("/collection").status_code == 302                  # session ended
    assert users.authenticate(conn, "bob", "bob's password") is None         # and no new login
    assert b"Wrong username or password" in app.test_client().post(
        "/login", data={"username": "bob", "password": "bob's password"}, follow_redirects=True).data

    client.post(f"/admin/users/{bob}/disabled", data={"disabled": "0"})
    assert visible() == [True] * 4 and users.authenticate(conn, "bob", "bob's password")
    assert ledger.qty_at(conn, binder, StockKey(SOL_C21)) == 1               # nothing was deleted


def test_admins_cannot_lock_everyone_out(client, bob, bob_client, conn):
    r = client.post(f"/admin/users/{ALICE}/disabled", data={"disabled": "1"}, follow_redirects=True)
    assert b"can&#39;t disable your own account" in r.data
    with pytest.raises(LedgerError, match="last admin"):
        users.set_disabled(conn, ALICE, True)
    assert client.post("/admin/users/999/disabled", data={"disabled": "1"}).status_code == 404
    assert bob_client.post(f"/admin/users/{ALICE}/disabled", data={"disabled": "0"}).status_code == 403
    users.create(conn, "admin2", "admin2 password", is_admin=True)
    users.set_disabled(conn, ALICE, True)              # another admin can still log in: allowed
    assert client.get("/admin/users").status_code == 302   # alice's own session is over


def test_cli_disable_enable(db_path, conn, bob):
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "franticsearch", "--db", db_path, "user", *a], capture_output=True, text=True)
    assert run("disable", "bob").returncode == 0 and users.get(conn, bob)["disabled"]
    assert "DISABLED" in run("list").stdout
    assert run("disable", "alice").returncode == 1        # the last admin
    assert run("enable", "bob").returncode == 0 and not users.get(conn, bob)["disabled"]
    assert run("enable", "nobody").returncode == 1
