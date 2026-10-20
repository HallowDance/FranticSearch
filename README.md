# FranticSearch

Self-hosted Magic: The Gathering collection & deck manager where **every physical card has a location** and decks are just locations. See [PLAN.md](PLAN.md) for the design, decisions and roadmap.

Status: phases 1–6 of the plan are shipped; the app runs for a few users. Plain server-rendered HTML (no JavaScript), usable on a phone.

## How it works

- **Locations** are binders, boxes, "lent to a friend" and so on. Every copy you own is in exactly one of them, and every change is a recorded movement in a ledger: add, move, recount, remove. Actions can be undone (except imports).
- **Decks** are target lists: which printing, how many, which board. Each deck also has its own physical spot (shown as *Decks / Deck name*). Cards get there only by confirmed moves:
  - the **pull list** says which free copies to take from which location;
  - the **return list** says where to put back cards the deck no longer wants;
  - **mark as built** ("I own this deck") adds whatever the deck is still short to your collection as new copies, right in the deck.
- What no free copy can cover is **short**: proxy it, or put it on the **buy list**.

## Features

- **Collection:** Scryfall card database (~119k printings, synced daily), locations with filters, collection browser, add / move / recount / remove, quick add, paste a card list, move ticked lines, text or image view, history with undo per card, location and deck.
- **Search:** name, type, rules text, colours / colour identity, mana value, set, format legality; shows how many copies you own and how many are free (not in a deck); quick add to a location or a deck.
- **Decks:** built here (add from search, a card page or a pasted list; per-card quantity, board, proxies, printing and finish). Status per deck (complete / short / excess), pull and return lists for one deck or all decks, mark as built, mana curve, colours, format checks (legality, copy limits, deck size, commander colour identity).
- **Shortfalls:** a buy list across all decks (noting when you own another printing), "proxy all short", a proxy list, exports as plain text, Moxfield, TCGplayer mass entry or CSV, and deck export.
- **Import:** Moxfield deck lists (both text exports, optionally marked as built) and collection CSVs, with a queue for lines that need a decision.
- **Users:** a few accounts, created by the admin (Users page or CLI; leave the password blank for a generated one), each with a separate collection. Accounts can be disabled and enabled again. Locations and decks can be shared read-only with other users or by link.
- **Backups:** gzipped online backups (nightly with the systemd timer) and a checked restore.

## Run locally

Needs Python 3.11+.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m franticsearch user add NAME --admin   # first account (asks for a password)
python -m franticsearch serve                   # http://127.0.0.1:6602
```

On first start, `serve` downloads the Scryfall card data in the background (~80 MB, about half a minute) and re-checks it daily. Use `--no-auto-sync` or `FS_AUTO_SYNC=0` to skip that.

### Commands

| Command | Does |
|---|---|
| `python -m franticsearch serve [--host H] [--port 6602] [--no-auto-sync]` | development web server |
| `python -m franticsearch migrate` | create/upgrade the schema (`serve` does this too) |
| `python -m franticsearch sync [--force]` | update the Scryfall card cache |
| `python -m franticsearch sync --file default-cards.jsonl.gz` | import a downloaded Scryfall bulk file (`.jsonl`/`.json`, gzipped or not) |
| `python -m franticsearch check` | verify stock equals the ledger (exit 1 on mismatch) |
| `python -m franticsearch backup [--dir DIR] [--keep 14]` | gzipped online backup, safe while the server runs; default `<db dir>/backups/` |
| `python -m franticsearch restore FILE [--force] [--port 6602]` | replace the database with a backup. Refuses while the server port is in use; keeps the current DB as `*.pre-restore-*`, migrates and checks the ledger |
| `python -m franticsearch user add NAME [--admin] [--password-stdin]` | create an account (the first admin adopts any data from before accounts existed) |
| `python -m franticsearch user passwd NAME` · `user list` | set a password, list accounts |
| `python -m franticsearch user disable NAME` · `user enable NAME` | block an account's login and hide what it shared, or undo that (the last admin can't be disabled) |

All commands take `--db PATH`.

### Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `FS_DB` | `data/franticsearch.db` | database path |
| `FS_AUTO_SYNC` | `1` | Scryfall sync on start and daily |
| `FS_SECRET_KEY` | generated, stored in the DB | session signing key |
| `FS_PROXY` | `0` | `1` behind a reverse proxy: trust its `X-Forwarded-For/-Proto/-Host/-Prefix` headers, so the app can live under a path such as `/franticsearch/` |
| `FS_SECURE_COOKIE` | `0` | `1` = send the session cookie over HTTPS only |

## Run as a service (systemd user units)

`deploy/systemd/` has user units:
- `franticsearch.service` runs the app from the repo's `.venv` with `FS_PROXY=1`.
- `franticsearch-tunnel.service` is an autossh reverse tunnel that exposes the app to a reverse proxy on another host. Edit the host and ports.
- `franticsearch-backup.timer` runs a backup nightly at 03:30, or at the next wake if the machine was asleep. It keeps 14.

The unit paths assume the repo is at `~/hobbies/dev/python/FranticSearch`; adjust `WorkingDirectory`/`ExecStart` otherwise.

```sh
cp deploy/systemd/* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now franticsearch franticsearch-tunnel franticsearch-backup.timer
systemctl --user restart franticsearch        # after updating the code
journalctl --user -u franticsearch -f         # logs
```

User services stop when you log out unless lingering is on (`loginctl enable-linger`).

To restore a backup: `systemctl --user stop franticsearch`, then `python -m franticsearch restore data/backups/<file>.db.gz`, then `systemctl --user start franticsearch`.

## Run with Docker

> Not yet tried in a real container: the Dockerfile's steps have only been replayed outside one.

```sh
docker compose up -d --build     # http://<host>:6602 (gunicorn, 1 worker, 8 threads)
docker compose exec franticsearch python -m franticsearch user add NAME --admin
```

The database lives in `./data/franticsearch.db` and backups go to `./data/backups/`. Run backups with `docker compose exec franticsearch python -m franticsearch backup`, e.g. from cron. Behind a reverse proxy, set `FS_PROXY=1` (and `FS_SECURE_COOKIE=1` for HTTPS) in `compose.yaml`.

## Tests

```sh
pytest        # ~12 s, no network
```

## Layout

| Path | What |
|---|---|
| `franticsearch/migrations/` | numbered SQL migrations (`PRAGMA user_version`) |
| `franticsearch/db.py` | connections, nested transactions, migration runner |
| `franticsearch/ledger.py` | the only code that changes stock: acquire / move / adjust / sell / undo, plus the consistency check |
| `franticsearch/users.py` | accounts |
| `franticsearch/locations.py` | locations: create, edit, archive, sharing |
| `franticsearch/decks.py` | decks, their deck locations and slots (editing, printing switch) |
| `franticsearch/deckplan.py` | derived deck state, pull / return lists, mark as built, buy list, deck checks |
| `franticsearch/importer.py` | Moxfield deck text / collection CSV import, paste a list, the unresolved queue |
| `franticsearch/exports.py` | card lists as text / Moxfield / TCGplayer / CSV |
| `franticsearch/backup.py` | backups and restore |
| `franticsearch/scryfall.py` | bulk-data sync (`default_cards`) into `printings` |
| `franticsearch/queries.py` | read-only queries for the UI, location filters, card search |
| `franticsearch/cardtypes.py` | type line → supertypes / types / subtypes |
| `franticsearch/app.py`, `templates/`, `static/` | Flask UI, plain HTML forms |
| `franticsearch/__main__.py`, `wsgi.py` | command line; WSGI entry point (`franticsearch.wsgi:app`) |
| `deploy/systemd/` | user units: app, tunnel, nightly backup |
| `Dockerfile`, `compose.yaml` | container build (gunicorn) |
| `tests/` | pytest suite with a small Scryfall fixture |
| `PLAN.md`, `CLAUDE.md` | design and roadmap; notes for AI-assisted development |

Card data and images © Wizards of the Coast, provided by [Scryfall](https://scryfall.com). FranticSearch is unofficial Fan Content and is not affiliated with Wizards of the Coast.
