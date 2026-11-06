-- 0002: reference data that the scanner and risk checks need beyond the spec's data model.

CREATE TABLE event_fetches (           -- one row per fetch attempt; F2 fails closed when stale
    id         INTEGER PRIMARY KEY,
    fetched_at DATETIME NOT NULL,
    source     TEXT     NOT NULL,
    ok         BOOLEAN  NOT NULL,
    rows       INTEGER  NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX ix_event_fetches_ok ON event_fetches (ok, fetched_at);

CREATE TABLE sectors (                 -- for risk.max_per_sector
    symbol     TEXT PRIMARY KEY,
    sector     TEXT     NOT NULL,
    source     TEXT     NOT NULL,      -- nse, manual
    updated_at DATETIME NOT NULL
);

CREATE TABLE holidays (                -- NSE F&O trading holidays
    date        DATE PRIMARY KEY,
    description TEXT,
    source      TEXT NOT NULL          -- nse, manual
);

CREATE TABLE option_turnover (         -- F1: daily stock-option premium turnover per underlying
    id       INTEGER PRIMARY KEY,
    date     DATE NOT NULL,
    symbol   TEXT NOT NULL,
    turnover REAL NOT NULL,
    source   TEXT NOT NULL,
    UNIQUE (date, symbol)
);

CREATE TABLE system_state (            -- kill switch, drawdown pause, peak equity, ...
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at DATETIME NOT NULL
);
