"""Flask web UI: plain server-rendered forms, POST-redirect-GET, no JavaScript required."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session, url_for,
)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix

from . import __version__, db, decks, importer, ledger, locations, queries, scryfall
from .ledger import CONDITION_NAMES, CONDITIONS, FINISHES, LANGUAGES, LedgerError, StockKey

log = logging.getLogger(__name__)

# Default destination for collection imports (created on first use).
UNSORTED = "Unsorted"

def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("FS_DB", os.path.join("data", "franticsearch.db")),
        AUTO_SYNC=os.environ.get("FS_AUTO_SYNC", "1") not in ("0", "false", "no", ""),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,  # uploaded CSVs
        # Behind a reverse proxy: trust its X-Forwarded-For/-Proto/-Host/-Prefix headers
        # (e.g. nginx serving the app under /franticsearch/). Off by default.
        PROXY=os.environ.get("FS_PROXY", "0") not in ("0", "false", "no", ""),
    )
    app.config.update(config or {})
    if app.config["PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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
            "BOARDS": decks.BOARDS,
            "BOARD_NAMES": decks.BOARD_NAMES,
            "FORMATS": decks.FORMATS,
            "last_location": session.get("last_location"),
        }

    @app.template_filter("fromjson")
    def _fromjson(value: str | None):
        return json.loads(value) if value else None

    @app.template_filter("mana")
    def _mana(text: str | None) -> Markup:
        return mana_symbols(text)

    _register_routes(app)
    if app.config["AUTO_SYNC"]:
        _start_auto_sync(app.config["DATABASE"])
    return app


# --------------------------------------------------------------------------- helpers

_SYMBOL = re.compile(r"\{([^{}]{1,8})\}")
_SYMBOL_FILES = {"½": "HALF", "∞": "INFINITY"}


def mana_symbols(text: str | None) -> Markup:
    """Escape ``text`` and render {W}, {2/U}, {T} ... as Scryfall's symbol images."""
    if not text:
        return Markup("")
    out, pos = [], 0
    for m in _SYMBOL.finditer(text):
        sym = m.group(1)
        name = _SYMBOL_FILES.get(sym, sym.replace("/", "").upper())
        out.append(escape(text[pos:m.start()]))
        if name.isascii() and name.isalnum():
            out.append(Markup(
                '<img class="sym" src="https://svgs.scryfall.io/card-symbols/{}.svg" alt="{}" title="{}">'
            ).format(name, m.group(0), m.group(0)))
        else:
            out.append(escape(m.group(0)))
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("").join(out)


def _done(message: str, batch: str | None) -> None:
    """Flash a success message, plus an Undo button for the batch it wrote."""
    flash(message, "ok")
    if batch:
        flash(batch, "undo")


def _remember_location(location_id: int) -> None:
    session["last_location"] = location_id

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
            sort=request.args.get("sort", "name"),
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
        if request.args.get("go"):  # nav search: straight to the card if it's unambiguous
            exact = ([r for r in results if r["name"].lower() == q.lower()]
                     or [r for r in results if r["name"].split(" // ")[0].lower() == q.lower()])
            exact = [r for r in exact if r["layout"] != "art_series"] or exact
            hit = exact[0] if len(exact) == 1 else results[0] if len(results) == 1 else None
            if hit:
                return redirect(url_for("card", card_key=hit["card_key"]))
        return render_template("add.html", q=q, results=results, locs=_manual_locations(g.db))

    @app.post("/stock/quick-add")
    def stock_quick_add():
        """Add copies of a card in its default printing (free owned, else newest paper)."""
        conn = g.db
        try:
            printings = queries.card_printings(conn, request.form.get("card_key", ""))
            if not printings:
                raise LedgerError("unknown card")
            loc = _int_arg("location_id")
            if loc is None:
                raise LedgerError("choose a location")
            qty = _form_qty()
            key = importer.default_key(conn, printings)
            batch = ledger.acquire(conn, loc, key, qty)
            _remember_location(loc)
            p = next(p for p in printings if p["scryfall_id"] == key.printing_id)
            _done(f"Added {qty}× {p['name']} ({p['set_code'].upper()} #{p['collector_number']}"
                  f"{', ' + key.finish if key.finish != 'nonfoil' else ''}) to "
                  f"{locations.get(conn, loc)['name']}.", batch)
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("add")))

    @app.post("/undo")
    def undo():
        try:
            ledger.undo(g.db, request.form.get("batch", ""))
            flash("Undone.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("history")))

    @app.post("/stock/acquire")
    def stock_acquire():
        conn = g.db
        key = _form_key()
        try:
            loc = _int_arg("location_id")
            if loc is None:
                raise LedgerError("choose a location")
            qty = _form_qty()
            batch = ledger.acquire(conn, loc, key, qty, note=request.form.get("note", "").strip())
            _remember_location(loc)
            _done(f"Added {qty} cop{'y' if qty == 1 else 'ies'}.", batch)
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
            batch = ledger.move(conn, src, dst, key, qty, note=request.form.get("note", "").strip())
            _done(f"Moved {qty}.", batch)
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
            batch = ledger.adjust(conn, loc, key, qty, note=request.form.get("note", "").strip())
            if batch:
                _done(f"Count set to {qty}.", batch)
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
            batch = ledger.sell(conn, loc, key, qty, note=request.form.get("note", "").strip())
            _done(f"Removed {qty} from the collection.", batch)
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("collection")))

    # ---- locations

    @app.get("/locations")
    def locations_list():
        return render_template(
            "locations.html", locs=locations.list_all(g.db),
            archived=locations.list_all(g.db, archived=True),
        )

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
        # lines a paste couldn't match: shown (prefilled) once, on this location's page
        paste = session.pop("paste") if (session.get("paste") or {}).get("loc") == location_id else None
        return render_template(
            "location.html", loc=loc,
            stock=queries.location_stock(conn, location_id, request.args.get("sort", "name")),
            deck=decks.by_location(conn, location_id), tick_all=request.args.get("all") == "1",
            paste=paste,
            locs=_manual_locations(conn), has_history=locations.has_history(conn, location_id),
            history=queries.history(conn, location_id=location_id)[:20],
        )

    @app.post("/locations/<int:location_id>/move-selected")
    def location_move_selected(location_id: int):
        conn = g.db
        back = _back(url_for("location_view", location_id=location_id))
        try:
            dst = _int_arg("to_id")
            if dst is None:
                raise LedgerError("choose a destination")
            items = []
            for row in request.form.getlist("row"):
                parts = row.split("|")
                if len(parts) != 4:
                    raise LedgerError("bad selection")
                key = StockKey(*parts)
                qty = ledger.qty_at(conn, location_id, key)
                if qty:
                    items.append((key, qty))
            batch = ledger.move_many(conn, location_id, dst, items)
            _done(f"Moved {sum(q for _, q in items)} copies ({len(items)} lines) to "
                  f"{locations.get(conn, dst)['name']}.", batch)
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(back)

    @app.post("/locations/<int:location_id>/paste")
    def location_paste(location_id: int):
        conn = g.db
        session.pop("paste", None)
        try:
            batch, added, left = importer.paste_into(conn, location_id, request.form.get("text", ""))
            if added:
                _remember_location(location_id)
                _done(f"Added {added} cop{'y' if added == 1 else 'ies'}.", batch)
            if left:
                # kept in the (cookie) session for the next page view, so keep it small
                session["paste"] = {"loc": location_id, "text": "\n".join(r for r, _ in left)[:1500],
                                    "problems": [f"{r}: {p}"[:120] for r, p in left][:10]}
                flash(f"{len(left)} line(s) didn't match; they are back in the box below.", "error")
            elif not added:
                flash("Nothing to add.", "error")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("location_view", location_id=location_id) + "#paste")

    @app.post("/locations/<int:location_id>/edit")
    def location_edit(location_id: int):
        f = request.form
        try:
            locations.update(g.db, location_id, f.get("name", ""), f.get("type", ""), f.get("notes", ""))
            flash("Location saved.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("location_view", location_id=location_id))

    @app.post("/locations/<int:location_id>/archive")
    def location_archive(location_id: int):
        archive = request.form.get("archived") == "1"
        try:
            locations.set_archived(g.db, location_id, archive)
            flash("Location archived." if archive else "Location restored.", "ok")
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

    # ---- import

    @app.get("/import")
    def import_index():
        conn = g.db
        locs = _manual_locations(conn)
        return render_template(
            "import.html", imports=importer.list_imports(conn), locs=locs,
            has_unsorted=any(l["name"].lower() == UNSORTED.lower() for l in locs),
            unsorted=UNSORTED,
        )

    @app.post("/import/collection")
    def import_collection():
        conn = g.db
        upload = request.files.get("file")
        try:
            if not upload or not upload.filename:
                raise LedgerError("choose a CSV file")
            data = upload.read()
            if request.form.get("location_id") == "new":
                loc = conn.execute(
                    "SELECT id FROM locations WHERE name = ? COLLATE NOCASE", (UNSORTED,)
                ).fetchone()
                loc_id = loc["id"] if loc else locations.create(conn, UNSORTED, "box")
            else:
                loc_id = _int_arg("location_id")
                if loc_id is None:
                    raise LedgerError("choose a location")
            import_id = importer.import_collection(conn, data, loc_id, filename=upload.filename)
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("import_index"))
        imp = importer.get_import(conn, import_id)
        flash(f"Imported {imp['imported']} copies into {imp['location_name']}."
              + (f" {imp['open']} line(s) need a decision below." if imp["open"] else ""), "ok")
        return redirect(url_for("import_view", import_id=import_id))

    @app.post("/import/deck")
    def import_deck():
        conn = g.db
        f = request.form
        upload = request.files.get("file")
        text, filename = f.get("text", ""), ""
        try:
            if upload and upload.filename:
                filename = upload.filename
                try:
                    text = upload.read().decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise LedgerError("the file is not UTF-8 text") from None
            if not text.strip():
                raise LedgerError("paste a deck list or choose a file")
            name = f.get("name", "").strip() or os.path.splitext(filename)[0]
            import_id = importer.import_deck(
                conn, text, name, f.get("format", ""), f.get("after_blank", "commander"),
                filename=filename,
            )
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("import_index"))
        imp = importer.get_import(conn, import_id)
        cmd = decks.commander_names(conn, imp["deck_id"])
        flash(f"Created deck {imp['deck_name']} with {imp['imported']} cards"
              + (f"; commander: {' & '.join(cmd)}." if cmd else "; no commander.")
              + (f" {imp['open']} line(s) need a decision below." if imp["open"] else ""), "ok")
        return redirect(url_for("import_view", import_id=import_id))

    @app.get("/import/<int:import_id>")
    def import_view(import_id: int):
        conn = g.db
        imp = importer.get_import(conn, import_id)
        if imp is None:
            abort(404)
        return render_template(
            "import_view.html", imp=imp, rows=importer.unresolved(conn, import_id),
            commanders=decks.commander_names(conn, imp["deck_id"]) if imp["deck_id"] else [],
        )

    @app.get("/import/line/<int:uid>")
    def import_fix(uid: int):
        conn = g.db
        u = importer.get_unresolved(conn, uid)
        if u is None:
            abort(404)
        q = request.args.get("q", u["name"]).strip()
        card_key = request.args.get("card")
        return render_template(
            "import_fix.html", u=u, imp=importer.get_import(conn, u["import_id"]), q=q,
            results=queries.search_cards(conn, q) if q and not card_key else [],
            printings=queries.card_printings(conn, card_key) if card_key else [],
            card_key=card_key,
        )

    @app.post("/import/line/<int:uid>/fix")
    def import_fix_post(uid: int):
        conn = g.db
        u = importer.get_unresolved(conn, uid)
        if u is None:
            abort(404)
        try:
            importer.fix(conn, uid, _form_key(), _form_qty())
            flash("Line fixed.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(request.referrer or url_for("import_fix", uid=uid))
        return redirect(url_for("import_view", import_id=u["import_id"]))

    @app.post("/import/line/<int:uid>/dismiss")
    def import_dismiss(uid: int):
        conn = g.db
        u = importer.get_unresolved(conn, uid)
        if u is None:
            abort(404)
        try:
            importer.dismiss(conn, uid)
            flash("Line dismissed.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("import_view", import_id=u["import_id"]))

    # ---- decks (read-only until phase 4)

    @app.get("/decks")
    def decks_list():
        return render_template("decks.html", decks=decks.list_all(g.db))

    @app.get("/decks/<int:deck_id>")
    def deck_view(deck_id: int):
        conn = g.db
        deck = decks.get(conn, deck_id)
        if deck is None:
            abort(404)
        imp = conn.execute("SELECT id FROM imports WHERE deck_id = ?", (deck_id,)).fetchone()
        sort = request.args.get("sort", "name")
        keys = {"name": lambda s: s["name"].lower(), "cost": lambda s: (s["cmc"] or 0, s["name"].lower()),
                "qty": lambda s: (s["qty_wanted"], s["name"].lower())}
        key = keys.get(sort.lstrip("-"), keys["name"])
        slots = decks.slots(conn, deck_id)
        boards = {b: [s for s in slots if s["board"] == b] for b in decks.BOARDS}
        return render_template(
            "deck.html", deck=deck, slots=slots, sort=sort,
            groups=[(g_, rows[::-1] if sort.startswith("-") else rows)
                    for g_, rows in decks.grouped(boards["main"], key)],
            others={b: sorted(boards[b], key=key, reverse=sort.startswith("-"))
                    for b in ("commander", "side", "maybe")},
            curve=decks.mana_curve(boards["main"] + boards["commander"]),
            import_id=imp["id"] if imp else None,
            deletable=not locations.has_history(conn, deck["location_id"]),
        )

    @app.post("/decks/<int:deck_id>/delete")
    def deck_delete(deck_id: int):
        try:
            decks.delete(g.db, deck_id)
            flash("Deck deleted.", "ok")
            return redirect(url_for("decks_list"))
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("deck_view", deck_id=deck_id))

    # ---- history & system

    @app.get("/history")
    def history():
        conn = g.db
        loc = _int_arg("location")
        page = _int_arg("page", 1) or 1
        return render_template(
            "history.html", rows=queries.history(conn, location_id=loc, page=page),
            locs=locations.list_all(conn, archived=None), location=loc, page=page,
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
