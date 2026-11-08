-- 0007: candidate strategies run side by side as separate paper books. Every candidate, trade and
-- equity point belongs to a strategy; rows from before this migration belong to the main rules.
ALTER TABLE candidates ADD COLUMN strategy TEXT NOT NULL DEFAULT 'main';
ALTER TABLE trades     ADD COLUMN strategy TEXT NOT NULL DEFAULT 'main';
CREATE INDEX ix_trades_strategy ON trades (strategy, status);
CREATE INDEX ix_candidates_strategy ON candidates (strategy, scan_id);

CREATE TABLE equity_by_strategy (
    date        DATE NOT NULL,
    strategy    TEXT NOT NULL DEFAULT 'main',
    equity      REAL NOT NULL,
    realised    REAL NOT NULL,
    unrealised  REAL NOT NULL,
    margin_used REAL NOT NULL,
    peak        REAL NOT NULL,
    drawdown    REAL NOT NULL,
    PRIMARY KEY (date, strategy)
);
INSERT INTO equity_by_strategy (date, strategy, equity, realised, unrealised, margin_used, peak, drawdown)
    SELECT date, 'main', equity, realised, unrealised, margin_used, peak, drawdown FROM equity;
DROP TABLE equity;
ALTER TABLE equity_by_strategy RENAME TO equity;
