"""Move what the earlier archiver saved (data/market_nse.db, one compressed blob per contract-day) into
the repository, once. Its per-contract progress comes along, so nothing already saved is fetched again.
The earlier store is left as it is."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data import barcodec
from app.data.models import Instrument

from .store import Lake

BATCH = 400_000


def kite_name(symbol: str, expiry: date, strike: Optional[float] = None, cp: Optional[str] = None) -> str:
    """Zerodha's name for a monthly stock contract, e.g. ANGELONE26NOV380CE or ANGELONE26NOVFUT."""
    base = f"{symbol}{expiry:%y}{expiry.strftime('%b').upper()}"
    if cp is None:
        return base + "FUT"
    k = int(strike) if float(strike).is_integer() else float(strike)
    return f"{base}{k}{cp}"


def _names(ctx) -> Dict[Tuple, str]:
    with Session(ctx.engine) as s:
        rows = s.execute(select(Instrument.underlying, Instrument.expiry, Instrument.strike, Instrument.instrument_type,
                                Instrument.tradingsymbol).where(Instrument.instrument_type.in_(("CE", "PE", "FUT"))))
        return {(u, e, round(float(k or 0), 2), t): ts for u, e, k, t, ts in rows}


def _candles(d: str, blob: bytes) -> pd.DataFrame:
    c = barcodec.decode(blob)
    base = np.datetime64(d, "m")
    ts = base + c["minute"].astype("timedelta64[m]")
    return pd.DataFrame({"ts": ts.astype("datetime64[s]"), "open": c["open"], "high": c["high"], "low": c["low"],
                         "close": c["close"], "volume": c["volume"].astype("int64"), "oi": c["oi"].astype("int64")})


def migrate(p, store_path: Optional[Path] = None, force: bool = False) -> str:
    ctx = p.ctx
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    if lake.catalog.get_meta("migrated_store") == "done" and not force:
        return "already migrated"
    path = Path(store_path) if store_path else ctx.cfg.resolve(ctx.cfg.paths.market_store)
    if not path.exists():
        lake.catalog.set_meta("migrated_store", "done")
        return f"nothing to migrate ({path} not found)"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = _names(ctx)
    counts = {"options": 0, "futures": 0, "skipped": 0}
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, dataset in (("option_bars", "options_1m"), ("future_bars", "futures_1m")):
            if table not in tables:
                continue
            is_opt = table == "option_bars"
            cols = "symbol, expiration, strike, cp, date, interval, data" if is_opt else \
                "symbol, expiration, NULL, NULL, date, interval, data"
            frames, n = [], 0
            for sym, exp, k, cp, d, interval, blob in conn.execute(f"SELECT {cols} FROM {table}"):
                if interval != "minute":
                    counts["skipped"] += 1
                    continue
                e = date.fromisoformat(exp)
                df = _candles(d, blob)
                key = (sym, e, round(float(k or 0), 2), cp if is_opt else "FUT")
                df["tradingsymbol"] = names.get(key) or kite_name(sym, e, k, cp if is_opt else None)
                df["expiry"] = e
                if is_opt:
                    df["underlying"], df["strike"], df["cp"] = sym, float(k), cp
                else:
                    df["underlying"] = sym
                frames.append(df)
                n += len(df)
                counts["options" if is_opt else "futures"] += len(df)
                if n >= BATCH:
                    lake.write(dataset, pd.concat(frames, ignore_index=True), tag="migrated")
                    frames, n = [], 0
            if frames:
                lake.write(dataset, pd.concat(frames, ignore_index=True), tag="migrated")
        if "archive_progress" in tables:
            rows = conn.execute("SELECT tradingsymbol, kind, symbol, expiration, last_date, requests, final "
                                "FROM archive_progress").fetchall()
            lake.catalog.executemany(
                "INSERT OR IGNORE INTO contracts (tradingsymbol, kind, underlying, expiry, last_fetched, passes, final, "
                "requests) VALUES (?,?,?,?,?,?,?,?)",
                [(ts, kind, sym, exp, last, 3 if final == 1 else 1, 1 if final == 1 else (2 if final == 2 else 0), req)
                 for ts, kind, sym, exp, last, req, final in rows])
    finally:
        conn.close()
    lake.catalog.set_meta("migrated_store", "done")
    return (f"migrated {counts['options']:,} option and {counts['futures']:,} futures candles from {path.name}"
            + (f"; {counts['skipped']:,} contract-days at other intervals left in place" if counts["skipped"] else ""))
