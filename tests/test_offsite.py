"""Off-machine backups: user-data backups (no card cache), gpg encryption, copy + prune."""

import gzip
import shutil
import sqlite3
import subprocess

import pytest
from conftest import ALICE, BOLT, DELVER, SOL_C21, SOL_ORACLE

from franticsearch import backup, db, ledger, locations, offsite, wishlist
from franticsearch.__main__ import main as cli
from franticsearch.ledger import StockKey

needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")


@pytest.fixture
def data(conn):
    """Alice owns a Sol Ring (C21) and wants Lightning Bolt in any printing."""
    box = locations.create(conn, "Box", "box", owner=ALICE)
    ledger.acquire(conn, box, StockKey(SOL_C21), 2)
    wishlist.add(conn, "aaaaaaaa-0000-0000-0000-000000000004", 1, owner=ALICE)   # Lightning Bolt
    db.set_meta(conn, "last_sync_at", "2026-10-27T00:00:00+00:00")
    db.set_meta(conn, "scryfall_updated_at", "x")
    return box


@pytest.fixture
def gpg_key(tmp_path):
    """A throwaway key pair: (public key file, gpg home holding the private key)."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    run = lambda *a: subprocess.run(["gpg", "--homedir", str(home), "--batch", "--quiet", *a],  # noqa: E731
                                    check=True, capture_output=True)
    run("--passphrase", "", "--pinentry-mode", "loopback", "--quick-gen-key", "test backups", "default",
        "default", "never")
    pub = tmp_path / "backup-key.asc"
    pub.write_bytes(run("--armor", "--export").stdout)
    yield pub, home
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], capture_output=True)


def _decrypt(home, enc, out):
    subprocess.run(["gpg", "--homedir", str(home), "--batch", "--quiet", "--pinentry-mode", "loopback",
                    "--passphrase", "", "--output", str(out), "--decrypt", str(enc)], check=True,
                   capture_output=True)


def test_user_data_backup_keeps_only_referenced_cards(tmp_path, conn, db_path, data):
    out = backup.backup(db_path, tmp_path / "u", user_data=True)
    assert out.name.startswith(backup.USER_PREFIX)
    plain = tmp_path / "plain.db"
    with gzip.open(out) as fin:
        plain.write_bytes(fin.read())
    c = sqlite3.connect(plain)
    ids = {r[0] for r in c.execute("SELECT scryfall_id FROM printings")}
    assert SOL_C21 in ids and BOLT in ids and DELVER not in ids     # stock + "any printing" wish
    assert c.execute("SELECT COUNT(*) FROM printings_fts WHERE printings_fts MATCH 'delver'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM printings_fts WHERE printings_fts MATCH 'sol'").fetchone()[0] > 0
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    meta = dict(c.execute("SELECT key, value FROM meta"))
    assert "last_sync_at" not in meta and meta["backup_kind"] == "user-data"
    c.close()
    # the live database still has everything
    assert conn.execute("SELECT 1 FROM printings WHERE scryfall_id = ?", (DELVER,)).fetchone()


def test_full_and_user_backups_are_pruned_separately(tmp_path, db_path, data):
    d = tmp_path / "bk"
    full = backup.backup(db_path, d, keep=1)
    user = backup.backup(db_path, d, keep=1, user_data=True)
    assert backup.list_backups(d) == [full] and backup.list_backups(d, user_data=True) == [user]
    assert backup.default_dir(db_path, user_data=True) == backup.default_dir(db_path) / "user-data"


def test_restore_user_data_backup(tmp_path, conn, db_path, data):
    out = backup.backup(db_path, tmp_path / "u", user_data=True)
    conn.close()
    r = backup.restore(db_path, out, port=1)
    assert r["user_data"] and r["problems"] == []
    c = db.connect(db_path)
    assert ledger.qty_at(c, data, StockKey(SOL_C21)) == 2
    assert [w["name"] for w in wishlist.lines(c, ALICE)] == ["Lightning Bolt"]
    assert db.get_meta(c, "backup_kind") is None and db.get_meta(c, "last_sync_at") is None
    c.close()


def test_split_dest():
    assert offsite.split_dest("theorycorner.com:franticsearch-backups") == ("theorycorner.com", "franticsearch-backups")
    assert offsite.split_dest("me@host:") == ("me@host", ".")
    assert offsite.split_dest("/mnt/usb/backups") == (None, "/mnt/usb/backups")
    assert offsite.split_dest("./a:b") == (None, "./a:b")


@needs_gpg
def test_push_to_local_dir_encrypts_and_prunes(tmp_path, db_path, data, gpg_key):
    pub, home = gpg_key
    dest = tmp_path / "remote"
    dest.mkdir()
    for day in ("01", "02", "03"):
        (dest / f"{backup.USER_PREFIX}200001{day}-000000.db.gz.gpg").write_bytes(b"old")
    (dest / "unrelated.txt").write_text("keep me")
    src = backup.backup(db_path, tmp_path / "u", user_data=True)
    r = offsite.push(src, pub, str(dest), keep=2)
    assert r["file"] == src.name + ".gpg"
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        [f"{backup.USER_PREFIX}20000103-000000.db.gz.gpg", r["file"], "unrelated.txt"])
    enc = dest / r["file"]
    assert b"SQLite" not in enc.read_bytes() and enc.read_bytes()[:2] != b"\x1f\x8b"
    _decrypt(home, enc, tmp_path / "back.db.gz")
    assert (tmp_path / "back.db.gz").read_bytes() == src.read_bytes()


@needs_gpg
def test_offsite_cli(tmp_path, db_path, data, gpg_key):
    pub, _ = gpg_key
    dest = tmp_path / "usb"
    assert cli(["--db", db_path, "offsite", "--key", str(pub), "--dest", str(dest)]) == 0
    assert len(list(dest.glob("*.gpg"))) == 1
    assert len(backup.list_backups(backup.default_dir(db_path, True), user_data=True)) == 1
    assert cli(["--db", db_path, "offsite", "--key", str(tmp_path / "missing.asc"), "--dest", str(dest)]) == 1


def test_push_reports_missing_tools(tmp_path, monkeypatch, db_path, data):
    pub = tmp_path / "k.asc"
    pub.write_text("not really a key")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    src = backup.backup(db_path, tmp_path / "u", user_data=True)
    with pytest.raises(offsite.OffsiteError, match="not installed"):
        offsite.push(src, pub, str(tmp_path / "d"))
