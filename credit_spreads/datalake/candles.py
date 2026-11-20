"""Nightly candle collection from Kite into the repository (after the close).

Kite serves candles only for listed contracts and deletes an option or future's history when it
expires, so every listed stock option of the next `lake.expiries` monthly series (all strikes, or a
band) and every stock future is fetched at 1 minute, with OI, in up to three passes:

- first pass when `first_pass_dte` calendar days or fewer are left: everything since listing;
- middle pass at `mid_pass_dte` days: everything since (so a machine that is off in expiry week
  loses at most the last three weeks);
- final pass in the series' last `final_pass_days` sessions.

Kite returns no candle for a minute without a trade; every candle it returns is kept. Each F&O
stock and each index in `lake.indices` is topped up to the last session at 1 minute; their older
history stays on Kite for years and `backfill_equity` fetches it going back. Requests are capped
per run, urgent work (final passes) first; progress is saved only after the rows are on disk, so an
interrupted run repeats a little work and loses nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.broker.base import SessionInvalid
from app.data.calendar import TradingCalendar
from app.data.history_archiver import chunks, last_session
from app.data.models import Instrument

from .store import Lake

log = logging.getLogger(__name__)

LIFE_DAYS = 100                     # a monthly series is listed about three months before expiry
FLUSH_ROWS = 750_000
MAX_ERRORS = 25


@dataclass
class CandleReport:
    requests: int = 0
    options: int = 0
    futures: int = 0
    symbols: int = 0
    rows: int = 0
    bytes: int = 0
    pending: int = 0
    urgent: int = 0
    missed: int = 0
    errors: int = 0
    backfilled: int = 0
    notes: List[str] = field(default_factory=list)

    def text(self) -> str:
        s = (f"candles: {self.requests:,} Kite requests, {self.options:,} options and {self.futures:,} futures "
             f"fetched, {self.symbols} stocks/indices topped up, {self.rows:,} rows ({self.bytes / 1e6:.1f} MB)")
        if self.backfilled:
            s += f", {self.backfilled:,} history chunks backfilled"
        if self.pending:
            s += f"; {self.pending:,} contracts left for the next run ({self.urgent:,} in a series about to expire)"
        if self.missed:
            s += f"; {self.missed:,} contracts expired before their final pass"
        if self.errors:
            s += f"; {self.errors} fetches failed (retried next run)"
        return s


class _Budget(Exception):
    pass


class _Skip(Exception):
    pass


def _master(ctx, d: date) -> List[Instrument]:
    with Session(ctx.engine) as s:
        return list(s.scalars(select(Instrument).where(Instrument.date == d)))


def _frame(rows: List[dict], extra: Dict[str, object]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"date": "ts"})
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None) if getattr(df["ts"].dt, "tz", None) else pd.to_datetime(df["ts"])
    for c in ("volume", "oi"):
        df[c] = pd.to_numeric(df[c] if c in df else 0, errors="coerce").fillna(0).astype("int64")
    for k, v in extra.items():
        df[k] = v
    return df


class Collector:
    """One run: shared request budget, buffered writes, progress committed after each flush."""

    def __init__(self, p, budget: int, rep: CandleReport):
        self.p, self.ctx, self.cfg = p, p.ctx, p.cfg.lake
        self.lake = Lake(p.cfg.resolve(self.cfg.root))
        self.budget, self.rep = budget, rep
        self.cal = TradingCalendar.load(self.ctx.engine)
        self.upto = last_session(self.cal, self.ctx.clock.now())
        self.end_dt = datetime.combine(self.upto, time(23, 59))
        self.buffers: Dict[str, List[pd.DataFrame]] = {"options_1m": [], "futures_1m": [], "equity_1m": []}
        self.buffered = 0
        self.pending_progress: List[Tuple[str, tuple]] = []

    # ------------------------------------------------------------------ kite
    def fetch(self, token: int, start: datetime, end: datetime, interval: str = "minute") -> List[dict]:
        cs = chunks(start, end, interval)
        if self.rep.requests + len(cs) > self.budget:
            raise _Budget()
        rows: List[dict] = []
        try:
            for s, e in cs:
                self.rep.requests += 1
                rows += self.ctx.broker.historical(token, s, e, interval)
        except SessionInvalid:
            raise
        except Exception as exc:
            self.rep.errors += 1
            log.warning("lake: history for token %s failed: %s", token, exc)
            if self.rep.errors >= MAX_ERRORS:
                raise
            raise _Skip() from exc
        return rows

    # ------------------------------------------------------------------ buffering
    def add(self, dataset: str, df: pd.DataFrame, progress: Tuple[str, tuple]) -> None:
        if len(df):
            self.buffers[dataset].append(df)
            self.buffered += len(df)
            self.rep.rows += len(df)
        self.pending_progress.append(progress)
        if self.buffered >= FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        for dataset, frames in self.buffers.items():
            if frames:
                files = self.lake.write(dataset, pd.concat(frames, ignore_index=True), tag="kite")
                self.rep.bytes += sum(f.stat().st_size for f in files)
                frames.clear()
        self.buffered = 0
        with self.lake.catalog._lock, self.lake.catalog.connect() as c:
            for sql, args in self.pending_progress:
                c.execute(sql, args)
        self.pending_progress = []

    # ------------------------------------------------------------------ contracts
    def stage(self, i: Instrument, passes: int, today: date) -> Optional[int]:
        """3 final, 2 middle, 1 first pass due; None when nothing is due."""
        left = self.cal.trading_days_between(today, i.expiry)
        dte = (i.expiry - today).days
        if left <= self.cfg.final_pass_days:
            return 3 if passes < 3 else None
        if self.cfg.mid_pass_dte and dte <= self.cfg.mid_pass_dte:
            return 2 if passes < 2 else None
        if dte <= self.cfg.first_pass_dte:
            return 1 if passes < 1 else None
        return None

    def contracts(self, master: List[Instrument], today: date) -> None:
        cat = self.lake.catalog
        gone = cat.execute("SELECT tradingsymbol, expiry, last_fetched FROM contracts WHERE final=0 AND passes < 3 "
                           "AND expiry < ?", (str(today),))
        for ts_, exp, last in gone:
            cat.gap("options_1m" if not ts_.endswith("FUT") else "futures_1m", ts_, exp,
                    f"expired before its final pass (kept up to {last})")
        if gone:
            cat.execute("UPDATE contracts SET final=2 WHERE final=0 AND passes < 3 AND expiry < ?", (str(today),))
            self.rep.missed += len(gone)
        futures = [i for i in master if i.instrument_type == "FUT" and i.expiry and i.instrument_token]
        expiries = sorted({i.expiry for i in futures})[: self.cfg.expiries]
        spots = self._spots(sorted({i.underlying for i in futures})) if self.cfg.strikes == "band" else {}
        prog = {r[0]: (r[1], r[2]) for r in cat.execute("SELECT tradingsymbol, last_fetched, passes FROM contracts")}
        work = []
        for i in master:
            if i.instrument_type not in ("FUT", "CE", "PE") or i.expiry not in expiries or not i.instrument_token:
                continue
            last, passes = prog.get(i.tradingsymbol, (None, 0))
            if i.instrument_type != "FUT" and self.cfg.strikes == "band" and last is None:
                s = spots.get(i.underlying)
                if not s or abs(i.strike / s - 1) > self.cfg.band_pct:
                    continue
            st = self.stage(i, passes, today)
            if st is not None:
                work.append((-st, i.expiry, i.instrument_type != "FUT", i.underlying, i.strike or 0, i.instrument_type, i,
                             last, st))
        work.sort(key=lambda w: w[:6])
        for n, (_, expiry, _, _, _, _, i, last, st) in enumerate(work):
            start = (date.fromisoformat(last) + timedelta(days=1)) if last else expiry - timedelta(days=LIFE_DAYS)
            rows: List[dict] = []
            if start <= self.upto:
                try:
                    rows = self.fetch(i.instrument_token, datetime.combine(start, time(0)), self.end_dt)
                except _Skip:
                    continue
                except _Budget:
                    self.rep.pending = len(work) - n
                    self.rep.urgent = sum(1 for w in work[n:] if w[8] == 3)
                    return
            passes = 3 if st == 3 else (2 if self.cfg.mid_pass_dte and (expiry - today).days <= self.cfg.mid_pass_dte
                                        else 1)
            is_opt = i.instrument_type != "FUT"
            df = _frame(rows, {"underlying": i.underlying, "expiry": i.expiry, "tradingsymbol": i.tradingsymbol}
                        | ({"strike": float(i.strike), "cp": i.instrument_type} if is_opt else {}))
            fetched = str(max(self.upto, start - timedelta(days=1)))
            self.add("options_1m" if is_opt else "futures_1m", df, (
                "INSERT INTO contracts (tradingsymbol, token, kind, underlying, expiry, strike, cp, lot, last_fetched, "
                "passes, final, requests, rows) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tradingsymbol) DO UPDATE "
                "SET last_fetched=excluded.last_fetched, passes=MAX(passes, excluded.passes), final=excluded.final, "
                "rows=rows+excluded.rows, requests=requests+excluded.requests",
                (i.tradingsymbol, i.instrument_token, "option" if is_opt else "future", i.underlying, str(i.expiry),
                 i.strike, i.instrument_type if is_opt else None, i.lot_size, fetched, passes, 1 if passes == 3 else 0,
                 len(chunks(datetime.combine(start, time(0)), self.end_dt, "minute")) if start <= self.upto else 0,
                 len(df))))
            if is_opt:
                self.rep.options += 1
            else:
                self.rep.futures += 1

    def _spots(self, symbols: List[str]) -> Dict[str, float]:
        try:
            q = self.p.ds.spot_quotes(symbols)
            return {k: (v.last or v.mid) for k, v in q.items() if (v.last or v.mid)}
        except Exception as exc:
            log.warning("lake: spot quotes for the strike band failed: %s", exc)
            return {}

    # ------------------------------------------------------------------ stocks and indices
    def tokens(self, master: List[Instrument]) -> Dict[str, Tuple[int, str]]:
        out = {i.underlying: (i.instrument_token, "stock") for i in master
               if i.instrument_type == "EQ" and i.instrument_token}
        for name in self.cfg.indices:
            tok = self.p.ds.index_token(name)
            if tok:
                out[name] = (tok, "index")
        return out

    def top_up(self, tokens: Dict[str, Tuple[int, str]]) -> None:
        cat = self.lake.catalog
        known = {r[0]: (r[1], r[2]) for r in cat.execute("SELECT symbol, first_ts, last_ts FROM series")}
        for sym, (tok, kind) in sorted(tokens.items()):
            first, last = known.get(sym, (None, None))
            start = (datetime.fromisoformat(last) + timedelta(minutes=1)) if last else \
                datetime.combine(self.upto - timedelta(days=59), time(0))
            if start > self.end_dt:
                continue
            try:
                rows = self.fetch(tok, start, self.end_dt)
            except _Skip:
                continue
            except _Budget:
                return
            df = _frame(rows, {"symbol": sym})
            if len(df):
                df["month"] = df["ts"].dt.strftime("%Y-%m")
                first = first or df["ts"].min().isoformat(sep=" ")
                last = df["ts"].max().isoformat(sep=" ")
            self.add("equity_1m", df, (
                "INSERT INTO series (symbol, token, kind, first_ts, last_ts, rows) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET token=excluded.token, last_ts=COALESCE(excluded.last_ts, last_ts), "
                "first_ts=COALESCE(first_ts, excluded.first_ts), rows=rows+excluded.rows",
                (sym, tok, kind, first, last, len(df))))
            self.rep.symbols += 1

    def backfill(self, tokens: Dict[str, Tuple[int, str]]) -> None:
        """Older 1-minute history, one 60-day chunk per symbol per round, until Kite has no more or
        `equity_minute_since` is reached."""
        cat = self.lake.catalog
        floor = datetime.combine(self.cfg.equity_minute_since, time(0))
        while True:
            todo = cat.execute("SELECT symbol, token, first_ts, empty_chunks FROM series WHERE backfill_done=0 "
                               "AND first_ts IS NOT NULL ORDER BY symbol")
            todo = [t for t in todo if t[0] in tokens]
            if not todo:
                return
            for sym, tok, first, empties in todo:
                end = datetime.fromisoformat(first) - timedelta(seconds=1)
                start = max(end - timedelta(days=59), floor)
                if end <= floor:
                    cat.execute("UPDATE series SET backfill_done=1 WHERE symbol=?", (sym,))
                    continue
                try:
                    rows = self.fetch(tokens[sym][0], start, end)
                except _Skip:
                    continue
                except _Budget:
                    return
                self.rep.backfilled += 1
                df = _frame(rows, {"symbol": sym})
                if len(df):
                    df["month"] = df["ts"].dt.strftime("%Y-%m")
                new_first = df["ts"].min().isoformat(sep=" ") if len(df) else start.isoformat(sep=" ")
                empties = 0 if len(df) else empties + 1
                done = 1 if empties >= 3 or start <= floor else 0
                self.add("equity_1m", df, ("UPDATE series SET first_ts=?, empty_chunks=?, backfill_done=?, rows=rows+? "
                                           "WHERE symbol=?", (new_first, empties, done, len(df), sym)))
            self.flush()

    # ------------------------------------------------------------------ instruments
    def snapshot_instruments(self, master: List[Instrument], today: date) -> None:
        if self.lake.catalog.get_meta("instruments_last") == str(today):
            return
        df = pd.DataFrame([{"instrument_token": i.instrument_token, "exchange": i.exchange,
                            "tradingsymbol": i.tradingsymbol, "underlying": i.underlying,
                            "instrument_type": i.instrument_type, "expiry": i.expiry, "strike": i.strike,
                            "lot_size": i.lot_size, "tick_size": i.tick_size,
                            "segment": f"{i.exchange}-{i.instrument_type}"} for i in master])
        if len(df):
            df["date"] = today
            self.lake.write("instruments", df, tag="kite")
        self.lake.catalog.set_meta("instruments_last", today)


def run(p, budget: Optional[int] = None, backfill_budget: Optional[int] = None,
        progress: Optional[Callable[[str], None]] = None) -> CandleReport:
    """The nightly job: contracts (urgent first), then stocks and indices; with `backfill_budget`,
    older stock and index history afterwards."""
    from app import market

    cfg = p.cfg.lake
    rep = CandleReport()
    if not cfg.enabled:
        return rep
    started = datetime.now()
    col = Collector(p, cfg.max_requests_per_run if budget is None else budget, rep)
    today = market.ensure_instruments(p.ctx)
    master = _master(p.ctx, today)
    col.snapshot_instruments(master, today)
    try:
        col.contracts(master, today)
        col.flush()
        tokens = col.tokens(master)
        col.top_up(tokens)
        col.flush()
        if backfill_budget:
            col.budget = rep.requests + backfill_budget
            col.backfill(tokens)
    finally:
        col.flush()
        col.lake.catalog.run("candles", started, datetime.now(), rep.requests, rep.rows, rep.bytes, rep.text())
    log.info(rep.text())
    if progress:
        progress(rep.text())
    return rep


def run_backfill(p, budget: Optional[int] = None) -> CandleReport:
    """Older 1-minute history of the stocks and indices (17:45), within its own request budget."""
    from app import market

    cfg = p.cfg.lake
    rep = CandleReport()
    budget = cfg.backfill_requests_per_run if budget is None else budget
    if not cfg.enabled or not budget:
        return rep
    started = datetime.now()
    col = Collector(p, budget, rep)
    master = _master(p.ctx, market.ensure_instruments(p.ctx))
    try:
        col.backfill(col.tokens(master))
    finally:
        col.flush()
        col.lake.catalog.run("backfill", started, datetime.now(), rep.requests, rep.rows, rep.bytes, rep.text())
    return rep


def compact_finished(p) -> int:
    """Merge the files of every series that has expired, and of every month that has ended."""
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    today = p.ctx.clock.today()
    n = 0
    for folder in lake.partitions("options_1m"):
        exp = date.fromisoformat(folder.name.split("=", 1)[1])
        if exp < today and (len(list(folder.glob("*.parquet"))) > 1):
            lake.compact(folder, "options_1m")
            n += 1
    for folder in lake.partitions("futures_1m"):
        exp = date.fromisoformat(folder.name.split("=", 1)[1])
        if exp < today and len(list(folder.glob("*.parquet"))) > 1:
            lake.compact(folder, "futures_1m")
            n += 1
    this_month = today.strftime("%Y-%m")
    for folder in lake.partitions("equity_1m"):
        if folder.name.split("=", 1)[1] < this_month and len(list(folder.glob("*.parquet"))) > 1:
            lake.compact(folder, "equity_1m")
            n += 1
    return n
