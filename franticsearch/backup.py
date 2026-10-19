"""Database backups and restore (phase 6; PLAN.md § Phase 5 & 6 as built).

Backups use SQLite's online backup API, so they are consistent while the server
runs, and are gzipped into ``<db dir>/backups/franticsearch-YYYYmmdd-HHMMSS.db.gz``.
A systemd user timer runs ``python -m franticsearch backup`` nightly.

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
KEEP = 14


class BackupError(RuntimeError):
    pass


def default_dir(db_path: str) -> Path:
    return Path(db_path).resolve().parent / "backups"


def _copy_db(src_path: str | Path, dst_path: str | Path) -> None:
    """Consistent copy of a (possibly live, WAL-mode) database file."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def backup(db_path: str, dest_dir: str | Path | None = None, keep: int = KEEP) -> Path:
    """Write a gzipped backup and keep only the newest ``keep``. Returns its path."""
    if not Path(db_path).exists():
        raise BackupError(f"no database at {db_path}")
    dest = Path(dest_dir) if dest_dir else default_dir(db_path)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target, n = dest / f"{PREFIX}{stamp}.db.gz", 1
    while target.exists():   # two backups within a second
        n += 1
        target = dest / f"{PREFIX}{stamp}-{n}.db.gz"
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        raw = Path(tmp) / "copy.db"
        _copy_db(db_path, raw)
        with open(raw, "rb") as fin, gzip.open(target.with_suffix(".part"), "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
    target.with_suffix(".part").rename(target)
    for old in list_backups(dest)[keep:]:
        old.unlink()
    return target


def list_backups(dest_dir: str | Path) -> list[Path]:
    """Backups in a directory, newest first."""
    d = Path(dest_dir)
    if not d.is_dir():
        return []
    return sorted(d.glob(f"{PREFIX}*.db.gz"), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


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
    "problems": ledger mismatches}.
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
    finally:
        conn.close()
    return {"saved": saved, "migrations": applied, "problems": problems}
