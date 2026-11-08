"""Signal layer: facts about one stock at one scan, computed once and shared by every strategy.

Daily bars and indicators, the ATM IV snapshot (stored for every universe stock), the IV
percentile, support/resistance levels, moving averages, recent large moves and realised volatility,
and option-chain snapshots per expiry from the scan's prefetched quotes. No thresholds are applied
here: each strategy's entry rules (rules.py) decide what the signals mean. The lookbacks that shape
the signals (IV history, the ATM expiry, the levels computation) come from the main config.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from .. import market
from ..analytics import vol
from ..analytics.vol import delta_iv
from ..data import instruments as inst
from ..data.calendar import TradingCalendar
from ..data.daily import load_daily
from ..data.levels_store import StoredLevel, load_levels
from ..data.models import IVSnapshot

SCAN_BAND = 0.30           # quote strikes within 30% of the futures price (performance, not a rule)


def iv_history(ctx, symbol: str, before: date, n: int) -> List[IVSnapshot]:
    with ctx.engine.connect() as conn:
        rows = conn.execute(select(IVSnapshot.date, IVSnapshot.atm_iv, IVSnapshot.ivp_252, IVSnapshot.method)
                            .where(IVSnapshot.symbol == symbol, IVSnapshot.date < before)
                            .order_by(IVSnapshot.date.desc()).limit(n)).all()
    return list(reversed(rows))


def iv_percentile(ctx, symbol: str, today: date, atm_iv: float, hv20_hist: List[float]) -> Tuple[Optional[float], str, Optional[float]]:
    """(percentile, method, proxy_threshold). True IV percentile once enough snapshots exist,
    else the rank of IV within the stock's rolling HV20 history (proxy)."""
    ic = ctx.cfg.iv
    hist = iv_history(ctx, symbol, today, ic.ivp_history_days)
    if len(hist) >= ic.ivp_min_history:
        return vol.percentile_rank([h.atm_iv for h in hist], atm_iv), "true", None
    hv_hist = hv20_hist[-ic.ivp_history_days:]
    return vol.percentile_rank(hv_hist, atm_iv), "proxy", vol.percentile_value(hv_hist, ic.proxy_hv_pctile)


@dataclass
class StockSignals:
    ctx: object                       # the main context (shared data access)
    symbol: str
    today: date
    expiries: List[date]
    daily: object                     # DataFrame of completed daily bars and indicators
    atr: float
    hv20: float
    atm_exp: date
    atm_snap: object                  # market.ChainSnapshot
    ivp: Optional[float]
    ivp_method: str
    proxy_threshold: Optional[float]
    _snaps: Dict[date, object] = field(default_factory=dict)
    _levels: Optional[List[StoredLevel]] = None

    @property
    def atm_iv(self) -> float:
        return self.atm_snap.atm_iv

    @property
    def spot(self) -> float:
        return self.atm_snap.spot

    def inputs(self) -> dict:
        s = self.atm_snap
        return dict(spot=s.spot, fut_atm=s.fut_price, atr14=self.atr, hv20=self.hv20, atm_iv=self.atm_iv,
                    atm_expiry=str(self.atm_exp), ivp=self.ivp, ivp_method=self.ivp_method,
                    proxy_threshold=self.proxy_threshold)

    def snapshot(self, expiry: date):
        """Chain snapshot for an expiry, built once per scan from the prefetched quotes."""
        if expiry == self.atm_exp:
            return self.atm_snap
        if expiry not in self._snaps:
            self._snaps[expiry] = market.snapshot_chain(self.ctx, self.symbol, expiry, band=SCAN_BAND,
                                                        quotes=self.ctx.cache.get("scan_quotes"))
        return self._snaps[expiry]

    def levels(self) -> List[StoredLevel]:
        if self._levels is None:
            self._levels = load_levels(self.ctx.engine, self.symbol, self.today)
        return self._levels

    def moving_average(self, n: int) -> Optional[float]:
        c = self.daily["close"].dropna()
        return float(c.iloc[-n:].mean()) if len(c) >= n else None

    def recent_moves(self, days: int, move_atr: float) -> Tuple[List[str], Optional[float]]:
        """Large moves (>= move_atr x the previous day's ATR) in the last `days` sessions, and the
        realised volatility of those sessions (annualised, zero-mean)."""
        d = self.daily.tail(days + 1)
        close, atr = d["close"], d["atr14"]
        moves = []
        for i in range(1, len(d)):
            pc, pa = close.iloc[i - 1], atr.iloc[i - 1]
            if pa and not math.isnan(pa) and abs(close.iloc[i] - pc) >= move_atr * pa:
                moves.append(f"{d.index[i]} move {abs(close.iloc[i] - pc) / pa:.1f}xATR")
        rets = [math.log(b / a) for a, b in zip(close.iloc[:-1], close.iloc[1:]) if a > 0 and b > 0]
        n_year = self.ctx.cfg.analytics.trading_days_per_year
        rv = math.sqrt(n_year * sum(r * r for r in rets) / len(rets)) if len(rets) >= 2 else None
        return moves, rv


def compute_signals(ctx, symbol: str, today: date, cal: TradingCalendar,
                    snapshots: List[dict]) -> Tuple[Optional[StockSignals], str]:
    """(signals, "") or (None, reason the stock has no usable data). Records the stock's IV snapshot
    whenever the ATM IV could be computed."""
    cfg = ctx.cfg
    exps = inst.expiries(ctx.engine, today, symbol)
    atm_exp = market.pick_expiry(exps, today, cfg.iv.atm_min_dte)
    if atm_exp is None:
        return None, "no expiry for ATM IV"
    daily = load_daily(ctx.engine, symbol, end=cal.prev_trading_day(today))
    if len(daily) < cfg.analytics.hv_window + 2 or daily["atr14"].dropna().empty:
        return None, f"only {len(daily)} daily bars stored"
    atr = float(daily["atr14"].dropna().iloc[-1])
    hv20 = float(daily["hv20"].dropna().iloc[-1]) if daily["hv20"].notna().any() else None
    if not hv20:
        return None, "HV20 unavailable"
    atm_snap = market.snapshot_chain(ctx, symbol, atm_exp, band=SCAN_BAND, quotes=ctx.cache.get("scan_quotes"))
    if atm_snap.atm_iv is None:
        return None, "could not compute ATM IV"
    ivp, method, proxy_thr = iv_percentile(ctx, symbol, today, atm_snap.atm_iv, list(daily["hv20"].dropna()))
    snapshots.append(dict(ts=ctx.clock.now(), date=today, symbol=symbol, expiry=atm_exp,
                          fut_price=atm_snap.fut_price, atm_iv=atm_snap.atm_iv,
                          iv_25d_put=delta_iv(atm_snap.ivs, 0.25, "PE"),
                          iv_25d_call=delta_iv(atm_snap.ivs, 0.25, "CE"), ivp_252=ivp, method=method))
    return StockSignals(ctx, symbol, today, exps, daily, atr, hv20, atm_exp, atm_snap, ivp, method, proxy_thr), ""
