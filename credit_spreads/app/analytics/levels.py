"""Mechanical higher-timeframe support and resistance (spec: "Higher-timeframe levels").

1. Swing highs and lows on daily and weekly bars: a fractal is a bar whose high (low) is
   above (below) the `n` bars on each side, over the last `lookback` sessions.
2. Pivots within `cluster_atr` x ATR of a cluster's running mean join that cluster.
3. Each pivot scores timeframe_weight x 0.5 ** (age_in_sessions / recency_halflife); a
   cluster's score is the sum (more touches and more recent touches score higher).
4. A cluster below spot is support, above spot is resistance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import pandas as pd


@dataclass(frozen=True)
class Pivot:
    date: date
    price: float
    kind: str           # "high" or "low"
    timeframe: str      # "daily" or "weekly"


@dataclass
class LevelCluster:
    price: float
    touches: int
    score: float
    timeframe: str                 # "weekly" if any weekly pivot, else "daily"
    last_touch: date
    pivots: List[Pivot] = field(default_factory=list)

    def kind(self, spot: float) -> str:
        return "support" if self.price < spot else "resistance"


def fractal_pivots(df: pd.DataFrame, n: int, timeframe: str) -> List[Pivot]:
    """Highs strictly above the n bars before and at least as high as the n bars after (so a
    flat double-top is counted once); lows mirrored."""
    highs, lows, idx = df["high"].to_numpy(float), df["low"].to_numpy(float), list(df.index)
    out: List[Pivot] = []
    for i in range(n, len(df) - n):
        h, lo = highs[i], lows[i]
        if h > highs[i - n:i].max() and h >= highs[i + 1:i + n + 1].max():
            out.append(Pivot(_as_date(idx[i]), float(h), "high", timeframe))
        if lo < lows[i - n:i].min() and lo <= lows[i + 1:i + n + 1].min():
            out.append(Pivot(_as_date(idx[i]), float(lo), "low", timeframe))
    return out


def _as_date(x) -> date:
    return x.date() if hasattr(x, "date") and callable(x.date) else x


def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy()
    d.index = pd.to_datetime(d.index)
    w = d.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return w


def compute_levels(daily: pd.DataFrame, atr_value: float, *, fractal_n: int = 5, lookback: int = 250,
                   cluster_atr: float = 0.5, recency_halflife: int = 60,
                   weekly_weight: float = 2.0) -> List[LevelCluster]:
    """Scored level clusters from completed daily bars (index = date, columns high/low/...)."""
    if daily.empty or not atr_value or atr_value <= 0:
        return []
    window = daily.iloc[-lookback:]
    pivots = fractal_pivots(window, fractal_n, "daily")
    weekly = weekly_bars(window)
    if len(weekly) > 2 * fractal_n:
        pivots += fractal_pivots(weekly, fractal_n, "weekly")
    if not pivots:
        return []
    session_dates = [_as_date(x) for x in window.index]
    last = session_dates[-1]

    def age(d: date) -> int:
        # sessions between the pivot and the last bar (weekly pivots dated by their week end)
        return sum(1 for s in session_dates if d < s <= last)

    threshold = cluster_atr * atr_value
    clusters: List[List[Pivot]] = []
    for p in sorted(pivots, key=lambda p: p.price):
        if clusters:
            members = clusters[-1]
            mean = sum(m.price for m in members) / len(members)
            if abs(p.price - mean) <= threshold:
                members.append(p)
                continue
        clusters.append([p])

    out = []
    for members in clusters:
        weights = [(weekly_weight if m.timeframe == "weekly" else 1.0) * 0.5 ** (age(m.date) / recency_halflife)
                   for m in members]
        wsum = sum(weights)
        price = sum(m.price * w for m, w in zip(members, weights)) / wsum
        out.append(LevelCluster(
            price=round(price, 2), touches=len(members), score=round(wsum, 4),
            timeframe="weekly" if any(m.timeframe == "weekly" for m in members) else "daily",
            last_touch=max(m.date for m in members), pivots=members))
    return sorted(out, key=lambda c: c.price)


def nearest_support(levels: List[LevelCluster], spot: float) -> Optional[LevelCluster]:
    below = [lv for lv in levels if lv.price < spot]
    return max(below, key=lambda lv: lv.price) if below else None


def nearest_resistance(levels: List[LevelCluster], spot: float) -> Optional[LevelCluster]:
    above = [lv for lv in levels if lv.price > spot]
    return min(above, key=lambda lv: lv.price) if above else None
