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

from . import __version__, db, deckplan, decks, importer, ledger, locations, queries, scryfall, users
from .ledger import CONDITION_NAMES, CONDITIONS, FINISHES, LANGUAGES, LedgerError, StockKey

log = logging.getLogger(__name__)

# Reachable without logging in (share links are read-only and unguessable).
PUBLIC_ENDPOINTS = {"login", "static", "shared_link"}

# How card lists are shown: a text table (with actions) or a grid of card images.
VIEWS = ("text", "images")

UNSORTED = locations.UNSORTED

def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("FS_DB", os.path.join("data", "franticsearch.db")),
        AUTO_SYNC=os.environ.get("FS_AUTO_SYNC", "1") not in ("0", "false", "no", ""),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,  # uploaded CSVs
        # Behind a reverse proxy: trust its X-Forwarded-For/-Proto/-Host/-Prefix headers
        # (e.g. nginx serving the app under /franticsearch/). Off by default.
        PROXY=os.environ.get("FS_PROXY", "0") not in ("0", "false", "no", ""),
        CSRF=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set FS_SECURE_COOKIE=1 when the app is only reached over HTTPS.
        SESSION_COOKIE_SECURE=os.environ.get("FS_SECURE_COOKIE", "0") not in ("0", "false", "no", ""),
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
        if request.args.get("view") in VIEWS:  # card list view, remembered for the session
            session["view"] = request.args["view"]

    @app.before_request
    def _auth():
        """The single auth hook: who is logged in, login required, CSRF on every POST."""
        g.user = users.get(g.db, session.get("uid"))
        if request.method == "POST" and app.config["CSRF"]:
            sent = request.form.get("csrf", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "missing or stale form token; reload the page and try again")
        if g.user is None and request.endpoint not in PUBLIC_ENDPOINTS:
            return redirect(url_for("login", next=request.script_root + request.full_path))
        return None

    def _csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

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
            "last_deck": session.get("last_deck"),
            "view": session.get("view", "text"),
            "user": g.get("user"),
            "csrf_token": _csrf_token,
            "VISIBILITIES": locations.VISIBILITIES,
            "VISIBILITY_NAMES": locations.VISIBILITY_NAMES,
            "COLORS": queries.COLORS,
            "MANA_VALUES": queries.MANA_VALUES,
            "COLOR_MODES": queries.COLOR_MODES,
            "COLOR_FIELDS": queries.COLOR_FIELDS,
            "MV_OPS": queries.MV_OPS,
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


def _uid() -> int:
    return g.user["id"]


def _manual_locations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The user's locations offered in manual forms. Deck locations change via pull/return lists."""
    return [l for l in locations.list_all(conn, _uid()) if l["type"] != "deck"]


# Ownership checks: anything that isn't the logged-in user's is "not found".

def _my_location(location_id: int | None) -> sqlite3.Row:
    loc = locations.get(g.db, location_id) if location_id is not None else None
    if loc is None or loc["owner_id"] != _uid():
        abort(404)
    return loc


def _my_location_arg(name: str) -> int:
    """A location id from the form that must belong to the user (LedgerError if missing)."""
    value = _int_arg(name)
    if value is None:
        raise LedgerError("choose a location")
    return _my_location(value)["id"]


def _my_deck(deck_id: int) -> sqlite3.Row:
    deck = decks.get(g.db, deck_id)
    if deck is None:
        abort(404)
    _my_location(deck["location_id"])
    return deck


def _my_import(import_id: int) -> sqlite3.Row:
    imp = importer.get_import(g.db, import_id)
    if imp is None or imp["owner_id"] != _uid():
        abort(404)
    return imp


def _my_unresolved(uid: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    u = importer.get_unresolved(g.db, uid)
    if u is None:
        abort(404)
    return u, _my_import(u["import_id"])


def _deck_context(deck: sqlite3.Row) -> dict:
    """Slots grouped and sorted for the deck page (owner and shared views)."""
    sort = request.args.get("sort", "name")
    keys = {"name": lambda s: s["name"].lower(), "cost": lambda s: (s["cmc"] or 0, s["name"].lower()),
            "qty": lambda s: (s["qty_wanted"], s["name"].lower())}
    key = keys.get(sort.lstrip("-"), keys["name"])
    st = deckplan.state(g.db, deck)
    slots = st["slots"]
    boards = {b: [s for s in slots if s["board"] == b] for b in decks.BOARDS}
    return dict(
        deck=deck, slots=slots, sort=sort, st=st,
        groups=[(g_, rows[::-1] if sort.startswith("-") else rows)
                for g_, rows in decks.grouped(boards["main"], key)],
        others={b: sorted(boards[b], key=key, reverse=sort.startswith("-"))
                for b in ("commander", "side", "maybe")},
        curve=decks.mana_curve(boards["main"] + boards["commander"]),
    )


def _filtered_stock(location_id: int) -> dict:
    """A location's stock, sorted and filtered by the request args, plus filter choices."""
    rows = queries.location_stock(g.db, location_id, request.args.get("sort", "name"))
    f = queries.StockFilter.from_args(request.args)
    return dict(stock=[r for r in rows if f.matches(r)], all_lines=len(rows),
                all_copies=sum(r["qty"] for r in rows), f=f, facets=queries.stock_facets(rows))


def _admin_only() -> None:
    if not g.user["is_admin"]:
        abort(403)


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
            totals=queries.totals(conn, _uid()),
            locs=locations.list_all(conn, _uid()),
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
        rows, total = queries.collection(conn, f, _uid())
        return render_template(
            "collection.html", rows=rows, total=total, f=f,
            locs=locations.list_all(conn, _uid()), page_size=queries.PAGE_SIZE,
        )

    @app.get("/card/<card_key>")
    def card(card_key: str):
        conn = g.db
        printings = queries.card_printings(conn, card_key)
        if not printings:
            abort(404)
        stock = queries.card_stock(conn, card_key, _uid())
        selected = request.args.get("printing")
        shown = next((p for p in printings if p["scryfall_id"] == selected), None)
        if shown is None and stock:
            shown = next(p for p in printings if p["scryfall_id"] == stock[0]["printing_id"])
        shown = shown or printings[0]
        return render_template(
            "card.html", printings=printings, stock=stock, shown=shown,
            card_key=card_key, locs=_manual_locations(conn),
            history=queries.history(conn, _uid(), card_key=card_key)[:20],
            others=queries.shared_stock(conn, card_key, _uid()),
            my_decks=decks.list_all(conn, _uid()),
        )

    # ---- adding cards

    @app.get("/add")
    def add():
        """Card search (phase 3), with quick add; ?go=1 is the nav box's jump-to-card."""
        q = request.args.get("q", "").strip()
        if request.args.get("go"):  # nav search: straight to the card if it's unambiguous
            results = queries.search_cards(g.db, q) if q else []
            exact = ([r for r in results if r["name"].lower() == q.lower()]
                     or [r for r in results if r["name"].split(" // ")[0].lower() == q.lower()])
            exact = [r for r in exact if r["layout"] != "art_series"] or exact
            hit = exact[0] if len(exact) == 1 else results[0] if len(results) == 1 else None
            if hit:
                return redirect(url_for("card", card_key=hit["card_key"]))
        f = queries.CardSearch.from_args(request.args)
        cards, total = queries.search(g.db, f, _uid())
        target = _my_deck(_int_arg("deck")) if request.args.get("deck") else None
        return render_template("add.html", f=f, cards=cards, total=total, target=target,
                               page_size=queries.SEARCH_PAGE, locs=_manual_locations(g.db))

    @app.post("/stock/quick-add")
    def stock_quick_add():
        """Add copies of a card in its default printing (free owned, else newest paper)."""
        conn = g.db
        try:
            printings = queries.card_printings(conn, request.form.get("card_key", ""))
            if not printings:
                raise LedgerError("unknown card")
            loc = _my_location_arg("location_id")
            qty = _form_qty()
            key = importer.default_key(conn, printings, owner=_uid())
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
        batch = request.form.get("batch", "")
        if ledger.batch_owners(g.db, batch) != {_uid()}:
            abort(404)
        try:
            ledger.undo(g.db, batch)
            flash("Undone.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("history")))

    @app.post("/stock/acquire")
    def stock_acquire():
        conn = g.db
        key = _form_key()
        try:
            loc = _my_location_arg("location_id")
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
            src, dst = _my_location_arg("from_id"), _my_location_arg("to_id")
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
            loc = _my_location_arg("location_id")
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
            loc = _my_location_arg("location_id")
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
            "locations.html", locs=locations.list_all(g.db, _uid()),
            archived=locations.list_all(g.db, _uid(), archived=True),
        )

    @app.post("/locations")
    def locations_create():
        f = request.form
        try:
            loc_id = locations.create(g.db, f.get("name", ""), f.get("type", ""), f.get("notes", ""),
                                      owner=_uid())
            flash("Location created.", "ok")
            return redirect(url_for("location_view", location_id=loc_id))
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("locations_list"))

    @app.get("/locations/<int:location_id>")
    def location_view(location_id: int):
        conn = g.db
        loc = locations.get(conn, location_id)
        if loc is None or not locations.can_view(loc, _uid()):
            abort(404)
        if loc["owner_id"] != _uid():  # someone else's shared location: read-only
            return _render_shared(loc)
        # lines a paste couldn't match: shown (prefilled) once, on this location's page
        paste = session.pop("paste") if (session.get("paste") or {}).get("loc") == location_id else None
        return render_template(
            "location.html", loc=loc, **_filtered_stock(location_id),
            deck=decks.by_location(conn, location_id), tick_all=request.args.get("all") == "1",
            paste=paste,
            locs=_manual_locations(conn), has_history=locations.has_history(conn, location_id),
            history=queries.history(conn, _uid(), location_id=location_id)[:20],
        )

    @app.post("/locations/<int:location_id>/move-selected")
    def location_move_selected(location_id: int):
        conn = g.db
        _my_location(location_id)
        back = _back(url_for("location_view", location_id=location_id))
        try:
            dst = _my_location_arg("to_id")
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
        _my_location(location_id)
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
        _my_location(location_id)
        try:
            locations.update(g.db, location_id, f.get("name", ""), f.get("type", ""), f.get("notes", ""))
            flash("Location saved.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("location_view", location_id=location_id))

    @app.post("/locations/<int:location_id>/archive")
    def location_archive(location_id: int):
        _my_location(location_id)
        archive = request.form.get("archived") == "1"
        try:
            locations.set_archived(g.db, location_id, archive)
            flash("Location archived." if archive else "Location restored.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("location_view", location_id=location_id))

    @app.post("/locations/<int:location_id>/delete")
    def location_delete(location_id: int):
        _my_location(location_id)
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
            "import.html", imports=importer.list_imports(conn, _uid()), locs=locs,
            has_unsorted=any(l["name"].lower() == UNSORTED.lower() for l in locs),
            unsorted=UNSORTED,
        )

    @app.post("/import/collection")
    def import_collection():
        conn = g.db
        upload = request.files.get("file")
        if request.form.get("location_id") != "new":
            _my_location(_int_arg("location_id"))   # someone else's location: not found
        try:
            if not upload or not upload.filename:
                raise LedgerError("choose a CSV file")
            data = upload.read()
            if request.form.get("location_id") == "new":
                loc_id = locations.unsorted(conn, owner=_uid())
            else:
                loc_id = _my_location_arg("location_id")
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
                filename=filename, owner=_uid(),
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
        imp = _my_import(import_id)
        return render_template(
            "import_view.html", imp=imp, rows=importer.unresolved(conn, import_id),
            commanders=decks.commander_names(conn, imp["deck_id"]) if imp["deck_id"] else [],
        )

    @app.get("/import/line/<int:uid>")
    def import_fix(uid: int):
        conn = g.db
        u, imp = _my_unresolved(uid)
        q = request.args.get("q", u["name"]).strip()
        card_key = request.args.get("card")
        return render_template(
            "import_fix.html", u=u, imp=imp, q=q,
            results=queries.search_cards(conn, q) if q and not card_key else [],
            printings=queries.card_printings(conn, card_key) if card_key else [],
            card_key=card_key,
        )

    @app.post("/import/line/<int:uid>/fix")
    def import_fix_post(uid: int):
        conn = g.db
        u, _ = _my_unresolved(uid)
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
        u, _ = _my_unresolved(uid)
        try:
            importer.dismiss(conn, uid)
            flash("Line dismissed.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("import_view", import_id=u["import_id"]))

    # ---- decks (phase 4: editor, derived state, pull / return lists)

    @app.get("/decks")
    def decks_list():
        conn = g.db
        rows = [(d, deckplan.status(deckplan.state(conn, d))) for d in decks.list_all(conn, _uid())]
        return render_template("decks.html", decks=rows)

    @app.post("/decks")
    def deck_create():
        f = request.form
        try:
            deck_id = decks.create(g.db, f.get("name", ""), f.get("format", ""), owner=_uid())
        except LedgerError as e:
            flash(str(e), "error")
            return redirect(url_for("decks_list"))
        flash("Deck created. Add cards from the search or paste a list.", "ok")
        return redirect(url_for("deck_view", deck_id=deck_id))

    @app.get("/decks/<int:deck_id>")
    def deck_view(deck_id: int):
        conn = g.db
        deck = decks.get(conn, deck_id)
        loc = locations.get(conn, deck["location_id"]) if deck else None
        if not locations.can_view(loc, _uid()):
            abort(404)
        if loc["owner_id"] != _uid():
            return _render_shared(loc)
        imp = conn.execute("SELECT id FROM imports WHERE deck_id = ?", (deck_id,)).fetchone()
        ctx = _deck_context(deck)
        return render_template(
            "deck.html", **ctx, loc=loc, readonly=False,
            paste=session.pop("paste") if (session.get("paste") or {}).get("deck") == deck_id else None,
            import_id=imp["id"] if imp else None, status=deckplan.status(ctx["st"]),
            stats=deckplan.analysis(ctx["st"], deck),
            deletable=not locations.has_history(conn, deck["location_id"]),
        )

    @app.post("/decks/<int:deck_id>/edit")
    def deck_edit(deck_id: int):
        _my_deck(deck_id)
        f = request.form
        try:
            decks.update(g.db, deck_id, f.get("name", ""), f.get("format", ""), f.get("notes", ""))
            flash("Deck saved.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("deck_view", deck_id=deck_id))

    @app.route("/decks/<int:deck_id>/slot", methods=["GET", "POST"])
    def deck_slot(deck_id: int):
        """Edit one slot: qty, proxies, board, printing, finish (or remove it)."""
        conn = g.db
        deck = _my_deck(deck_id)
        sid = request.values.get("slot", "")
        slot = decks.get_slot(conn, deck_id, sid)
        if slot is None:
            abort(404)
        if request.method == "POST":
            f = request.form
            try:
                qty = 0 if f.get("remove") else _form_qty()
                decks.edit_slot(conn, deck_id, sid, qty=qty, proxy=_form_qty("proxy") if qty else 0,
                                board=f.get("board", slot["board"]),
                                printing_id=f.get("printing_id") or None, finish=f.get("finish") or None)
                flash("Slot removed." if not qty else "Slot saved.", "ok")
                return redirect(url_for("deck_view", deck_id=deck_id))
            except LedgerError as e:
                flash(str(e), "error")
                return redirect(url_for("deck_slot", deck_id=deck_id, slot=sid))
        p = conn.execute("SELECT * FROM printings WHERE scryfall_id = ?", (slot["printing_id"],)).fetchone()
        card_key = p["oracle_id"] or p["scryfall_id"]
        counts = {(r["printing_id"], r["finish"]): r for r in conn.execute(
            """SELECT s.printing_id, s.finish, SUM(s.qty) AS owned,
                      SUM(CASE WHEN l.type <> 'deck' THEN s.qty ELSE 0 END) AS free
                 FROM stock s JOIN locations l ON l.id = s.location_id
                 JOIN printings p ON p.scryfall_id = s.printing_id
                WHERE l.owner_id = ? AND COALESCE(p.oracle_id, p.scryfall_id) = ?
                GROUP BY s.printing_id, s.finish""", (_uid(), card_key))}
        return render_template("deck_slot.html", deck=deck, slot=slot, sid=sid, p=p, card_key=card_key,
                               printings=queries.card_printings(conn, card_key), counts=counts)

    @app.post("/decks/add-card")
    def deck_add_card():
        """Add a card to a deck (``deck_id`` field): a given printing (card page) or the
        default one (search)."""
        conn = g.db
        deck_id = _int_arg("deck_id")
        if deck_id is None:
            abort(404)
        deck = _my_deck(deck_id)
        f = request.form
        try:
            qty = _form_qty()
            if f.get("printing_id"):
                key = StockKey(f["printing_id"], f.get("finish", "nonfoil"))
            else:
                printings = queries.card_printings(conn, f.get("card_key", ""))
                if not printings:
                    raise LedgerError("unknown card")
                key = importer.default_key(conn, printings, owner=_uid())
            decks.add_slot(conn, deck_id, key, f.get("board", "main"), qty)
            p = conn.execute("SELECT name, set_code, collector_number FROM printings WHERE scryfall_id = ?",
                             (key.printing_id,)).fetchone()
            session["last_deck"] = deck_id
            flash(f"Added {qty}× {p['name']} ({p['set_code'].upper()} #{p['collector_number']}"
                  f"{', ' + key.finish if key.finish != 'nonfoil' else ''}) to {deck['name']}"
                  f" ({decks.BOARD_NAMES.get(f.get('board', 'main'), '')}).", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("deck_view", deck_id=deck_id)))

    @app.post("/decks/<int:deck_id>/paste")
    def deck_paste(deck_id: int):
        _my_deck(deck_id)
        session.pop("paste", None)
        try:
            added, left = importer.paste_into_deck(g.db, deck_id, request.form.get("text", ""),
                                                   request.form.get("board", "main"), owner=_uid())
            if added:
                flash(f"Added {added} card{'' if added == 1 else 's'} to the deck.", "ok")
            if left:
                session["paste"] = {"deck": deck_id, "text": "\n".join(r for r, _ in left)[:1500],
                                    "problems": [f"{r}: {p}"[:120] for r, p in left][:10]}
                flash(f"{len(left)} line(s) didn't match; they are back in the box below.", "error")
            elif not added:
                flash("Nothing to add.", "error")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("deck_view", deck_id=deck_id) + "#paste")

    def _pull_page(deck_ids, deck=None):
        lines, shortfalls = deckplan.pull_plan(g.db, _uid(), deck_ids)
        return render_template("pull.html", deck=deck, lines=lines, shortfalls=shortfalls)

    def _pull_post(deck_ids, back: str):
        selected = None if request.form.get("all") else set(request.form.getlist("line"))
        try:
            batch, n = deckplan.confirm_pulls(g.db, _uid(), selected, deck_ids)
            _done(f"Pulled {n} card{'' if n == 1 else 's'}.", batch)
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(back)

    @app.get("/pull")
    def pull_all():
        return _pull_page(None)

    @app.post("/pull")
    def pull_all_post():
        return _pull_post(None, url_for("pull_all"))

    @app.get("/decks/<int:deck_id>/pull")
    def deck_pull(deck_id: int):
        return _pull_page([deck_id], _my_deck(deck_id))

    @app.post("/decks/<int:deck_id>/pull")
    def deck_pull_post(deck_id: int):
        _my_deck(deck_id)
        return _pull_post([deck_id], url_for("deck_pull", deck_id=deck_id))

    @app.post("/decks/<int:deck_id>/retarget")
    def deck_retarget(deck_id: int):
        """Switch (part of) a short slot to another printing of the card."""
        _my_deck(deck_id)
        f = request.form
        if decks.get_slot(g.db, deck_id, f.get("slot", "")) is None:
            abort(404)
        try:
            decks.retarget(g.db, deck_id, f["slot"], StockKey(f.get("printing_id", ""), f.get("finish", "nonfoil")),
                           _form_qty())
            flash("Slot switched to that printing.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(_back(url_for("deck_pull", deck_id=deck_id)))

    @app.get("/decks/<int:deck_id>/return")
    def deck_return(deck_id: int):
        conn = g.db
        deck = _my_deck(deck_id)
        return render_template("return.html", deck=deck, lines=deckplan.return_plan(conn, deck),
                               locs=_manual_locations(conn), unsorted=UNSORTED)

    @app.post("/decks/<int:deck_id>/return")
    def deck_return_post(deck_id: int):
        deck = _my_deck(deck_id)
        f = request.form
        picked = None if f.get("all") else set(f.getlist("line"))
        choices = {}
        for name, value in f.items():
            if not name.startswith("to:"):
                continue
            line_id = name[3:]
            if picked is not None and line_id not in picked:
                continue
            choices[line_id] = "unsorted" if value == "unsorted" else _my_location(_int_arg(name))["id"]
        try:
            batch, n = deckplan.confirm_returns(g.db, deck, choices, owner=_uid())
            _done(f"Returned {n} card{'' if n == 1 else 's'}.", batch)
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("deck_return", deck_id=deck_id))

    @app.post("/decks/<int:deck_id>/build")
    def deck_build(deck_id: int):
        deck = _my_deck(deck_id)
        try:
            batch, pulled, retargeted = deckplan.mark_built(g.db, deck, owner=_uid())
            msg = f"Pulled {pulled} card{'' if pulled == 1 else 's'} into the deck"
            msg += f"; {retargeted} slot cop{'y' if retargeted == 1 else 'ies'} switched to the printing you own." if retargeted else "."
            st = deckplan.state(g.db, decks.get(g.db, deck_id))
            if st["short"]:
                msg += f" Still {st['short']} short: proxy or buy them."
            _done(msg, batch)
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("deck_view", deck_id=deck_id))

    def _render_shared(loc: sqlite3.Row, anonymous: bool = False):
        """Read-only view of a shared location (or deck) for other users / share links."""
        conn = g.db
        owner = users.get(conn, loc["owner_id"])
        deck = decks.by_location(conn, loc["id"])
        if deck:
            return render_template("deck.html", **_deck_context(deck), loc=loc, readonly=True,
                                   owner=owner, anonymous=anonymous)
        return render_template(
            "shared.html", loc=loc, owner=owner, anonymous=anonymous, **_filtered_stock(loc["id"]),
        )

    @app.get("/s/<token>")
    def shared_link(token: str):
        loc = locations.by_token(g.db, token)
        if loc is None or loc["archived"]:
            abort(404)
        return _render_shared(loc, anonymous=g.user is None)

    @app.get("/people")
    def people():
        return render_template("people.html", shared=locations.shared(g.db, _uid()))

    @app.post("/locations/<int:location_id>/visibility")
    def location_visibility(location_id: int):
        loc = _my_location(location_id)
        try:
            locations.set_visibility(g.db, location_id, request.form.get("visibility", ""),
                                     new_link=request.form.get("new_link") == "1")
            flash("Sharing updated.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        deck = decks.by_location(g.db, loc["id"])
        return redirect(url_for("deck_view", deck_id=deck["id"]) if deck
                        else url_for("location_view", location_id=location_id))

    # ---- accounts

    @app.route("/login", methods=["GET", "POST"])
    def login():
        conn = g.db
        nxt = request.values.get("next", "")
        nxt = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index")
        if request.method == "POST":
            user = users.authenticate(conn, request.form.get("username", ""),
                                      request.form.get("password", ""))
            if user:
                session.clear()  # new session id contents: no fixation, fresh CSRF token
                session["uid"] = user["id"]
                return redirect(nxt)
            flash("Wrong username or password.", "error")
        return render_template("login.html", next=nxt, no_users=users.count(conn) == 0)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("Logged out.", "ok")
        return redirect(url_for("login"))

    @app.route("/account", methods=["GET", "POST"])
    def account():
        if request.method == "POST":
            f = request.form
            try:
                if not users.authenticate(g.db, g.user["username"], f.get("current", "")):
                    raise LedgerError("current password is wrong")
                if f.get("new", "") != f.get("repeat", ""):
                    raise LedgerError("the new passwords don't match")
                users.set_password(g.db, _uid(), f.get("new", ""))
                flash("Password changed.", "ok")
            except LedgerError as e:
                flash(str(e), "error")
            return redirect(url_for("account"))
        return render_template("account.html")

    @app.route("/admin/users", methods=["GET", "POST"])
    def admin_users():
        _admin_only()
        if request.method == "POST":
            f = request.form
            try:
                users.create(g.db, f.get("username", ""), f.get("password", ""),
                             is_admin=f.get("is_admin") == "1")
                flash(f"User {f.get('username', '').strip()} created.", "ok")
            except LedgerError as e:
                flash(str(e), "error")
            return redirect(url_for("admin_users"))
        return render_template("admin_users.html", users=users.list_all(g.db))

    @app.post("/admin/users/<int:user_id>/password")
    def admin_set_password(user_id: int):
        _admin_only()
        try:
            users.set_password(g.db, user_id, request.form.get("password", ""))
            flash("Password set.", "ok")
        except LedgerError as e:
            flash(str(e), "error")
        return redirect(url_for("admin_users"))

    @app.post("/decks/<int:deck_id>/delete")
    def deck_delete(deck_id: int):
        _my_deck(deck_id)
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
            "history.html", rows=queries.history(conn, _uid(), location_id=loc, page=page),
            locs=locations.list_all(conn, _uid(), archived=None), location=loc, page=page,
            page_size=queries.PAGE_SIZE,
        )

    @app.get("/system")
    def system():
        _admin_only()
        conn = g.db
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM meta WHERE key <> 'secret_key'")}
        return render_template(
            "system.html", meta=meta, running=scryfall.is_running(),
            schema=db.schema_version(conn), totals=queries.totals(conn, None),
            problems=ledger.check(conn) if request.args.get("check") else None,
            db_path=app.config["DATABASE"], auto_sync=app.config["AUTO_SYNC"],
        )

    @app.post("/system/sync")
    def system_sync():
        _admin_only()
        if scryfall.is_running():
            flash("A sync is already running.", "error")
        else:
            _run_sync_in_background(app.config["DATABASE"], force=request.form.get("force") == "1")
            flash("Scryfall sync started in the background. Reload this page to follow it.", "ok")
        return redirect(url_for("system"))
