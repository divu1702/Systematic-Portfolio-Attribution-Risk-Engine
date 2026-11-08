"""Kite history archiver: build an Indian option history while it is still available.

Kite serves candles only for listed contracts. When a monthly series expires its option history is
gone, and expired futures survive only as a daily "continuous" series. So for every listed option
within +-`archive.band` of the stock price and every listed futures contract, the archiver saves
candles at `archive.interval` (1-minute by default), in two passes per contract:

- first pass, once `archive.first_pass_dte` calendar days or fewer are left: everything since
  listing (a monthly series is listed about three months before expiry);
- final pass, in the series' last `archive.days_before_expiry` trading days: everything since.

Two passes fit Kite's 60-day limit on 1-minute requests (one request each), and if the machine is
off in expiry week only the last month of that series is lost (reported, not silent). Strikes that
move into the band later are picked up with their full history. Each stock's daily bars (up to
`archive.equity_days`) are saved and kept up to date, and its continuous futures saved once; a
stock's own intraday history stays on Kite for years, so it is left for later.

Storage: one compressed blob per contract-day with only the minutes that traded
(app/data/barcodec.py); daily candles are derived from the same data into option_candles and
future_candles for the study. Work is ordered by urgency (final passes first) and capped at
`archive.max_requests_per_run` Kite requests, counted after splitting at Kite's span limits.
Progress is saved per contract, so an interrupted run continues where it stopped. Kite's 3
requests a second limit is enforced by the broker. Candles carry trade prices, not quotes; the
live recorder (market_recorder.py) adds real bid/ask.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from ..backtest.store import connect
from ..broker.base import HIST_MAX_DAYS, SessionInvalid
from . import barcodec
from .calendar import TradingCalendar
from .models import Instrument

log = logging.getLogger(__name__)

LIFE_DAYS = 100                 # first fetch starts this long before expiry (listing is ~3 months before)
SESSION_DONE = time(15, 40)     # after this a trading day's candles are complete
MAX_ERRORS = 20                 # failed fetches tolerated per run before giving up (something systemic)


@dataclass
class ArchiveReport:
    interval: str = "minute"
    requests: int = 0
    equities: int = 0
    futures: int = 0
    options: int = 0
    candles: int = 0
    days: int = 0                       # contract-days stored
    bytes: int = 0                      # compressed size of what was stored
    pending: int = 0                    # contracts still to fetch when the budget ran out
    urgent: int = 0                     # ... of which final passes (their series expires within days)
    missed: int = 0                     # contracts that expired before their final pass
    errors: int = 0                     # fetches that failed (retried next run)
    expiring: List[str] = field(default_factory=list)

    def text(self) -> str:
        s = (f"archive ({self.interval}): {self.requests} requests, {self.options} option contracts and "
             f"{self.futures} futures saved ({self.candles:,} candles, {self.days:,} contract-days, "
             f"{self.bytes / 1e6:.1f} MB), {self.equities} stocks' bars updated")
        if self.expiring:
            s += f"; series completed: {', '.join(self.expiring)}"
        if self.pending:
            s += f"; {self.pending:,} contracts left for the next run"
            if self.urgent:
                s += f" ({self.urgent:,} of them in a series about to expire)"
        if self.missed:
            s += (f"; {self.missed:,} contracts expired before their final pass (kept up to their first "
                  f"pass)")
        if self.errors:
            s += f"; {self.errors} fetches failed (retried next run)"
        return s


class _Skip(Exception):
    """This instrument failed; leave its progress unchanged so the next run retries it."""


def _store(ctx):
    return connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))


def last_session(cal: TradingCalendar, now: datetime) -> date:
    """The latest trading day whose session has closed."""
    d = now.date()
    if cal.is_trading_day(d) and now.time() >= SESSION_DONE:
        return d
    return cal.prev_trading_day(d)


def chunks(start: datetime, end: datetime, interval: str) -> List[Tuple[datetime, datetime]]:
    """Split [start, end] at Kite's longest span for the interval: one request each."""
    span = timedelta(days=HIST_MAX_DAYS.get(interval, 60))
    out, s = [], start
    while s <= end:
        e = min(s + span, end)
        out.append((s, e))
        s = e + timedelta(seconds=1)
    return out


def _master(ctx, d: date) -> List[Instrument]:
    from sqlalchemy.orm import Session

    with Session(ctx.engine) as s:
        return list(s.scalars(select(Instrument).where(Instrument.date == d)))


def _spots(ctx, store, symbols: List[str], d: date) -> Dict[str, float]:
    """Latest close per stock from the store's bars, else a live quote."""
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


def _day(r) -> date:
    return r["date"].date() if hasattr(r["date"], "date") else r["date"]


def _save(store, i: Instrument, rows: List[dict], interval: str, rep: ArchiveReport) -> None:
    """Intraday candles -> one blob per contract-day (traded minutes) plus a derived daily row;
    daily candles go straight to the daily table."""
    sym, exp = i.underlying, str(i.expiry)
    is_opt = i.instrument_type in ("CE", "PE")
    key = (sym, exp, float(i.strike), i.instrument_type) if is_opt else (sym, exp)
    daily_rows = []
    if interval == "day":
        daily_rows = [(str(_day(r)), r) for r in rows]
    else:
        for d, day in sorted(barcodec.by_day(rows).items()):
            traded = [c for c in day if (c.get("volume") or 0) > 0]
            daily_rows.append((str(d), barcodec.daily_from(day)))
            if not traded:
                continue
            blob = barcodec.encode(barcodec.candle_columns(traded))
            if is_opt:
                store.execute("INSERT OR REPLACE INTO option_bars VALUES (?,?,?,?,?,?,?,?,?)",
                              (*key, str(d), interval, len(traded), i.lot_size, blob))
            else:
                store.execute("INSERT OR REPLACE INTO future_bars VALUES (?,?,?,?,?,?)",
                              (*key, str(d), interval, len(traded), blob))
            rep.candles += len(traded)
            rep.days += 1
            rep.bytes += len(blob)
    if is_opt:
        store.executemany("INSERT OR REPLACE INTO option_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
            (d, *key, r["open"], r["high"], r["low"], r["close"], int(r.get("volume") or 0),
             int(r.get("oi") or 0), i.lot_size) for d, r in daily_rows])
    else:
        store.executemany("INSERT OR REPLACE INTO future_candles VALUES (?,?,?,?,?,?,?,?,?)", [
            (d, *key, r["open"], r["high"], r["low"], r["close"], int(r.get("volume") or 0),
             int(r.get("oi") or 0)) for d, r in daily_rows])
    if interval == "day":
        rep.candles += len(daily_rows)
        rep.days += len(daily_rows)


def run_archive(ctx, budget: Optional[int] = None, progress: Optional[Callable[[str], None]] = None) -> ArchiveReport:
    """One archiving pass, most urgent work first, until done or `budget` Kite requests are used."""
    ac = ctx.cfg.archive
    budget = ac.max_requests_per_run if budget is None else budget
    rep = ArchiveReport(interval=ac.interval)
    if not ac.enabled or ctx.is_mock and not ctx.cache.get("archive_on_mock"):
        return rep
    from .. import market

    today = market.ensure_instruments(ctx)
    cal = TradingCalendar.load(ctx.engine)
    upto = last_session(cal, ctx.clock.now())
    end_dt = datetime.combine(upto, time(23, 59))
    inst = _master(ctx, today)
    stocks = sorted({i.underlying for i in inst if i.instrument_type == "FUT"})
    equity = {i.underlying: i for i in inst if i.instrument_type == "EQ"}
    futures = [i for i in inst if i.instrument_type == "FUT"]
    options = [i for i in inst if i.instrument_type in ("CE", "PE")]
    expiries = sorted({i.expiry for i in futures})[: ac.max_expiries]
    store = _store(ctx)

    def fetch(token: int, start: date, interval: str, continuous: bool = False) -> Optional[List[dict]]:
        """Candles from `start` to the last closed session; None when the budget would be exceeded,
        [] marked as an error (and logged) when Kite fails for this instrument."""
        cs = chunks(datetime.combine(start, time(0)), end_dt, interval)
        if rep.requests + len(cs) > budget:
            return None
        rows: List[dict] = []
        try:
            for s, e in cs:
                rep.requests += 1
                rows += ctx.broker.historical(token, s, e, interval, continuous=continuous)
        except SessionInvalid:
            raise
        except Exception as exc:                      # one bad instrument must not stall every run
            rep.errors += 1
            log.warning("archive: history for token %s failed: %s", token, exc)
            if rep.errors >= MAX_ERRORS:
                raise
            raise _Skip() from exc
        return rows

    try:
        # Contracts that expired before their final pass: report once.
        gone = store.execute("SELECT COUNT(*) FROM archive_progress WHERE final=0 AND expiration < ?",
                             (str(today),)).fetchone()[0]
        if gone:
            rep.missed = gone
            store.execute("UPDATE archive_progress SET final=2 WHERE final=0 AND expiration < ?", (str(today),))

        # 1. Stock bars: new stocks from equity_days back, known ones topped up to the last session.
        for sym in stocks:
            if sym not in equity or not equity[sym].instrument_token:
                continue
            last = store.execute("SELECT MAX(date) FROM bars WHERE symbol=?", (sym,)).fetchone()[0]
            start = (date.fromisoformat(last) + timedelta(days=1)) if last else upto - timedelta(days=ac.equity_days - 1)
            if start > upto:
                continue
            try:
                rows = fetch(equity[sym].instrument_token, start, "day")
            except _Skip:
                continue
            if rows is None:
                break
            store.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", [
                (sym, str(_day(r)), r["open"], r["high"], r["low"], r["close"], int(r.get("volume") or 0))
                for r in rows])
            rep.equities += 1
        # 2. Continuous futures (daily; Kite keeps them), once per stock.
        done_cont = {k for (k,) in store.execute("SELECT key FROM archive_log WHERE kind='future_cont'")}
        near_fut: Dict[str, Instrument] = {}
        for f in sorted(futures, key=lambda f: f.expiry):
            near_fut.setdefault(f.underlying, f)
        for sym, f in near_fut.items():
            if sym in done_cont or not f.instrument_token:
                continue
            try:
                rows = fetch(f.instrument_token, upto - timedelta(days=ac.equity_days - 1), "day", continuous=True)
            except _Skip:
                continue
            if rows is None:
                break
            store.executemany("INSERT OR REPLACE INTO future_candles VALUES (?,?,?,?,?,?,?,?,?)", [
                (str(_day(r)), sym, "continuous", r["open"], r["high"], r["low"], r["close"],
                 int(r.get("volume") or 0), int(r.get("oi") or 0)) for r in rows])
            store.execute("INSERT OR REPLACE INTO archive_log VALUES (?,?,?,?)",
                          ("future_cont", sym, str(today), len(rows)))
        store.commit()

        # 3. Option and futures contracts: final passes first, then first passes.
        prog = {ts: (last, final) for ts, last, final in
                store.execute("SELECT tradingsymbol, last_date, final FROM archive_progress")}
        spots = _spots(ctx, store, stocks, today)

        def in_band(i: Instrument) -> bool:
            s = spots.get(i.underlying)
            return bool(s) and abs(i.strike / s - 1) <= ac.band

        work: List[Tuple[int, date, int, str, Instrument]] = []
        for i in futures + options:
            if i.expiry not in expiries or not i.instrument_token:
                continue
            last, final = prog.get(i.tradingsymbol, (None, 0))
            if final:
                continue
            if i.instrument_type != "FUT" and last is None and not in_band(i):
                continue                                  # outside the band and never followed
            if cal.trading_days_between(today, i.expiry) <= ac.days_before_expiry:
                work.append((0, i.expiry, i.instrument_type != "FUT", i.underlying, i))
            elif last is None and (i.expiry - today).days <= ac.first_pass_dte:
                work.append((1, i.expiry, i.instrument_type != "FUT", i.underlying, i))
        work.sort(key=lambda w: (w[0], w[1], w[2], w[3], w[4].strike or 0, w[4].instrument_type))
        unfinished: Dict[date, int] = {}
        for n, (stage, expiry, _, _, i) in enumerate(work):
            last, _ = prog.get(i.tradingsymbol, (None, 0))
            start = date.fromisoformat(last) + timedelta(days=1) if last else expiry - timedelta(days=LIFE_DAYS)
            final = 1 if stage == 0 else 0
            used = 0
            if start <= upto:
                before = rep.requests
                try:
                    rows = fetch(i.instrument_token, start, ac.interval)
                except _Skip:
                    if stage == 0:
                        unfinished[expiry] = unfinished.get(expiry, 0) + 1
                    continue
                if rows is None:
                    rep.pending = len(work) - n
                    for st, e, *_ in work[n:]:
                        if st == 0:
                            unfinished[e] = unfinished.get(e, 0) + 1
                    break
                used = rep.requests - before
                _save(store, i, rows, ac.interval, rep)
                if i.instrument_type == "FUT":
                    rep.futures += 1
                else:
                    rep.options += 1
            store.execute(
                "INSERT INTO archive_progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(tradingsymbol) DO UPDATE SET "
                "last_date=excluded.last_date, requests=requests+excluded.requests, final=excluded.final",
                (i.tradingsymbol, "future" if i.instrument_type == "FUT" else "option", i.underlying,
                 str(i.expiry), str(max(upto, start - timedelta(days=1))), used, final))
            if n % 200 == 199:
                store.commit()
                if progress:
                    progress(rep.text())
        store.commit()
        rep.urgent = sum(unfinished.values())
        window = sorted({w[1] for w in work if w[0] == 0})
        rep.expiring = [str(e) for e in window if not unfinished.get(e)]
    finally:
        store.commit()
        store.close()
    log.info(rep.text())
    return rep


def archive_status(ctx) -> str:
    path = ctx.cfg.resolve(ctx.cfg.paths.market_store)
    store = _store(ctx)
    try:
        ob = store.execute("SELECT COUNT(*), COALESCE(SUM(n),0), COALESCE(SUM(LENGTH(data)),0), COUNT(DISTINCT symbol), "
                           "MIN(date), MAX(date) FROM option_bars").fetchone()
        fb = store.execute("SELECT COUNT(*), COALESCE(SUM(n),0), COALESCE(SUM(LENGTH(data)),0) FROM future_bars").fetchone()
        oc = store.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(date), MAX(date) FROM option_candles").fetchone()
        n_bar = store.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM bars").fetchone()
        prog = store.execute("SELECT kind, final, COUNT(*) FROM archive_progress GROUP BY kind, final").fetchall()
        qs = store.execute("SELECT COUNT(*), COALESCE(SUM(n),0), COALESCE(SUM(LENGTH(data)),0) FROM quote_snaps").fetchone()
        staged = store.execute("SELECT COUNT(*) FROM quote_snap_rows").fetchone()[0]
    finally:
        store.close()
    names = {0: "first pass done", 1: "complete", 2: "missed final pass"}
    lines = [f"intraday option candles: {ob[1]:,} in {ob[0]:,} contract-days for {ob[3]} stocks "
             f"({ob[4]} to {ob[5]}), {ob[2] / 1e6:.1f} MB compressed",
             f"intraday futures candles: {fb[1]:,} in {fb[0]:,} contract-days, {fb[2] / 1e6:.1f} MB",
             f"daily option candles: {oc[0]:,} for {oc[1]} stocks ({oc[2]} to {oc[3]})",
             f"stock bars: {n_bar[0]:,} for {n_bar[1]} stocks (latest {n_bar[2]})",
             "contracts: " + (", ".join(f"{k} {names.get(f, f)} {n:,}" for k, f, n in prog) or "none yet"),
             f"quote snapshots: {qs[1]:,} quotes in {qs[0]:,} contract-days, {qs[2] / 1e6:.1f} MB"
             + (f" (+{staged:,} from today, compacted after the close)" if staged else ""),
             f"store size: {path.stat().st_size / 1e6:.1f} MB" if path.exists() else "store not created yet"]
    return "\n".join(lines)
