# CLAUDE.md

FranticSearch: self-hosted MTG collection & deck manager where **every physical card has a location and decks are just locations**. A few users, each with a separate collection, can share locations or decks. Design, decisions and roadmap live in [PLAN.md](PLAN.md); treat it as the source of truth (§ Phases has per-phase status, the numbered workflow sections record decisions).

## Working agreement

- Stick to PLAN.md. When something isn't covered by the plan, **ask the user** instead of assuming; record answered decisions in PLAN.md.
- One feature branch per phase or feature, then a PR. The user asks for "open and merge" explicitly; don't merge on your own.
  - Use **merge commits** (`gh pr merge --merge`), never squash: branches are sometimes stacked.
  - For a stacked branch, merge the lower PR first, then open the next against `main`.
- After code changes, run `pytest` and check the change against the real DB too, e.g. read-only GETs via `app.test_client()` with `session["uid"] = 1`. Then restart the server (`systemctl --user restart franticsearch`): it caches templates.
- UI: functional, server-rendered HTML forms, no JS (not even inline); deliberately plain 90s/00s FOSS look (grey chrome, blue links, bordered tables). Keep it usable at phone width (no horizontal page scroll). Don't spend long on polish.
  - Without JS a `<select>` does nothing until submitted: for switching a view (format, mode), prefer one link per choice with the active one in bold (as the view switch and buy list do), and put what changes near the top of the page.
  - Phone check: headless `chromium --headless --window-size=390,1800 --screenshot=…` against a copy of the DB, with a throwaway app that sets `session["uid"]` in a `before_request` inserted before `_auth`.

## Commands

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,serve]"
pytest                                   # full suite, ~12 s, no network (134 tests)
python -m franticsearch serve            # dev server http://127.0.0.1:6602 (auto-syncs Scryfall); live uses gunicorn
python -m franticsearch serve --no-auto-sync
python -m franticsearch migrate          # apply pending migrations (serve also does this)
python -m franticsearch sync [--force]   # Scryfall default_cards → printings (~80 MB .jsonl.gz, ~25 s)
python -m franticsearch sync --file x.jsonl.gz   # import a local bulk file (.jsonl/.json, gz or not)
python -m franticsearch check            # stock vs ledger invariant (exit 1 on mismatch)
python -m franticsearch backup [--dir D] [--keep 14]   # gzipped online backup → data/backups/
python -m franticsearch restore FILE [--force]         # stop the server first; saves the current DB
python -m franticsearch user add NAME [--admin] [--password-stdin]   # first admin adopts ownerless data
python -m franticsearch user passwd NAME [--password-stdin] | user list
python -m franticsearch user disable NAME | user enable NAME   # also on the web Users page
docker compose up -d --build             # production-style, DB in ./data (never actually run yet)
```

Env: `FS_DB` (default `data/franticsearch.db`), `FS_AUTO_SYNC` (default `1`), `FS_SECRET_KEY` (optional; else generated and stored in `meta`), `FS_PROXY` (`1` = trust a reverse proxy's `X-Forwarded-For/-Proto/-Host/-Prefix`, needed under `/franticsearch/`), `FS_SECURE_COOKIE` (`1` = session cookie only over HTTPS; off because the dev server is also used over plain http), `FS_LOG_LEVEL` (default `INFO`; logs go to stderr → the journal).

## Architecture

| File | Role |
|---|---|
| `franticsearch/migrations/NNN_*.sql` | Schema; latest is `007_user_disabled.sql` (006 = `movements.owner_id`, set by `ledger._record`; 007 = `users.disabled`). Append a new numbered file for every change; never edit applied ones. The runner disables FKs per migration (so tables can be rebuilt) and runs `foreign_key_check` before COMMIT. Version in `PRAGMA user_version`. |
| `db.py` | Connections (WAL, FKs on, autocommit), **re-entrant** `transaction()` (joins an open one, so domain functions compose atomically), migration runner, `meta` helpers |
| `ledger.py` | **The only code that changes `stock`.** acquire(_many) / move(_many) / transfer_many (several from→to pairs, one batch) / adjust / sell / undo write stock + `movements` in one transaction. Refuses moves between two users' locations. `check()` verifies the invariant. Also `LANGUAGES`, `CONDITIONS`, `StockKey`, `validate_key`. |
| `users.py` | Accounts: create (first admin adopts ownerless locations/imports/movements), authenticate (constant-ish time; refuses disabled accounts), set password, `generate_password()`, `set_disabled()` (never the last enabled admin) |
| `locations.py` | Location CRUD (owner, names unique per owner), archive, visibility (`private` < `users` < `link`) + share tokens, `can_view`, `shared()` for the People page, `unsorted()` (the owner's "Unsorted" box, created on use) |
| `decks.py` | Deck + its same-named deck location (`update` renames both), `add_slot`, `edit_slot` (qty / proxy / board / printing / finish; merges, qty 0 removes), `retarget`, slot ids `printing\|finish\|cond\|lang\|board`, `slots(counts=True)` (in-deck / free / where / any-printing-outside-this-deck subqueries, scoped to the deck owner; `counts=False` skips them), `grouped()` by card type, `mana_curve()`, delete (only if the deck location has no history). `BOARDS`, `FORMATS`, `COMMANDER_FORMATS`. |
| `deckplan.py` | Phases 4–5, computed never stored: `buy_list()`, `proxy_short()`, `state(counts=False)` (per-slot in_deck / short, excess lines; the deck page passes `counts=True`), `pull_plan()` / `confirm_pulls()` (one deck or all; free copies planned once), `return_plan()` / `confirm_returns()` (default = last pull source, else Unsorted), `mark_built()` (acquire what's short as new copies into the deck location; not pulls), `analysis()` (colours, legality, copy limits, size, commander identity). Confirm functions recompute the plan, so stale forms can't over-move. |
| `importer.py` | Moxfield deck text / collection CSV: parse → `resolve()` (set+cn → name+set → `default_key()` = owner's free copy, else newest released regular paper printing) → import (per-owner sha256 dedupe). Unresolved queue fix/dismiss. `paste_into()` (location) and `paste_into_deck()` (slots) for quick list entry. |
| `cardtypes.py` | Type line → (supertypes, card types, subtypes) over all faces |
| `queries.py` | Read-only queries. Collection / location / history / totals per owner; `shared_stock`; `StockFilter` (location filters, in Python); `CardSearch` + `search()` (phase 3, SQL); whitelisted `order_by()` for `?sort=` |
| `exports.py` | Card lists as text: `text`, `moxfield` (deck exports with board headers the importer reads back), `tcgplayer`, `csv`. Used by the buy list, deck export, proxy list. |
| `backup.py` | `backup()` (SQLite backup API → gzip, prune to N), `list_backups()`, `restore()` (port check, integrity check, saves current DB, migrate + ledger check). Nightly via `deploy/systemd/franticsearch-backup.timer`. |
| `logs.py` | `configure()`: one stderr handler for server + CLI (called by `__main__` and `wsgi`, not `create_app`); no timestamps under journald. Logged: logins (failed = WARNING, with IP), admin user actions, migrations applied, sync start/result/failure, 500 tracebacks |
| `scryfall.py` | Bulk sync: gzipped JSONL from `jsonl_download_uri` (legacy JSON arrays via `ijson`). Upserts, never deletes printings. Stores `face_name`, `set_type`, `digital`, `promo` for default-printing choice. Skips unchanged rows. Failures are logged, recorded in `meta` and raised as `SyncFailed`. |
| `app.py` + `templates/` + `static/style.css` | Flask, POST-redirect-GET, flash messages. `macros.html`: card links with hover image, tiles, sort headers, view switch, stock actions, filters, sharing box, `place` / `loc_link` (location names, "Decks / <name>"). Routes are grouped by area in `_register_routes`; redirects to `next` go through `safe_next()`; background syncs through `_sync_once()` (never raises, logs); deck routes live under `/decks/<id>/…` plus `/pull`, `/buy`, `/decks/add-card` (deck id in the form). |
| `tests/` | pytest. `conftest.py`: `conn` fixture creates admin user `alice` (`ALICE` = 1) and imports `fixtures_default_cards.json` (SOL_C21 / SOL_CMM / DELVER / BOLT). `client` is logged in as alice with CSRF off; use `login_as(client, uid)` for others. `test_users.py` covers isolation; `test_decks.py` (phase 4), `test_shortfalls_backup.py` (phases 5–6), `test_filters.py`, `test_importer.py`, `test_web.py`, `test_ledger.py`, `test_db.py` (migrations), `test_scryfall.py`, `test_logs.py`. |

Keep domain logic framework-free (unit-testable) and the Flask layer thin.

## Rules for new code (security & domain)

- **Auth is one hook** (`_auth` in `app.py`'s `before_request`): login required except `PUBLIC_ENDPOINTS` (login, static, `shared_link`), plus CSRF on every POST.
  - **Every `<form method="post">` needs** `<input type="hidden" name="csrf" value="{{ csrf_token() }}">`.
- **Every route that takes an id must check ownership** → 404, using `_my_location`, `_my_location_arg` (for form fields), `_my_deck`, `_my_import` or `_my_unresolved`. Undo checks `ledger.batch_owners`.
  - Add a case to `test_other_users_ids_are_not_found` for each new id-taking route.
- Domain functions take `owner=` (keyword). Lists, totals, defaults and "free" counts are per user.
- **Shared/read-only views must not leak private data**, e.g. a deck's "where (free)" names the owner's other locations, so it's hidden for others. Anonymous share-link views get no `/card/` links (`card_link(href=None)`).
- Stock key = printing (Scryfall id) + finish + condition (default `NM`) + language (default `en`). Condition/lang are hidden in the UI unless set.
- Ledger reasons: `import`, `acquire` (no source) · `sell` (no destination) · `adjust` (one side) · `move` (non-deck ↔ non-deck) · `pull` (into a deck location) · `return` (out of one).
  - `ledger.move()` / `transfer_many()` infer move/pull/return. Undo writes compensating rows tagged `undo_of`; imports can't be undone.
- Deck locations change only via the deck's pull/return lists and "mark as built" (acquires new copies into the deck: "I own this deck"); manual forms exclude them.
- **Deck locations are hidden plumbing in the UI** (PLAN.md § Decks in the UI): show location names with `m.place(name, type)` / `m.loc_link(id, name, type)` so decks read "Decks / <name>"; lists show one "Decks" row; `/locations/<deck loc>` redirects to the deck page. Queries that feed location names should also return the location type.
- **Deck slots match copies by printing + finish**; condition/language don't matter for decks (slots are stored NM/en).
- Strict ownership: a copy is in exactly one location; no cross-deck swap suggestions.
- Locations with ledger history can't be deleted; once empty they can be **archived**.

## Deployment (live, as of 2026-10-21)

- **Public URL**: `https://theorycorner.com/franticsearch/`, with the app's own login.
  - Users: `hallowdance` (admin, the owner) and `FallenShaman` (their brother). Passwords are not stored in the repo; reset with `user passwd`.
- **Chain**: nginx on `theorycorner.com` (`/etc/nginx/sites-enabled/theorycorner.com`, the main `server` block) → `location /franticsearch/` → `127.0.0.1:16602` → reverse SSH tunnel → this machine's gunicorn on `:6602`.
  - nginx sets `X-Forwarded-Prefix /franticsearch`; `location = /franticsearch/login` is rate-limited by zone `fs_login`, defined in `/etc/nginx/conf.d/franticsearch.conf`.
  - Config backups are in the server user's home: `theorycorner.com.nginx.bak-*`.
- **Port 16602 is opened on all interfaces** (sshd has `GatewayPorts yes`), but **ufw blocks it** from outside (incoming default deny, IPv6 too). Only nginx reaches it. Keep it that way.
- **The server and tunnel are systemd user services** (since 2026-10-19): `franticsearch.service` (`FS_PROXY=1 .venv/bin/gunicorn -c deploy/gunicorn.conf.py franticsearch.wsgi:app` in the repo, since 2026-10-21: 1 worker × 8 threads, access log with response times in the journal) and `franticsearch-tunnel.service` (autossh `-R 127.0.0.1:16602:127.0.0.1:6602 theorycorner.com`). Both `Restart=always` and enabled for `default.target`.
  - Unit files are in `deploy/systemd/`; install with `cp deploy/systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now franticsearch franticsearch-tunnel franticsearch-backup.timer`.
  - **Restart after code changes**: `systemctl --user restart franticsearch`. Logs: `journalctl --user -u franticsearch -f` (app log + gunicorn access log with response times).
  - **After changing `deploy/systemd/franticsearch.service`**: copy it to `~/.config/systemd/user/`, `systemctl --user daemon-reload`, restart.
  - The service runs whatever is checked out in the repo, so the live site serves the current branch.
  - Lingering is off: the services start at login and stop at logout (`loginctl enable-linger` would keep them running without a session). They're down while the laptop sleeps; autossh reconnects after wake.
  - A 502 on the public URL means one of them is down: `systemctl --user status franticsearch franticsearch-tunnel`.
- **Live DB**: `data/franticsearch.db` (gitignored, ~240 MB, nearly all of it the card cache), schema 7. Old manual backup from before migration 005: `data/franticsearch.db.bak-pre-2b-20261018`.
- **Backups**: `franticsearch-backup.timer` runs `python -m franticsearch backup` nightly at 03:30 (catches up after sleep) into `data/backups/` (gitignored), keeping 14 (~57 MB each). The System page lists them. Check with `systemctl --user list-timers franticsearch-backup.timer`. Local only, no off-machine copy. First (manual) backup: 2026-10-19; the first timer run was due the night of 2026-10-20.
  - **To restore**: `systemctl --user stop franticsearch`, then `python -m franticsearch restore data/backups/<file>.db.gz` (saves the current DB as `<db>.pre-restore-<ts>`, migrates, runs the ledger check), then `systemctl --user start franticsearch`.
- **Git**: `origin` is SSH (`git@github.com:HallowDance/FranticSearch.git`); `gh` is logged in as HallowDance.

## Status (2026-10-21)

- **All six phases are shipped**, PRs #1–#26 merged to `main`. Still a prototype:
  - #1 phase 1 + Scryfall JSONL sync fix · #2 phase 2 import · #3 reverse-proxy support
  - #4 UI improvements (quick add, paste, move ticked, undo, nav search, sorting, deck groups, mana symbols, image view)
  - #5 plan · #6 phase 2b users & sharing · #7 location filters · #8 phase 3 card search · #9 docs handoff
  - #10 systemd user services for server + tunnel
  - #11 phase 4 decks (editor, pull/return lists, mark as built, checks; "Decks / <name>" display)
  - #12 phases 5 & 6 (buy list, exports, proxies, backups + restore, ledger views, phone layout)
  - #13 docs · #14 buy list format links · #15 mark as built = add new copies in the deck (import checkbox + deck button) · #16 README refresh
  - #17 docs · #18 code review + expansion guide (`docs/SCALING.md`; hosting model: self-hosted per friend group, never open sign-up)
  - #19 security fixes (open redirect via `next`, import double-submit, secret-key race) · #20 performance (pull plan, lean deck state, migration 006 `movements.owner_id`, sync change guard, `PRAGMA optimize`)
  - #21 admin Users page (generated passwords, disable/enable; migration 007) · #22 deck where-columns only while short, owned-elsewhere fix · #23 deck Status column · #24 share-link hover images · #25 logging · #26 gunicorn for the live instance
- 134 tests pass.
- Verified on real data: Scryfall sync (118,893 printings); Moxfield deck text, both exports (100/100); Moxfield collection CSV (337 rows, 0 unresolved, in "Rares Binder"); phase 4–6 pages via read-only GETs; mark as built (old pull version, then the new acquire version: Millicent 100 short → complete) and restore on copies of the live DB; phone layout via headless Chromium at 390 px.
- **Unverified**: `docker compose up --build` itself (no container runtime on the dev machine).
- **Next**: nothing in progress. Candidates: `docs/SCALING.md` § Prioritised backlog (Stage 1: off-machine backups first, timers, Secure cookie; Stage 3: FOSS packaging, licence) and PLAN.md § What's next (Scryfall query syntax, trades, prices). **CI/CD waits for the first proper release.** Ask the user which.
