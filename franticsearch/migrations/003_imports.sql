-- Phase 2: Moxfield import (collection CSV, deck text) and the unresolved queue.

-- Printing fields used to pick a sensible default printing for name-only lines.
-- face_name = front face for multi-face cards ("Delver of Secrets"), else the name.
ALTER TABLE printings ADD COLUMN face_name TEXT;
ALTER TABLE printings ADD COLUMN set_type TEXT;
ALTER TABLE printings ADD COLUMN digital INTEGER;
ALTER TABLE printings ADD COLUMN promo INTEGER;
UPDATE printings SET face_name = CASE WHEN instr(name, ' // ') > 0
                                      THEN substr(name, 1, instr(name, ' // ') - 1)
                                      ELSE name END;
CREATE INDEX printings_name_nocase ON printings(name COLLATE NOCASE);
CREATE INDEX printings_face_nocase ON printings(face_name COLLATE NOCASE);
-- set_type/digital/promo are only filled by a sync: make the next one re-import.
DELETE FROM meta WHERE key IN ('scryfall_updated_at', 'last_sync_at');

-- One row per imported file / pasted deck. sha256 blocks importing the same thing twice.
CREATE TABLE imports (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind        TEXT    NOT NULL CHECK (kind IN ('collection', 'deck')),
    filename    TEXT    NOT NULL DEFAULT '',
    sha256      TEXT    NOT NULL UNIQUE,
    location_id INTEGER REFERENCES locations(id),                 -- collection imports
    deck_id     INTEGER REFERENCES decks(id) ON DELETE CASCADE,  -- deck imports
    batch_id    TEXT,                                            -- ledger batch (collection)
    lines       INTEGER NOT NULL DEFAULT 0,   -- card lines read
    imported    INTEGER NOT NULL DEFAULT 0,   -- copies (collection) / cards (deck) resolved
    skipped     INTEGER NOT NULL DEFAULT 0,   -- e.g. proxies
    CHECK ((kind = 'collection') = (location_id IS NOT NULL))
);

-- Lines that could not be matched to a printing (or had unreadable fields).
CREATE TABLE import_unresolved (
    id               INTEGER PRIMARY KEY,
    import_id        INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    raw              TEXT    NOT NULL,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    name             TEXT    NOT NULL DEFAULT '',
    set_code         TEXT    NOT NULL DEFAULT '',
    collector_number TEXT    NOT NULL DEFAULT '',
    finish           TEXT,                       -- NULL = not specified
    condition        TEXT    NOT NULL DEFAULT 'NM',
    lang             TEXT    NOT NULL DEFAULT 'en',
    board            TEXT,                       -- deck imports
    problem          TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'fixed', 'dismissed'))
);
CREATE INDEX import_unresolved_import ON import_unresolved(import_id, status);
