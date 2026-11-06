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


def snapshot_chain(ctx, symbol: str, expiry: date, d: Optional[date] = None,
                   band: Optional[float] = 0.35) -> ChainSnapshot:
    """Quote the futures, the stock and every option within `band` (fraction of spot) of the
    money, and back out IVs from mid prices with Black-76 on the futures price."""
    d = d or ctx.clock.today()
    chain = inst.load_chain(ctx.engine, d, symbol, expiry)
    head = [chain.equity_key] + ([chain.future.key] if chain.future else [])
    hq = ctx.broker.quotes(head)
    spot_q = hq.get(chain.equity_key)
    spot = (spot_q.mid or spot_q.last) if spot_q else None
    fut_q = hq.get(chain.future.key) if chain.future else None
    fut = (fut_q.mid or fut_q.last) if fut_q else None
    ref = fut or spot
    legs = [leg for leg in chain.legs.values()
            if band is None or ref is None or abs(leg.strike / ref - 1) <= band]
    quotes = dict(hq)
    quotes.update(ctx.broker.quotes([leg.key for leg in legs]))
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
