"""Analytics for the level system (spec: The four setups, Strength signals, Confirmation, Sizing).

Every function is pure: bars, candles and quotes in, numbers out, so each one is tested against
hand-computed values. Conventions used throughout:

- `bars`: daily candles indexed by date (open, high, low, close, volume), oldest first.
- ATR is Wilder's 14-day ATR on the completed sessions before the day being judged (the value known
  at that day's open), so a session's own range never moves its own thresholds.
- A swing high is a high strictly above the `side_bars` sessions before it and at least as high as the
  `side_bars` sessions after it (a flat double top counts once); swing lows mirror this. A swing is
  known only once all `side_bars` sessions after it have closed, so swings judged for Day 0 use bars
  before Day 0. Major swings must also be unbroken: no close beyond them since they formed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from app.analytics import black76
from app.analytics.vol import atr as wilder_atr

BEAR_CALL, BULL_PUT = "bear_call", "bull_put"


def own_type(direction: str) -> str:
    """The option type a spread sells: calls for bear calls, puts for bull puts."""
    return "CE" if direction == BEAR_CALL else "PE"


def opposite_type(direction: str) -> str:
    return "PE" if direction == BEAR_CALL else "CE"


# ----------------------------------------------------------------------------- ATR and volume
def _before(bars: pd.DataFrame, d: date) -> pd.DataFrame:
    return bars[bars.index < d]


def atr_before(bars: pd.DataFrame, d: date, period: int = 14) -> Optional[float]:
    """Wilder's ATR on the completed sessions before `d`."""
    hist = _before(bars, d)
    if len(hist) < period:
        return None
    v = wilder_atr(hist["high"], hist["low"], hist["close"], period).iloc[-1]
    return None if pd.isna(v) else float(v)


def avg_volume(bars: pd.DataFrame, d: date, n: int = 20) -> Optional[float]:
    """Mean cash-market volume of the `n` sessions before `d`."""
    hist = _before(bars, d).tail(n)
    if len(hist) < n:
        return None
    return float(hist["volume"].astype(float).mean())


def volume_multiple(bars: pd.DataFrame, d: date, n: int = 20) -> Optional[float]:
    avg = avg_volume(bars, d, n)
    if not avg or d not in bars.index:
        return None
    return float(bars.loc[d, "volume"]) / avg


# ----------------------------------------------------------------------------- swings
@dataclass(frozen=True)
class Swing:
    date: date
    price: float
    kind: str           # "high" or "low"
    pos: int            # row position in the bars frame


def swings(bars: pd.DataFrame, side_bars: int, kind: str, end: Optional[date] = None,
           lookback: Optional[int] = None, unbroken: bool = False) -> List[Swing]:
    """Swing highs or lows confirmed by the sessions before `end` (all of `bars` when None), oldest
    first. `lookback`: only swings within the last `lookback` sessions before `end`. `unbroken`: only
    swings no later close has gone beyond (above a high, below a low)."""
    hist = bars if end is None else _before(bars, end)
    n = len(hist)
    highs, lows, closes = (hist[c].to_numpy(float) for c in ("high", "low", "close"))
    first = max(side_bars, n - lookback if lookback else 0)
    out: List[Swing] = []
    for i in range(first, n - side_bars):
        if kind == "high":
            v = highs[i]
            if not (v > highs[i - side_bars:i].max() and v >= highs[i + 1:i + side_bars + 1].max()):
                continue
            if unbroken and i + 1 < n and closes[i + 1:].max() > v:
                continue
        elif kind == "low":
            v = lows[i]
            if not (v < lows[i - side_bars:i].min() and v <= lows[i + 1:i + side_bars + 1].min()):
                continue
            if unbroken and i + 1 < n and closes[i + 1:].min() < v:
                continue
        else:
            raise ValueError(f"kind must be 'high' or 'low', got {kind!r}")
        out.append(Swing(hist.index[i], float(v), kind, i))
    return out


def trend(bars: pd.DataFrame, side_bars: int = 5, end: Optional[date] = None) -> str:
    """'up' when the last two swing highs and the last two swing lows both rise, 'down' when both
    fall, otherwise 'neutral' (spec: Setup 3 trend; Nifty 50 trend at entry)."""
    hs, ls = swings(bars, side_bars, "high", end), swings(bars, side_bars, "low", end)
    if len(hs) < 2 or len(ls) < 2:
        return "neutral"
    if hs[-1].price > hs[-2].price and ls[-1].price > ls[-2].price:
        return "up"
    if hs[-1].price < hs[-2].price and ls[-1].price < ls[-2].price:
        return "down"
    return "neutral"


def trend_agrees(nifty_trend: str, direction: str) -> Optional[bool]:
    """Whether the market trend favours the trade (bull puts in an uptrend, bear calls in a downtrend);
    None when the trend is neutral."""
    if nifty_trend == "neutral":
        return None
    return (nifty_trend == "up") == (direction == BULL_PUT)


# ----------------------------------------------------------------------------- intraday signals
def vwap(candles: Sequence[dict]) -> Optional[float]:
    """Volume-weighted average of each candle's typical price (high + low + close) / 3."""
    num = den = 0.0
    for c in candles:
        v = float(c.get("volume") or 0)
        if v <= 0:
            continue
        num += v * (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        den += v
    return num / den if den > 0 else None


def hvn(candles: Sequence[dict], bucket: float) -> Optional[float]:
    """High-volume node: the centre of the price bucket (width `bucket`, edges at multiples of it)
    holding the most volume, each candle's volume assigned at its typical price. Ties go to the
    lower bucket."""
    if not candles or bucket <= 0:
        return None
    vol: Dict[int, float] = {}
    for c in candles:
        v = float(c.get("volume") or 0)
        if v <= 0:
            continue
        tp = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        b = int(math.floor(tp / bucket + 1e-9))
        vol[b] = vol.get(b, 0.0) + v
    if not vol:
        return None
    best = max(sorted(vol), key=lambda b: vol[b])
    return (best + 0.5) * bucket


def dist_atr(x: Optional[float], level: float, atr: Optional[float]) -> Optional[float]:
    """Signed distance from the level in ATR (positive above the level)."""
    if x is None or not atr:
        return None
    return (x - level) / atr


# ----------------------------------------------------------------------------- zone strikes and OI
def setup1_zone(direction: str, level: float, extreme: float, strikes: Sequence[float],
                n_other: int = 3) -> Tuple[List[float], List[float]]:
    """Setup 1. Bear call: protected = listed strikes from the one at or just below the level up to the
    one at or just above Day 0's high; other side = the `n_other` strikes below that zone. Bull put
    mirrored (from at or just above the level down to at or just below Day 0's low; other side above)."""
    ks = sorted(set(strikes))
    if direction == BEAR_CALL:
        lo = max((k for k in ks if k <= level), default=None)
        hi = min((k for k in ks if k >= extreme), default=None)
        if lo is None or hi is None:
            return [], []
        prot = [k for k in ks if lo <= k <= hi]
        other = [k for k in ks if k < lo][-n_other:][::-1]
    else:
        hi = min((k for k in ks if k >= level), default=None)
        lo = max((k for k in ks if k <= extreme), default=None)
        if lo is None or hi is None:
            return [], []
        prot = [k for k in ks if lo <= k <= hi][::-1]
        other = [k for k in ks if k > hi][:n_other]
    return prot, other


def level_zone(direction: str, level: float, strikes: Sequence[float], n: int = 3) -> Tuple[List[float], List[float]]:
    """Setups 2-4: protected = the `n` strikes strictly beyond the level on the side the trade protects
    (above for bear calls, below for bull puts); other side = the `n` strikes at or beyond the level
    on the opposite side. Nearest first."""
    ks = sorted(set(strikes))
    above_strict = [k for k in ks if k > level][:n]
    below_strict = [k for k in ks if k < level][-n:][::-1]
    at_or_above = [k for k in ks if k >= level][:n]
    at_or_below = [k for k in ks if k <= level][-n:][::-1]
    if direction == BEAR_CALL:
        return above_strict, at_or_below
    return below_strict, at_or_above


@dataclass
class ZoneOI:
    protected: float            # own type, protected strikes
    other: float                # opposite type, other-side strikes
    calls: float                # every call at the watched strikes
    puts: float                 # every put at the watched strikes
    chain_total: float          # every option of the expiry
    missing: List[str] = field(default_factory=list)

    @property
    def share(self) -> Optional[float]:
        """Zone OI share: OI at the watched strikes / OI of the whole expiry's chain."""
        return (self.calls + self.puts) / self.chain_total if self.chain_total else None

    @property
    def local_pcr(self) -> Optional[float]:
        return self.puts / self.calls if self.calls else None


def zone_oi(oi: Dict[Tuple[float, str], float], direction: str, protected: Sequence[float],
            other: Sequence[float]) -> ZoneOI:
    """Sum OI (quantity) over each group of strikes; `oi` maps (strike, "CE"/"PE") to OI for the whole
    expiry's chain."""
    own, opp = own_type(direction), opposite_type(direction)
    missing = [f"{k:g}{own}" for k in protected if (k, own) not in oi] + \
              [f"{k:g}{opp}" for k in other if (k, opp) not in oi]
    watched = set(protected) | set(other)
    return ZoneOI(
        protected=float(sum(oi.get((k, own), 0) for k in protected)),
        other=float(sum(oi.get((k, opp), 0) for k in other)),
        calls=float(sum(oi.get((k, "CE"), 0) for k in watched)),
        puts=float(sum(oi.get((k, "PE"), 0) for k in watched)),
        chain_total=float(sum(oi.values())), missing=missing)


@dataclass(frozen=True)
class OITest:
    passed: bool
    protected_change: Optional[float]      # e.g. -0.04 = protected-side OI 4% below Day 0
    other_change: Optional[float]
    reason: str = ""


def oi_test(day0: ZoneOI, now: ZoneOI, tolerance: float = 0.10) -> OITest:
    """Protected side: own-type OI must not fall more than `tolerance` below Day 0. Other side:
    opposite-type OI must not rise more than `tolerance` above Day 0. Totals, never strike by strike.
    A side with no OI on Day 0 cannot fall; on the other side any new OI counts as a rise."""
    if now.missing or day0.missing:
        return OITest(False, None, None, "OI missing for " + ", ".join(sorted(set(now.missing + day0.missing))))
    pc = now.protected / day0.protected - 1.0 if day0.protected > 0 else None
    oc = now.other / day0.other - 1.0 if day0.other > 0 else (math.inf if now.other > 0 else None)
    fails = []
    if pc is not None and pc < -tolerance - 1e-12:
        fails.append(f"protected-side OI {pc:+.1%} (limit -{tolerance:.0%})")
    if oc is not None and oc > tolerance + 1e-12:
        fails.append("other-side OI rose from zero" if math.isinf(oc) else
                     f"other-side OI {oc:+.1%} (limit +{tolerance:.0%})")
    return OITest(not fails, pc, None if oc is not None and math.isinf(oc) else oc, "; ".join(fails))


# ----------------------------------------------------------------------------- Black-76 repricing
def years_to_expiry(now: datetime, expiry: date, less_days: float = 0.0) -> float:
    """Calendar-time years from `now` to 15:30 on expiry day, less `less_days` days."""
    end = datetime.combine(expiry, datetime.min.time()).replace(hour=15, minute=30)
    return max(black76.year_fraction((end - now).total_seconds()) - less_days / 365.0, 0.0)


def leg_iv(mid: Optional[float], F: float, K: float, T: float, r: float, cp: str) -> Optional[float]:
    if not mid or mid <= 0 or T <= 0:
        return None
    return black76.implied_vol(mid, F, K, T, r, cp)


@dataclass(frozen=True)
class KillLoss:
    per_lot: float              # rupees lost per lot if price reaches the level
    cost_to_close: float        # per share, at natural prices, repriced at the level
    short_iv: float             # current IVs (before the cushion)
    long_iv: float
    forward: float              # the forward with spot moved to the level
    T: float


def loss_at_kill(*, level: float, spot: float, future: float, now: datetime, expiry: date, cp: str,
                 short_k: float, long_k: float, short_q, long_q, credit: float, lot_size: int, r: float,
                 cushion_points: float, exit_charges_per_lot) -> Optional[KillLoss]:
    """Spec (Sizing, step 1): reprice the spread with spot at the level, each leg at its current IV plus
    `cushion_points` volatility points, one day less to expiry (Black-76, the same-expiry future as the
    forward, scaled to the level by today's future/spot ratio). The repriced short leg is bought back at
    model value plus its current half spread, the long leg sold at model value less its half spread.
    Loss per lot = (cost to close - entry credit) x lot size + exit charges.
    `exit_charges_per_lot(short_px, long_px)` gives the exit charges for one lot."""
    T_now = years_to_expiry(now, expiry)
    if T_now <= 0 or spot <= 0 or future <= 0:
        return None
    s_mid, l_mid = short_q.mid, long_q.mid
    s_iv = leg_iv(s_mid, future, short_k, T_now, r, cp)
    l_iv = leg_iv(l_mid, future, long_k, T_now, r, cp)
    if s_iv is None or l_iv is None:
        return None
    F_kill = level * future / spot
    T_kill = max(T_now - 1.0 / 365.0, 1e-6)
    bump = cushion_points / 100.0
    s_model = black76.price(F_kill, short_k, T_kill, s_iv + bump, r, cp)
    l_model = black76.price(F_kill, long_k, T_kill, l_iv + bump, r, cp)
    s_px = s_model + (short_q.ask - short_q.bid) / 2.0
    l_px = max(l_model - (long_q.ask - long_q.bid) / 2.0, 0.0)
    ctc = s_px - l_px
    per_lot = (ctc - credit) * lot_size + exit_charges_per_lot(s_px, l_px)
    return KillLoss(per_lot, ctc, s_iv, l_iv, F_kill, T_kill)
