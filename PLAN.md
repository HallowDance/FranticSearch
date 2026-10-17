# FranticSearch — Plan

A self-hosted MTG collection & deck manager built around one idea: **every physical card has a location, and decks are just locations.** Moving a card into or out of a deck is a recorded, confirmed physical move — no hand-maintained "binders" mirroring decks.

## Decisions

| Topic | Decision |
|---|---|
| Platform | Self-hosted web app, single user, SQLite; usable from desktop and phone browsers |
| Copy model | Quantity per **printing key** per location. Key = Scryfall printing + finish (+ optional condition/lang, default NM/EN) |
| Locations | Flat, named, typed: `binder` · `box` · `deck` · `lent` · `other`. Each deck owns exactly one location |
| Deck slots | Always an **exact printing** + finish |
| Contention | **Strict**: a copy is in exactly one place. If the needed copies are all in other decks, the slot is short |
| Deck ↔ collection | **Pull/return workflow**: a deck is a target list; stock only moves when you confirm a physical move |
| Extras in scope | Moxfield/CSV import · proxies & shortfall/buy list |
| Out of scope (for now) | Prices, card scanning, multi-user |

## Data model

| Table | Columns / purpose |
|---|---|
| `printings` | Scryfall bulk cache: `scryfall_id` (PK), `oracle_id`, `name`, `set`, `collector_number`, `finishes`, `lang`, `type_line`, image URIs. FTS index on `name` |
| `locations` | `id`, `name`, `type`, `notes` |
| `stock` | (`location_id`, `printing_id`, `finish`, `condition`, `lang`) → `qty` (> 0). **Single source of truth for physical whereabouts** |
| `movements` | Append-only ledger: `id`, `ts`, `from_location`, `to_location`, printing key, `qty`, `reason` (`import` · `acquire` · `pull` · `return` · `adjust` · `sell`), `note`, `batch_id` |
| `decks` | `id`, `name`, `format`, `location_id`, `notes` |
| `deck_slots` | `deck_id`, printing key, `board` (`main` · `side` · `commander` · `maybe`), `qty_wanted`, `qty_proxy` |

Rules:
- `stock` and `movements` are always written in the same transaction; `stock` must equal the ledger's projection (a check command verifies this).
- `acquire`/`import` have a null `from`; `sell`/removal have a null `to`.
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

### 5. History & undo
Ledger view per card, per location, per deck. Undo the last batch by writing compensating movements (never deleting rows).

## Import / migration from Moxfield

1. **Collection CSV** (Moxfield export: Count, Name, Edition, Collector Number, Foil, Condition, Language…) → into a chosen location, default "Unsorted". Resolution by (set, collector number) → fallback (name, set) → unresolved queue for manual fix.
2. **Deck text export** (`1 Sol Ring (C21) 263 *F*`, sections for commander/sideboard/maybeboard) → creates deck + slots.
3. **"Mark as built"** per imported deck: confirms the cards physically live in that deck → moves matching stock from non-deck locations into the deck location; leftovers show as short (proxy or buy). Surfaces cases where Moxfield double-counted a card across decks.
4. Importing is idempotent per file (hash recorded) to prevent accidental double imports.

## Stack

- **SvelteKit** (TypeScript) — UI + server routes in one app
- **SQLite** via `better-sqlite3` + **Drizzle** ORM/migrations
- **Scryfall bulk data** (`default_cards`) streamed into `printings` on first run and nightly; images hotlinked from Scryfall CDN. Respect Scryfall's API guidelines (bulk files, not per-card requests).
- **Vitest** for domain logic (stock/ledger invariants, pull/return planning, import parsing); Playwright for a few end-to-end flows
- **Docker** single container, DB in a mounted volume; backup = copy the DB file

Domain logic (pull/return planning, derived deck state, import parsing) lives in a framework-free `src/lib/domain/` module so it is unit-testable.

## Phases

| # | Phase | Deliverables |
|---|---|---|
| 1 | Foundation | Repo scaffold, Docker, schema + migrations, Scryfall sync, locations CRUD, stock + ledger with invariant check, collection browser, manual add/move/adjust |
| 2 | Import | Moxfield collection CSV, deck text import, unresolved queue |
| 3 | Decks | Slot editor (exact-printing search), derived deck state, pull list, return list, "mark as built" |
| 4 | Shortfalls | Proxies, cross-deck buy list, exports |
| 5 | Polish | Batch undo, ledger views, mobile layout pass, backup/restore |
| later | — | Prices, scanning, Moxfield re-sync diff |

## Open questions

1. **Deckbuilding home** — keep building in Moxfield and re-import (needs a re-import diff that turns changes into pulls/returns), or build decks here (needs good card search; possibly Scryfall query syntax)?
2. **Condition / language** — keep as optional defaulted fields in the printing key, or drop entirely?
3. **Access** — LAN-only with no auth, or internet-exposed behind a single password?
