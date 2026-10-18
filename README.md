# FranticSearch

Self-hosted Magic: The Gathering collection & deck manager where **every physical card has a location** and decks are just locations. See [PLAN.md](PLAN.md) for the design and roadmap.

Status: **Phase 1 (foundation)**: card database, locations, stock + ledger, collection browser, manual add / move / adjust / remove.

## Run with Docker

```sh
docker compose up -d --build     # http://<host>:6602
```

The database lives in `./data/franticsearch.db`. The Scryfall card data (~110k printings) downloads on the first start and is re-checked daily. Watch progress on the **System** page. There is no authentication: keep it on your LAN.

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

Settings (environment): `FS_DB` (database path, default `data/franticsearch.db`), `FS_AUTO_SYNC` (`1`/`0`, default `1`), `FS_SECRET_KEY` (optional; otherwise generated and stored in the DB).

## Tests

```sh
pytest
```

## Layout

| Path | What |
|---|---|
| `franticsearch/migrations/` | numbered SQL migrations (`PRAGMA user_version`) |
| `franticsearch/ledger.py` | the only code that changes stock: acquire / move / adjust / sell + consistency check |
| `franticsearch/locations.py` | location CRUD |
| `franticsearch/scryfall.py` | bulk-data sync (`default_cards`) into `printings` |
| `franticsearch/queries.py` | read-only queries for the UI |
| `franticsearch/app.py`, `templates/`, `static/` | Flask UI, plain HTML forms |

Card data and images © Wizards of the Coast, provided by [Scryfall](https://scryfall.com). FranticSearch is unofficial Fan Content and is not affiliated with Wizards of the Coast.
