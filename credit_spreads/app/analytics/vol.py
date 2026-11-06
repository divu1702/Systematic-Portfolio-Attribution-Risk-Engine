"""Realised volatility, ATR, percentiles and implied-vol surface helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from . import black76


# --------------------------------------------------------------------------- realised vol

def hv(close: pd.Series, window: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Rolling close-to-close historical volatility, annualised (e.g. HV20)."""
    rets = np.log(close.astype(float)).diff()
    return rets.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(periods_per_year)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's ATR: seeded with the simple mean of the first `window` true ranges."""
    tr = true_range(high.astype(float), low.astype(float), close.astype(float))
    out = pd.Series(np.nan, index=tr.index, dtype=float)
    if len(tr) < window:
        return out
    vals = tr.to_numpy()
    a = vals[:window].mean()
    out.iloc[window - 1] = a
    for i in range(window, len(vals)):
        a = (a * (window - 1) + vals[i]) / window
        out.iloc[i] = a
    return out


def percentile_rank(history: Iterable[float], value: float) -> Optional[float]:
    """Percent of history strictly below value (0-100). None if history is empty."""
    h = np.asarray([x for x in history if x is not None and not math.isnan(x)], dtype=float)
    if h.size == 0 or value is None or math.isnan(value):
        return None
    return float((h < value).mean() * 100.0)


def rolling_percentile_rank(series: pd.Series, window: int) -> pd.Series:
    """For each point, its percentile rank within the trailing `window` values (inclusive)."""
    vals = series.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        if math.isnan(vals[i]):
            continue
        lo = max(0, i - window + 1)
        hist = vals[lo:i + 1]
        hist = hist[~np.isnan(hist)]
        if hist.size >= 2:
            out[i] = (hist[:-1] < vals[i]).mean() * 100.0
    return pd.Series(out, index=series.index)


def percentile_value(history: Sequence[float], pctile: float) -> Optional[float]:
    h = np.asarray([x for x in history if x is not None and not math.isnan(x)], dtype=float)
    if h.size == 0:
        return None
    return float(np.percentile(h, pctile))


# --------------------------------------------------------------------------- implied vol

@dataclass(frozen=True)
class OptionIV:
    strike: float
    cp: str               # CE / PE
    mid: float
    iv: Optional[float]
    delta: Optional[float]


def option_iv(bid: float, ask: float, F: float, K: float, T: float, r: float, cp: str,
              lo: float = 0.01, hi: float = 5.0) -> OptionIV:
    """IV from the mid price. Quotes with a zero bid or ask are skipped (iv=None)."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return OptionIV(K, cp, 0.0, None, None)
    mid = 0.5 * (bid + ask)
    iv = black76.implied_vol(mid, F, K, T, r, cp, lo, hi)
    d = black76.delta(F, K, T, iv, r, cp) if iv else None
    return OptionIV(K, cp, mid, iv, d)


def strike_iv(ivs: List[OptionIV], K: float) -> Optional[float]:
    """IV at a strike: mean of call and put IVs where both are available."""
    vals = [o.iv for o in ivs if o.strike == K and o.iv]
    return float(np.mean(vals)) if vals else None


def atm_iv(ivs: List[OptionIV], F: float) -> Optional[float]:
    """Interpolate the IVs of the two strikes nearest the futures price (spec: ATM IV)."""
    strikes = sorted({o.strike for o in ivs if o.iv})
    if not strikes:
        return None
    below = [k for k in strikes if k <= F]
    above = [k for k in strikes if k >= F]
    if not below or not above:
        return None
    k1, k2 = below[-1], above[0]
    v1, v2 = strike_iv(ivs, k1), strike_iv(ivs, k2)
    if v1 is None or v2 is None:
        return None
    if k1 == k2:
        return v1
    w = (F - k1) / (k2 - k1)
    return v1 + w * (v2 - v1)


def delta_iv(ivs: List[OptionIV], target: float, cp: str) -> Optional[float]:
    """IV at a target absolute delta (e.g. 0.25) on one side, by linear interpolation in delta
    between the two OTM options that bracket it."""
    side = sorted([o for o in ivs if o.cp == cp and o.iv and o.delta is not None],
                  key=lambda o: abs(o.delta))
    for a, b in zip(side, side[1:]):
        da, db = abs(a.delta), abs(b.delta)
        if da <= target <= db and db > da:
            w = (target - da) / (db - da)
            return a.iv + w * (b.iv - a.iv)
    return None
