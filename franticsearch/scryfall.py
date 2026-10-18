"""Scryfall bulk-data sync into the local ``printings`` cache.

Uses the ``default_cards`` bulk file (every printing, in English or its only
language) per Scryfall's guidelines: one bulk download instead of per-card
API calls, with a descriptive User-Agent and an Accept header.

Scryfall serves the bulk file as gzipped JSON Lines (``jsonl_download_uri``);
older files were one JSON array (``download_uri``). ``import_stream`` accepts
either, gzipped or not.

Printings are upserted and never deleted, because stock and the ledger
reference them.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from typing import IO, Any, Callable, Iterable

import ijson
import requests

from . import __version__
from .db import get_meta, set_meta, transaction

log = logging.getLogger(__name__)

BULK_INDEX_URL = "https://api.scryfall.com/bulk-data/default-cards"
HEADERS = {
    "User-Agent": f"FranticSearch/{__version__} (self-hosted MTG collection manager)",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}
BATCH_SIZE = 2000
SYNC_MAX_AGE_HOURS = 24

_COLUMNS = (
    "scryfall_id", "oracle_id", "name", "lang", "set_code", "set_name",
    "collector_number", "released_at", "rarity", "layout", "finishes",
    "mana_cost", "cmc", "colors", "color_identity", "type_line",
    "oracle_text", "legalities", "image_small", "image_normal",
)
_UPSERT_SQL = (
    f"INSERT INTO printings({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"
    f" ON CONFLICT(scryfall_id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _COLUMNS[1:])
)

# Only one sync per process at a time (web button + background thread).
_sync_lock = threading.Lock()


def _faces(card: dict) -> list[dict]:
    return card.get("card_faces") or []


def _from_faces(card: dict, field: str, sep: str) -> str | None:
    if card.get(field) is not None:
        return card[field]
    parts = [f[field] for f in _faces(card) if f.get(field)]
    return sep.join(parts) if parts else None


def card_to_row(card: dict) -> tuple:
    """Map one Scryfall card object onto the ``printings`` columns."""
    faces = _faces(card)
    images = card.get("image_uris") or (faces[0].get("image_uris") if faces else None) or {}
    oracle_id = card.get("oracle_id") or (faces[0].get("oracle_id") if faces else None)
    colors = card.get("colors")
    if colors is None and faces:
        colors = sorted({c for f in faces for c in f.get("colors") or []})
    finishes = card.get("finishes")
    if not finishes:  # very old objects predate the field
        finishes = [f for f, flag in (("nonfoil", card.get("nonfoil")), ("foil", card.get("foil"))) if flag]
    cmc = card.get("cmc")
    return (
        card["id"],
        oracle_id,
        card["name"],
        card.get("lang", "en"),
        card["set"],
        card.get("set_name", card["set"].upper()),
        card["collector_number"],
        card.get("released_at"),
        card.get("rarity"),
        card.get("layout"),
        json.dumps(finishes),
        _from_faces(card, "mana_cost", " // "),
        float(cmc) if cmc is not None else None,
        json.dumps(colors or []),
        json.dumps(card.get("color_identity") or []),
        _from_faces(card, "type_line", " // "),
        _from_faces(card, "oracle_text", "\n//\n"),
        json.dumps(card.get("legalities") or {}),
        images.get("small"),
        images.get("normal"),
    )


def _batched(items: Iterable[Any], n: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def _iter_cards(fp: IO[bytes]) -> Iterable[dict]:
    """Yield objects from a bulk file: JSON array or JSON Lines, optionally gzipped."""
    if not hasattr(fp, "peek"):
        fp = io.BufferedReader(fp)
    if fp.peek(2)[:2] == b"\x1f\x8b":
        fp = io.BufferedReader(gzip.GzipFile(fileobj=fp))
    if fp.peek(256).lstrip()[:1] == b"[":
        yield from ijson.items(fp, "item", use_float=True)
    else:
        for line in fp:
            if line.strip():
                yield json.loads(line)


def import_stream(
    conn: sqlite3.Connection, fp: IO[bytes], progress: Callable[[int], None] | None = None
) -> int:
    """Stream a Scryfall bulk file into ``printings``. Returns cards processed."""
    total = 0
    for batch in _batched(_iter_cards(fp), BATCH_SIZE):
        rows = [card_to_row(c) for c in batch if c.get("object") == "card"]
        with transaction(conn):
            conn.executemany(_UPSERT_SQL, rows)
        total += len(rows)
        if progress:
            progress(total)
    return total


def _status(conn: sqlite3.Connection, status: str, **extra: str) -> None:
    with transaction(conn):
        set_meta(conn, "sync_status", status)
        for k, v in extra.items():
            set_meta(conn, k, v)


def sync(
    conn: sqlite3.Connection,
    force: bool = False,
    session: requests.Session | None = None,
) -> str:
    """Download and import ``default_cards`` if Scryfall has a newer file.

    Returns a short human-readable result. Raises on network/parse errors after
    recording them in ``meta`` (``sync_status`` = ``error``).
    """
    if not _sync_lock.acquire(blocking=False):
        return "a sync is already running"
    http = session or requests.Session()
    try:
        _status(conn, "checking")
        index = http.get(BULK_INDEX_URL, headers=HEADERS, timeout=30)
        index.raise_for_status()
        info = index.json()
        updated_at = info["updated_at"]
        if not force and get_meta(conn, "scryfall_updated_at") == updated_at:
            _status(conn, "idle", last_sync_at=_now(), last_sync_result="already up to date")
            return "already up to date"

        _status(conn, "downloading")
        with tempfile.TemporaryFile() as tmp:
            url = info.get("jsonl_download_uri") or info["download_uri"]
            with http.get(url, headers=HEADERS, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    tmp.write(chunk)
            tmp.seek(0)
            _status(conn, "importing")
            count = import_stream(
                conn, tmp, progress=lambda n: _status(conn, "importing", sync_progress=str(n))
            )
        result = f"imported {count} printings (Scryfall file of {updated_at})"
        _status(
            conn, "idle",
            scryfall_updated_at=updated_at, last_sync_at=_now(),
            last_sync_result=result, sync_error="",
        )
        return result
    except Exception as exc:
        log.exception("Scryfall sync failed")
        _status(conn, "error", sync_error=f"{type(exc).__name__}: {exc}", last_attempt_at=_now())
        raise
    finally:
        _sync_lock.release()


def is_running() -> bool:
    return _sync_lock.locked()


def needs_sync(conn: sqlite3.Connection) -> bool:
    """True if the cache is empty or the last successful check is older than a day."""
    if conn.execute("SELECT 1 FROM printings LIMIT 1").fetchone() is None:
        return True
    last = get_meta(conn, "last_sync_at")
    if not last:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
    return age.total_seconds() > SYNC_MAX_AGE_HOURS * 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
