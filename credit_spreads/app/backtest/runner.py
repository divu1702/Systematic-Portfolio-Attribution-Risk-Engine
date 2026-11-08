"""End-of-day replay of real market data through the live code path.

Each trading day: morning refresh (instrument master, holidays, sectors, events) -> monitor open
spreads at the close (all exit rules, the close doubling as the 15:15 thesis check) -> scan and
paper entries -> end-of-day job (candles, levels, equity). The first `warmup` days scan without
entering, to build the IV history that F4 and F5 need.

Limits of an end-of-day replay: stops and targets are checked once a day at the close (live checks
every 5 minutes), there is no 09:20 gap check, and fills use closing bid/ask plus the configured
slippage. Order-book size is not in the data, so depth never binds.
"""

from __future__ import annotations

import logging
import sqlite3
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from sqlalchemy import delete

from ..clock import SimClock
from ..config import Config, CostsCfg, MarketCfg
from ..context import Context
from ..data.db import init_db
from ..data.models import UnderlyingDaily
from ..fmt import set_currency
from ..jobs import eod_job, monitor_job, morning_refresh, scan_job
from ..reporting.notifier import LogNotifier
from ..risk.limits import open_trades
from .broker import HistoricalBroker, HistoricalExchange, read_universe

log = logging.getLogger(__name__)

UNIVERSE_US = Path(__file__).resolve().parent / "us_universe.csv"


def us_config(cfg: Config, capital: float = 100_000.0, per_contract: float = 0.70) -> Config:
    """US market profile: dollars, per-contract fees (commission + exchange/regulatory fees),
    a US risk-free rate, and no candle replay (end-of-day data only). Strategy thresholds are
    left exactly as configured."""
    return cfg.model_copy(update={
        "capital": float(capital),
        "market": MarketCfg(name="us", currency="USD"),
        "analytics": cfg.analytics.model_copy(update={"risk_free": 0.045}),
        "costs": CostsCfg(model="per_contract", per_contract=per_contract, brokerage_per_order=0,
                          stt_sell_premium=0, exchange_txn_pct=0, sebi_fee_per_crore=0, stamp_buy_pct=0, gst=0),
        "monitor": cfg.monitor.model_copy(update={"replay_missed": False}),
    })


def nse_config(cfg: Config, capital: Optional[float] = None) -> Config:
    """Indian profile for replaying the recorded NSE store: the configured rupee capital and Zerodha
    costs, end-of-day replay only, and no recording/backups/heartbeat from inside a backtest."""
    return cfg.model_copy(update={
        "capital": float(capital or cfg.capital),
        "monitor": cfg.monitor.model_copy(update={"replay_missed": False}),
        "ops": cfg.ops.model_copy(update={"record_market": False, "backup_keep": 0, "heartbeat": False}),
    })


def store_universe(store_path: Path) -> Dict[str, str]:
    """Symbols (and recorded sectors) present in a market store."""
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        secs = dict(conn.execute("SELECT symbol, sector FROM sectors").fetchall())
        syms = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM chain_days_loaded WHERE rows > 0")]
    finally:
        conn.close()
    return {s: secs.get(s, "UNKNOWN") for s in syms}


def trading_days(store_path: Path, start: date, end: date, min_share: float = 0.5,
                 symbols: Optional[Sequence[str]] = None) -> List[date]:
    """Days in range where most of the universe's symbols have a bar and loaded option chains."""
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        if symbols is None:
            symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM bars")]
        keep = set(symbols)
        need = max(1, int(len(keep) * min_share))
        bars: Dict[str, int] = {}
        for d, sym in conn.execute("SELECT date, symbol FROM bars WHERE date BETWEEN ? AND ?", (str(start), str(end))):
            if sym in keep:
                bars[d] = bars.get(d, 0) + 1
        chains: Dict[str, int] = {}
        for d, sym in conn.execute("SELECT date, symbol FROM chain_days_loaded WHERE rows > 0 AND date BETWEEN ? AND ?",
                                   (str(start), str(end))):
            if sym in keep:
                chains[d] = chains.get(d, 0) + 1
    finally:
        conn.close()
    return sorted(date.fromisoformat(d) for d, n in bars.items() if n >= need and chains.get(d, 0) >= need)


@dataclass
class BacktestRun:
    ctx: Context
    days: List[date]
    warmup: int
    seconds: float = 0.0
    entries: List[str] = field(default_factory=list)
    rejections: List[str] = field(default_factory=list)
    exits: List[str] = field(default_factory=list)


def make_context(cfg: Config, store_path: Path, db_path: Path, first_day: date,
                 universe: Optional[Dict[str, str]] = None, notice_days: int = 14) -> Context:
    universe = universe or (read_universe(UNIVERSE_US) if cfg.market.name == "us" else store_universe(store_path))
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    clock = SimClock(datetime.combine(first_day, time(8, 0)))
    us = cfg.market.name == "us"
    broker = HistoricalBroker(store_path, clock, list(universe), r=cfg.analytics.risk_free,
                              lot=100 if us else 1, tick=0.01 if us else 0.05)
    engine = init_db(db_path)
    set_currency(cfg.market.currency)
    ctx = Context(cfg, {}, clock, engine, broker, LogNotifier(keep=20000),
                  nse=HistoricalExchange(store_path, clock, list(universe), universe, notice_days))
    ctx.banner = (f"Backtest on real end-of-day US option quotes (DoltHub), {len(universe)} stocks. "
                  "Stops checked once a day at the close; fills at closing bid/ask plus slippage." if us else
                  f"Backtest on NSE quotes recorded by this system at each 15:00 scan, {len(universe)} stocks. "
                  "Stops checked once a day; fills at recorded bid/ask with recorded depth.")
    return ctx


def run_backtest(ctx: Context, days: Sequence[date], warmup: int,
                 progress: Optional[Callable[[int, int], None]] = None) -> BacktestRun:
    run = BacktestRun(ctx, list(days), warmup)
    t0 = _time.time()
    broker: HistoricalBroker = ctx.broker
    for i, d in enumerate(days):
        # A split rewrites price history: drop stored bars so they are reloaded split-adjusted.
        for sym in broker.splits_on(d):
            with ctx.engine.begin() as conn:
                conn.execute(delete(UnderlyingDaily).where(UnderlyingDaily.symbol == sym))
            ctx.cache = {k: v for k, v in ctx.cache.items() if not k.startswith("levels_")}
        ctx.clock.set(datetime.combine(d, time(8, 45)))
        morning_refresh(ctx)
        ctx.clock.set(datetime.combine(d, time(15, 20)))
        if open_trades(ctx.engine):
            run.exits += monitor_job(ctx, "thesis")
        ctx.clock.set(datetime.combine(d, time(15, 25)))
        res = scan_job(ctx, enter=i >= warmup)
        for m in res.entry_messages:
            if m.startswith("["):                 # paper sleeves: summarised per book at the end
                continue
            (run.entries if m.startswith("ENTRY") else run.rejections).append(f"{d} {m}")
        ctx.clock.set(datetime.combine(d, time(15, 35)))
        eod_job(ctx)
        if progress:
            progress(i + 1, len(days))
    run.seconds = _time.time() - t0
    return run
