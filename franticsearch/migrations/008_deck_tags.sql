-- Tags on the cards of a deck (any number per card), for grouping and sorting
-- the deck page. Keyed on the card (oracle id, else the printing's Scryfall id,
-- as ``card_key`` elsewhere), not the slot: tags survive printing and board
-- changes, and a card removed and put back keeps them. Tags are matched
-- case-insensitively; the first spelling used in a deck is kept.
CREATE TABLE deck_tags (
    deck_id  INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    card_key TEXT    NOT NULL,
    tag      TEXT    NOT NULL COLLATE NOCASE,
    PRIMARY KEY (deck_id, card_key, tag)
);
