-- 0001: initial schema, one table per entry in the spec's data model.
-- Conventions: timestamps are naive IST; money is rupees; prices are per share;
-- JSON blobs are TEXT. app/data/models.py must stay in sync (tests/test_db.py checks).

CREATE TABLE instruments (
    id               INTEGER PRIMARY KEY,
    date             DATE    NOT NULL,          -- instrument-master snapshot date
    instrument_token INTEGER,
    exchange         TEXT    NOT NULL,          -- NFO, NSE
    tradingsymbol    TEXT    NOT NULL,
    underlying       TEXT    NOT NULL,
    instrument_type  TEXT    NOT NULL,          -- CE, PE, FUT, EQ
    expiry           DATE,
    strike           REAL,
    lot_size         INTEGER NOT NULL,
    tick_size        REAL    NOT NULL,
    UNIQUE (date, exchange, tradingsymbol)
);
CREATE INDEX ix_instruments_chain ON instruments (date, underlying, expiry);

CREATE TABLE underlying_daily (
    id            INTEGER PRIMARY KEY,
    date          DATE    NOT NULL,
    symbol        TEXT    NOT NULL,
    open          REAL    NOT NULL,
    high          REAL    NOT NULL,
    low           REAL    NOT NULL,
    close         REAL    NOT NULL,
    volume        INTEGER,
    atr14         REAL,
    hv20          REAL,
    hv_pctile_252 REAL,
    UNIQUE (date, symbol)
);

CREATE TABLE iv_snapshots (
    id          INTEGER PRIMARY KEY,
    ts          DATETIME NOT NULL,
    date        DATE     NOT NULL,              -- one snapshot per symbol per day
    symbol      TEXT     NOT NULL,
    expiry      DATE     NOT NULL,
    fut_price   REAL     NOT NULL,
    atm_iv      REAL     NOT NULL,
    iv_25d_put  REAL,
    iv_25d_call REAL,
    ivp_252     REAL,
    method      TEXT CHECK (method IN ('true', 'proxy')),
    UNIQUE (date, symbol)
);

CREATE TABLE levels (
    id        INTEGER PRIMARY KEY,
    date      DATE    NOT NULL,
    symbol    TEXT    NOT NULL,
    price     REAL    NOT NULL,
    kind      TEXT    NOT NULL CHECK (kind IN ('support', 'resistance')),
    timeframe TEXT    NOT NULL,                 -- daily, weekly, manual
    touches   INTEGER NOT NULL DEFAULT 0,
    score     REAL    NOT NULL DEFAULT 0,
    source    TEXT    NOT NULL CHECK (source IN ('auto', 'manual'))
);
CREATE INDEX ix_levels_symbol_date ON levels (symbol, date);

CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT     NOT NULL,
    event_date  DATE     NOT NULL,
    event_type  TEXT     NOT NULL,              -- board_meeting, results, agm, record_date, ...
    description TEXT,
    source      TEXT     NOT NULL,              -- nse, manual
    fetched_at  DATETIME NOT NULL,
    UNIQUE (symbol, event_date, event_type, source)
);
CREATE INDEX ix_events_symbol_date ON events (symbol, event_date);

CREATE TABLE candidates (
    id            INTEGER PRIMARY KEY,
    ts            DATETIME NOT NULL,
    scan_id       TEXT     NOT NULL,            -- groups every candidate from one scan run
    symbol        TEXT     NOT NULL,
    side          TEXT CHECK (side IN ('bull_put', 'bear_call')),
    short_k       REAL,
    long_k        REAL,
    expiry        DATE,
    dte           INTEGER,
    lot_size      INTEGER,
    credit_nat    REAL,                         -- per share, natural prices, after costs
    width         REAL,
    max_loss      REAL,                         -- rupees per lot
    p_market      REAL,
    p_model       REAL,
    ev            REAL,                         -- rupees per lot
    ev_ratio      REAL,                         -- ev / max_loss
    f1_pass       BOOLEAN,                      -- NULL = filter not evaluated
    f2_pass       BOOLEAN,
    f3_pass       BOOLEAN,
    f4_pass       BOOLEAN,
    f5_pass       BOOLEAN,
    f6_pass       BOOLEAN,
    f7_pass       BOOLEAN,
    f8_pass       BOOLEAN,
    f9_pass       BOOLEAN,
    accepted      BOOLEAN  NOT NULL DEFAULT 0,
    reject_reason TEXT,
    inputs_json   TEXT                          -- every input used, for later threshold tuning
);
CREATE INDEX ix_candidates_scan ON candidates (scan_id);
CREATE INDEX ix_candidates_symbol_ts ON candidates (symbol, ts);

CREATE TABLE trades (
    trade_id        TEXT PRIMARY KEY,
    mode            TEXT     NOT NULL CHECK (mode IN ('paper', 'live')),
    status          TEXT     NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    candidate_id    INTEGER REFERENCES candidates (id),
    symbol          TEXT     NOT NULL,
    sector          TEXT,
    side            TEXT     NOT NULL CHECK (side IN ('bull_put', 'bear_call')),
    expiry          DATE     NOT NULL,
    legs_json       TEXT     NOT NULL,
    lots            INTEGER  NOT NULL,
    lot_size        INTEGER  NOT NULL,
    entry_ts        DATETIME NOT NULL,
    entry_credit    REAL     NOT NULL,          -- per share, at actual fills
    costs           REAL     NOT NULL DEFAULT 0, -- rupees, entry
    max_loss        REAL     NOT NULL,          -- rupees, whole position
    margin          REAL,
    margin_fallback BOOLEAN  NOT NULL DEFAULT 0, -- basket-margin API failed; max_loss x mult used
    iv_at_entry     REAL,
    hv20            REAL,
    ivp             REAL,
    ivp_method      TEXT CHECK (ivp_method IN ('true', 'proxy')),
    level_ref       REAL,
    exit_ts         DATETIME,
    exit_cost       REAL,                       -- per share, cost to close at actual fills
    exit_costs      REAL,                       -- rupees, exit
    exit_reason     TEXT,
    pnl_gross       REAL,
    pnl_net         REAL,
    mae             REAL,
    mfe             REAL,
    gap_flag        BOOLEAN  NOT NULL DEFAULT 0
);
CREATE INDEX ix_trades_status ON trades (status);

CREATE TABLE marks (
    id             INTEGER PRIMARY KEY,
    ts             DATETIME NOT NULL,
    trade_id       TEXT     NOT NULL REFERENCES trades (trade_id),
    spot           REAL,
    legs_json      TEXT,                        -- bid/ask per leg
    cost_to_close  REAL,                        -- per share, natural prices
    unrealised_pnl REAL
);
CREATE INDEX ix_marks_trade_ts ON marks (trade_id, ts);

CREATE TABLE orders (
    order_id   TEXT PRIMARY KEY,
    trade_id   TEXT     REFERENCES trades (trade_id),
    purpose    TEXT     NOT NULL CHECK (purpose IN ('entry', 'exit')),
    leg        TEXT     NOT NULL CHECK (leg IN ('short', 'long')),
    side       TEXT     NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty        INTEGER  NOT NULL,
    limit_px   REAL,
    status     TEXT     NOT NULL,
    fill_px    REAL,
    broker_ref TEXT,
    ts         DATETIME NOT NULL
);
CREATE INDEX ix_orders_trade ON orders (trade_id);

CREATE TABLE equity (
    date        DATE PRIMARY KEY,
    equity      REAL NOT NULL,
    realised    REAL NOT NULL,
    unrealised  REAL NOT NULL,
    margin_used REAL NOT NULL,
    peak        REAL NOT NULL,
    drawdown    REAL NOT NULL
);
