"""Shared data service: one source of market and reference data for every system on the platform.

Both systems use the earlier system's broker object, so there is one Kite session, one daily login and
one set of rate limits (quotes 1 request a second, history 3 a second), and nobody competes for them.
Reference data comes from the earlier system's database too: the instrument master (refreshed daily),
daily bars, corporate events (NSE with an honest client, the owner's CSV imports, events_override.csv),
sectors and NSE holidays. Lot sizes, strikes, expiries and holidays are always read from these, never
hard-coded.
"""

from __future__ import annotations

import logging
import math
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import func, select

from app import market
from app.data import events as ev_data
from app.data import instruments as inst
from app.data.calendar import TradingCalendar
from app.data.daily import load_daily
from app.data.models import Sector
from app.data.refdata import UNKNOWN_SECTOR

from . import nse_files
from .db import get_state, put_state

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Leg:
    """One option leg: what the broker and the margin API need to know."""
    tradingsymbol: str
    strike: float
    cp: str                 # CE / PE
    side: str               # SELL (short) / BUY (long)
    qty: int

    @property
    def key(self) -> str:
        return f"NFO:{self.tradingsymbol}"


class DataService:
    def __init__(self, ctx, pcfg, pengine):
        self.ctx, self.cfg, self.pengine = ctx, pcfg, pengine

    # ------------------------------------------------------------------ time and calendar
    @property
    def broker(self):
        return self.ctx.broker

    def now(self) -> datetime:
        return self.ctx.clock.now()

    def today(self) -> date:
        return self.ctx.clock.today()

    def calendar(self) -> TradingCalendar:
        return TradingCalendar.load(self.ctx.engine)

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        return self.calendar().is_trading_day(d or self.today())

    # ------------------------------------------------------------------ instruments
    def ensure_instruments(self) -> date:
        return market.ensure_instruments(self.ctx)

    def stocks(self) -> List[str]:
        return inst.fno_stocks(self.ctx.engine, self.ensure_instruments())

    def expiries(self, symbol: str) -> List[date]:
        return inst.expiries(self.ctx.engine, self.ensure_instruments(), symbol)

    def chain(self, symbol: str, expiry: date):
        return inst.load_chain(self.ctx.engine, self.ensure_instruments(), symbol, expiry)

    def expiry_for(self, symbol: str, d: date, min_dte: Optional[int] = None) -> Tuple[Optional[date], Optional[date]]:
        """The spec's expiry rule: the current monthly expiry if it is at least `min_dte` calendar days
        away on `d`, otherwise the next month. Returns (chosen, the other candidate)."""
        min_dte = self.cfg.expiry.min_dte_current_month if min_dte is None else min_dte
        exps = [e for e in self.expiries(symbol) if e >= d]
        if not exps:
            return None, None
        current = exps[0]
        nxt = exps[1] if len(exps) > 1 else None
        return (current, nxt) if (current - d).days >= min_dte else (nxt, current)

    # ------------------------------------------------------------------ quotes and candles
    def quotes(self, keys: Sequence[str]):
        keys = list(dict.fromkeys(keys))
        return self.broker.quotes(keys) if keys else {}

    def spot_quotes(self, symbols: Iterable[str]):
        q = self.quotes([f"NSE:{s}" for s in symbols])
        return {k.split(":", 1)[1]: v for k, v in q.items()}

    def daily_bars(self, symbol: str, end: Optional[date] = None) -> pd.DataFrame:
        """Completed daily bars (index: date; open, high, low, close, volume) up to `end`."""
        df = load_daily(self.ctx.engine, symbol, end=end)
        return df[["open", "high", "low", "close", "volume"]].copy() if len(df) else df

    @staticmethod
    def with_session(bars: pd.DataFrame, d: date, quote) -> pd.DataFrame:
        """Append (or replace) the row for `d` built from a live quote: the day's open, high and low so
        far, the last price as the close, and the volume traded so far. At 15:20 this stands in for the
        close; the 18:30 re-check compares it with the official close."""
        if quote is None or not quote.last:
            return bars
        row = pd.DataFrame({"open": [quote.day_open or quote.last], "high": [max(quote.day_high, quote.last)],
                            "low": [min(quote.day_low or quote.last, quote.last)], "close": [quote.last],
                            "volume": [quote.volume]}, index=[d])
        base = bars[bars.index < d] if len(bars) else bars
        return pd.concat([base, row]) if len(base) else row

    def equity_token(self, symbol: str) -> Optional[int]:
        return inst.equity_token(self.ctx.engine, self.ensure_instruments(), symbol)

    def minute_candles(self, symbol: str, start: date, end: date) -> List[dict]:
        """1-minute cash-market candles from `start` to `end` (inclusive), for VWAP and the HVN."""
        tok = self.equity_token(symbol)
        if not tok:
            return []
        return self.broker.historical(tok, datetime.combine(start, time(9, 0)), datetime.combine(end, time(15, 30)),
                                      "minute")

    def index_token(self, name: str) -> Optional[int]:
        key = f"index_token:{name}"
        tok = get_state(self.pengine, key)
        if tok:
            return int(tok)
        rows = self.broker.instruments("NSE")
        tok = next((r.get("instrument_token") for r in rows
                    if r.get("segment") == "INDICES" and r.get("tradingsymbol") == name), None)
        if tok:
            put_state(self.pengine, key, int(tok), self.now())
        return int(tok) if tok else None

    def index_bars(self, name: str, days: int = 400) -> pd.DataFrame:
        tok = self.index_token(name)
        if not tok:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        end = self.now()
        rows = self.broker.historical(tok, end - timedelta(days=days), end, "day")
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df.index = [r.date() if hasattr(r, "date") else r for r in df.pop("date")]
        return df[["open", "high", "low", "close"]].assign(volume=df.get("volume", 0))

    # ------------------------------------------------------------------ margins
    def margin(self, legs: Sequence[Leg], prices: Optional[Dict[str, float]] = None) -> float:
        """Zerodha's basket margin for these legs held together."""
        orders = [{"exchange": "NFO", "tradingsymbol": lg.tradingsymbol, "transaction_type": lg.side,
                   "variety": "regular", "product": "NRML", "order_type": "LIMIT", "quantity": lg.qty,
                   "price": (prices or {}).get(lg.tradingsymbol, 0.0)} for lg in sorted(legs, key=lambda x: x.side)]
        return float(self.broker.basket_margin(orders))

    # ------------------------------------------------------------------ events and sectors
    def events_fresh(self) -> bool:
        return ev_data.is_fresh(self.ctx.engine, self.now(), self.cfg.screener.event_staleness_days)

    def events_between(self, symbol: str, start: date, end: date):
        return ev_data.events_between(self.ctx.engine, symbol, start, end)

    def events_import_dir(self) -> Path:
        return self.ctx.cfg.resolve(self.ctx.cfg.paths.imports) / "events"

    def newest_events_file_age_days(self) -> Optional[float]:
        folder = self.events_import_dir()
        files = [p for p in folder.glob("*.csv")] if folder.exists() else []
        if not files:
            return None
        newest = max(p.stat().st_mtime for p in files)
        return (_time.time() - newest) / 86400.0

    def sector_of(self, symbol: str) -> str:
        """NSE industry classification if it is fresh (90 days), else sectors.csv if it is fresh, else UNKNOWN."""
        fresh = timedelta(days=self.cfg.data.sectors_file_fresh_days)
        with self.ctx.engine.connect() as conn:
            row = conn.execute(select(Sector.sector, Sector.updated_at).where(Sector.symbol == symbol)).first()
        if row and row.sector and row.updated_at and self.now() - row.updated_at <= fresh:
            return row.sector
        manual = self._sectors_file()
        if symbol in manual:
            return manual[symbol]
        return row.sector if row and row.sector else UNKNOWN_SECTOR

    def _sectors_file(self) -> Dict[str, str]:
        path = self.cfg.resolve(self.cfg.paths.sectors_file)
        if not path.exists():
            return {}
        age = (_time.time() - path.stat().st_mtime) / 86400.0
        if age > self.cfg.data.sectors_file_fresh_days:
            log.warning("%s is %.0f days old (fresh for %d): not used", path.name, age, self.cfg.data.sectors_file_fresh_days)
            return {}
        import csv

        out = {}
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or row.get("Symbol") or "").strip().upper()
                sec = (row.get("sector") or row.get("Industry") or row.get("industry") or "").strip()
                if sym and sec:
                    out[sym] = sec
        return out

    # ------------------------------------------------------------------ official end of day
    def official_eod(self, d: date, symbols: Sequence[str] = (), option_keys: Sequence[str] = ()) -> nse_files.OfficialEOD:
        """NSE's bhavcopy (archive, then the import folder); missing pieces from Kite after the close:
        the day's candle for each symbol and post-close OI for each option key."""
        lots = {}
        today = self.ensure_instruments()
        for s in symbols:
            try:
                exps = inst.expiries(self.ctx.engine, today, s)
                if exps:
                    lots[s] = inst.load_chain(self.ctx.engine, today, s, exps[0]).lot_size
            except LookupError:
                pass
        eod = nse_files.official_eod(d, self.ctx.cfg.resolve(self.ctx.cfg.paths.imports) / "bhavcopy", lots,
                                     oi_unit=get_state(self.pengine, "nse_oi_unit", "lots"))
        eod = eod or nse_files.OfficialEOD(d, "kite")
        missing = [s for s in symbols if s not in eod.closes]
        for s in missing:
            tok = self.equity_token(s)
            if not tok:
                continue
            try:
                rows = self.broker.historical(tok, datetime.combine(d, time(0)), datetime.combine(d, time(23, 59)), "day")
            except Exception as exc:                  # no Kite session in the evening: NSE's files are all there is
                log.warning("Kite end-of-day candle for %s unavailable: %s", s, exc)
                continue
            if rows:
                r = rows[-1]
                eod.closes[s] = {k: float(r[k]) for k in ("open", "high", "low", "close")} | {"volume": r.get("volume")}
        if option_keys:
            try:
                q = self.quotes(option_keys)
                eod.kite_oi = {k: v.oi for k, v in q.items()}      # post-close OI, quantity
            except Exception as exc:
                log.warning("Kite post-close OI unavailable: %s", exc)
        if missing and eod.source != "kite":
            eod.source += "+kite"
        return eod
