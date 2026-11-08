"""Local market store for backtests: daily bars, end-of-day option chains (real bid/ask), earnings
dates and splits. One SQLite file per market, separate from the trading database."""

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
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
