# FranticSearch — Plan

A self-hosted MTG collection & deck manager built around one idea: **every physical card has a location, and decks are just locations.** Moving a card into or out of a deck is a recorded, confirmed physical move — no hand-maintained "binders" mirroring decks.

## Decisions

| Topic | Decision |
|---|---|
| Platform | Self-hosted web app, single user, SQLite; usable from desktop and phone browsers |
| Access | **LAN only, no auth** for now. Proper hosting + auth later — keep a single auth hook (SvelteKit `handle`) so it can be added without touching routes |
| Deckbuilding | **In FranticSearch.** Decks are built and edited here; Moxfield deck import is for one-time migration only |
| Copy model | Quantity per **printing key** per location. Key = Scryfall printing + finish + condition + language. Condition/language are optional and default to NM/EN — hidden in the UI unless set |
| Locations | Flat, named, typed: `binder` · `box` · `deck` · `lent` · `other`. Each deck owns exactly one location |
| Deck slots | Always an **exact printing** + finish |
| Contention | **Strict**: a copy is in exactly one place. If the needed copies are all in other decks, the slot is short |
| Deck ↔ collection | **Pull/return workflow**: a deck is a target list; stock only moves when you confirm a physical move |
| Extras in scope | Moxfield/CSV import · proxies & shortfall/buy list |
| Out of scope (for now) | Prices, card scanning, multi-user, auth/public hosting, Moxfield re-sync |

## Data model

| Table | Columns / purpose |
|---|---|
| `printings` | Scryfall bulk cache: `scryfall_id` (PK), `oracle_id`, `name`, `set`, `set_name`, `collector_number`, `released_at`, `finishes`, `lang`, `mana_cost`, `cmc`, `colors`, `color_identity`, `type_line`, `oracle_text`, `legalities`, image URIs (incl. card faces). FTS index on `name`, `type_line`, `oracle_text` |
| `locations` | `id`, `name`, `type`, `notes` |
| `stock` | (`location_id`, `printing_id`, `finish`, `condition`, `lang`) → `qty` (> 0). **Single source of truth for physical whereabouts** |
| `movements` | Append-only ledger: `id`, `ts`, `from_location`, `to_location`, printing key, `qty`, `reason` (`import` · `acquire` · `pull` · `return` · `move` · `adjust` · `sell`), `note`, `batch_id` |
| `decks` | `id`, `name`, `format`, `location_id`, `notes` |
| `deck_slots` | `deck_id`, printing key, `board` (`main` · `side` · `commander` · `maybe`), `qty_wanted`, `qty_proxy` |

Rules:
- `stock` and `movements` are always written in the same transaction; `stock` must equal the ledger's projection (a check command verifies this).
- `acquire`/`import` have a null `from`; `sell`/removal have a null `to`.
- A manual move between two non-deck locations is `move`; any move into a deck location is a `pull`, out of one a `return`.
- Deck locations are only changed through their deck's pull/return lists, never by the manual move forms.
- A location can be deleted only if it never held anything (the ledger refers to it); otherwise empty it and rename it.
- `batch_id` groups one user action (e.g. "confirm pull list") so it can be undone as a unit.

### Derived deck state (computed, never stored)

Per slot:
- `in_deck` = `stock` at the deck's location for the slot's printing key
- `short` = `max(0, qty_wanted − in_deck − qty_proxy)`

Per deck:
- **Excess** = stock at the deck location beyond what slots want (removed cards, lowered counts, changed printings)
- Status: *complete* / *N short* / *N excess*

`maybe` board slots never count toward short or excess.

## Core workflows

### 1. Pull list
For one deck or all decks: for each short slot, find the same printing key in **non-deck** locations and propose moves — "Pull 2× Sol Ring (C21 #263, nonfoil) from Binder A". Tick lines individually or confirm all → `pull` movements in one batch.

Helper (informational, strictness unchanged): if the exact printing isn't free but a *different* printing of the same card is, show it with a one-click "retarget slot to this printing".

### 2. Return list
Excess copies in a deck location become return lines. Default destination = the location the copy was last pulled from (from the ledger), else a configurable default ("Unsorted"). Confirm → `return` movements.

Changing a slot's printing = old printing becomes excess (return) + new printing becomes short (pull).

### 3. Proxies & shortfalls
`qty_proxy` covers part of a shortfall. The **buy list** aggregates remaining `short` across all decks, grouped by printing, exportable as plain text / Moxfield / TCGplayer mass-entry / CSV.

### 4. Collection browsing
Card → printings → per-location quantities. Filters: location, location type, "not in any deck", set, name search. Manual actions: add (acquire), move, adjust, remove (sell/trade).

### 5. Deck editor & card search
Decks are built here, so the editor is a first-class screen:
- **Card search** over the local `printings` cache: name (FTS, fuzzy), type, oracle text, colors / color identity, mana value, set, format legality. Results grouped by card, expandable to printings.
- **Ownership-aware**: each result shows owned qty and *free* qty (outside decks) per printing. Adding a card defaults to a free owned printing, else the newest printing; printing is always switchable.
- Slot editing: qty, board, finish, proxy qty; change printing (triggers return + pull).
- Live deck panel: per-slot status (in deck / short / proxy), counts per board, mana curve, color breakdown, format legality and commander color-identity warnings.
- Paste a decklist (Moxfield/MTGA text) into an existing deck as a bulk add.
- Later: Scryfall query syntax subset (`t:`, `o:`, `c<=`, `mv=` …) on top of the structured filters.

### 6. History & undo
Ledger view per card, per location, per deck. Undo the last batch by writing compensating movements (never deleting rows).

## Import / migration from Moxfield

1. **Collection CSV** (Moxfield export: Count, Name, Edition, Collector Number, Foil, Condition, Language…) → into a chosen location, default "Unsorted". Resolution by (set, collector number) → fallback (name, set) → unresolved queue for manual fix.
2. **Deck text export** (`1 Sol Ring (C21) 263 *F*`, sections for commander/sideboard/maybeboard) → creates deck + slots.
3. **"Mark as built"** per imported deck: confirms the cards physically live in that deck → moves matching stock from non-deck locations into the deck location; leftovers show as short (proxy or buy). Surfaces cases where Moxfield double-counted a card across decks.
4. Importing is idempotent per file (hash recorded) to prevent accidental double imports.

## Stack

- **Python 3.11+ / Flask + Jinja**: server-rendered HTML forms, no JavaScript build step; deliberately plain 90s/00s FOSS look
- **SQLite** via the standard library `sqlite3` (WAL mode); numbered plain-SQL migrations tracked in `PRAGMA user_version`
- **Scryfall bulk data** (`default_cards`) streamed with `ijson` into `printings` on first run and re-checked daily (background thread); images hotlinked from Scryfall CDN. Follows Scryfall's API guidelines (bulk file, User-Agent + Accept headers)
- **pytest** for domain logic (ledger invariants, pull/return planning, import parsing) and Flask test-client page tests
- **Docker** single container running gunicorn (1 worker, threads), DB in a mounted volume; backup = copy the DB file

Domain logic (`ledger.py`, later pull/return planning and import parsing) is framework-free and unit-tested; the Flask layer only parses forms and renders.

## Phases

| # | Phase | Deliverables |
|---|---|---|
| 1 | Foundation | Repo scaffold, Docker, schema + migrations, Scryfall sync, locations CRUD, stock + ledger with invariant check, collection browser, manual add/move/adjust |
| 2 | Import | Moxfield collection CSV, deck text import, unresolved queue |
| 3 | Card search | Local search over `printings` (name/type/text/colors/MV/set/legality), ownership-aware results |
| 4 | Decks | Deck editor (slots, boards, proxies, printing switch), live stats + legality, derived deck state, pull list, return list, "mark as built" |
| 5 | Shortfalls | Proxies, cross-deck buy list, exports |
| 6 | Polish | Batch undo, ledger views, mobile layout pass, backup/restore |
| later | — | Scryfall query syntax, public hosting + auth, prices, scanning |

## Open questions

None blocking. Revisit at hosting time: auth method (single password vs. external SSO/VPN) and deployment target.
