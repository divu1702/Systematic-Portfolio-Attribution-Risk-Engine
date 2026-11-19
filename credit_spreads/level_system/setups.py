"""The four setup detectors and their mirrors (spec: The four setups).

`detect(bars, d, cfg)` returns every setup whose Day 0 is session `d`. `bars` holds completed daily
candles plus, at 15:20, the live row for `d` (the snapshot stands in for the close). Pure functions:
no database, no broker.

Owner-approved readings (19 Nov 2026):
- Only levels not yet broken by a close count (major swings; also each range edge in Setup 4).
- Setup 3: the pullback is at least 2 sessions after the highest high since the last minor swing low,
  with no close above that high; its level is the lowest low from the pullback through Day 0.
- Setup 4: Day 0 is the touch session or one of the 3 sessions after it.
- Setup 2: the breakout level is the highest unbroken major swing high below Day 0's close (mirror: the
  lowest swing low above it); Setup 1 uses the highest swing high Day 0 crossed and closed back below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

import pandas as pd

from .analytics import BEAR_CALL, BULL_PUT, Swing, atr_before, swings, volume_multiple

NAMES = {1: "failed extension", 2: "breakout hold", 3: "trend pullback", 4: "range rejection"}


@dataclass
class Arm:
    """A setup armed on Day 0."""
    setup: int
    direction: str
    level: float
    day0: date
    atr: float
    volume_mult: Optional[float]
    min_vol_mult: float
    extreme: Optional[float] = None        # Setup 1: Day 0's high (bear call) or low (bull put)
    breakout_extreme: Optional[float] = None   # Setup 2: the breakout candle's low (high for the mirror)
    detail: dict = field(default_factory=dict)

    @property
    def vol_ok(self) -> bool:
        return self.volume_mult is not None and self.volume_mult >= self.min_vol_mult - 1e-12

    @property
    def name(self) -> str:
        return f"Setup {self.setup} ({NAMES[self.setup]}{', mirror' if self.mirror else ''})"

    @property
    def mirror(self) -> bool:
        """Setups 1 and 4 sell calls in their main form; Setups 2 and 3 sell puts."""
        return self.direction == (BULL_PUT if self.setup in (1, 4) else BEAR_CALL)


def _row(bars: pd.DataFrame, d: date):
    return bars.loc[d]


def _prev(bars: pd.DataFrame, d: date):
    hist = bars[bars.index < d]
    return hist.iloc[-1] if len(hist) else None


def failed_extension(bars, d, cfg, atr, vm) -> List[Arm]:
    """Setup 1: Day 0's high above an unbroken major swing high, close back below it (mirror: low
    below a swing low, close back above). Several crossed: the highest (lowest) one."""
    row, major, mv = _row(bars, d), cfg.swings.major, cfg.setups.s1_failed_extension.min_vol_mult
    out = []
    hs = [s for s in swings(bars, major.side_bars, "high", end=d, lookback=major.lookback, unbroken=True)
          if row["high"] > s.price > row["close"]]
    if hs:
        lv = max(hs, key=lambda s: s.price)
        out.append(Arm(1, BEAR_CALL, lv.price, d, atr, vm, mv, extreme=float(row["high"]),
                       detail={"swing_date": lv.date.isoformat(), "crossed": len(hs)}))
    ls = [s for s in swings(bars, major.side_bars, "low", end=d, lookback=major.lookback, unbroken=True)
          if row["low"] < s.price < row["close"]]
    if ls:
        lv = min(ls, key=lambda s: s.price)
        out.append(Arm(1, BULL_PUT, lv.price, d, atr, vm, mv, extreme=float(row["low"]),
                       detail={"swing_date": lv.date.isoformat(), "crossed": len(ls)}))
    return out


def breakout_hold(bars, d, cfg, atr, vm) -> List[Arm]:
    """Setup 2: Day 0 closes above an unbroken major swing high (mirror: below a swing low)."""
    row, major, mv = _row(bars, d), cfg.swings.major, cfg.setups.s2_breakout_hold.min_vol_mult
    out = []
    hs = [s for s in swings(bars, major.side_bars, "high", end=d, lookback=major.lookback, unbroken=True)
          if row["close"] > s.price]
    if hs:
        lv = max(hs, key=lambda s: s.price)
        out.append(Arm(2, BULL_PUT, lv.price, d, atr, vm, mv, breakout_extreme=float(row["low"]),
                       detail={"swing_date": lv.date.isoformat(), "crossed": len(hs),
                               "gap": bool(row["open"] > lv.price)}))
    ls = [s for s in swings(bars, major.side_bars, "low", end=d, lookback=major.lookback, unbroken=True)
          if row["close"] < s.price]
    if ls:
        lv = min(ls, key=lambda s: s.price)
        out.append(Arm(2, BEAR_CALL, lv.price, d, atr, vm, mv, breakout_extreme=float(row["high"]),
                       detail={"swing_date": lv.date.isoformat(), "crossed": len(ls),
                               "gap": bool(row["open"] < lv.price)}))
    return out


def trend_pullback(bars, d, cfg, atr, vm) -> List[Arm]:
    """Setup 3: rising minor swings, a pullback of at least `min_pullback_sessions` after the highest
    high since the last minor swing low that stays above that low, then a close above the previous
    session's high. Level: the lowest low from the pullback through Day 0. Mirror for downtrends."""
    s3, side = cfg.setups.s3_trend_pullback, cfg.swings.minor.side_bars
    row, prev = _row(bars, d), _prev(bars, d)
    if prev is None:
        return []
    hist = bars[bars.index < d]
    hs, ls = swings(bars, side, "high", end=d), swings(bars, side, "low", end=d)
    if len(hs) < 2 or len(ls) < 2:
        return []
    out = []
    if hs[-1].price > hs[-2].price and ls[-1].price > ls[-2].price and row["close"] > prev["high"]:
        anchor = ls[-1]
        after = hist.iloc[anchor.pos + 1:]
        if len(after):
            highs = after["high"].to_numpy(float)
            peak = anchor.pos + 1 + int(len(highs) - 1 - highs[::-1].argmax())      # the last time it peaked
            pull = hist.iloc[peak + 1:]
            if len(pull) >= s3.min_pullback_sessions:
                level = float(min(pull["low"].min(), row["low"]))
                if level > anchor.price:
                    out.append(Arm(3, BULL_PUT, level, d, atr, vm, s3.min_vol_mult,
                                   detail={"swing_low": anchor.price, "peak": float(hist.iloc[peak]["high"]),
                                           "pullback_sessions": len(pull)}))
    if hs[-1].price < hs[-2].price and ls[-1].price < ls[-2].price and row["close"] < prev["low"]:
        anchor = hs[-1]
        after = hist.iloc[anchor.pos + 1:]
        if len(after):
            lows = after["low"].to_numpy(float)
            trough = anchor.pos + 1 + int(len(lows) - 1 - lows[::-1].argmin())
            bounce = hist.iloc[trough + 1:]
            if len(bounce) >= s3.min_pullback_sessions:
                level = float(max(bounce["high"].max(), row["high"]))
                if level < anchor.price:
                    out.append(Arm(3, BEAR_CALL, level, d, atr, vm, s3.min_vol_mult,
                                   detail={"swing_high": anchor.price, "trough": float(hist.iloc[trough]["low"]),
                                           "pullback_sessions": len(bounce)}))
    return out


def _clusters(points: List[Swing], tol: float) -> List[List[Swing]]:
    """Maximal groups of at least two swings whose prices lie within `tol` of each other."""
    pts = sorted(points, key=lambda s: s.price)
    out, seen = [], set()
    for i in range(len(pts)):
        j = i
        while j + 1 < len(pts) and pts[j + 1].price - pts[i].price <= tol + 1e-12:
            j += 1
        if j > i:
            key = (i, j)
            if not any(a <= i and j <= b for a, b in seen):
                seen.add(key)
                out.append(pts[i:j + 1])
    return out


def range_rejection(bars, d, cfg, atr, vm) -> List[Arm]:
    """Setup 4: a range of clustered minor swing highs and lows (last `lookback` sessions, height at least
    `min_height_atr` x ATR); a touch of the top within `touch_atr` x ATR without closing above it; then,
    on the touch session or within `day0_within_sessions` after it, a close below the previous session's
    low. Level: the range top. Mirror at the bottom. Edges closed beyond since they formed do not count."""
    s4, side = cfg.setups.s4_range_rejection, cfg.swings.minor.side_bars
    row, prev = _row(bars, d), _prev(bars, d)
    if prev is None:
        return []
    upto = bars[bars.index <= d]
    pos_d = len(upto) - 1
    hs = swings(bars, side, "high", end=d, lookback=s4.lookback)
    ls = swings(bars, side, "low", end=d, lookback=s4.lookback)
    tops = [(sum(s.price for s in c) / len(c), c) for c in _clusters(hs, s4.cluster_atr * atr)]
    bottoms = [(sum(s.price for s in c) / len(c), c) for c in _clusters(ls, s4.cluster_atr * atr)]
    closes, highs, lows = (upto[c].to_numpy(float) for c in ("close", "high", "low"))
    touch_tol = s4.touch_atr * atr
    first_touch = max(0, pos_d - s4.day0_within_sessions)
    out = []

    def intact(edge: float, since: int, above: bool) -> bool:
        seg = closes[since:pos_d + 1]
        return not len(seg) or (seg.max() <= edge if above else seg.min() >= edge)

    if row["close"] < prev["low"]:
        best = None
        for top, members in tops:
            if not any(top - b >= s4.min_height_atr * atr for b, _ in bottoms):
                continue
            if not intact(top, min(s.pos for s in members) + 1, above=True):
                continue
            for t in range(pos_d, first_touch - 1, -1):
                if top - touch_tol <= highs[t] <= top + touch_tol and closes[t] <= top:
                    gap = abs(highs[t] - top)
                    if best is None or gap < best[2]:
                        best = (top, t, gap, members)
                    break
        if best:
            top, t, _, members = best
            out.append(Arm(4, BEAR_CALL, float(top), d, atr, vm, s4.min_vol_mult,
                           detail={"touch_date": upto.index[t].isoformat(), "edge_swings": len(members),
                                   "bottom": max(b for b, _ in bottoms if top - b >= s4.min_height_atr * atr)}))
    if row["close"] > prev["high"]:
        best = None
        for bottom, members in bottoms:
            if not any(t_ - bottom >= s4.min_height_atr * atr for t_, _ in tops):
                continue
            if not intact(bottom, min(s.pos for s in members) + 1, above=False):
                continue
            for t in range(pos_d, first_touch - 1, -1):
                if bottom - touch_tol <= lows[t] <= bottom + touch_tol and closes[t] >= bottom:
                    gap = abs(lows[t] - bottom)
                    if best is None or gap < best[2]:
                        best = (bottom, t, gap, members)
                    break
        if best:
            bottom, t, _, members = best
            out.append(Arm(4, BULL_PUT, float(bottom), d, atr, vm, s4.min_vol_mult,
                           detail={"touch_date": upto.index[t].isoformat(), "edge_swings": len(members),
                                   "top": min(t_ for t_, _ in tops if t_ - bottom >= s4.min_height_atr * atr)}))
    return out


def detect(bars: pd.DataFrame, d: date, cfg) -> List[Arm]:
    """Every setup with Day 0 on `d`. A Setup 4 move that also qualifies as Setup 1 in the same direction
    (on Day 0 or any session since the touch) is treated as Setup 1."""
    if d not in bars.index:
        return []
    atr = atr_before(bars, d, cfg.atr_period)
    if atr is None or atr <= 0:
        return []
    vm = volume_multiple(bars, d, cfg.volume_avg_sessions)
    s1 = failed_extension(bars, d, cfg, atr, vm)
    out = s1 + breakout_hold(bars, d, cfg, atr, vm) + trend_pullback(bars, d, cfg, atr, vm)
    for a in range_rejection(bars, d, cfg, atr, vm):
        touch = date.fromisoformat(a.detail["touch_date"])
        days = [x for x in bars.index if touch <= x <= d]
        if any(b.direction == a.direction for x in days for b in
               (s1 if x == d else failed_extension(bars[bars.index <= x], x, cfg, atr, vm))):
            continue
        out.append(a)
    return out
