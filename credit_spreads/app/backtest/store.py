"""Local market store for backtests: daily bars, option chain snapshots (real bid/ask), earnings
dates and splits. One SQLite file per market, separate from the trading database.

Two writers fill it: the DoltHub loader (US end-of-day data) and the live recorder
(app/data/market_recorder.py), which saves every 15:00 scan's Kite quotes so NSE history with
real bid/ask, depth, OI, volume and lot sizes builds up while paper trading. The optional
tables (chain_extras, futures, events_seen, sectors, quote snapshots) are only written by the recorder;
option_candles, future_candles, option_bars, future_bars and archive_* by the history archiver."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS chains (
    date TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL, strike REAL NOT NULL,
    cp TEXT NOT NULL, bid REAL, ask REAL, iv REAL, delta REAL,
    PRIMARY KEY (date, symbol, expiration, strike, cp)
);
CREATE INDEX IF NOT EXISTS ix_chains_symbol_date ON chains (symbol, date);
CREATE TABLE IF NOT EXISTS earnings (
    symbol TEXT NOT NULL, date TEXT NOT NULL, timing TEXT,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS splits (
    symbol TEXT NOT NULL, ex_date TEXT NOT NULL, to_factor REAL, for_factor REAL,
    PRIMARY KEY (symbol, ex_date)
);
CREATE TABLE IF NOT EXISTS chain_days_loaded (      -- resumable loading, empty days included
    symbol TEXT NOT NULL, date TEXT NOT NULL, rows INTEGER NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS chain_extras (         -- recorder only: per-quote size and contract facts
    date TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL, strike REAL NOT NULL, cp TEXT NOT NULL,
    lot INTEGER, tick REAL, bid_qty INTEGER, ask_qty INTEGER, bid_depth INTEGER, ask_depth INTEGER,
    oi INTEGER, volume INTEGER,
    PRIMARY KEY (date, symbol, expiration, strike, cp)
);
CREATE TABLE IF NOT EXISTS futures (              -- recorder only: futures quotes (the forward)
    date TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL, bid REAL, ask REAL, last REAL,
    PRIMARY KEY (date, symbol, expiration)
);
CREATE TABLE IF NOT EXISTS events_seen (          -- recorder only: when each event first became known
    symbol TEXT NOT NULL, event_date TEXT NOT NULL, event_type TEXT NOT NULL, first_seen TEXT NOT NULL,
    PRIMARY KEY (symbol, event_date, event_type)
);
CREATE TABLE IF NOT EXISTS sectors (symbol TEXT PRIMARY KEY, sector TEXT);
CREATE TABLE IF NOT EXISTS option_candles (       -- archiver: Kite daily candles per option contract
    date TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL, strike REAL NOT NULL, cp TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER, oi INTEGER, lot INTEGER,
    PRIMARY KEY (date, symbol, expiration, strike, cp)
);
CREATE INDEX IF NOT EXISTS ix_option_candles_symbol ON option_candles (symbol, expiration);
CREATE TABLE IF NOT EXISTS future_candles (       -- archiver: per expiry, or expiration = 'continuous'
    date TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER, oi INTEGER,
    PRIMARY KEY (date, symbol, expiration)
);
CREATE TABLE IF NOT EXISTS archive_log (          -- archiver: what has been fetched, for resuming
    kind TEXT NOT NULL, key TEXT NOT NULL, archived_at TEXT NOT NULL, rows INTEGER NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS option_bars (          -- archiver: intraday candles, one blob per contract-day
    symbol TEXT NOT NULL, expiration TEXT NOT NULL, strike REAL NOT NULL, cp TEXT NOT NULL, date TEXT NOT NULL,
    interval TEXT NOT NULL, n INTEGER NOT NULL, lot INTEGER, data BLOB NOT NULL,   -- app/data/barcodec.py
    PRIMARY KEY (symbol, expiration, strike, cp, date)
);
CREATE INDEX IF NOT EXISTS ix_option_bars_date ON option_bars (date);
CREATE TABLE IF NOT EXISTS future_bars (          -- archiver: intraday futures candles per contract-day
    symbol TEXT NOT NULL, expiration TEXT NOT NULL, date TEXT NOT NULL,
    interval TEXT NOT NULL, n INTEGER NOT NULL, data BLOB NOT NULL,
    PRIMARY KEY (symbol, expiration, date)
);
CREATE TABLE IF NOT EXISTS archive_progress (     -- archiver: how far each contract's history is saved
    tradingsymbol TEXT PRIMARY KEY, kind TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL,
    last_date TEXT, requests INTEGER NOT NULL DEFAULT 0, final INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quote_snap_rows (      -- recorder: today's intraday quote snapshots, before compaction
    ts TEXT NOT NULL, symbol TEXT NOT NULL, expiration TEXT NOT NULL, strike REAL NOT NULL, cp TEXT NOT NULL,
    bid REAL, ask REAL, last REAL, bid_qty INTEGER, ask_qty INTEGER, bid_depth INTEGER, ask_depth INTEGER,
    volume INTEGER, oi INTEGER,
    PRIMARY KEY (ts, symbol, expiration, strike, cp)
);
CREATE TABLE IF NOT EXISTS quote_snaps (          -- recorder: intraday quote snapshots, one blob per contract-day
    symbol TEXT NOT NULL, expiration TEXT NOT NULL, strike REAL NOT NULL, cp TEXT NOT NULL, date TEXT NOT NULL,
    n INTEGER NOT NULL, data BLOB NOT NULL,       -- cp FUT or EQ for futures and the stock (strike 0)
    PRIMARY KEY (symbol, expiration, strike, cp, date)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
