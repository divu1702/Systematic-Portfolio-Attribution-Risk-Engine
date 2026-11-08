"""Entry rules: what one strategy concludes from a stock's signals.

Stock-level gates, in order: the DTE window, F2 events, F3 IV/HV (0 = off), F4 IV percentile and the
F5 timing rule (iv.timing: the spec's "cause passed", the review's "shock passed", or off). Then the
side to sell and the price behind the trade (direction.method): a scored support/resistance level
near the price (the spec), or the trend average (bull put above it, bear call below). That price is
the trade's thesis-stop reference; a level also anchors F6. Every threshold comes from the
strategy's own config (ctx.cfg).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from .. import market
from ..data import events as ev_data
from ..data.calendar import TradingCalendar
from ..data.levels_store import StoredLevel
from .candidates import flags_upto
from .signals import StockSignals, iv_history
from .spreads import BEAR_CALL, BULL_PUT


def f4_pass(ctx, ivp: Optional[float], method: str) -> bool:
    ic = ctx.cfg.iv
    if ivp is None:
        return False
    return ivp >= (ic.min_ivp if method == "true" else ic.proxy_hv_pctile)


def f5_check(ctx, symbol: str, today: date, daily, cal: TradingCalendar) -> Tuple[bool, str, Dict]:
    """The spec's F5: IV was below its F4 threshold within the lookback and a cause (price move >= k x
    ATR, or a results/news date) happened on or after that low-IV day and before today."""
    ic = ctx.cfg.iv
    hist = iv_history(ctx, symbol, today, ic.spike_lookback)
    window_days = set(cal.last_n(cal.prev_trading_day(today), ic.spike_lookback))
    hist = [h for h in hist if h.date in window_days]
    if not hist:
        return False, "no IV snapshots in look-back (history still building)", {}
    lows = [h.date for h in hist
            if h.ivp_252 is not None and h.ivp_252 < (ic.min_ivp if h.method == "true" else ic.proxy_hv_pctile)]
    if not lows:
        return False, f"IV not below threshold in last {ic.spike_lookback} sessions", {}
    first_low = min(lows)
    causes = []
    d = daily[daily.index >= first_low]
    prev_close = daily["close"].shift(1)
    prev_atr = daily["atr14"].shift(1)
    for dt_, row in d.iterrows():
        pc, pa = prev_close.get(dt_), prev_atr.get(dt_)
        if pc and pa and not math.isnan(pa) and abs(row["close"] - pc) >= ic.spike_move_atr * pa:
            causes.append(f"{dt_} move {abs(row['close'] - pc) / pa:.1f}xATR")
    for e in ev_data.events_between(ctx.engine, symbol, first_low, today - timedelta(days=1)):
        causes.append(f"{e.event_date} {e.event_type}")
    info = {"low_iv_days": [str(x) for x in lows], "causes": causes}
    if not causes:
        return False, "IV rose without a qualifying price move or event", info
    return True, "", info


def choose_direction(levels: List[StoredLevel], spot: float, atr: float, max_dist_atr: float):
    """Bull put if spot is within max_dist_atr x ATR above a support; bear call if within that
    distance below a resistance; if both, the higher-scored level wins. Returns (side, level)."""
    lim = max_dist_atr * atr
    sup = [lv for lv in levels if 0 <= spot - lv.price <= lim]
    res = [lv for lv in levels if 0 <= lv.price - spot <= lim]
    best_sup = max(sup, key=lambda lv: lv.price) if sup else None      # nearest support
    best_res = min(res, key=lambda lv: lv.price) if res else None      # nearest resistance
    if best_sup and best_res:
        return (BULL_PUT, best_sup) if best_sup.score >= best_res.score else (BEAR_CALL, best_res)
    if best_sup:
        return BULL_PUT, best_sup
    if best_res:
        return BEAR_CALL, best_res
    return None, None


@dataclass
class EntryDecision:
    trade_exp: Optional[date]
    dte: Optional[int]
    stage: str = ""                        # first failing stage; "" = go on to build spreads
    reason: str = ""
    flag_n: Optional[int] = None
    side: Optional[str] = None
    anchor: Optional[float] = None         # thesis-stop reference: the level or the trend average
    anchor_kind: Optional[str] = None      # "level" (also anchors F6) or "trend"
    flags: Dict[str, Optional[bool]] = field(default_factory=dict)
    inputs: Dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.stage


def decide_entry(ctx, sig: StockSignals, cal: TradingCalendar) -> EntryDecision:
    cfg = ctx.cfg
    trade_exp = market.pick_expiry(sig.expiries, sig.today, cfg.dte_min, cfg.dte_max)
    d = EntryDecision(trade_exp, market.dte(trade_exp, sig.today) if trade_exp else None,
                      flags=flags_upto(1, True), inputs=sig.inputs())

    def fail(stage: str, reason: str, flag_n: Optional[int] = None, **inputs) -> EntryDecision:
        d.stage, d.reason, d.flag_n = stage, reason, flag_n
        d.inputs.update(inputs)
        if flag_n:
            d.flags[f"f{flag_n}"] = False
        return d

    if trade_exp is None:
        return fail("DTE window", f"no expiry {cfg.dte_min}-{cfg.dte_max} days out")
    f2 = ev_data.check_f2(ctx, sig.symbol, trade_exp)
    if not f2.passed:
        return fail("F2 events", f2.reason, 2, events=f2.events)
    d.flags["f2"] = True
    ratio = sig.atm_iv / sig.hv20
    d.inputs["iv_hv"] = ratio
    if cfg.iv.min_iv_hv_ratio > 0:
        if ratio < cfg.iv.min_iv_hv_ratio:
            return fail("F3 IV/HV", f"IV/HV {ratio:.2f} < {cfg.iv.min_iv_hv_ratio}", 3)
        d.flags["f3"] = True
    if not f4_pass(ctx, sig.ivp, sig.ivp_method):
        thr = cfg.iv.min_ivp if sig.ivp_method == "true" else cfg.iv.proxy_hv_pctile
        return fail("F4 IV percentile", f"{sig.ivp_method} IVP {sig.ivp or 0:.0f} < {thr:g}", 4)
    d.flags["f4"] = True
    if cfg.iv.timing == "cause_passed":
        ok5, why5, info5 = f5_check(ctx, sig.symbol, sig.today, sig.daily, cal)
        d.inputs["f5"] = info5
        if not ok5:
            return fail("F5 cause passed", why5, 5)
        d.flags["f5"] = True
    elif cfg.iv.timing == "shock_passed":
        n = cfg.iv.calm_days
        moves, rv = sig.recent_moves(n, cfg.iv.spike_move_atr)
        d.inputs["f5"] = {"recent_moves": moves, "rv_recent": rv, "calm_days": n}
        if moves:
            return fail("F5 shock passed", f"large move in the last {n} sessions ({moves[-1]})", 5)
        if rv is None or rv >= sig.atm_iv:
            return fail("F5 shock passed", f"realised vol over {n} sessions {rv or 0:.0%} not below IV "
                                           f"{sig.atm_iv:.0%}", 5)
        d.flags["f5"] = True
    d.inputs["timing"] = cfg.iv.timing
    if cfg.direction.method == "levels":
        side, level = choose_direction(sig.levels(), sig.spot, sig.atr, cfg.direction.max_dist_atr)
        if side is None:
            return fail("direction", f"no scored level within {cfg.direction.max_dist_atr} x ATR of spot")
        d.side, d.anchor, d.anchor_kind = side, level.price, "level"
        d.inputs.update(level=level.price, level_score=level.score, level_source=level.source)
    else:
        n = cfg.direction.trend_days
        avg = sig.moving_average(n)
        if avg is None:
            return fail("direction", f"fewer than {n} daily bars for the trend average")
        d.side = BULL_PUT if sig.spot > avg else BEAR_CALL
        d.anchor, d.anchor_kind = avg, "trend"
        d.inputs.update(level=avg, trend_avg=avg, trend_days=n)
    d.inputs["direction_method"] = cfg.direction.method
    return d
