from franticsearch import db


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "x.db")
    first = db.migrate(conn)
    assert first == ["001_initial.sql"]
    assert db.migrate(conn) == []
    assert db.schema_version(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"printings", "locations", "stock", "movements", "decks", "deck_slots", "meta"} <= tables


def test_meta_roundtrip(conn):
    db.set_meta(conn, "k", "v1")
    db.set_meta(conn, "k", "v2")
    assert db.get_meta(conn, "k") == "v2"
    assert db.get_meta(conn, "missing", "d") == "d"
