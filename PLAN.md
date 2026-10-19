# FranticSearch — Plan

A self-hosted MTG collection & deck manager built around one idea: **every physical card has a location, and decks are just locations.** Moving a card into or out of a deck is a recorded, confirmed physical move — no hand-maintained "binders" mirroring decks.

## Decisions

| Topic | Decision |
|---|---|
| Platform | Self-hosted web app for **a few users with separate collections** (from phase 2b; single user before), SQLite; usable from desktop and phone browsers |
| Access | Phases 1–2: no auth (LAN, then nginx basic auth). From phase 2b: **in-app login**, enforced by a single auth hook (`before_request`) so routes don't each handle it. See § Users & sharing |
| Deckbuilding | **In FranticSearch.** Decks are built and edited here; Moxfield deck import is for one-time migration only |
| Copy model | Quantity per **printing key** per location. Key = Scryfall printing + finish + condition + language. Condition/language are optional and default to NM/EN — hidden in the UI unless set |
| Locations | Flat, named, typed: `binder` · `box` · `deck` · `lent` · `other`. Each deck owns exactly one location |
| Deck slots | Always an **exact printing** + finish |
| Contention | **Strict**: a copy is in exactly one place. If the needed copies are all in other decks, the slot is short |
| Deck ↔ collection | **Pull/return workflow**: a deck is a target list; stock only moves when you confirm a physical move |
| Extras in scope | Moxfield/CSV import · proxies & shortfall/buy list |
| Out of scope (for now) | Prices, card scanning, open sign-up / large-scale hosting, trades between users (later, see § Users & sharing), Moxfield re-sync |

## Data model

| Table | Columns / purpose |
|---|---|
| `printings` | Scryfall bulk cache: `scryfall_id` (PK), `oracle_id`, `name`, `set`, `set_name`, `collector_number`, `released_at`, `finishes`, `lang`, `mana_cost`, `cmc`, `colors`, `color_identity`, `type_line`, `oracle_text`, `legalities`, image URIs (incl. card faces). FTS index on `name`, `type_line`, `oracle_text` |
| `locations` | `id`, `name`, `type`, `notes`, `archived` |
| `stock` | (`location_id`, `printing_id`, `finish`, `condition`, `lang`) → `qty` (> 0). **Single source of truth for physical whereabouts** |
| `movements` | Append-only ledger: `id`, `ts`, `from_location`, `to_location`, printing key, `qty`, `reason` (`import` · `acquire` · `pull` · `return` · `move` · `adjust` · `sell`), `note`, `batch_id` |
| `decks` | `id`, `name`, `format`, `location_id`, `notes` |
| `deck_slots` | `deck_id`, printing key, `board` (`main` · `side` · `commander` · `maybe`), `qty_wanted`, `qty_proxy` |

Rules:
- `stock` and `movements` are always written in the same transaction; `stock` must equal the ledger's projection (a check command verifies this).
- `acquire`/`import` have a null `from`; `sell`/removal have a null `to`.
- A manual move between two non-deck locations is `move`; any move into a deck location is a `pull`, out of one a `return`.
- Deck locations are only changed through their deck's pull/return lists, never by the manual move forms.
- A location can be deleted only if it never held anything (the ledger refers to it). Otherwise, once empty, it can be **archived**: hidden from lists and forms, kept in history, restorable. Nothing can be added to an archived location.
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

Card search as built (phase 3, 2026-10-18):
- The Add-cards page became **Search** (`/add`; the nav box's `?go=1` still jumps to an exact name match).
- Fields:
  - name: FTS word prefixes; if nothing matches, a substring match as the "fuzzy" fallback (no typo tolerance without an SQLite extension)
  - type-line words, all required
  - rules-text words (FTS)
  - colours or colour identity, *including / exactly / at most*, plus colourless; *at most* includes colourless, like Scryfall's `c<=`
  - mana value `= ≤ ≥`
  - set code
  - legal (or restricted) in a format
  - "cards I own" / "own, not in a deck"
- Results are grouped by card, 50 per page, sortable by name / cost / owned.
  - With a name, the shortest matching names come first.
  - Each result shows owned and free counts, an expandable list of its matching printings (owned first, newest first, capped at 25 with a link to the card page), and quick add.
- Left out by default: digital-only printings, art-series cards, and tokens/emblems unless the type includes "token".
- `queries.CardSearch` / `queries.search` build one SQL query. Owned/free counts come from a per-user join on `stock`. Real data: 5–410 ms per page.

### 6. History & undo
Ledger view per card, per location, per deck. Undo the last batch by writing compensating movements (never deleting rows).

Decided (2026-10-18, UI improvements, pulled forward from phase 6):
- Any batch can be undone, not only the last: from the "Undo that" button after an action, or per batch on the History page. Compensating rows carry `movements.undo_of`; a batch can be undone once, an undo can't itself be undone, and it fails (changing nothing) if the copies have moved since. One-sided rows reverse as `adjust`; moves reverse as move/pull/return by location type.
- **Imports can't be undone** (yet): their records/hashes and queue would need unwinding too.

### 7. Quick entry & reorganising (UI improvements, 2026-10-18)
- **Quick add** on the Add page (now Search): qty + location per search result, default printing as in deck import (free owned, else newest released paper). The last location used is remembered in the session and preselected (here and on the card page).
- **Paste a list into a location**: Moxfield-style lines (count optional, `(SET) number`, `*F*`); default printing for lines without a set. Quick entry, not an import: no hash, no queue, recorded as `acquire`, undoable. Unmatched lines come back once, prefilled, with their problems.
- **Move ticked**: checkboxes on a location page move *all copies* of the ticked lines to one non-deck location in one batch (partial moves stay on the per-row form). "tick all" is a link, no JS.
- **Nav search** box on every page: jumps to the card on an exact (full or front-face) name match, preferring real cards over art-series cards; else shows the result list.
- **Sortable columns** via `?sort=key` / `-key` on the collection, location and deck tables (whitelisted server-side).
- **Deck page**: main deck grouped by type (first match of Creature, Planeswalker, Battle, Land, Instant, Sorcery, Artifact, Enchantment on the front face), shown Creatures → … → Lands; text mana curve over non-land main + commander cards.
- **Location filters** (2026-10-18): on a location's page and its shared view.
  - Fields: name contains; type (card types + supertypes like Legendary); subtype; colour (W/U/B/R/G, colourless, multicolour); mana value (0–6, 7+); set; finish; qty ≥ N.
  - Dropdowns list only what occurs in that location. All faces of multi-face cards count.
  - Filters combine with sorting and the image view. "Tick all" ticks only the rows shown.
  - Done in Python over the location's rows (`queries.StockFilter`, `cardtypes.parse`). The collection and deck pages can reuse it.
- **Text | Images view** on the collection, location, deck and Add pages: a grid of card images (lazy-loaded, with a copy-count badge; the deck keeps its type groups). The choice is remembered in the session for all card lists. Row actions (move, recount, remove, quick add, tick boxes) live in the text view only.
- **Mana symbols** rendered as Scryfall's SVGs (hotlinked, like card images) in costs and oracle text; alt text keeps `{W}` for copy/paste.

## Phase 4 as built (2026-10-19)

Decided with the user:
- **Slots match copies by printing + finish.** Condition and language don't matter for decks; slots are stored as NM/en. In-deck, free, short, excess and the pull list all ignore condition/language (a deck happily holds an LP copy).
- **Mark as built retargets**: where a slot's exact printing isn't free but another printing of the card is, the slot switches to that printing, then every free copy is pulled. Leftovers stay short.

Details:
- `deckplan.state()`: copies of a printing + finish at the deck location are shared over its slots in board order (commander, main, side). `short = wanted − in deck − proxies`. Excess = copies nobody wants, including maybe-board cards; worn/foreign copies are offered for return first.
- **Pull list** per deck (`/decks/<id>/pull`) or for all decks (`/pull`): a free copy is planned for one deck only, most-copies location first. Tick lines or "pull all"; one batch (`ledger.transfer_many`), undoable. Unfillable copies are listed as *still short*, with the other free printings of the card ("use this printing" retargets that many copies of the slot) and the other decks holding the printing.
- **Return list** (`/decks/<id>/return`): per excess line a destination select, defaulting to the non-deck location the printing last came into the deck from, else "Unsorted" (created/unarchived on use).
- Confirming recomputes the plan and executes only lines still in it, so a stale page can't move more than planned.
- **Editor**: new deck form on the Decks page; deck settings (name renames the deck location too, format, notes); a slot page for qty / proxies / board / printing (with owned/free per printing) / finish, qty 0 removes; changing printing = old copies become excess, new printing short. Add cards from Search (`/add?deck=<id>` turns quick add into "add to deck" with a board), from the card page (any printing/finish), or by pasting a list (headers pick the board, else the chosen one).
- **Checks** on the deck page: banned / not legal cards for the deck's format, copy limits (4, or 1 in commander formats; basics and "any number" cards exempt; restricted = 1), deck size (100 for commander/duel/predh/paupercommander/brawl, 60 for standardbrawl/oathbreaker; else main ≥ 60 and side ≤ 15), commander present and colour identity. Plus non-land cards per colour and counts per board.
- The Decks list shows each deck's status (complete / N short / N excess).

### Decks in the UI (decided 2026-10-19)

Each deck still owns one location of type `deck` (data model unchanged): it's what keeps "every card has a location" true for cards in decks, and the ledger, pull/return and undo work on it. But it was confusing to see every deck twice, once as a deck and once as a same-named location. The user chose a **display-only** fix over real nested locations (`parent_id`), which would need a migration and rules for parent counts and moves into a parent:

- Wherever a location name is shown (collection, card page, history, collection/history filters, pull list's "in other decks"), a deck location reads **Decks / <deck name>** (`macros.place` / `macros.loc_link`; `queries.collection` builds the same string in SQL).
- The Locations list and the Overview show one **Decks** row (total copies in all decks, linking to the Decks page) instead of one row per deck location. Deck locations don't count in the Overview's location total.
- `/locations/<deck location>` **redirects to the deck page** (`#contents`) for the owner; other users with access still get the read-only shared deck view.
- The deck page is the one page for a deck: besides the target list it shows **Physically in the deck** (the deck location's stock, open when something there isn't wanted) and the recent moves in and out.
- If nesting is wanted later for user locations (Shelf / Box / Binder), that's a separate decision; this change doesn't block it.

## Users & sharing (phase 2b)

Decided 2026-10-18: a few people on one instance, each with a **separate collection**; locations (incl. decks) can be shared.

- **Accounts**: created by the admin (`python -m franticsearch user add NAME [--admin]`, plus an admin page). No sign-up. Users change their own password. Passwords hashed with werkzeug (`scrypt`/`pbkdf2`).
- **Login**: in-app login page and session cookie. Replaces the nginx basic-auth prompt once shipped. Login rate-limited (nginx `limit_req`). All POST forms carry a per-session CSRF token. Session cookie `HttpOnly`, `SameSite=Lax`, `Secure` behind HTTPS.
- **Ownership**: `users` table; `locations.owner_id` (and `imports.owner_id`). Stock, movements and decks belong to a user through their locations.
  - Location names are unique **per user**. This needs a table rebuild, since the unique constraint is global today.
  - The ledger refuses moves between two users' locations until trades exist.
  - Every route that takes a location / deck / import / queue line / batch id checks ownership, with tests per route. URL-guessing someone else's ids must 404.
- **Existing data**: the first admin account created adopts all existing locations and imports.
- **Shared card data**: `printings` and the Scryfall sync are global. The System page (sync, check) is admin-only.
- **Visibility per location** (and so per deck, via its deck location): `private` (default) · `users` (any logged-in user) · `link` (anyone with an unguessable share URL `/s/<token>`, no login). Shared views are **read-only**: contents with quantities, the image view, the deck page. No history, notes or actions. A link can be revoked (new token).
- **Discovery**: a "People" page lists users and their shared locations. Card pages show copies in other users' shared locations ("Alex: 2 in Trade binder").
- **Per user**: last location, view mode, undo. Undo only for your own batches.
- **Shared views never show the owner's private data**: a shared deck shows qty / card / cost / printing only. No "where (free)" columns, which would name the owner's other locations.
- Implementation notes (2026-10-18):
  - Migration 005 rebuilds `locations` (owner, visibility, share token, `UNIQUE(owner_id, name)`) and `imports` (owner, `UNIQUE(owner_id, sha256)`). The runner turns foreign keys off per migration and runs `foreign_key_check` before COMMIT.
  - Ownership is enforced in two places. Routes 404 on anything not yours, via `_my_location` / `_my_deck` / `_my_import` / `_my_unresolved` and batch owners for undo. The ledger refuses moves between two owners' locations.
  - Sessions are cleared on login. Cookies: `SameSite=Lax`; `FS_SECURE_COOKIE=1` for `Secure` when only reached over HTTPS.
- **Later: trades.** Moving copies from one user's location to another's as a confirmed two-sided action, a `trade` reason pair in both ledgers. The schema above doesn't block it.

## Import / migration from Moxfield

1. **Collection CSV** (Moxfield export: Count, Name, Edition, Collector Number, Foil, Condition, Language…) → into a chosen location, default "Unsorted". Resolution by (set, collector number) → fallback (name, set) → unresolved queue for manual fix.
2. **Deck text export** (`1 Sol Ring (C21) 263 *F*`, sections for commander/sideboard/maybeboard) → creates deck + slots.
3. **"Mark as built"** per imported deck: confirms the cards physically live in that deck → moves matching stock from non-deck locations into the deck location; leftovers show as short (proxy or buy). Surfaces cases where Moxfield double-counted a card across decks.
4. Importing is idempotent per file (hash recorded) to prevent accidental double imports.

Decided for phase 2 (2026-10-18):
- **Duplicates are always refused** (sha256 of the CSV bytes / of the deck text with normalised line endings), no override. Deleting an imported deck deletes its import record too, so that list can be imported again.
- **Proxy rows** in the collection CSV are skipped (counted in the import summary); proxies belong on deck slots.
- **Deck text**: Moxfield has two text exports. The plain one is `qty name` lines, a blank line, then the commander; the one with set codes (`1 Name (SET) 123 *F*`) has no blank line and lists the commander **first**. Commander detection is automatic: section headers (`Commander`, `Sideboard:`, `//Maybeboard` …) win; else lines after the first blank line go to a board chosen on the import form (default: commander); else, for commander formats, the first card is the commander (only one; a second partner is moved by hand in the phase 4 editor). The import summary names the commander. `(SET) number` and `*F*`/`*E*` are honoured when present.
- **Default printing** for lines without a set: a free owned printing (most copies outside decks), else the newest *released paper* printing, ranking Secret Lair / The List / joke sets below regular sets and never picking tokens, art cards, oversized or digital-only printings. Names match the full name or the front face.
- Collection CSV columns are matched by name (Moxfield's documented header plus common aliases); unreadable count/foil/condition/language values send the row to the unresolved queue instead of being guessed.
- Unresolved queue: per line, *fix* (search card → pick printing, finish, condition, language, qty; collection lines are acquired with reason `import`, deck lines become slots) or *dismiss*.
- Collection imports go into one chosen non-deck location; "Unsorted" (a box) is created on first use.
- Imported decks were **read-only** until phase 4: Decks list + deck page (slots per board with in-deck / free / where / any-printing counts) and delete (only while no cards were ever moved into the deck). The deck's location has the deck's name.

## Stack

- **Python 3.11+ / Flask + Jinja**: server-rendered HTML forms, no JavaScript build step; deliberately plain 90s/00s FOSS look
- **SQLite** via the standard library `sqlite3` (WAL mode); numbered plain-SQL migrations tracked in `PRAGMA user_version`
- **Scryfall bulk data** (`default_cards`, served by Scryfall as gzipped JSON Lines, ~80 MB) streamed into `printings` on first run and re-checked daily (background thread); images hotlinked from Scryfall CDN. Follows Scryfall's API guidelines (bulk file, User-Agent + Accept headers)
- **pytest** for domain logic (ledger invariants, pull/return planning, import parsing) and Flask test-client page tests
- **Docker** single container running gunicorn (1 worker, threads), DB in a mounted volume; backup = copy the DB file

Domain logic (`ledger.py`, later pull/return planning and import parsing) is framework-free and unit-tested; the Flask layer only parses forms and renders.

## Phases

| # | Phase | Deliverables | Status |
|---|---|---|---|
| 1 | Foundation | Repo scaffold, Docker, schema + migrations, Scryfall sync, locations CRUD, stock + ledger with invariant check, collection browser, manual add/move/adjust | **Shipped** (PR #1). `docker compose up --build` itself not yet verified |
| 2 | Import | Moxfield collection CSV, deck text import, unresolved queue | **Shipped** (PR #2). Collection CSV verified with a real Moxfield export on 2026-10-18 (337 rows, 0 unresolved) |
| 2b | Users & sharing | Accounts (admin-created), login + CSRF, per-user collections, ownership checks on every route, location visibility (private / users / link), People page, others' copies on card pages | **Shipped** (PR #6) |
| 3 | Card search | Local search over `printings` (name/type/text/colors/MV/set/legality), ownership-aware results | **Shipped** (PR #8) |
| 4 | Decks | Deck editor (slots, boards, proxies, printing switch), live stats + legality, derived deck state, pull list, return list, "mark as built" | **Built** (branch `phase-4`), see § Phase 4 as built |
| 5 | Shortfalls | Proxies, cross-deck buy list, exports | **Next.** Proxies per slot already exist (phase 4) |
| 6 | Polish | Batch undo, ledger views, mobile layout pass, backup/restore | Partly: batch undo shipped (PR #4) |
| later | — | Scryfall query syntax, public hosting + auth, prices, scanning, trades between users | Hosting + auth done: reverse proxy (PR #3), in-app login (PR #6). Still to do: run the tunnel/server as permanent services; backups |

### Shipped so far (as of 2026-10-19)

- **PR #1, Phase 1 foundation.** Plus a sync fix: Scryfall now serves `default_cards` only as gzipped JSON Lines (`jsonl_download_uri`); the importer reads that and legacy JSON arrays. Verified live: ~119k printings in ~25 s.
- **PR #2, Phase 2 import.**
  - Moxfield deck text, both exports, creates deck + deck location + slots. Commander detection: headers, then the block after a blank line, then the first line for commander formats.
  - Collection CSV goes into a location. Proxies are skipped.
  - Unresolved queue with fix/dismiss. Duplicate imports are refused by hash.
  - Read-only Decks pages.
  - Card image on hover.
  - Decisions are in § Import / migration.
- **PR #3, reverse-proxy support.** `FS_PROXY=1` trusts `X-Forwarded-*` including the prefix. Served at `https://theorycorner.com/franticsearch/` via nginx and a reverse SSH tunnel from the dev machine (CLAUDE.md § Deployment). nginx basic auth was replaced by the in-app login in PR #6. The tunnel is not a permanent service yet.
- **PR #4, UI improvements** (§ 6, § 7):
  - quick add
  - paste a list into a location
  - move ticked lines
  - batch undo (migration 004)
  - nav search
  - sortable columns
  - deck type groups + mana curve
  - mana symbols
  - Text | Images view
- **PR #5, plan update.** Shipped status, and the users & sharing design.
- **PR #6, Phase 2b users & sharing** (§ Users & sharing): admin-created accounts, login + CSRF, separate collections with ownership checks on every route, private / users / link sharing, People page. Live with two users.
- **PR #7, location filters** (§ 7): name, type, subtype, colour, MV, set, finish, qty on location pages and shared views.
- **PR #8, Phase 3 card search** (§ 5): the Search page, with owned/free counts and quick add.

## Open questions

- None blocking.
- Deployment target: today the app runs on the dev machine behind a reverse SSH tunnel. Revisit before relying on it: permanent services, or move the app onto the server with gunicorn as the Dockerfile does. Also scheduled backups of the SQLite file.
