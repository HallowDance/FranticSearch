# FranticSearch

Self-hosted Magic: The Gathering collection & deck manager where **every physical card has a location** and decks are just locations. See [PLAN.md](PLAN.md) for the design and roadmap.

Status: phases 1–6 shipped. Features:
- **Collection:** card database, locations with filters, stock + ledger with batch undo, collection browser, manual add / move / adjust / remove, quick add, paste a list.
- **Decks:** built here (search or paste to add, per-slot qty / board / proxies / printing). Status per deck (short / excess), pull and return lists that move the physical cards, "mark as built" (I own this deck: add its cards to the collection, in the deck), mana curve and format checks (legality, copies, size, commander colour identity).
- **Import:** Moxfield (deck lists → decks; collection CSV → a location), with a queue for lines that need a decision.
- **Search:** card search with your owned / free counts.
- **Users:** a few users with separate collections; locations and decks can be shared.
- **Shortfalls:** a buy list across all decks, proxies ("proxy all short", proxy print list), exports as plain text / Moxfield / TCGplayer / CSV, deck export.
- **Backups:** `python -m franticsearch backup` (nightly via a systemd timer) and `restore`.

## Run with Docker

```sh
docker compose up -d --build     # http://<host>:6602
```

The database lives in `./data/franticsearch.db`. The Scryfall card data (~119k printings) downloads on the first start and is re-checked daily. Create the first account with `docker compose exec franticsearch python -m franticsearch user add NAME --admin`, then log in. Each user has a separate collection and can share locations or decks (with other users, or via a read-only link).

## Run locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m franticsearch sync          # first-time Scryfall download (or let `serve` do it)
python -m franticsearch serve         # http://127.0.0.1:6602
```

Other commands:

| Command | Does |
|---|---|
| `python -m franticsearch migrate` | create/upgrade the schema |
| `python -m franticsearch sync [--force]` | update the Scryfall cache |
| `python -m franticsearch sync --file default-cards.jsonl.gz` | import a manually downloaded Scryfall bulk file (`.jsonl`/`.json`, gzipped or not) |
| `python -m franticsearch check` | verify stock equals the ledger (exit 1 on mismatch) |
| `python -m franticsearch backup [--dir DIR] [--keep 14]` | gzipped online backup (safe while the server runs), default `data/backups/` |
| `python -m franticsearch restore FILE` | replace the database with a backup (stop the server first; the current DB is kept as `*.pre-restore-*`) |
| `python -m franticsearch user add NAME --admin` | create an account (the first admin adopts any existing data); `user passwd NAME`, `user list` |

Settings (environment): `FS_DB` (database path, default `data/franticsearch.db`), `FS_AUTO_SYNC` (`1`/`0`, default `1`), `FS_SECRET_KEY` (optional; otherwise generated and stored in the DB), `FS_PROXY` (`1` when behind a reverse proxy: trusts its `X-Forwarded-For/-Proto/-Host/-Prefix` headers, so the app can live under a path such as `/franticsearch/`; default `0`), `FS_SECURE_COOKIE` (`1` = send the session cookie over HTTPS only).

After the first start, create an account: `python -m franticsearch user add NAME --admin`.

## Run as a service (systemd user units)

`deploy/systemd/` has user units for the app (`franticsearch.service`, run from the repo's `.venv` with `FS_PROXY=1`), a reverse SSH tunnel to a reverse proxy (`franticsearch-tunnel.service`, autossh; adjust the host) and nightly backups (`franticsearch-backup.timer`). The unit paths assume the repo lives at `~/hobbies/dev/python/FranticSearch`.

```sh
cp deploy/systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now franticsearch franticsearch-tunnel franticsearch-backup.timer
systemctl --user restart franticsearch        # after updating the code
```

With Docker, run backups with `docker compose exec franticsearch python -m franticsearch backup` (e.g. from cron).

## Tests

```sh
pytest
```

## Layout

| Path | What |
|---|---|
| `franticsearch/migrations/` | numbered SQL migrations (`PRAGMA user_version`) |
| `franticsearch/ledger.py` | the only code that changes stock: acquire / move / adjust / sell / undo + consistency check |
| `franticsearch/users.py` | accounts |
| `franticsearch/locations.py` | location CRUD, archiving, sharing |
| `franticsearch/cardtypes.py` | type line → supertypes / types / subtypes |
| `franticsearch/decks.py` | decks, their deck locations and slots (editing, printing switch) |
| `franticsearch/deckplan.py` | derived deck state, pull / return lists, mark as built, deck checks |
| `franticsearch/importer.py` | Moxfield deck text / collection CSV import and the unresolved queue |
| `franticsearch/exports.py` | card lists as text / Moxfield / TCGplayer / CSV |
| `franticsearch/backup.py` | backups and restore |
| `franticsearch/scryfall.py` | bulk-data sync (`default_cards`) into `printings` |
| `franticsearch/queries.py` | read-only queries for the UI, location filters, card search |
| `franticsearch/app.py`, `templates/`, `static/` | Flask UI, plain HTML forms |

Card data and images © Wizards of the Coast, provided by [Scryfall](https://scryfall.com). FranticSearch is unofficial Fan Content and is not affiliated with Wizards of the Coast.
