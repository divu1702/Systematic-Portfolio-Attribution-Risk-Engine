"""End-of-day equity, drawdown and the drawdown pause."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from ..data import state
from ..data.models import Equity, Mark, Trade
from .limits import open_trades


@dataclass(frozen=True)
class EquityPoint:
    date: date
    equity: float
    realised: float
    unrealised: float
    margin_used: float
    peak: float
    drawdown: float


def compute_equity(ctx, asof: date) -> EquityPoint:
    end = datetime.combine(asof, time(23, 59, 59))
    with ctx.engine.connect() as conn:
        realised = float(conn.execute(select(func.sum(Trade.pnl_net)).where(
            Trade.status == "closed", Trade.exit_ts <= end)).scalar() or 0.0)
    unreal, margin = 0.0, 0.0
    for t in open_trades(ctx.engine):
        with ctx.engine.connect() as conn:
            u = conn.execute(select(Mark.unrealised_pnl).where(Mark.trade_id == t.trade_id, Mark.ts <= end)
                             .order_by(Mark.ts.desc()).limit(1)).scalar()
        unreal += (u or 0.0) - t.costs          # entry costs are already paid
        margin += t.margin or 0.0
    eq = ctx.cfg.capital + realised + unreal
    with ctx.engine.connect() as conn:
        prev_peak = conn.execute(select(func.max(Equity.peak)).where(Equity.date < asof)).scalar()
    peak = max(eq, prev_peak or ctx.cfg.capital)
    return EquityPoint(asof, round(eq, 2), round(realised, 2), round(unreal, 2), round(margin, 2), round(peak, 2),
                       round(1 - eq / peak, 6) if peak > 0 else 0.0)


def eod_equity(ctx) -> EquityPoint:
    p = compute_equity(ctx, ctx.clock.today())
    stmt = insert(Equity).values(**p.__dict__)
    with ctx.engine.begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["date"], set_={
            k: getattr(stmt.excluded, k) for k in p.__dict__ if k != "date"}))
    if p.drawdown >= ctx.cfg.risk.dd_pause and not state.flag(ctx.engine, state.DD_PAUSED):
        reason = f"drawdown {p.drawdown:.1%} from peak {p.peak:,.0f} on {p.date}"
        state.put(ctx.engine, state.DD_PAUSED, reason, ts=ctx.clock.now())
        ctx.notifier.send(f"Drawdown pause: {reason}. No new entries until you reset it.", "risk")
    return p
