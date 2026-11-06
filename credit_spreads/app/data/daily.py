"""End-of-day underlying candles with ATR14, HV20 and the HV percentile (underlying_daily)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

import pandas as pd
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert

from ..analytics import vol
from . import instruments as inst
from .models import UnderlyingDaily

log = logging.getLogger(__name__)


def update_underlying_daily(ctx, symbols: Iterable[str], through: date, history_days: int = 420) -> int:
    """Store completed daily candles up to `through` and recompute indicators. Returns symbols updated."""
    a = ctx.cfg.analytics
    master = inst.latest_master_date(ctx.engine)
    n = 0
    for sym in symbols:
        tok = inst.equity_token(ctx.engine, master, sym) if master else None
        if not tok:
            log.warning("no equity instrument token for %s; skipping daily candles", sym)
            continue
        with ctx.engine.connect() as conn:
            last = conn.execute(select(func.max(UnderlyingDaily.date)).where(UnderlyingDaily.symbol == sym)).scalar()
        start = (last + timedelta(days=1)) if last else through - timedelta(days=history_days)
        if start > through:
            continue
        candles = ctx.broker.historical(tok, datetime.combine(start, time(0)),
                                        datetime.combine(through, time(23, 59)), "day")
        rows = [c for c in candles if start <= c["date"].date() <= through]
        if not rows:
            continue
        with ctx.engine.begin() as conn:
            for c in rows:
                stmt = insert(UnderlyingDaily).values(
                    date=c["date"].date(), symbol=sym, open=c["open"], high=c["high"], low=c["low"],
                    close=c["close"], volume=int(c.get("volume") or 0))
                conn.execute(stmt.on_conflict_do_update(index_elements=["date", "symbol"], set_={
                    "open": stmt.excluded.open, "high": stmt.excluded.high, "low": stmt.excluded.low,
                    "close": stmt.excluded.close, "volume": stmt.excluded.volume}))
        _recompute(ctx.engine, sym, a.atr_window, a.hv_window, a.trading_days_per_year)
        n += 1
    return n


def _recompute(engine: Engine, sym: str, atr_n: int, hv_n: int, per_year: int) -> None:
    df = load_daily(engine, sym)
    if df.empty:
        return
    df["atr14"] = vol.atr(df["high"], df["low"], df["close"], atr_n)
    df["hv20"] = vol.hv(df["close"], hv_n, per_year)
    df["hv_pctile_252"] = vol.rolling_percentile_rank(df["hv20"], 252)
    with engine.begin() as conn:
        for d, r in df.iterrows():
            conn.execute(update(UnderlyingDaily).where(UnderlyingDaily.symbol == sym, UnderlyingDaily.date == d)
                         .values(atr14=_f(r["atr14"]), hv20=_f(r["hv20"]), hv_pctile_252=_f(r["hv_pctile_252"])))


def _f(x) -> Optional[float]:
    return None if pd.isna(x) else float(x)


def load_daily(engine: Engine, sym: str, end: Optional[date] = None, n: Optional[int] = None) -> pd.DataFrame:
    q = select(UnderlyingDaily).where(UnderlyingDaily.symbol == sym)
    if end:
        q = q.where(UnderlyingDaily.date <= end)
    q = q.order_by(UnderlyingDaily.date.desc())
    if n:
        q = q.limit(n)
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "atr14", "hv20", "hv_pctile_252"])
    df = pd.DataFrame(rows).drop(columns=["id", "symbol"]).set_index("date").sort_index()
    return df
