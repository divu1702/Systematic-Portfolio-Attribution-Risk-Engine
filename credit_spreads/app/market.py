"""Market snapshots built from the broker: today's instrument master, expiries, and option
chains with quotes and implied vols. Used by the chain demo, the scanner and the monitor."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, List, Optional

from .analytics import black76
from .analytics.vol import OptionIV, atm_iv, option_iv
from .broker.base import Quote
from .data import instruments as inst

log = logging.getLogger(__name__)

EXPIRY_TIME = time(15, 30)


def ensure_instruments(ctx) -> date:
    """Refresh the instrument master if today's copy is not in the database yet."""
    today = ctx.clock.today()
    if inst.latest_master_date(ctx.engine) != today:
        inst.refresh_instruments(ctx.engine, ctx.broker, today)
    return today


def years_to_expiry(expiry: date, now: datetime) -> float:
    return black76.year_fraction((datetime.combine(expiry, EXPIRY_TIME) - now).total_seconds())


def dte(expiry: date, today: date) -> int:
    return (expiry - today).days


def pick_expiry(expiries: List[date], today: date, min_days: int, max_days: Optional[int] = None) -> Optional[date]:
    for e in sorted(expiries):
        d = dte(e, today)
        if d >= min_days and (max_days is None or d <= max_days):
            return e
    return None


def prefetch_scan_quotes(ctx, symbols: List[str], d: date, expiries_for, band: float) -> Dict[str, Quote]:
    """Fetch every quote a scan needs in as few broker calls as possible: first the stocks and
    futures, then all option legs within `band` of each futures price. Kite allows one quote
    request a second with up to 500 instruments each, so ~220 stocks take about a minute this way
    instead of several hundred single-stock requests."""
    plans = {}
    head: List[str] = []
    for sym in symbols:
        chains = {}
        for e in expiries_for(sym):
            try:
                chains[e] = inst.load_chain(ctx.engine, d, sym, e)
            except LookupError:
                continue
        plans[sym] = chains
        for ch in chains.values():
            head.append(ch.equity_key)
            if ch.future:
                head.append(ch.future.key)
    cache = ctx.broker.quotes(list(dict.fromkeys(head)))
    legs: List[str] = []
    for sym, chains in plans.items():
        for ch in chains.values():
            fq = cache.get(ch.future.key) if ch.future else None
            sq = cache.get(ch.equity_key)
            ref = (fq.mid or fq.last) if fq else ((sq.mid or sq.last) if sq else None)
            legs += [leg.key for leg in ch.legs.values() if ref is None or abs(leg.strike / ref - 1) <= band]
    cache.update(ctx.broker.quotes(list(dict.fromkeys(legs))))
    return cache


@dataclass
class ChainSnapshot:
    chain: inst.Chain
    ts: datetime
    spot: Optional[float]
    fut_price: Optional[float]
    T: float
    quotes: Dict[str, Quote]
    ivs: List[OptionIV]

    def quote(self, strike: float, cp: str) -> Optional[Quote]:
        leg = self.chain.leg(strike, cp)
        return self.quotes.get(leg.key) if leg else None

    def iv(self, strike: float, cp: str) -> Optional[OptionIV]:
        for o in self.ivs:
            if o.strike == strike and o.cp == cp:
                return o
        return None

    @property
    def atm_iv(self) -> Optional[float]:
        return atm_iv(self.ivs, self.fut_price) if self.fut_price else None


def _quotes(ctx, keys: List[str], cache: Optional[Dict[str, Quote]]) -> Dict[str, Quote]:
    """Quotes from a prefetched cache where possible; anything missing goes to the broker."""
    if cache is None:
        return ctx.broker.quotes(keys)
    out = {k: cache[k] for k in keys if k in cache}
    missing = [k for k in keys if k not in cache]
    if missing:
        out.update(ctx.broker.quotes(missing))
    return out


def snapshot_chain(ctx, symbol: str, expiry: date, d: Optional[date] = None,
                   band: Optional[float] = 0.35, quotes: Optional[Dict[str, Quote]] = None) -> ChainSnapshot:
    """Quote the futures, the stock and every option within `band` (fraction of spot) of the
    money, and back out IVs from mid prices with Black-76 on the futures price. `quotes` is an
    optional prefetched cache (see prefetch_scan_quotes)."""
    d = d or ctx.clock.today()
    chain = inst.load_chain(ctx.engine, d, symbol, expiry)
    head = [chain.equity_key] + ([chain.future.key] if chain.future else [])
    hq = _quotes(ctx, head, quotes)
    spot_q = hq.get(chain.equity_key)
    spot = (spot_q.mid or spot_q.last) if spot_q else None
    fut_q = hq.get(chain.future.key) if chain.future else None
    fut = (fut_q.mid or fut_q.last) if fut_q else None
    ref = fut or spot
    legs = [leg for leg in chain.legs.values()
            if band is None or ref is None or abs(leg.strike / ref - 1) <= band]
    leg_quotes = _quotes(ctx, [leg.key for leg in legs], quotes)
    quotes = dict(hq)
    quotes.update(leg_quotes)
    now = ctx.clock.now()
    T = years_to_expiry(expiry, now)
    a = ctx.cfg.analytics
    ivs = []
    if fut and T > 0:
        for leg in legs:
            q = quotes.get(leg.key)
            if q is None:
                continue
            ivs.append(option_iv(q.bid, q.ask, fut, leg.strike, T, a.risk_free, leg.cp, a.iv_min, a.iv_max))
    return ChainSnapshot(chain, now, spot, fut, T, quotes, ivs)
