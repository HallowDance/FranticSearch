-- Wishlist: one per user, separate from decks and their buy list (PLAN.md § Wishlist).
-- A line wants a card in any printing (printing_id NULL) or a specific printing;
-- finish NULL = any finish. Identical lines are merged (qty added), hence the
-- unique index over the NULL-able columns.
CREATE TABLE wishes (
    id          INTEGER PRIMARY KEY,
    owner_id    INTEGER NOT NULL REFERENCES users(id),
    card_key    TEXT    NOT NULL,            -- oracle id, else the printing's Scryfall id
    printing_id TEXT    REFERENCES printings(scryfall_id),
    finish      TEXT    CHECK (finish IS NULL OR finish IN ('nonfoil', 'foil', 'etched')),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE UNIQUE INDEX wishes_line ON wishes(owner_id, card_key, COALESCE(printing_id, ''), COALESCE(finish, ''));

-- Who can see a user's wishlist (as for locations). No row = private.
CREATE TABLE wishlists (
    owner_id    INTEGER PRIMARY KEY REFERENCES users(id),
    visibility  TEXT    NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'users', 'link')),
    share_token TEXT    UNIQUE
);
