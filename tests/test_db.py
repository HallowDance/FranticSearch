from franticsearch import db


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    first = db.migrate(conn)
    assert first == ["001_initial.sql", "002_location_archive.sql", "003_imports.sql",
                     "004_undo.sql", "005_users.sql", "006_movement_owner.sql", "007_user_disabled.sql",
                     "008_deck_tags.sql", "009_wishlist.sql"]
    assert db.migrate(conn) == []
    assert db.schema_version(conn) == 9
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"printings", "locations", "stock", "movements", "decks", "deck_slots", "meta",
            "imports", "import_unresolved", "users", "deck_tags", "wishes", "wishlists"} <= tables


def test_meta_roundtrip(conn):
    db.set_meta(conn, "k", "v1")
    db.set_meta(conn, "k", "v2")
    assert db.get_meta(conn, "k") == "v2"
    assert db.get_meta(conn, "missing", "d") == "d"


def test_nested_transaction_is_one_unit(conn):
    try:
        with db.transaction(conn):
            db.set_meta(conn, "outer", "1")
            with db.transaction(conn):
                db.set_meta(conn, "inner", "1")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.get_meta(conn, "outer") is None and db.get_meta(conn, "inner") is None
    assert not conn.in_transaction


def test_006_backfills_movement_owners(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "old.db")
    all_migrations = db._migrations()
    monkeypatch.setattr(db, "_migrations", lambda: all_migrations[:5])
    db.migrate(conn)
    conn.executescript("""
        INSERT INTO users(id, username, password_hash) VALUES (1, 'a', 'x'), (2, 'b', 'x');
        INSERT INTO printings(scryfall_id, name, lang, set_code, set_name, collector_number, finishes)
            VALUES ('p', 'Card', 'en', 's', 'Set', '1', '["nonfoil"]');
        INSERT INTO locations(id, owner_id, name, type) VALUES (1, 1, 'A', 'box'), (2, 1, 'B', 'box'),
                                                                (3, 2, 'C', 'box'), (4, NULL, 'Old', 'box');
        INSERT INTO movements(batch_id, from_location_id, to_location_id, printing_id, finish,
                              condition, lang, qty, reason) VALUES
            ('b1', NULL, 1, 'p', 'nonfoil', 'NM', 'en', 2, 'acquire'),
            ('b2', 1, 2, 'p', 'nonfoil', 'NM', 'en', 1, 'move'),
            ('b3', 2, NULL, 'p', 'nonfoil', 'NM', 'en', 1, 'sell'),
            ('b4', NULL, 3, 'p', 'nonfoil', 'NM', 'en', 1, 'acquire'),
            ('b5', NULL, 4, 'p', 'nonfoil', 'NM', 'en', 1, 'acquire');
    """)
    monkeypatch.setattr(db, "_migrations", lambda: all_migrations)
    assert db.migrate(conn)[0] == "006_movement_owner.sql"
    owners = dict(conn.execute("SELECT batch_id, owner_id FROM movements").fetchall())
    assert owners == {"b1": 1, "b2": 1, "b3": 1, "b4": 2, "b5": None}
