"""Local market store for backtests: daily bars, option chain snapshots (real bid/ask), earnings
dates and splits. One SQLite file per market, separate from the trading database.

Two writers fill it: the DoltHub loader (US end-of-day data) and the live recorder
(app/data/market_recorder.py), which saves every 15:00 scan's Kite quotes so NSE history with
real bid/ask, depth, OI, volume and lot sizes builds up while paper trading. The optional
tables (chain_extras, futures, events_seen, sectors) are only written by the recorder."""

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
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
