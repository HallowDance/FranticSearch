-- Batch undo: an undo writes compensating movements tagged with the batch they reverse.
ALTER TABLE movements ADD COLUMN undo_of TEXT;
CREATE INDEX movements_undo_of ON movements(undo_of);
