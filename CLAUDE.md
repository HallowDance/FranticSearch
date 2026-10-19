# CLAUDE.md

FranticSearch: self-hosted MTG collection & deck manager where **every physical card has a location and decks are just locations**. A few users, each with a separate collection, can share locations or decks. Design, decisions and roadmap live in [PLAN.md](PLAN.md); treat it as the source of truth (§ Phases has per-phase status, the numbered workflow sections record decisions).

## Working agreement

- Stick to PLAN.md. When something isn't covered by the plan, **ask the user** instead of assuming; record answered decisions in PLAN.md.
- One feature branch per phase or feature, then a PR. The user asks for "open and merge" explicitly; don't merge on your own.
  - Use **merge commits** (`gh pr merge --merge`), never squash: branches are sometimes stacked.
  - For a stacked branch, merge the lower PR first, then open the next against `main`.
- After code changes, run `pytest` and check the change against the real DB too, e.g. read-only GETs via `app.test_client()` with `session["uid"] = 1`. Then restart the dev server: it caches templates.
- UI: functional, server-rendered HTML forms, no JS (not even inline); deliberately plain 90s/00s FOSS look (grey chrome, blue links, bordered tables). Keep it usable at phone width (no horizontal page scroll). Don't spend long on polish.

## Commands

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,serve]"
pytest                                   # full suite, ~10 s, no network (91 tests)
python -m franticsearch serve            # dev server http://127.0.0.1:6602 (auto-syncs Scryfall)
python -m franticsearch serve --no-auto-sync
python -m franticsearch migrate          # apply pending migrations (serve also does this)
python -m franticsearch sync [--force]   # Scryfall default_cards → printings (~80 MB .jsonl.gz, ~25 s)
python -m franticsearch sync --file x.jsonl.gz   # import a local bulk file (.jsonl/.json, gz or not)
python -m franticsearch check            # stock vs ledger invariant (exit 1 on mismatch)
python -m franticsearch user add NAME [--admin] [--password-stdin]   # first admin adopts ownerless data
python -m franticsearch user passwd NAME [--password-stdin] | user list
docker compose up -d --build             # production-style, DB in ./data (never actually run yet)
```

Env: `FS_DB` (default `data/franticsearch.db`), `FS_AUTO_SYNC` (default `1`), `FS_SECRET_KEY` (optional; else generated and stored in `meta`), `FS_PROXY` (`1` = trust a reverse proxy's `X-Forwarded-For/-Proto/-Host/-Prefix`, needed under `/franticsearch/`), `FS_SECURE_COOKIE` (`1` = session cookie only over HTTPS; off because the dev server is also used over plain http).

## Architecture

| File | Role |
|---|---|
| `franticsearch/migrations/NNN_*.sql` | Schema; latest is `005_users.sql`. Append a new numbered file for every change; never edit applied ones. The runner disables FKs per migration (so tables can be rebuilt) and runs `foreign_key_check` before COMMIT. Version in `PRAGMA user_version`. |
| `db.py` | Connections (WAL, FKs on, autocommit), **re-entrant** `transaction()` (joins an open one, so domain functions compose atomically), migration runner, `meta` helpers |
| `ledger.py` | **The only code that changes `stock`.** acquire(_many) / move(_many) / transfer_many (several from→to pairs, one batch) / adjust / sell / undo write stock + `movements` in one transaction. Refuses moves between two users' locations. `check()` verifies the invariant. Also `LANGUAGES`, `CONDITIONS`, `StockKey`, `validate_key`. |
| `users.py` | Accounts: create (first admin adopts ownerless locations/imports), authenticate (constant-ish time), set password |
| `locations.py` | Location CRUD (owner, names unique per owner), archive, visibility (`private` < `users` < `link`) + share tokens, `can_view`, `shared()` for the People page |
| `decks.py` | Deck + its same-named deck location (`update` renames both), `add_slot`, `edit_slot` (qty / proxy / board / printing / finish; merges, qty 0 removes), `retarget`, slot ids `printing\|finish\|cond\|lang\|board`, `slots()` (in-deck / free / where / any-printing, scoped to the deck owner), `grouped()` by card type, `mana_curve()`, delete (only if the deck location has no history). `BOARDS`, `FORMATS`, `COMMANDER_FORMATS`. |
| `deckplan.py` | Phase 4, computed never stored: `state()` (per-slot in_deck / short, excess lines), `pull_plan()` / `confirm_pulls()` (one deck or all; free copies planned once), `return_plan()` / `confirm_returns()` (default = last pull source, else Unsorted), `mark_built()` (retarget to an owned free printing, then pull), `analysis()` (colours, legality, copy limits, size, commander identity). Confirm functions recompute the plan, so stale forms can't over-move. |
| `importer.py` | Moxfield deck text / collection CSV: parse → `resolve()` (set+cn → name+set → `default_key()` = owner's free copy, else newest released regular paper printing) → import (per-owner sha256 dedupe). Unresolved queue fix/dismiss. `paste_into()` for quick list entry. |
| `cardtypes.py` | Type line → (supertypes, card types, subtypes) over all faces |
| `queries.py` | Read-only queries. Collection / location / history / totals per owner; `shared_stock`; `StockFilter` (location filters, in Python); `CardSearch` + `search()` (phase 3, SQL); whitelisted `order_by()` for `?sort=` |
| `scryfall.py` | Bulk sync: gzipped JSONL from `jsonl_download_uri` (legacy JSON arrays via `ijson`). Upserts, never deletes printings. Stores `face_name`, `set_type`, `digital`, `promo` for default-printing choice. |
| `app.py` + `templates/` + `static/style.css` | Flask, POST-redirect-GET, flash messages. `macros.html`: card links with hover image, tiles, sort headers, view switch, stock actions, filters, sharing box. |
| `tests/` | pytest. `conftest.py`: `conn` fixture creates admin user `alice` (`ALICE` = 1) and imports `fixtures_default_cards.json` (SOL_C21 / SOL_CMM / DELVER / BOLT). `client` is logged in as alice with CSRF off; use `login_as(client, uid)` for others. `test_users.py` covers isolation; `test_filters.py`, `test_importer.py`, `test_web.py`. |

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
- Deck locations change only via the deck's pull/return lists and "mark as built"; manual forms exclude them.
- **Deck locations are hidden plumbing in the UI** (PLAN.md § Decks in the UI): show location names with `m.place(name, type)` / `m.loc_link(id, name, type)` so decks read "Decks / <name>"; lists show one "Decks" row; `/locations/<deck loc>` redirects to the deck page. Queries that feed location names should also return the location type.
- **Deck slots match copies by printing + finish**; condition/language don't matter for decks (slots are stored NM/en).
- Strict ownership: a copy is in exactly one location; no cross-deck swap suggestions.
- Locations with ledger history can't be deleted; once empty they can be **archived**.

## Deployment (live, as of 2026-10-19)

- **Public URL**: `https://theorycorner.com/franticsearch/`, with the app's own login.
  - Users: `hallowdance` (admin, the owner) and `FallenShaman` (their brother). Passwords are not stored in the repo; reset with `user passwd`.
- **Chain**: nginx on `theorycorner.com` (`/etc/nginx/sites-enabled/theorycorner.com`, the main `server` block) → `location /franticsearch/` → `127.0.0.1:16602` → reverse SSH tunnel → this machine's dev server on `:6602`.
  - nginx sets `X-Forwarded-Prefix /franticsearch`; `location = /franticsearch/login` is rate-limited by zone `fs_login`, defined in `/etc/nginx/conf.d/franticsearch.conf`.
  - Config backups are in the server user's home: `theorycorner.com.nginx.bak-*`.
- **Port 16602 is opened on all interfaces** (sshd has `GatewayPorts yes`), but **ufw blocks it** from outside (incoming default deny, IPv6 too). Only nginx reaches it. Keep it that way.
- **The tunnel and dev server are not services.** They run as background jobs of a Claude Code session and die with it or when the laptop sleeps. To bring them up:

  ```sh
  FS_PROXY=1 python -m franticsearch serve      # from the repo, venv active
  while true; do ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 -R 127.0.0.1:16602:127.0.0.1:6602 theorycorner.com; sleep 10; done
  ```

  A 502 on the public URL means the tunnel or dev server is down. Making them permanent services (systemd --user / autossh, or gunicorn on the server) is an open to-do.
- **Live DB**: `data/franticsearch.db` (gitignored, ~220 MB with the card cache). Backup before migration 005: `data/franticsearch.db.bak-pre-2b-20261018`.
- **Git**: `origin` is SSH (`git@github.com:HallowDance/FranticSearch.git`); `gh` is logged in as HallowDance.

## Status (2026-10-19)

- Merged to `main`:
  - #1 phase 1 + Scryfall JSONL sync fix
  - #2 phase 2 import
  - #3 reverse-proxy support
  - #4 UI improvements (quick add, paste, move ticked, undo, nav search, sorting, deck groups, mana symbols, image view)
  - #5 plan
  - #6 phase 2b users & sharing
  - #7 location filters
  - #8 phase 3 card search
- Open: #10 systemd services for server + tunnel; phase 4 decks (branch `phase-4`).
- 91 tests pass.
- Verified on real data: Scryfall sync (118,893 printings); Moxfield deck text, both exports (100/100); Moxfield collection CSV (337 rows, 0 unresolved, in "Rares Binder").
- **Unverified**: `docker compose up --build` itself. There's no container runtime on the dev machine; the Dockerfile's steps were replayed outside a container.
- **Phase 4, decks** (branch `phase-4`): editor (new deck, settings, slot page with printing switch, add from search / card page / pasted list), derived status, pull list (per deck and all decks, with "use this printing"), return list, mark as built, stats + format checks. Verified on a copy of the live DB (mark as built: 4 retargeted, 4 pulled, check OK).
- **Next: Phase 5, shortfalls** (PLAN.md § Phases): cross-deck buy list and exports.
- Other open to-dos:
  - Make the tunnel/server permanent.
  - Backups (the app is internet-facing now).
  - `FS_SECURE_COOKIE=1` once only HTTPS is used.
  - Trades between users (PLAN.md § Users & sharing, "later").
