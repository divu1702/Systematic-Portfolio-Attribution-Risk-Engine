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


def trading_days(store_path: Path, start: date, end: date, min_share: float = 0.5) -> List[date]:
    """Days in range where most symbols have a bar and option chains were loaded."""
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        n_sym = conn.execute("SELECT COUNT(DISTINCT symbol) FROM bars").fetchone()[0] or 1
        bars = {r[0]: r[1] for r in conn.execute(
            "SELECT date, COUNT(*) FROM bars WHERE date BETWEEN ? AND ? GROUP BY date", (str(start), str(end)))}
        chains = {r[0] for r in conn.execute(
            "SELECT date FROM chain_days_loaded WHERE rows > 0 AND date BETWEEN ? AND ? GROUP BY date "
            "HAVING COUNT(*) >= ?", (str(start), str(end), max(1, int(n_sym * min_share))))}
    finally:
        conn.close()
    return sorted(date.fromisoformat(d) for d, n in bars.items() if n >= n_sym * min_share and d in chains)


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
    universe = universe or read_universe(UNIVERSE_US)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    clock = SimClock(datetime.combine(first_day, time(8, 0)))
    broker = HistoricalBroker(store_path, clock, list(universe), r=cfg.analytics.risk_free)
    engine = init_db(db_path)
    set_currency(cfg.market.currency)
    ctx = Context(cfg, {}, clock, engine, broker, LogNotifier(keep=20000),
                  nse=HistoricalExchange(store_path, clock, list(universe), universe, notice_days))
    ctx.banner = (f"Backtest on real end-of-day US option quotes (DoltHub), {len(universe)} stocks. "
                  "Stops checked once a day at the close; fills at closing bid/ask plus slippage.")
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
            (run.entries if m.startswith("ENTRY") else run.rejections).append(f"{d} {m}")
        ctx.clock.set(datetime.combine(d, time(15, 35)))
        eod_job(ctx)
        if progress:
            progress(i + 1, len(days))
    run.seconds = _time.time() - t0
    return run
