-- Locations that held cards can't be deleted (the ledger refers to them);
-- once empty they can be archived: hidden from lists and forms, kept in history.
ALTER TABLE locations ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1));
