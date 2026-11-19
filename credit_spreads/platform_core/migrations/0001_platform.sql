-- 0001: the shared paper broker (accounts, orders, ledger), every account's trades and marks, and the
-- level system's levels, setups, screener and close re-checks (Level-Based Credit Spreads spec).
CREATE TABLE accounts (
    id              TEXT PRIMARY KEY,         -- oi_tight, oi_loose, level_strict, level_research, discretionary
    strategy        TEXT NOT NULL,
    opening_capital REAL NOT NULL,
    start_date      DATE                      -- go_live_date; NULL until the owner sets it
);

CREATE TABLE trades (
    id              TEXT PRIMARY KEY,
    account         TEXT NOT NULL REFERENCES accounts(id),
    source          TEXT NOT NULL,            -- system | discretionary | oi_system
    status          TEXT NOT NULL DEFAULT 'open',
    symbol          TEXT NOT NULL,
    sector          TEXT,
    setup_tag       TEXT,                     -- 1-4 or none (level, discretionary); the book name (oi accounts)
    setup_id        INTEGER,
    direction       TEXT NOT NULL,            -- bear_call | bull_put
    legs_json       TEXT NOT NULL,
    expiry          DATE NOT NULL,
    lots            INTEGER NOT NULL,
    lot_size        INTEGER NOT NULL,
    credit          REAL NOT NULL,            -- per share, from the fills, before charges
    entry_charges   REAL NOT NULL DEFAULT 0,
    margin          REAL,
    rom             REAL,                     -- (credit x quantity - round-trip charges) / margin, at entry
    level           REAL,                     -- kill level (level system), stop level (discretionary)
    loss_at_kill    REAL,                     -- per lot, at entry
    one_r           REAL,                     -- rupees
    max_loss        REAL NOT NULL,            -- rupees, whole position
    entry_ts        DATETIME NOT NULL,
    exit_ts         DATETIME,
    exit_reason     TEXT,
    exit_cost       REAL,                     -- per share paid to close
    exit_charges    REAL,
    pnl_inr         REAL,
    pnl_r           REAL,
    holding_days    INTEGER,
    book            TEXT,                     -- strict | research (level system)
    failed_filters  TEXT,                     -- JSON {filter: actual value}
    nifty_trend     TEXT,
    owner_stop      REAL,
    reason          TEXT,
    context_json    TEXT
);
CREATE INDEX ix_trades_account ON trades (account, status);

CREATE TABLE orders (
    id               TEXT PRIMARY KEY,
    account          TEXT NOT NULL REFERENCES accounts(id),
    trade_id         TEXT,
    purpose          TEXT NOT NULL,           -- entry | exit | settlement
    legs_json        TEXT NOT NULL,           -- per leg: symbol, side, qty, requested (natural) and filled price
    status           TEXT NOT NULL,           -- filled | rejected
    rejection_reason TEXT,
    ts               DATETIME NOT NULL
);
CREATE INDEX ix_orders_account ON orders (account, ts);

CREATE TABLE ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL REFERENCES accounts(id),
    ts              DATETIME NOT NULL,
    entry_type      TEXT NOT NULL,            -- opening | premium | charges | settlement | realised_pnl | margin_block | margin_release | margin_adjust
    amount          REAL NOT NULL,
    cash_balance    REAL NOT NULL,
    used_margin     REAL NOT NULL,
    trade_id        TEXT,
    note            TEXT
);
CREATE INDEX ix_ledger_account ON ledger (account, id);

CREATE TABLE marks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              DATETIME NOT NULL,
    trade_id        TEXT NOT NULL REFERENCES trades(id),
    spot            REAL,
    legs_json       TEXT,                     -- bid and ask of each leg
    cost_to_close   REAL                      -- per share, natural prices
);
CREATE INDEX ix_marks_trade ON marks (trade_id, ts);

CREATE TABLE levels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE NOT NULL,
    symbol          TEXT NOT NULL,
    setup           INTEGER NOT NULL,
    level           REAL NOT NULL,
    direction       TEXT NOT NULL,
    day0_date       DATE NOT NULL,
    volume_mult     REAL,
    vwap_dist_atr   REAL,
    hvn_dist_atr    REAL,
    breakout_low    REAL,
    setup_id        INTEGER
);
CREATE INDEX ix_levels_symbol ON levels (symbol, date);

CREATE TABLE setups (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    setup             INTEGER NOT NULL,
    direction         TEXT NOT NULL,          -- bear_call | bull_put
    state             TEXT NOT NULL,          -- armed | confirming | confirmed | cancelled | expired
    level             REAL NOT NULL,
    day0_date         DATE NOT NULL,
    route             TEXT,                   -- Setup 2: A (retest) or B (acceptance)
    window_start      DATE,                   -- day after which the confirmation window runs
    expiry            DATE,                   -- Day 0's 15-day-rule expiry: the zone OI test uses it throughout
    zone_json         TEXT,
    day0_oi_json      TEXT,
    zone_oi_share     REAL,
    local_pcr         REAL,
    sessions_json     TEXT,                   -- per session: close, beyond the level, OI test values, books
    volume_mult       REAL,
    failed_filters    TEXT,
    signals_json      TEXT,                   -- strength signals (logged, never used by the rules)
    strict_trade_id   TEXT,
    research_trade_id TEXT,
    note              TEXT,
    created_at        DATETIME NOT NULL,
    updated_at        DATETIME NOT NULL
);
CREATE INDEX ix_setups_state ON setups (state, symbol);

CREATE TABLE screener_daily (
    date            DATE NOT NULL,
    symbol          TEXT NOT NULL,
    event_flag      BOOLEAN,
    liquidity_flag  BOOLEAN,
    rom_call        REAL,
    rom_put         REAL,
    rank            INTEGER,
    detail_json     TEXT,
    PRIMARY KEY (date, symbol)
);

CREATE TABLE close_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            DATE NOT NULL,
    kind            TEXT NOT NULL,            -- day0 | confirmation | kill
    ref             TEXT,                     -- setup or trade id
    symbol          TEXT,
    snapshot_value  REAL,
    official_value  REAL,
    mismatch        BOOLEAN NOT NULL,
    note            TEXT
);

CREATE TABLE uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              DATETIME NOT NULL,
    kind            TEXT NOT NULL,
    filename        TEXT NOT NULL,
    rows            INTEGER
);

CREATE TABLE platform_state (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      DATETIME
);
