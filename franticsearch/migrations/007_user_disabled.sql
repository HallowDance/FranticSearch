-- Admins can disable an account: no login, sessions end, and what it shared is
-- hidden from others (People, card pages, shared views, share links) until it's
-- enabled again. Nothing is deleted.
ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1));
