-- Phase 2b: users with separate collections; locations can be shared.
-- See PLAN.md "Users & sharing". Runs with foreign keys off (table rebuild);
-- the runner checks foreign keys before committing.

CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Rebuild locations: owner, visibility, share token; names unique per owner.
-- owner_id is NULL only for data from before users existed; the first admin adopts it.
CREATE TABLE locations_new (
    id          INTEGER PRIMARY KEY,
    owner_id    INTEGER REFERENCES users(id),
    name        TEXT    NOT NULL COLLATE NOCASE,
    type        TEXT    NOT NULL CHECK (type IN ('binder', 'box', 'deck', 'lent', 'other')),
    notes       TEXT    NOT NULL DEFAULT '',
    archived    INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    visibility  TEXT    NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'users', 'link')),
    share_token TEXT    UNIQUE,
    UNIQUE (owner_id, name)
);
INSERT INTO locations_new(id, name, type, notes, archived)
    SELECT id, name, type, notes, archived FROM locations;
DROP TABLE locations;
ALTER TABLE locations_new RENAME TO locations;
CREATE INDEX locations_owner ON locations(owner_id);

-- Rebuild imports: owner; the duplicate check (sha256) is per owner, so two users
-- can import the same deck list.
CREATE TABLE imports_new (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    kind        TEXT    NOT NULL CHECK (kind IN ('collection', 'deck')),
    filename    TEXT    NOT NULL DEFAULT '',
    sha256      TEXT    NOT NULL,
    owner_id    INTEGER REFERENCES users(id),
    location_id INTEGER REFERENCES locations(id),
    deck_id     INTEGER REFERENCES decks(id) ON DELETE CASCADE,
    batch_id    TEXT,
    lines       INTEGER NOT NULL DEFAULT 0,
    imported    INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    CHECK ((kind = 'collection') = (location_id IS NOT NULL)),
    UNIQUE (owner_id, sha256)
);
INSERT INTO imports_new(id, ts, kind, filename, sha256, location_id, deck_id, batch_id, lines, imported, skipped)
    SELECT id, ts, kind, filename, sha256, location_id, deck_id, batch_id, lines, imported, skipped FROM imports;
DROP TABLE imports;
ALTER TABLE imports_new RENAME TO imports;
