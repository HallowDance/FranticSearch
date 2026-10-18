# CLAUDE.md

FranticSearch: self-hosted MTG collection & deck manager where **every physical card has a location and decks are just locations**. Design, decisions and roadmap live in [PLAN.md](PLAN.md); treat it as the source of truth.

## Working agreement

- Stick to PLAN.md. When something isn't covered by the plan, **ask the user** instead of assuming; record answered decisions in PLAN.md.
- Work on a feature branch per phase and open a PR (e.g. `phase-1` → PR #1).
- UI: functional, server-rendered HTML forms, no JS build; deliberately plain 90s/00s FOSS look (grey chrome, blue links, bordered tables). Don't spend long on polish, but keep it usable at phone width (no horizontal page scroll).

## Commands

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,serve]"
pytest                                   # full suite (fast, no network)
python -m franticsearch serve            # dev server http://127.0.0.1:6602 (auto-syncs Scryfall)
python -m franticsearch serve --no-auto-sync
python -m franticsearch sync [--force]   # Scryfall default_cards → printings (~80 MB .jsonl.gz, ~25 s)
python -m franticsearch sync --file x.jsonl.gz   # import a local bulk file (.jsonl/.json, gz or not)
python -m franticsearch check            # stock vs ledger invariant (exit 1 on mismatch)
docker compose up -d --build             # production-style, DB in ./data
```

Env: `FS_DB` (default `data/franticsearch.db`), `FS_AUTO_SYNC` (default `1`), `FS_SECRET_KEY` (optional), `FS_PROXY` (default `0`; `1` = trust a reverse proxy's `X-Forwarded-*` headers incl. `X-Forwarded-Prefix`, for serving under a path like `/franticsearch/`).

## Architecture

| File | Role |
|---|---|
| `franticsearch/migrations/NNN_*.sql` | Schema. Append a new numbered file for every change; never edit applied ones. Version in `PRAGMA user_version`. |
| `franticsearch/db.py` | Connections (WAL, FKs on, autocommit + explicit `transaction()`), migration runner, `meta` key/value helpers |
| `franticsearch/ledger.py` | **The only code that changes `stock`.** acquire / move / adjust / sell write stock + a `movements` row in one transaction; `check()` verifies the invariant |
| `franticsearch/locations.py` | Location CRUD, archive/unarchive |
| `franticsearch/decks.py` | Deck + its deck location, slots, read-only slot view, delete |
| `franticsearch/importer.py` | Moxfield deck text / collection CSV: parse → resolve (set+cn → name+set → default printing) → import; unresolved queue fix/dismiss |
| `franticsearch/scryfall.py` | Bulk sync (gzipped JSONL from `jsonl_download_uri`; legacy JSON arrays via `ijson`; upsert, never deletes printings) |
| `franticsearch/queries.py` | Read-only queries for the UI |
| `franticsearch/app.py` + `templates/` + `static/style.css` | Flask routes (POST-redirect-GET, flash messages); `macros.html` holds shared stock-row actions |
| `tests/` | pytest; `fixtures_default_cards.json` is a hand-written Scryfall-format sample (SOL_C21/SOL_CMM/DELVER/BOLT ids in `conftest.py`) |

Keep domain logic framework-free (unit-testable) and the Flask layer thin.

## Domain rules (already decided)

- Stock key = printing (Scryfall id) + finish + condition (default `NM`) + language (default `en`). Condition/lang are hidden in the UI unless set.
- `db.transaction()` is re-entrant (joins an open transaction), so domain functions compose atomically.
- Ledger reasons: `import`, `acquire` (no source) · `sell` (no destination) · `adjust` (one side) · `move` (non-deck ↔ non-deck) · `pull` (into a deck location) · `return` (out of one). `ledger.move()` infers move/pull/return.
- Deck locations change only via the deck's pull/return lists (Phase 4); manual forms exclude them.
- Strict ownership: a copy is in exactly one location; no cross-deck swap suggestions.
- Locations with ledger history can't be deleted; once empty they can be **archived** (hidden, nothing can be added, restorable).
- LAN-only, no auth for now; keep auth addable in one place later.

## Status

- **Phase 1 (foundation)** is implemented on branch `phase-1`, PR #1.
- **Phase 2 (import)** is on branch `phase-2` (branched from `phase-1`). 49 tests pass. Deck import verified with both Moxfield text exports (plain, and with set codes: 100/100 resolved to the exact printings). The collection CSV parser is built from Moxfield's documented header only: **verify with a real export**.
- Live Scryfall sync verified 2026-10-18 (CLI and gunicorn background auto-sync from an empty DB: status `idle`, 118,893 printings, check OK). Scryfall had dropped `download_uri` (JSON array) for `jsonl_download_uri` (gzipped JSONL); the importer handles both.
- **Still unverified**: `docker compose up --build` itself (no container runtime on the dev machine). The Dockerfile's steps were replayed outside a container (non-editable install, its gunicorn CMD, all pages 200), so what remains is the image build and the `./data` volume.
- Next: Phase 3, card search (see PLAN.md § Phases).
