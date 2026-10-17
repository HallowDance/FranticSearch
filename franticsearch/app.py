"""Flask web UI: plain server-rendered forms, POST-redirect-GET, no JavaScript required."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, url_for,
)

from . import __version__, db, ledger, locations, queries, scryfall
from .ledger import CONDITION_NAMES, CONDITIONS, FINISHES, LedgerError, StockKey

log = logging.getLogger(__name__)

# Scryfall language codes, in the order Scryfall lists them.
LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ja": "Japanese", "ko": "Korean", "ru": "Russian",
    "zhs": "Simplified Chinese", "zht": "Traditional Chinese", "he": "Hebrew",
    "la": "Latin", "grc": "Ancient Greek", "ar": "Arabic", "sa": "Sanskrit",
    "ph": "Phyrexian", "qya": "Quenya",
}


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("FS_DB", os.path.join("data", "franticsearch.db")),
        AUTO_SYNC=os.environ.get("FS_AUTO_SYNC", "1") not in ("0", "false", "no", ""),
    )
    app.config.update(config or {})

    conn = db.connect(app.config["DATABASE"])
    db.migrate(conn)
    secret = os.environ.get("FS_SECRET_KEY") or db.get_meta(conn, "secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        with db.transaction(conn):
            db.set_meta(conn, "secret_key", secret)
    app.secret_key = secret
    conn.close()

    @app.before_request
    def _open_db() -> None:
        g.db = db.connect(app.config["DATABASE"])

    @app.teardown_request
    def _close_db(_exc: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def _globals() -> dict:
        return {
            "version": __version__,
            "FINISHES": FINISHES,
            "CONDITIONS": CONDITIONS,
            "CONDITION_NAMES": CONDITION_NAMES,
            "LANGUAGES": LANGUAGES,
            "USER_TYPES": locations.USER_TYPES,
        }

    @app.template_filter("fromjson")
    def _fromjson(value: str | None):
        return json.loads(value) if value else None

    _register_routes(app)
    if app.config["AUTO_SYNC"]:
        _start_auto_sync(app.config["DATABASE"])
    return app


# --------------------------------------------------------------------------- helpers

def _int_arg(name: str, default: int | None = None) -> int | None:
    try:
        return int(request.values.get(name, ""))
    except ValueError:
        return default


def _form_key() -> StockKey:
    f = request.form
    return StockKey(
        printing_id=f.get("printing_id", ""),
        finish=f.get("finish", "nonfoil"),
        condition=f.get("condition", "NM") or "NM",
        lang=(f.get("lang", "en") or "en").strip().lower(),
    )


def _form_qty(name: str = "qty") -> int:
    try:
        qty = int(request.form.get(name, ""))
    except ValueError:
        raise LedgerError("quantity must be a whole number") from None
    return qty


def _back(default: str) -> str:
    """Redirect target: a local 'next' path if given, else ``default``."""
    nxt = request.form.get("next") or request.args.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


def _manual_locations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Locations offered in manual forms. Deck locations change via pull/return lists."""
    return [l for l in locations.list_all(conn) if l["type"] != "deck"]


def _run_sync_in_background(db_path: str, force: bool) -> None:
    def work() -> None:
        conn = db.connect(db_path)
        try:
            scryfall.sync(conn, force=force)
        except Exception:
            pass  # recorded in meta by scryfall.sync
        finally:
            conn.close()

    threading.Thread(target=work, name="scryfall-sync", daemon=True).start()


def _start_auto_sync(db_path: str) -> None:
    """Sync on first run, then re-check hourly and sync when the cache is a day old."""

    def loop() -> None:
        while True:
            conn = db.connect(db_path)
            try:
                if scryfall.needs_sync(conn):
                    scryfall.sync(conn)
            except Exception:
                pass  # recorded in meta; retried next hour
            finally:
                conn.close()
            time.sleep(3600)

    threading.Thread(target=loop, name="scryfall-auto-sync", daemon=True).start()


# --------------------------------------------------------------------------- routes

def _register_routes(app: Flask) -> None:

    @app.get("/")
    def index():
        conn = g.db
        return render_template(
            "index.html",
            totals=queries.totals(conn),
            locs=locations.list_all(conn),
            sync_status=db.get_meta(conn, "sync_status", "never run"),
            last_sync_at=db.get_meta(conn, "last_sync_at"),
        )

    # ---- collection browser

    @app.get("/collection")
    def collection():
        conn = g.db
        f = queries.CollectionFilter(
            q=request.args.get("q", "").strip(),
            location_id=_int_arg("location"),
            location_type=request.args.get("ltype", ""),
            not_in_deck=request.args.get("notindeck") == "1",
            set_code=request.args.get("set", "").strip(),
            page=_int_arg("page", 1) or 1,
        )
        rows, total = queries.collection(conn, f)
        return render_template(
            "collection.html", rows=rows, total=total, f=f,
            locs=locations.list_all(conn), page_size=queries.PAGE_SIZE,
        )

    @app.get("/card/<card_key>")
    def card(card_key: str):
        conn = g.db
        printings = queries.card_printings(conn, card_key)
        if not printings:
            abort(404)
        stock = queries.card_stock(conn, card_key)
        selected = request.args.get("printing")
        shown = next((p for p in printings if p["scryfall_id"] == selected), None)
        if shown is None and stock:
            shown = next(p for p in printings if p["scryfall_id"] == stock[0]["printing_id"])
        shown = shown or printings[0]
        return render_template(
            "card.html", printings=printings, stock=stock, shown=shown,
            card_key=card_key, locs=_manual_locations(conn),
            history=queries.history(conn, card_key=card_key)[:20],
        )

    # ---- adding cards

    @app.get("/add")
    def add():
        q = request.args.get("q", "").strip()
        results = queries.search_cards(g.db, q) if q else []
        return render_template("add.html", q=q, results=results)

    @app.post("/stock/acquire")
    def stock_acquire():
        conn = g.db
        key = _form_key()
        try:
            loc = _int_arg("location_id")
            if loc is None:
                raise LedgerError("choose a location")
            qty = _form_qty()
            ledger.acquire(conn, loc, key, qty, note=request.form.get("note", "").strip())
            flash(f"Added {qty} cop{'y' if qty == 1 else 'ies'}.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("collection")))

    # ---- moving / adjusting / removing existing stock

    @app.post("/stock/move")
    def stock_move():
        conn = g.db
        key = _form_key()
        try:
            src, dst = _int_arg("from_id"), _int_arg("to_id")
            if src is None or dst is None:
                raise LedgerError("choose a destination")
            qty = _form_qty()
            ledger.move(conn, src, dst, key, qty, note=request.form.get("note", "").strip())
            flash(f"Moved {qty}.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("collection")))

    @app.post("/stock/adjust")
    def stock_adjust():
        conn = g.db
        key = _form_key()
        try:
            loc = _int_arg("location_id")
            if loc is None:
                raise LedgerError("missing location")
            qty = _form_qty()
            if ledger.adjust(conn, loc, key, qty, note=request.form.get("note", "").strip()):
                flash(f"Count set to {qty}.", "ok")
            else:
                flash("Count unchanged.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("collection")))

    @app.post("/stock/sell")
    def stock_sell():
        conn = g.db
        key = _form_key()
        try:
            loc = _int_arg("location_id")
            if loc is None:
                raise LedgerError("missing location")
            qty = _form_qty()
            ledger.sell(conn, loc, key, qty, note=request.form.get("note", "").strip())
            flash(f"Removed {qty} from the collection.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("collection")))

    # ---- locations

    @app.get("/locations")
    def locations_list():
        return render_template("locations.html", locs=locations.list_all(g.db))

    @app.post("/locations")
    def locations_create():
        f = request.form
        try:
            loc_id = locations.create(g.db, f.get("name", ""), f.get("type", ""), f.get("notes", ""))
            flash("Location created.", "ok")
            return redirect(url_for("location_view", location_id=loc_id))
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("locations_list"))

    @app.get("/locations/<int:location_id>")
    def location_view(location_id: int):
        conn = g.db
        loc = locations.get(conn, location_id)
        if loc is None:
            abort(404)
        return render_template(
            "location.html", loc=loc, stock=queries.location_stock(conn, location_id),
            locs=_manual_locations(conn),
            history=queries.history(conn, location_id=location_id)[:20],
        )

    @app.post("/locations/<int:location_id>/edit")
    def location_edit(location_id: int):
        f = request.form
        try:
            locations.update(g.db, location_id, f.get("name", ""), f.get("type", ""), f.get("notes", ""))
            flash("Location saved.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("location_view", location_id=location_id))

    @app.post("/locations/<int:location_id>/delete")
    def location_delete(location_id: int):
        try:
            locations.delete(g.db, location_id)
            flash("Location deleted.", "ok")
            return redirect(url_for("locations_list"))
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("location_view", location_id=location_id))

    # ---- history & system

    @app.get("/history")
    def history():
        conn = g.db
        loc = _int_arg("location")
        page = _int_arg("page", 1) or 1
        return render_template(
            "history.html", rows=queries.history(conn, location_id=loc, page=page),
            locs=locations.list_all(conn), location=loc, page=page,
            page_size=queries.PAGE_SIZE,
        )

    @app.get("/system")
    def system():
        conn = g.db
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta WHERE key <> 'secret_key'")}
        return render_template(
            "system.html", meta=meta, running=scryfall.is_running(),
            schema=db.schema_version(conn), totals=queries.totals(conn),
            problems=ledger.check(conn) if request.args.get("check") else None,
            db_path=app.config["DATABASE"], auto_sync=app.config["AUTO_SYNC"],
        )

    @app.post("/system/sync")
    def system_sync():
        if scryfall.is_running():
            flash("A sync is already running.", "error")
        else:
            _run_sync_in_background(app.config["DATABASE"], force=request.form.get("force") == "1")
            flash("Scryfall sync started in the background. Reload this page to follow it.", "ok")
        return redirect(url_for("system"))
