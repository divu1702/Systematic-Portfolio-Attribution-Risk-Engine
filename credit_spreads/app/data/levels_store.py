"""Level engine job: compute levels for each symbol from stored daily bars, apply the owner's
levels_manual.csv (manual levels replace computed ones for that symbol), and store them."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional

from sqlalchemy import Engine, delete, func, insert, select

from ..analytics.levels import compute_levels
from . import csvs
from .daily import load_daily
from .models import Level

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredLevel:
    price: float
    kind: str
    timeframe: str
    touches: int
    score: float
    source: str


def refresh_levels(ctx, symbols: Iterable[str], asof: date) -> Dict[str, int]:
    lc = ctx.cfg.levels
    manual: Dict[str, List[csvs.ManualLevel]] = {}
    for m in csvs.manual_levels(ctx.cfg.resolve(ctx.cfg.paths.levels_manual)):
        manual.setdefault(m.symbol, []).append(m)
    counts = {}
    for sym in symbols:
        daily = load_daily(ctx.engine, sym, end=asof)
        if daily.empty:
            continue
        spot = float(daily["close"].iloc[-1])
        rows = []
        if sym in manual:
            for m in manual[sym]:
                kind = m.kind or ("support" if m.price < spot else "resistance")
                rows.append(dict(date=asof, symbol=sym, price=m.price, kind=kind, timeframe="manual",
                                 touches=0, score=lc.manual_score, source="manual"))
        else:
            atr = daily["atr14"].dropna()
            if atr.empty:
                continue
            for c in compute_levels(daily, float(atr.iloc[-1]), fractal_n=lc.fractal_n, lookback=lc.lookback,
                                    cluster_atr=lc.cluster_atr, recency_halflife=lc.recency_halflife,
                                    weekly_weight=lc.weekly_weight):
                rows.append(dict(date=asof, symbol=sym, price=c.price, kind=c.kind(spot), timeframe=c.timeframe,
                                 touches=c.touches, score=c.score, source="auto"))
        with ctx.engine.begin() as conn:
            conn.execute(delete(Level).where(Level.symbol == sym, Level.date == asof))
            if rows:
                conn.execute(insert(Level), rows)
        counts[sym] = len(rows)
    return counts


def load_levels(engine: Engine, symbol: str, asof: date) -> List[StoredLevel]:
    """Most recent stored levels on or before `asof`."""
    with engine.connect() as conn:
        d = conn.execute(select(func.max(Level.date)).where(Level.symbol == symbol, Level.date <= asof)).scalar()
        if d is None:
            return []
        rows = conn.execute(select(Level).where(Level.symbol == symbol, Level.date == d)
                            .order_by(Level.price)).mappings().all()
    return [StoredLevel(r["price"], r["kind"], r["timeframe"], r["touches"], r["score"], r["source"]) for r in rows]
