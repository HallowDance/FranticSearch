"""Database backups and restore (phase 6; PLAN.md § Phase 5 & 6 as built).

Backups use SQLite's online backup API, so they are consistent while the server
runs, and are gzipped into ``<db dir>/backups/franticsearch-YYYYmmdd-HHMMSS.db.gz``.
A systemd user timer runs ``python -m franticsearch backup`` nightly.

A *user-data* backup (``user_data=True``) leaves out the Scryfall card cache, except the
printings the data refers to: ~0.5 MB instead of ~57 MB, for copies that leave the
machine (``offsite.py``). Its sync timestamps are cleared, so after a restore the
next sync (the server runs one on start) downloads the whole card cache again.

Restore is CLI only: it refuses while something listens on the server port
(unless forced), saves the current database next to it first, then checks the
restored file (integrity, migrations, ledger).
"""

from __future__ import annotations

import gzip
import os
import shutil
import socket
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from . import db, ledger

PREFIX = "franticsearch-"
USER_PREFIX = "franticsearch-user-"
KEEP = 14

# Printings a user-data backup keeps: everything the user data refers to, plus all
# printings of cards wanted in "any printing" (so the wishlist still shows them).
_KEEP_PRINTINGS = """
    scryfall_id IN (SELECT printing_id FROM stock UNION SELECT printing_id FROM movements
                    UNION SELECT printing_id FROM deck_slots
                    UNION SELECT printing_id FROM wishes WHERE printing_id IS NOT NULL)
    OR COALESCE(oracle_id, scryfall_id) IN (SELECT card_key FROM wishes)"""


class BackupError(RuntimeError):
    pass


def default_dir(db_path: str, user_data: bool = False) -> Path:
    d = Path(db_path).resolve().parent / "backups"
    return d / "user-data" if user_data else d


def _copy_db(src_path: str | Path, dst_path: str | Path) -> None:
    """Consistent copy of a (possibly live, WAL-mode) database file."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _strip_cards(path: Path) -> None:
    """Turn a full copy into a user-data backup: drop unreferenced printings, rebuild the
    search index, clear the sync timestamps, VACUUM."""
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(f"DELETE FROM printings WHERE NOT ({_KEEP_PRINTINGS})")
            conn.execute("INSERT INTO printings_fts(printings_fts) VALUES('rebuild')")
            conn.execute("DELETE FROM meta WHERE key IN ('scryfall_updated_at', 'last_sync_at')")
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('backup_kind', 'user-data')")
        conn.execute("VACUUM")
    finally:
        conn.close()


def backup(db_path: str, dest_dir: str | Path | None = None, keep: int = KEEP,
           user_data: bool = False) -> Path:
    """Write a gzipped backup and keep only the newest ``keep`` of its kind. Returns its path.
    ``user_data``: leave out the card cache (see the module docstring)."""
    if not Path(db_path).exists():
        raise BackupError(f"no database at {db_path}")
    dest = Path(dest_dir) if dest_dir else default_dir(db_path, user_data)
    dest.mkdir(parents=True, exist_ok=True)
    prefix = USER_PREFIX if user_data else PREFIX
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target, n = dest / f"{prefix}{stamp}.db.gz", 1
    while target.exists():   # two backups within a second
        n += 1
        target = dest / f"{prefix}{stamp}-{n}.db.gz"
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        raw = Path(tmp) / "copy.db"
        _copy_db(db_path, raw)
        if user_data:
            _strip_cards(raw)
        with open(raw, "rb") as fin, gzip.open(target.with_suffix(".part"), "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
    target.with_suffix(".part").rename(target)
    for old in list_backups(dest, user_data)[keep:]:
        old.unlink()
    return target


def list_backups(dest_dir: str | Path, user_data: bool = False) -> list[Path]:
    """Full (or user-data) backups in a directory, newest first."""
    d = Path(dest_dir)
    if not d.is_dir():
        return []
    found = d.glob(f"{USER_PREFIX if user_data else PREFIX}*.db.gz")
    if not user_data:
        found = (p for p in found if not p.name.startswith(USER_PREFIX))
    return sorted(found, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


def server_running(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _check_file(path: Path) -> None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise BackupError(f"not a usable database: {e}") from None
    if ok != "ok":
        raise BackupError(f"integrity check failed: {ok}")
    if not {"stock", "movements", "locations"} <= tables:
        raise BackupError("this isn't a FranticSearch database")


def restore(db_path: str, source: str | Path, *, force: bool = False, port: int = 6602) -> dict:
    """Replace the database with a backup (.db or .db.gz).

    Returns {"saved": path of the previous database copy or None, "migrations": [...],
    "problems": ledger mismatches, "user_data": True for a backup without the card cache}.
    """
    source = Path(source)
    if not source.is_file():
        raise BackupError(f"no such file: {source}")
    if not force and server_running(port):
        raise BackupError(f"something is listening on port {port} (the server?). Stop it first "
                          "(systemctl --user stop franticsearch) or pass --force")
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        plain = Path(tmp) / "restore.db"
        if source.suffix == ".gz":
            with gzip.open(source, "rb") as fin, open(plain, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)
        else:
            shutil.copyfile(source, plain)
        _check_file(plain)
        saved = None
        if target.exists():
            saved = target.with_name(f"{target.name}.pre-restore-{datetime.now():%Y%m%d-%H%M%S}")
            _copy_db(target, saved)
        _copy_db(plain, target)   # page-level copy into the live file (WAL-safe)
    conn = db.connect(str(target))
    try:
        applied = db.migrate(conn)
        problems = ledger.check(conn)
        user_data = db.get_meta(conn, "backup_kind") == "user-data"
        conn.execute("DELETE FROM meta WHERE key = 'backup_kind'")   # the live DB is just a DB again
    finally:
        conn.close()
    return {"saved": saved, "migrations": applied, "problems": problems, "user_data": user_data}
