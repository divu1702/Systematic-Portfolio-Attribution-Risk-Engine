"""Daily instrument-master refresh and option-chain lookups.

Lot sizes, expiries, strikes and strike steps are always read from the broker's instrument
master for the day, never hard-coded. Index options are excluded: an F&O underlying counts as
a stock only if it has an EQ listing on NSE (indices are in the INDICES segment).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

from sqlalchemy import Engine, delete, func, insert, select
from sqlalchemy.orm import Session

from ..broker.base import Broker
from .models import Instrument

log = logging.getLogger(__name__)

OPTION_TYPES = ("CE", "PE")


@dataclass(frozen=True)
class RefreshResult:
    date: date
    stocks: int
    rows: int
    excluded_underlyings: int


def refresh_instruments(engine: Engine, broker: Broker, today: date, keep_days: int = 30) -> RefreshResult:
    nse = broker.instruments("NSE")
    nfo = broker.instruments("NFO")
    equities = {r["tradingsymbol"]: r for r in nse
                if r.get("instrument_type") == "EQ" and r.get("segment") == "NSE"}
    rows, stocks, excluded = [], set(), set()
    for r in nfo:
        if r.get("instrument_type") not in ("FUT", "CE", "PE"):
            continue
        name = r.get("name")
        if name not in equities:
            excluded.add(name)
            continue
        stocks.add(name)
        rows.append(_row(today, r, name))
    for name in stocks:
        rows.append(_row(today, equities[name], name))
    with engine.begin() as conn:
        conn.execute(delete(Instrument).where(Instrument.date == today))
        conn.execute(delete(Instrument).where(Instrument.date < today - timedelta(days=keep_days)))
        if rows:
            conn.execute(insert(Instrument), rows)
    log.info("instrument master %s: %d stock underlyings, %d rows, %d non-stock underlyings excluded",
             today, len(stocks), len(rows), len(excluded))
    return RefreshResult(today, len(stocks), len(rows), len(excluded))


def _row(today: date, r: dict, underlying: str) -> dict:
    strike = r.get("strike")
    return {
        "date": today, "instrument_token": r.get("instrument_token"), "exchange": r["exchange"],
        "tradingsymbol": r["tradingsymbol"], "underlying": underlying,
        "instrument_type": r["instrument_type"], "expiry": r.get("expiry") or None,
        "strike": float(strike) if strike else None, "lot_size": int(r.get("lot_size") or 1),
        "tick_size": float(r.get("tick_size") or 0.05),
    }


def latest_master_date(engine: Engine) -> Optional[date]:
    with engine.connect() as conn:
        return conn.execute(select(func.max(Instrument.date))).scalar()


def fno_stocks(engine: Engine, d: date) -> List[str]:
    with engine.connect() as conn:
        q = (select(Instrument.underlying).where(Instrument.date == d, Instrument.instrument_type == "FUT")
             .distinct().order_by(Instrument.underlying))
        return [r[0] for r in conn.execute(q)]


def expiries(engine: Engine, d: date, underlying: str) -> List[date]:
    with engine.connect() as conn:
        q = (select(Instrument.expiry).where(Instrument.date == d, Instrument.underlying == underlying,
                                             Instrument.instrument_type.in_(OPTION_TYPES))
             .distinct().order_by(Instrument.expiry))
        return [r[0] for r in conn.execute(q)]


@dataclass(frozen=True)
class ChainLeg:
    tradingsymbol: str
    token: int
    strike: float
    cp: str
    lot_size: int
    tick_size: float

    @property
    def key(self) -> str:
        return f"NFO:{self.tradingsymbol}"


@dataclass
class Chain:
    underlying: str
    expiry: date
    lot_size: int
    tick_size: float
    future: Optional[ChainLeg]
    equity_key: str
    equity_token: Optional[int]
    legs: Dict[tuple, ChainLeg]          # (strike, cp) -> leg

    @property
    def strikes(self) -> List[float]:
        return sorted({k for k, _ in self.legs})

    @property
    def strike_step(self) -> float:
        return strike_step(self.strikes)

    def leg(self, strike: float, cp: str) -> Optional[ChainLeg]:
        return self.legs.get((round(strike, 2), cp))

    def keys(self) -> List[str]:
        return [leg.key for leg in self.legs.values()]


def strike_step(strikes: List[float]) -> float:
    """Most common gap between adjacent listed strikes."""
    s = sorted(set(strikes))
    gaps = [round(b - a, 4) for a, b in zip(s, s[1:]) if b > a]
    if not gaps:
        raise ValueError("need at least two strikes to infer the strike step")
    return Counter(gaps).most_common(1)[0][0]


def load_chain(engine: Engine, d: date, underlying: str, expiry: date) -> Chain:
    with Session(engine) as session:
        rows = session.scalars(select(Instrument).where(
            Instrument.date == d, Instrument.underlying == underlying,
            (Instrument.expiry == expiry) | (Instrument.instrument_type == "EQ"))).all()
    legs, fut, eq = {}, None, None
    lot = tick = None
    for inst in rows:
        if inst.instrument_type == "EQ":
            eq = inst
        elif inst.instrument_type == "FUT":
            fut = ChainLeg(inst.tradingsymbol, inst.instrument_token, 0.0, "FUT", inst.lot_size, inst.tick_size)
        else:
            legs[(round(inst.strike, 2), inst.instrument_type)] = ChainLeg(
                inst.tradingsymbol, inst.instrument_token, inst.strike, inst.instrument_type,
                inst.lot_size, inst.tick_size)
            lot, tick = inst.lot_size, inst.tick_size
    if not legs:
        raise LookupError(f"no options for {underlying} expiring {expiry} in the {d} instrument master")
    return Chain(underlying, expiry, lot, tick, fut, f"NSE:{underlying}",
                 eq.instrument_token if eq else None, legs)


def equity_token(engine: Engine, d: date, underlying: str) -> Optional[int]:
    with engine.connect() as conn:
        return conn.execute(select(Instrument.instrument_token).where(
            Instrument.date == d, Instrument.underlying == underlying,
            Instrument.instrument_type == "EQ")).scalar()
