-- Initial schema. See PLAN.md "Data model".

-- Scryfall bulk cache (default_cards). JSON-shaped columns hold JSON text.
CREATE TABLE printings (
    scryfall_id      TEXT PRIMARY KEY,
    oracle_id        TEXT,
    name             TEXT NOT NULL,
    lang             TEXT NOT NULL,
    set_code         TEXT NOT NULL,
    set_name         TEXT NOT NULL,
    collector_number TEXT NOT NULL,
    released_at      TEXT,
    rarity           TEXT,
    layout           TEXT,
    finishes         TEXT NOT NULL,          -- JSON array: nonfoil / foil / etched
    mana_cost        TEXT,
    cmc              REAL,
    colors           TEXT,                   -- JSON array
    color_identity   TEXT,                   -- JSON array
    type_line        TEXT,
    oracle_text      TEXT,
    legalities       TEXT,                   -- JSON object
    image_small      TEXT,
    image_normal     TEXT
);
CREATE INDEX printings_name ON printings(name);
CREATE INDEX printings_oracle ON printings(oracle_id);
CREATE INDEX printings_set ON printings(set_code, collector_number);

CREATE VIRTUAL TABLE printings_fts USING fts5(
    name, type_line, oracle_text,
    content='printings', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER printings_ai AFTER INSERT ON printings BEGIN
    INSERT INTO printings_fts(rowid, name, type_line, oracle_text)
    VALUES (new.rowid, new.name, new.type_line, new.oracle_text);
END;
CREATE TRIGGER printings_ad AFTER DELETE ON printings BEGIN
    INSERT INTO printings_fts(printings_fts, rowid, name, type_line, oracle_text)
    VALUES ('delete', old.rowid, old.name, old.type_line, old.oracle_text);
END;
CREATE TRIGGER printings_au AFTER UPDATE ON printings BEGIN
    INSERT INTO printings_fts(printings_fts, rowid, name, type_line, oracle_text)
    VALUES ('delete', old.rowid, old.name, old.type_line, old.oracle_text);
    INSERT INTO printings_fts(rowid, name, type_line, oracle_text)
    VALUES (new.rowid, new.name, new.type_line, new.oracle_text);
END;

CREATE TABLE locations (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type  TEXT NOT NULL CHECK (type IN ('binder', 'box', 'deck', 'lent', 'other')),
    notes TEXT NOT NULL DEFAULT ''
);

-- Physical whereabouts. Must always equal the projection of `movements`.
CREATE TABLE stock (
    location_id INTEGER NOT NULL REFERENCES locations(id),
    printing_id TEXT    NOT NULL REFERENCES printings(scryfall_id),
    finish      TEXT    NOT NULL CHECK (finish IN ('nonfoil', 'foil', 'etched')),
    condition   TEXT    NOT NULL DEFAULT 'NM' CHECK (condition IN ('NM', 'LP', 'MP', 'HP', 'DMG')),
    lang        TEXT    NOT NULL DEFAULT 'en',
    qty         INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (location_id, printing_id, finish, condition, lang)
);
CREATE INDEX stock_printing ON stock(printing_id);

-- Append-only ledger. A NULL from = card entered the collection; NULL to = left it.
CREATE TABLE movements (
    id               INTEGER PRIMARY KEY,
    ts               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    batch_id         TEXT    NOT NULL,
    from_location_id INTEGER REFERENCES locations(id),
    to_location_id   INTEGER REFERENCES locations(id),
    printing_id      TEXT    NOT NULL REFERENCES printings(scryfall_id),
    finish           TEXT    NOT NULL,
    condition        TEXT    NOT NULL,
    lang             TEXT    NOT NULL,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    reason           TEXT    NOT NULL
        CHECK (reason IN ('import', 'acquire', 'pull', 'return', 'move', 'adjust', 'sell')),
    note             TEXT    NOT NULL DEFAULT '',
    CHECK (from_location_id IS NOT NULL OR to_location_id IS NOT NULL),
    CHECK (from_location_id IS NULL OR to_location_id IS NULL OR from_location_id <> to_location_id),
    CHECK (reason NOT IN ('import', 'acquire') OR from_location_id IS NULL),
    CHECK (reason <> 'sell' OR to_location_id IS NULL),
    CHECK (reason NOT IN ('pull', 'return', 'move')
           OR (from_location_id IS NOT NULL AND to_location_id IS NOT NULL))
);
CREATE INDEX movements_from ON movements(from_location_id);
CREATE INDEX movements_to ON movements(to_location_id);
CREATE INDEX movements_printing ON movements(printing_id);
CREATE INDEX movements_batch ON movements(batch_id);

-- Decks (used from phase 4). Each deck owns exactly one location of type 'deck'.
CREATE TABLE decks (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    format      TEXT    NOT NULL DEFAULT '',
    location_id INTEGER NOT NULL UNIQUE REFERENCES locations(id),
    notes       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE deck_slots (
    deck_id     INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    printing_id TEXT    NOT NULL REFERENCES printings(scryfall_id),
    finish      TEXT    NOT NULL CHECK (finish IN ('nonfoil', 'foil', 'etched')),
    condition   TEXT    NOT NULL DEFAULT 'NM' CHECK (condition IN ('NM', 'LP', 'MP', 'HP', 'DMG')),
    lang        TEXT    NOT NULL DEFAULT 'en',
    board       TEXT    NOT NULL DEFAULT 'main'
        CHECK (board IN ('main', 'side', 'commander', 'maybe')),
    qty_wanted  INTEGER NOT NULL CHECK (qty_wanted > 0),
    qty_proxy   INTEGER NOT NULL DEFAULT 0 CHECK (qty_proxy >= 0),
    PRIMARY KEY (deck_id, printing_id, finish, condition, lang, board)
);

-- Small key/value store (Scryfall sync state etc.).
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
