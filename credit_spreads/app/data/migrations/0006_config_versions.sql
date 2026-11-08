-- 0006: tie every candidate and trade to the exact thresholds that produced it, so threshold
-- changes (made in batches, per the tuning rule) can be compared cleanly.
CREATE TABLE config_versions (
    hash        TEXT PRIMARY KEY,
    first_seen  DATETIME NOT NULL,
    config_json TEXT NOT NULL
);
ALTER TABLE candidates ADD COLUMN config_hash TEXT;
ALTER TABLE trades     ADD COLUMN config_hash TEXT;
