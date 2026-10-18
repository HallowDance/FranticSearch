from franticsearch import db


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    first = db.migrate(conn)
    assert first == ["001_initial.sql", "002_location_archive.sql", "003_imports.sql"]
    assert db.migrate(conn) == []
    assert db.schema_version(conn) == 3
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"printings", "locations", "stock", "movements", "decks", "deck_slots", "meta",
            "imports", "import_unresolved"} <= tables


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
