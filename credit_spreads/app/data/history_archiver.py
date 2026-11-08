"""Kite history archiver: build an Indian option history while it is still available.

Kite serves daily candles only for *listed* contracts; once a monthly series expires its option
history is gone (only futures survive, as a "continuous" series). So the archiver:

1. Backfills, once: up to `archive.equity_days` of daily stock bars; each stock's continuous
   futures series; and daily candles (OHLC, volume, OI) since listing for every listed option
   within +-`archive.band` of the stock price, plus each listed futures contract.
2. Tops up each expiring monthly series in its last `archive.days_before_expiry` trading days,
   so the whole life of every series is saved before Kite drops it.

Candles carry trade prices, not quotes; the live recorder (market_recorder.py) adds real bid/ask.
Work is ordered by urgency (expiring series first) and capped at `archive.max_requests_per_run`
broker calls per run, so the nightly job finishes the backfill over a few evenings; everything is
resumable via the store's archive_log. Kite's 3 requests a second limit is enforced by the broker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from ..backtest.store import connect
from .calendar import TradingCalendar
from .models import Instrument

log = logging.getLogger(__name__)


@dataclass
class ArchiveReport:
    requests: int = 0
    equities: int = 0
    futures: int = 0
    options: int = 0
    candles: int = 0
    pending: int = 0                       # items still to fetch when the budget ran out
    expiring: List[str] = field(default_factory=list)

    def text(self) -> str:
        s = (f"archive: {self.requests} requests, {self.candles:,} candles saved "
             f"({self.equities} stocks, {self.futures} futures, {self.options} option contracts)")
        if self.expiring:
            s += f"; expiring series archived: {', '.join(self.expiring)}"
        if self.pending:
            s += f"; {self.pending:,} items left for the next run"
        return s


def _store(ctx):
    return connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))


def _done(store, kind: str) -> Dict[str, str]:
    return {k: a for k, a in store.execute("SELECT key, archived_at FROM archive_log WHERE kind=?", (kind,))}


def _mark(store, kind: str, key: str, d: date, rows: int) -> None:
    store.execute("INSERT OR REPLACE INTO archive_log VALUES (?,?,?,?)", (kind, key, str(d), rows))


def _master(ctx, d: date) -> List[Instrument]:
    from sqlalchemy.orm import Session

    with Session(ctx.engine) as s:
        return list(s.scalars(select(Instrument).where(Instrument.date == d)))


def _spots(ctx, store, symbols: List[str], d: date) -> Dict[str, float]:
    """Latest close per stock: archived bars first, then the trading database, then a live quote."""
    out = {}
    for sym in symbols:
        row = store.execute("SELECT close FROM bars WHERE symbol=? AND date<=? ORDER BY date DESC LIMIT 1",
                            (sym, str(d))).fetchone()
        if row and row[0]:
            out[sym] = float(row[0])
    missing = [s for s in symbols if s not in out]
    if missing:
        q = ctx.broker.quotes([f"NSE:{s}" for s in missing])
        for s in missing:
            qq = q.get(f"NSE:{s}")
            if qq and (qq.mid or qq.last):
                out[s] = float(qq.mid or qq.last)
    return out


def run_archive(ctx, budget: Optional[int] = None, progress: Optional[Callable[[str], None]] = None) -> ArchiveReport:
    """One archiving pass, most urgent work first, until done or `budget` broker calls are used."""
    ac = ctx.cfg.archive
    budget = ac.max_requests_per_run if budget is None else budget
    rep = ArchiveReport()
    if not ac.enabled or ctx.is_mock and not ctx.cache.get("archive_on_mock"):
        return rep
    from .. import market

    today = market.ensure_instruments(ctx)
    now = ctx.clock.now()
    cal = TradingCalendar.load(ctx.engine)
    inst = _master(ctx, today)
    stocks = sorted({i.underlying for i in inst if i.instrument_type == "FUT"})
    equity = {i.underlying: i for i in inst if i.instrument_type == "EQ"}
    futures = [i for i in inst if i.instrument_type == "FUT"]
    options = [i for i in inst if i.instrument_type in ("CE", "PE")]
    expiries = sorted({i.expiry for i in futures})[: ac.max_expiries]
    expiring = {e for e in expiries if cal.trading_days_between(today, e) <= ac.days_before_expiry}
    store = _store(ctx)

    def budget_left() -> bool:
        return rep.requests < budget

    def fetch(token: int, start: date, continuous: bool = False) -> List[dict]:
        rep.requests += 1
        return ctx.broker.historical(token, datetime.combine(start, time(0)), now, "day", continuous=continuous)

    def save_options(i: Instrument, rows: List[dict]) -> int:
        store.executemany("INSERT OR REPLACE INTO option_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (str(r["date"].date() if hasattr(r["date"], "date") else r["date"]), i.underlying, str(i.expiry),
             float(i.strike), i.instrument_type, r["open"], r["high"], r["low"], r["close"],
             int(r.get("volume") or 0), int(r.get("oi") or 0), i.lot_size) for r in rows])
        return len(rows)

    def save_future(sym: str, expiration: str, rows: List[dict]) -> int:
        store.executemany("INSERT OR REPLACE INTO future_candles VALUES (?,?,?,?,?,?,?,?,?)", [
            (str(r["date"].date() if hasattr(r["date"], "date") else r["date"]), sym, expiration, r["open"], r["high"],
             r["low"], r["close"], int(r.get("volume") or 0), int(r.get("oi") or 0)) for r in rows])
        return len(rows)

    try:
        # 1. Stock bars (needed for the strike band) and continuous futures, once per stock.
        done_eq, done_cont = _done(store, "equity"), _done(store, "future_cont")
        for sym in stocks:
            if not budget_left():
                break
            if sym not in done_eq and sym in equity and equity[sym].instrument_token:
                rows = fetch(equity[sym].instrument_token, today - timedelta(days=ac.equity_days))
                store.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", [
                    (sym, str(r["date"].date()), r["open"], r["high"], r["low"], r["close"], int(r.get("volume") or 0))
                    for r in rows])
                _mark(store, "equity", sym, today, len(rows))
                rep.equities += 1
                rep.candles += len(rows)
        near_fut = {}
        for f in sorted(futures, key=lambda f: f.expiry):
            near_fut.setdefault(f.underlying, f)
        for sym, f in near_fut.items():
            if not budget_left():
                break
            if sym not in done_cont and f.instrument_token:
                rows = fetch(f.instrument_token, today - timedelta(days=ac.equity_days), continuous=True)
                rep.candles += save_future(sym, "continuous", rows)
                _mark(store, "future_cont", sym, today, len(rows))
                rep.futures += 1
        store.commit()

        spots = _spots(ctx, store, stocks, today)

        def in_band(i: Instrument) -> bool:
            s = spots.get(i.underlying)
            return bool(s) and abs(i.strike / s - 1) <= ac.band

        # 2. Expiring series first (time-critical), then the one-off backfill of listed series.
        passes: List[Tuple[str, List[Instrument], List[Instrument]]] = []
        if expiring:
            passes.append(("expiry", [f for f in futures if f.expiry in expiring],
                           [o for o in options if o.expiry in expiring and in_band(o)]))
        passes.append(("backfill", [f for f in futures if f.expiry in expiries],
                       [o for o in options if o.expiry in expiries and in_band(o)]))
        for kind, futs, opts in passes:
            done_f, done_o = _done(store, f"future_{kind}"), _done(store, f"option_{kind}")
            for f in futs:
                if not budget_left():
                    break
                if f.tradingsymbol in done_f:
                    continue
                rows = fetch(f.instrument_token, today - timedelta(days=200))
                rep.candles += save_future(f.underlying, str(f.expiry), rows)
                _mark(store, f"future_{kind}", f.tradingsymbol, today, len(rows))
                rep.futures += 1
            for n, o in enumerate(opts):
                if not budget_left():
                    break
                if o.tradingsymbol in done_o:
                    continue
                rows = fetch(o.instrument_token, today - timedelta(days=200))
                rep.candles += save_options(o, rows)
                _mark(store, f"option_{kind}", o.tradingsymbol, today, len(rows))
                rep.options += 1
                if rep.options % 200 == 0:
                    store.commit()
                    if progress:
                        progress(rep.text())
            store.commit()
            rep.pending += sum(1 for f in futs if f.tradingsymbol not in _done(store, f"future_{kind}"))
            rep.pending += sum(1 for o in opts if o.tradingsymbol not in _done(store, f"option_{kind}"))
            if kind == "expiry" and not any(o.tradingsymbol not in _done(store, "option_expiry") for o in opts):
                rep.expiring = [str(e) for e in sorted(expiring)]
    finally:
        store.commit()
        store.close()
    log.info(rep.text())
    return rep


def archive_status(ctx) -> str:
    store = _store(ctx)
    try:
        n_opt = store.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM option_candles").fetchone()
        n_fut = store.execute("SELECT COUNT(*) FROM future_candles").fetchone()[0]
        n_bar = store.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM bars").fetchone()
        log_rows = store.execute("SELECT kind, COUNT(*) FROM archive_log GROUP BY kind").fetchall()
    finally:
        store.close()
    lines = [f"option candles: {n_opt[0]:,} for {n_opt[1]} stocks ({n_opt[2]} to {n_opt[3]})",
             f"futures candles: {n_fut:,}", f"stock bars: {n_bar[0]:,} for {n_bar[1]} stocks",
             "archived items: " + (", ".join(f"{k} {n}" for k, n in log_rows) or "none yet")]
    return "\n".join(lines)
