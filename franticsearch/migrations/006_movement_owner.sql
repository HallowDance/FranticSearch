-- Each movement belongs to one user (moves never cross collections): store it, so
-- a user's history is an index range instead of a scan of everyone's ledger.
-- NULL only for data from before users existed; the first admin adopts it.
ALTER TABLE movements ADD COLUMN owner_id INTEGER REFERENCES users(id);
UPDATE movements SET owner_id = (
    SELECT l.owner_id FROM locations l
     WHERE l.id = COALESCE(movements.from_location_id, movements.to_location_id));
CREATE INDEX movements_owner ON movements(owner_id, id);
