import gzip
import io
import json

import pytest

from conftest import DELVER, FIXTURE, SOL_C21

from franticsearch import db, queries, scryfall


def test_import_maps_fields(conn):
    sol = conn.execute("SELECT * FROM printings WHERE scryfall_id = ?", (SOL_C21,)).fetchone()
    assert sol["name"] == "Sol Ring" and sol["set_code"] == "c21" and sol["collector_number"] == "263"
    assert json.loads(sol["finishes"]) == ["nonfoil"]
    assert sol["image_normal"].endswith("x.jpg")


def test_double_faced_card_uses_faces(conn):
    d = conn.execute("SELECT * FROM printings WHERE scryfall_id = ?", (DELVER,)).fetchone()
    assert d["mana_cost"] == "{U}"  # empty back-face cost is dropped
    assert "Human Wizard" in d["type_line"] and "Human Insect" in d["type_line"]
    assert "Flying" in d["oracle_text"]
    assert json.loads(d["colors"]) == ["U"]
    assert d["image_small"].endswith("d1.jpg")


def test_reimport_upserts_without_duplicates(conn):
    with open(FIXTURE, "rb") as fp:
        scryfall.import_stream(conn, fp)
    assert conn.execute("SELECT COUNT(*) FROM printings").fetchone()[0] == 4
    # FTS stays in sync through the update trigger
    assert [r["name"] for r in queries.search_cards(conn, "bolt")] == ["Lightning Bolt"]


def test_non_card_objects_are_skipped(tmp_path):
    conn = db.connect(tmp_path / "y.db")
    db.migrate(conn)
    n = scryfall.import_stream(conn, io.BytesIO(b'[{"object": "not_a_card"}]'))
    assert n == 0


def _jsonl(cards):
    return b"".join(json.dumps(c).encode() + b"\n" for c in cards)


@pytest.mark.parametrize("encode", [
    lambda b: b,
    lambda b: gzip.compress(b),
    lambda b: _jsonl(json.loads(b)),
    lambda b: gzip.compress(_jsonl(json.loads(b))),
], ids=["json", "json.gz", "jsonl", "jsonl.gz"])
def test_import_accepts_bulk_formats(tmp_path, encode):
    conn = db.connect(tmp_path / "f.db")
    db.migrate(conn)
    assert scryfall.import_stream(conn, io.BytesIO(encode(FIXTURE.read_bytes()))) == 4


class FakeResponse:
    def __init__(self, payload=None, body=b""):
        self._payload, self._body = payload, body

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, updated_at):
        self.updated_at = updated_at
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw["headers"]))
        if url == scryfall.BULK_INDEX_URL:
            return FakeResponse({
                "updated_at": self.updated_at,
                "jsonl_download_uri": "https://data.example/cards.jsonl.gz",
            })
        assert url == "https://data.example/cards.jsonl.gz"
        return FakeResponse(body=gzip.compress(_jsonl(json.loads(FIXTURE.read_bytes()))))


def test_sync_downloads_then_skips_unchanged(tmp_path):
    conn = db.connect(tmp_path / "z.db")
    db.migrate(conn)
    s = FakeSession("2026-10-17T09:00:00+00:00")
    assert "imported 4" in scryfall.sync(conn, session=s)
    assert all("FranticSearch" in h["User-Agent"] and h["Accept"] for _, h in s.calls)
    assert db.get_meta(conn, "sync_status") == "idle"
    assert not scryfall.needs_sync(conn)
    assert scryfall.sync(conn, session=s) == "already up to date"
    assert len(s.calls) == 3  # index, download, index
    assert "imported" in scryfall.sync(conn, session=s, force=True)
