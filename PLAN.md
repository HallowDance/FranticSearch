# FranticSearch — Plan

A self-hosted MTG collection & deck manager built around one idea: **every physical card has a location, and decks are just locations.** Moving a card into or out of a deck is a recorded, confirmed physical move — no hand-maintained "binders" mirroring decks.

## Decisions

| Topic | Decision |
|---|---|
| Platform | Self-hosted web app for **a few users with separate collections** (from phase 2b; single user before), SQLite; usable from desktop and phone browsers |
| Access | Phases 1–2: no auth (LAN, then nginx basic auth). From phase 2b: **in-app login**, enforced by a single auth hook (`before_request`) so routes don't each handle it. See § Users & sharing |
| Deckbuilding | **In FranticSearch.** Decks are built and edited here; Moxfield deck import is for one-time migration only |
| Copy model | Quantity per **printing key** per location. Key = Scryfall printing + finish + condition + language. Condition/language are optional and default to NM/EN — hidden in the UI unless set |
| Locations | Flat, named, typed: `binder` · `box` · `deck` · `lent` · `other`. Each deck owns exactly one location, shown in the UI as "Decks / <deck>" (§ Decks in the UI) |
| Deck slots | Always an **exact printing** + finish; condition and language don't matter for decks (§ Phase 4 as built) |
| Contention | **Strict**: a copy is in exactly one place. If the needed copies are all in other decks, the slot is short |
| Deck ↔ collection | **Pull/return workflow**: a deck is a target list; stock only moves when you confirm a physical move. A deck's cards join the collection only via the pull list (copies you have) or **mark as built** ("I own this deck": new copies, in the deck) |
| Extras in scope | Moxfield/CSV import · proxies & shortfall/buy list · exports · backups |
| Hosting model | **FOSS, self-hosted per person or friend group** (decided 2026-10-20). Never open sign-up or a shared public service: accounts stay admin-created. Federation between instances may be explored later |
| Out of scope (for now) | Prices, card scanning, trades between users (later, see § Users & sharing), Moxfield re-sync. Open sign-up is out of scope permanently |

## Data model

| Table | Columns / purpose |
|---|---|
| `printings` | Scryfall bulk cache: `scryfall_id` (PK), `oracle_id`, `name`, `set`, `set_name`, `collector_number`, `released_at`, `finishes`, `lang`, `mana_cost`, `cmc`, `colors`, `color_identity`, `type_line`, `oracle_text`, `legalities`, image URIs (incl. card faces). FTS index on `name`, `type_line`, `oracle_text` |
| `users` | `id`, `username`, `password_hash`, `is_admin` (phase 2b), `disabled` (007) |
| `locations` | `id`, `owner_id`, `name` (unique per owner), `type`, `notes`, `archived`, `visibility`, `share_token` |
| `stock` | (`location_id`, `printing_id`, `finish`, `condition`, `lang`) → `qty` (> 0). **Single source of truth for physical whereabouts** |
| `movements` | Append-only ledger: `id`, `ts`, `owner_id`, `from_location`, `to_location`, printing key, `qty`, `reason` (`import` · `acquire` · `pull` · `return` · `move` · `adjust` · `sell`), `note`, `batch_id`, `undo_of` (the batch an undo reverses) |
| `decks` | `id`, `name`, `format`, `location_id`, `notes` |
| `deck_slots` | `deck_id`, printing key (always NM/en), `board` (`main` · `side` · `commander` · `maybe`), `qty_wanted`, `qty_proxy` |
| `deck_tags` | `deck_id`, `card_key` (oracle id, else Scryfall id), `tag` (case-insensitive). Tags on a deck's cards (008, § 7 Deck tags) |
| `imports`, `import_unresolved` | One row per imported file / deck list (owner, sha256 for duplicate refusal), and the lines that need a decision (phase 2) |
| `meta` | Key/value: Scryfall sync state, generated secret key |

Schema history: `migrations/001`–`008` (005 = users & sharing; 006 = `movements.owner_id`, the owner of each movement, for fast per-user history, docs/SCALING.md § P3; 007 = `users.disabled`; 008 = `deck_tags`).

Rules:
- `stock` and `movements` are always written in the same transaction; `stock` must equal the ledger's projection (a check command verifies this).
- `acquire`/`import` have a null `from`; `sell`/removal have a null `to`.
- A manual move between two non-deck locations is `move`; any move into a deck location is a `pull`, out of one a `return`.
- Deck locations are only changed through their deck's pull/return lists and "mark as built" (which acquires straight into the deck), never by the manual move forms.
- A location can be deleted only if it never held anything (the ledger refers to it). Otherwise, once empty, it can be **archived**: hidden from lists and forms, kept in history, restorable. Nothing can be added to an archived location.
- `batch_id` groups one user action (e.g. "confirm pull list") so it can be undone as a unit.

### Derived deck state (computed, never stored)

Per slot:
- `in_deck` = `stock` at the deck's location for the slot's printing + finish (any condition/language), shared out over the slots of that printing in board order commander → main → side
- `short` = `max(0, qty_wanted − in_deck − qty_proxy)`

Per deck:
- **Excess** = stock at the deck location beyond what slots want (removed cards, lowered counts, changed printings)
- Status: *complete* / *N short* / *N excess*

`maybe` board slots never count toward short or excess.

## Core workflows

### 1. Pull list
For one deck or all decks: for each short slot, find the same printing + finish in **non-deck** locations and propose moves — "Pull 2× Sol Ring (C21 #263, nonfoil) from Binder A". Tick lines individually or confirm all → `pull` movements in one batch.

Helper (informational, strictness unchanged): if the exact printing isn't free but a *different* printing of the same card is, show it with a one-click "retarget slot to this printing".

### 2. Return list
Excess copies in a deck location become return lines. Default destination = the non-deck location the printing last came into the deck from (from the ledger), else "Unsorted" (a box, created on first use); changeable per line. Confirm → `return` movements.

Changing a slot's printing = old printing becomes excess (return) + new printing becomes short (pull).

### 3. Proxies & shortfalls
`qty_proxy` covers part of a shortfall. The **buy list** aggregates remaining `short` across all decks, grouped by printing, exportable as plain text / Moxfield / TCGplayer mass-entry / CSV. As built: § Phases 5 & 6 as built.

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
Ledger view per card, per location, per deck (History page filters, linked from each page). Undo any batch by writing compensating movements (never deleting rows).

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
- **Deck page layout** (2026-10-26): the View, Group and List switches share one line. On screens ≥ 1000 px the card list is the left column (as wide as its content) and Stats, Physically in the deck and Add cards from a list sit in a right column; narrower screens keep one column in the same order. CSS grid only. The status line is one line (status · in deck · Add cards · Paste a list · Manage deck); when the deck is short or has excess, one more line of actions (Pull list, Proxy all, Mark as built, Buy list · Return list). The import link moved to the Manage page.
- **Deck tags** (decided 2026-10-25): free-text tags on a deck's cards, for grouping and sorting the deck page.
  - A tag belongs to the **card within a deck** (deck + oracle id), not the slot: it applies on every board and survives printing changes and "Put it back". Each deck has its own tags.
  - **Any number per card**; comma-separated on the card's edit page (the only place to edit them). Case-insensitive: a tag already in the deck keeps its first spelling. Max 20 per card, 40 characters each.
  - "Group main by: **Type** | **Tag**" (shown once the deck has tags; `?group=tag`, not remembered): a card with several tags is listed under each tag, untagged cards last under "Untagged"; the Main total counts each card once. Other boards stay flat.
  - A sortable Tags column (by first tag, untagged last). Shared views show tags too.
  - Not in Moxfield import/export for now (Moxfield's `#tag` suffix could be read and written later).
- **Collection and deck filters** (decided 2026-10-21): the same filter form as locations (`macros.stock_filter`, `queries.StockFilter`), in Python.
  - **Collection**: the location fields plus location, location kind and "not in any deck". A card shows if any of its copies match. Qty ≥ counts its matching copies together. Grouping, sorting and paging happen in Python over the user's stock lines (8k lines: ~120 ms). The Name box is "name contains", as on locations.
  - **Decks**: the location fields (no Qty) plus board and status (short / with proxies / complete). Stats, curve and warnings describe the whole deck. Shared deck views get the filters without Status (it would reveal what's physically in the deck).
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
- ~~Mark as built retargets and pulls~~ — **changed 2026-10-19**: "Mark as built" means *I own this deck*: every card it's still short is **added to the collection as new copies, directly in the deck location** (one `acquire` batch, note "mark as built", undoable). Free copies elsewhere are left alone (the pull list moves those); proxies and the maybeboard aren't added. A deck's cards are not part of the collection unless it's marked as built or filled via the pull list. Offered as a checkbox on the deck import form and as a button on any deck with something short.

Details:
- `deckplan.state()`: copies of a printing + finish at the deck location are shared over its slots in board order (commander, main, side). `short = wanted − in deck − proxies`. Excess = copies nobody wants, including maybe-board cards; worn/foreign copies are offered for return first.
- **Pull list** per deck (`/decks/<id>/pull`) or for all decks (`/pull`): a free copy is planned for one deck only, most-copies location first. Tick lines or "pull all"; one batch (`ledger.transfer_many`), undoable. Unfillable copies are listed as *still short*, with the other free printings of the card ("use this printing" retargets that many copies of the slot) and the other decks holding the printing.
- **Return list** (`/decks/<id>/return`): per excess line a destination select, defaulting to the non-deck location the printing last came into the deck from, else "Unsorted" (created/unarchived on use).
- Confirming recomputes the plan and executes only lines still in it, so a stale page can't move more than planned.
- **Editor**: new deck form on the Decks page; deck settings (name renames the deck location too, format, notes); a slot page for qty / proxies / board / printing (with owned/free per printing) / finish, qty 0 removes; changing printing = old copies become excess, new printing short. Add cards from Search (`/add?deck=<id>` turns quick add into "add to deck" with a board), from the card page (any printing/finish), or by pasting a list (headers pick the board, else the chosen one).
- **Checks** on the deck page: banned / not legal cards for the deck's format, copy limits (4, or 1 in commander formats; basics and "any number" cards exempt; restricted = 1), deck size (100 for commander/duel/predh/paupercommander/brawl, 60 for standardbrawl/oathbreaker; else main ≥ 60 and side ≤ 15), commander present and colour identity. Plus non-land cards per colour and counts per board.
- The Decks list shows each deck's status (complete / N short / N excess).
- **Edit mode** (decided 2026-10-25): "List: View · Edit" links on the deck page (owner only, `?edit=1`, kept through filters, sorting and the view switch). In Edit mode every card gets one-click **−** / **+** (count; −1 on the last copy removes it) and **Remove** (the whole card), in the text view's first column and under each image tile (✕). Removing only changes the list: its copies in the deck become excess, i.e. the return list. The "Removed …" message has a **Put it back** button (restores count, board, printing, proxies; the slot travels in the flash, nothing private). The slot page stays for printing / finish / board / proxies.
- **Manage page** (decided 2026-10-22): deck settings (name, format, notes), sharing, export and delete live on `/decks/<id>/manage`, linked as "Manage deck" from the deck's status line, to keep the deck page to the list itself. Saving settings or sharing returns there.
- **Deck table columns** (decided 2026-10-21): one *Status* column replaces In deck / Proxy / Short: `✓`, `✓ 1 proxy`, or `1 in deck, 1 proxy, **1 short**` (maybeboard: "doesn't count"). *Free* and *Where (free)* appear only while the deck is short, and are filled in only on short rows; a complete deck shows Qty / Card / Cost / Printing / Status. The hint "N in other printings or other decks" counts only copies outside this deck. The slot's edit page lists all your copies of the card with their locations (this deck / free / other decks).

### Decks in the UI (decided 2026-10-19)

Each deck still owns one location of type `deck` (data model unchanged): it's what keeps "every card has a location" true for cards in decks, and the ledger, pull/return and undo work on it. But it was confusing to see every deck twice, once as a deck and once as a same-named location. The user chose a **display-only** fix over real nested locations (`parent_id`), which would need a migration and rules for parent counts and moves into a parent:

- Wherever a location name is shown (collection, card page, history, collection/history filters, pull list's "in other decks"), a deck location reads **Decks / <deck name>** (`macros.place` / `macros.loc_link`; `queries.collection` builds the same string in SQL).
- The Locations list and the Overview show one **Decks** row (total copies in all decks, linking to the Decks page) instead of one row per deck location. Deck locations don't count in the Overview's location total.
- `/locations/<deck location>` **redirects to the deck page** (`#contents`) for the owner; other users with access still get the read-only shared deck view.
- The deck page is the one page for a deck: besides the target list it shows **Physically in the deck** (the deck location's stock, open when something there isn't wanted) and the recent moves in and out.
- If nesting is wanted later for user locations (Shelf / Box / Binder), that's a separate decision; this change doesn't block it.

## Phases 5 & 6 as built (2026-10-19)

Decided with the user:
- **Buy list per printing, flag alternatives**: lines are per printing + finish (what `pull_plan` can't fill from free copies, after proxies), each with "N free in another printing" (a retarget could replace buying). A filter hides those lines; exports honour it.
- **Backups: local, nightly.** `franticsearch-backup.timer` (systemd user timer, 03:30, `Persistent=true` so a sleeping laptop catches up on wake) runs `python -m franticsearch backup`: SQLite online backup API, gzipped to `<db dir>/backups/franticsearch-YYYYmmdd-HHMMSS.db.gz`, newest 14 kept (~57 MB each as of 2026-10-21, almost all of it the Scryfall card cache). No off-machine copy and no download button (not chosen).
- **Restore: CLI only.** `python -m franticsearch restore FILE [--force] [--port N]` refuses while something listens on the server port, checks the file (integrity, has FranticSearch tables), saves the current DB as `<db>.pre-restore-<ts>`, copies the backup in page by page (WAL-safe), runs pending migrations and the ledger check.
- **Extras**: "Proxy all N short" on a deck; proxy print list; deck export.

Details:
- `exports.py` renders card lists: plain text, Moxfield (`1 Sol Ring (C21) 263 *F*`; deck exports get `Commander` / `Deck` / `Sideboard` / `Maybeboard` headers, which the deck importer reads back), TCGplayer mass entry (`1 Sol Ring [C21]`, no finish), CSV (Moxfield collection columns, plus `Board` for decks).
- **Buy list** `/buy` (link on the Decks page): the list in a textarea above the table, with one link per format ("Copy as: Moxfield · Plain text · …"; a dropdown + button looked broken without JS, fixed 2026-10-19) and a download link (`/buy/export?format=…&download=1`). Lines name the decks that need them.
- **Deck export** `/decks/<id>/export?format=…` (shown inline, `download=1` to save); `what=proxies` exports only proxied copies (for a proxy printing site). Owner only.
- **Ledger views**: History filters by location (decks as "Decks / <name>") and by card (`/history?card=<key>`); card, location and deck pages link to their full history, which has undo.
- **Phone layout pass** (checked with headless Chromium at 390 px on a copy of the live DB): row forms (location actions, quick add, alternatives, return destinations) stay on one line so rows stay short and the table scrolls sideways inside its box; the page itself never scrolls sideways. A deck's Qty links to its edit page, so editing works without scrolling the table. Textareas fill the width.
- The System page lists the backups.

## Users & sharing (phase 2b)

Decided 2026-10-18: a few people on one instance, each with a **separate collection**; locations (incl. decks) can be shared.

- **Accounts**: created by the admin (`python -m franticsearch user add NAME [--admin]`, plus an admin page). No sign-up. Users change their own password. Passwords hashed with werkzeug (`scrypt`/`pbkdf2`).
- **Admin Users page** (decided 2026-10-21):
  - A blank password when creating an account or setting one generates a password (14 characters, no look-alikes). It's shown once, rendered into the response, never kept in the cookie session.
  - Accounts can be **disabled** and enabled again (migration 007 `users.disabled`; also `user disable` / `user enable` on the CLI). Disabled = no login, existing sessions end, and what the account shared is hidden from others: People page, card pages, shared views, share links 404. Nothing is deleted.
  - You can't disable your own account, and the last enabled admin can't be disabled.
- **Login**: in-app login page and session cookie (replaced the earlier nginx basic-auth prompt). Login rate-limited (nginx `limit_req`). All POST forms carry a per-session CSRF token. Session cookie `HttpOnly`, `SameSite=Lax`, `Secure` behind HTTPS.
- **Ownership**: `users` table; `locations.owner_id` (and `imports.owner_id`). Stock, movements and decks belong to a user through their locations.
  - Location names are unique **per user** (migration 005 rebuilt the table for this).
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
3. **"Mark as built"** (import checkbox or deck page button): you own the deck, so its short cards are added to the collection as new copies, in the deck location. To fill a deck from copies you already have, use the pull list instead. (Phase 4; semantics changed 2026-10-19, see § Phase 4 as built.)
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
- **Docker** single container running gunicorn (1 worker, threads), DB in a mounted volume (not yet run in a real container). The live instance runs gunicorn (`deploy/gunicorn.conf.py`, same 1 worker × 8 threads) as a systemd user service instead, since 2026-10-21 (CLAUDE.md § Deployment)
- **Backups**: `python -m franticsearch backup` (SQLite online backup, gzipped), nightly via a systemd user timer; `restore` from the CLI

Domain logic (`ledger.py`, `deckplan.py` for deck state and pull/return planning, `importer.py`, `exports.py`, `backup.py`) is framework-free and unit-tested; the Flask layer only parses forms and renders.

## Phases

| # | Phase | Deliverables | Status |
|---|---|---|---|
| 1 | Foundation | Repo scaffold, Docker, schema + migrations, Scryfall sync, locations CRUD, stock + ledger with invariant check, collection browser, manual add/move/adjust | **Shipped** (PR #1). `docker compose up --build` itself not yet verified |
| 2 | Import | Moxfield collection CSV, deck text import, unresolved queue | **Shipped** (PR #2). Collection CSV verified with a real Moxfield export on 2026-10-18 (337 rows, 0 unresolved) |
| 2b | Users & sharing | Accounts (admin-created), login + CSRF, per-user collections, ownership checks on every route, location visibility (private / users / link), People page, others' copies on card pages | **Shipped** (PR #6) |
| 3 | Card search | Local search over `printings` (name/type/text/colors/MV/set/legality), ownership-aware results | **Shipped** (PR #8) |
| 4 | Decks | Deck editor (slots, boards, proxies, printing switch), live stats + legality, derived deck state, pull list, return list, "mark as built" | **Shipped** (PR #11), see § Phase 4 as built |
| 5 | Shortfalls | Proxies, cross-deck buy list, exports | **Shipped** (PR #12), see § Phases 5 & 6 as built |
| 6 | Polish | Batch undo, ledger views, mobile layout pass, backup/restore | **Shipped** (PR #12; batch undo in PR #4) |
| later | — | Scryfall query syntax, public hosting + auth, prices, scanning, trades between users | Hosting + auth done: reverse proxy (PR #3), in-app login (PR #6), systemd services (PR #10), nightly local backups (PR #12). The rest: § What's next |

### Shipped so far (as of 2026-10-21)

- **PR #1, Phase 1 foundation.** Plus a sync fix: Scryfall now serves `default_cards` only as gzipped JSON Lines (`jsonl_download_uri`); the importer reads that and legacy JSON arrays. Verified live: ~119k printings in ~25 s.
- **PR #2, Phase 2 import.**
  - Moxfield deck text, both exports, creates deck + deck location + slots. Commander detection: headers, then the block after a blank line, then the first line for commander formats.
  - Collection CSV goes into a location. Proxies are skipped.
  - Unresolved queue with fix/dismiss. Duplicate imports are refused by hash.
  - Read-only Decks pages.
  - Card image on hover.
  - Decisions are in § Import / migration.
- **PR #3, reverse-proxy support.** `FS_PROXY=1` trusts `X-Forwarded-*` including the prefix. Served at `https://theorycorner.com/franticsearch/` via nginx and a reverse SSH tunnel from the dev machine (CLAUDE.md § Deployment). nginx basic auth was replaced by the in-app login in PR #6. Server and tunnel became systemd user services on 2026-10-19.
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
- **PR #10, permanent services**: the app server and the reverse SSH tunnel run as systemd user services (`deploy/systemd/`).
- **PR #11, Phase 4 decks** (§ Phase 4 as built, § Decks in the UI): deck editor, derived state, pull / return lists, mark as built, deck checks; deck locations shown as "Decks / <name>".
- **PR #12, Phases 5 & 6** (§ Phases 5 & 6 as built): buy list, exports, proxies, deck export, backups + restore, history by card, phone layout pass.
- **PR #13, docs** after phases 4–6, including § What's next.
- **PR #14, buy list format links**: the format dropdown looked broken without JS; now one link per format, list above the table.
- **PR #15, mark as built acquires**: "Mark as built" adds the deck's short cards to the collection as new copies, in the deck (import checkbox + deck page button), replacing pull + retarget (§ Phase 4 as built).
- **PR #16, README** brought up to date.
- **PR #17, docs** recording #13–#16.
- **PR #18, code review + expansion guide**: [docs/SCALING.md](docs/SCALING.md) (measurements on a scaled copy of the live DB, findings, staged guide, backlog); the hosting model decision (§ Decisions).
- **PR #19, security fixes**: open redirect via `next` (`safe_next()`), a duplicate-import race that gave a 500, the first-boot secret-key race.
- **PR #20, performance**: pull plan and deck state without per-slot subqueries or quadratic loops; migration 006 `movements.owner_id` for per-user history; the Scryfall sync skips unchanged printings; `PRAGMA optimize`. On a scaled copy: `/pull` 2.0 s → 0.3 s, `/history` 1.0 s → 15 ms.
- **PR #21, admin Users page**: generated passwords (shown once), disable / enable accounts (migration 007, CLI `user disable` / `user enable`; § Users & sharing).
- **PR #22, deck page**: where-to-fetch columns only while the deck is short; the "owned elsewhere" hint no longer counts the deck's own copies; the slot page lists all your copies.
- **PR #23, deck page Status column** (✓ / ✓ 1 proxy / 1 in deck, **1 short**) instead of In deck / Proxy / Short.
- **PR #24, share-link fix**: card images showed inline for visitors without a login.
- **PR #25, logging** (`logs.py`, `FS_LOG_LEVEL`): no silent background errors; logins, admin actions, migrations, syncs and 500s are logged.
- **PR #26, gunicorn** for the live instance (`deploy/gunicorn.conf.py`), instead of the Flask dev server.

## What's next

All six phases are shipped (2026-10-19), plus follow-ups #14–#26 (to 2026-10-21). The app is still a **prototype**; nothing is in progress, and the user picks the next item. Candidates:

A code review focused on scalability, with a staged infrastructure guide and a prioritised backlog, is in [docs/SCALING.md](docs/SCALING.md) (2026-10-20). Its items are recommendations until picked here; the backlog's Status column shows what's done (#19, #20, #25, #26).

**CI/CD waits for the first proper release** (decided 2026-10-21): once the prototype is finished, set up CI (tests and lint on every PR) and a deploy flow (tagged releases, a separate live checkout; docs/SCALING.md § O6–O8). Until then, deploys stay manual: merge, then restart.

Features (from "later"):
- **Scryfall query syntax** subset (`t:`, `o:`, `c<=`, `mv=` …) in the Search box, on top of the structured filters (§ 5).
- **Trades between users**: a confirmed two-sided move with a `trade` reason pair in both ledgers (§ Users & sharing). Needs a migration (reason CHECK constraint).
- **Prices** (Scryfall bulk data has them) for the collection and the buy list.
- Card scanning stays out of scope.

Operations:
- **Off-machine copies of the backups** (only local ones exist; the laptop is a single point of failure). Not chosen in phase 6; still open, and the most important remaining item (docs/SCALING.md § Stage 1: encrypted with `age`, copied to the server over SSH; smaller user-data backups, § P7).
- `FS_SECURE_COOKIE=1` once the site is only used over HTTPS.
- `loginctl enable-linger` if the site should run while the owner is logged out (it still stops while the laptop sleeps).
- Sync, migrate and the ledger check as systemd timers, with failure notifications (docs/SCALING.md § O2, O4, O5).
- Verify `docker compose up --build` in a real container.
- Before others self-host it: a **licence** (none chosen yet), templated unit files, install docs (docs/SCALING.md § Stage 3).
- The live instance stays on the laptop behind the reverse SSH tunnel (decided 2026-10-20); moving it onto the server is not planned for now.

Considered and dropped (2026-10-19):
- A proxy **PDF** (card images 9 per page for printing): the user decided it isn't needed; the text proxy list for proxy printing sites stays.

Smaller ideas noticed while building (not decided):
- Nested user locations (Shelf / Box / Binder); § Decks in the UI leaves room for it.
- Undo for imports (§ 6).

## Open questions

- None blocking. See § What's next for what to pick up.
