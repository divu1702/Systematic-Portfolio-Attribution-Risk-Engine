-- 0004: trade proposals awaiting the owner's one-tap Telegram approval (live mode, v1 is
-- semi-automatic). Paper mode enters without approval.
CREATE TABLE proposals (
    id           INTEGER PRIMARY KEY,
    ts           DATETIME NOT NULL,
    symbol       TEXT     NOT NULL,
    side         TEXT     NOT NULL,
    summary      TEXT     NOT NULL,
    payload_json TEXT     NOT NULL,
    status       TEXT     NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_at   DATETIME,
    expires_at   DATETIME NOT NULL
);
CREATE INDEX ix_proposals_status ON proposals (status);
