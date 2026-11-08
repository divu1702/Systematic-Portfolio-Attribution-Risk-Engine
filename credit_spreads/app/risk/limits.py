"""Portfolio risk limits and kill switches (spec: Position sizing and portfolio risk limits,
Live execution and kill-switches). Anything returned here blocks a new entry; exits are never
blocked."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..clock import parse_hhmm
from ..data import state
from ..data.models import Order, Trade
from .sizing import current_equity, lots_for, plan_lots


def open_trades(engine, strategy: Optional[str] = None) -> List[Trade]:
    """Open spreads of one strategy's book, or of every book when `strategy` is None."""
    q = select(Trade).where(Trade.status == "open")
    if strategy is not None:
        q = q.where(Trade.strategy == strategy)
    with Session(engine) as s:
        return list(s.scalars(q.order_by(Trade.entry_ts)))


def position_risk(t) -> float:
    """Planned risk of an open spread on the sizing basis (older rows fall back to max loss)."""
    return t.risk if getattr(t, "risk", None) else t.max_loss


def realised_today(engine, day, strategy: Optional[str] = None) -> float:
    start = datetime.combine(day, time(0))
    q = select(func.sum(Trade.pnl_net)).where(Trade.status == "closed", Trade.exit_ts >= start,
                                              Trade.exit_ts < start + timedelta(days=1))
    if strategy is not None:
        q = q.where(Trade.strategy == strategy)
    with engine.connect() as conn:
        v = conn.execute(q).scalar()
    return float(v or 0.0)


def rejections_today(engine, day) -> int:
    start = datetime.combine(day, time(0))
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(Order).where(
            Order.status == "rejected", Order.ts >= start, Order.ts < start + timedelta(days=1))).scalar() or 0)


def kill_switches(ctx, equity: Optional[float] = None) -> List[str]:
    """Reasons new entries into this context's book are blocked right now (empty = allowed). The
    kill switch, opening window and rejection count apply to every book; the drawdown pause and
    the daily loss stop to each book separately."""
    cfg, now = ctx.cfg, ctx.clock.now()
    out = []
    if (r := state.flag(ctx.engine, state.KILLED)):
        out.append(f"kill switch: {r}")
    if (r := state.flag(ctx.engine, state.dd_key(ctx.strategy))):
        how = "" if ctx.strategy == "main" else f" --strategy {ctx.strategy}"
        out.append(f"drawdown pause: {r} (reset manually with `python -m app resume --drawdown{how}`)")
    equity = current_equity(ctx) if equity is None else equity
    lost = -realised_today(ctx.engine, now.date(), ctx.strategy)
    if lost >= cfg.risk.daily_stop * equity:
        out.append(f"daily loss stop: realised loss today {lost:,.0f} >= {cfg.risk.daily_stop:.0%} of equity")
    s = cfg.schedule
    if parse_hhmm(s.no_entry_start) <= now.time() < parse_hhmm(s.no_entry_end):
        out.append(f"opening window {s.no_entry_start}-{s.no_entry_end}: no new entries")
    n_rej = rejections_today(ctx.engine, now.date())
    if n_rej > cfg.killswitch.max_rejections_per_day:
        out.append(f"{n_rej} order rejections today (> {cfg.killswitch.max_rejections_per_day})")
    return out


def stale_quotes(ctx, quotes) -> List[str]:
    """Keys whose quote is older than killswitch.stale_quote_secs."""
    now, limit = ctx.clock.now(), timedelta(seconds=ctx.cfg.killswitch.stale_quote_secs)
    return [k for k, q in quotes.items() if q.ts is None or now - q.ts > limit]


def size_and_check(ctx, cand, sector: str, margin_needed_per_lot: float, open_: List[Trade],
                   equity: float) -> Tuple[int, List[str]]:
    """Lots = floor(per-trade budget / risk per lot) on the sizing basis, limited by the per-trade
    gap cap, then trimmed to fit the total-risk cap and the total gap cap; then the count,
    direction, sector and margin limits. Returns (lots, violations)."""
    r = ctx.cfg.risk
    v: List[str] = []
    risk_lot = getattr(cand, "risk_per_lot", 0) or cand.max_loss
    lots, why = plan_lots(ctx.cfg, equity, risk_lot, cand.max_loss)
    if lots == 0:
        return 0, [why]
    room = r.max_total * equity - sum(position_risk(t) for t in open_)
    lots = min(lots, max(0, math.floor(room / risk_lot + 1e-9)))
    if lots == 0:
        v.append(f"total risk would exceed {r.max_total:.0%} of equity")
    elif r.gap_cap_total:
        gap_room = r.gap_cap_total * equity - sum(t.max_loss for t in open_)
        lots = min(lots, max(0, math.floor(gap_room / cand.max_loss + 1e-9)))
        if lots == 0:
            v.append(f"combined max loss would exceed the {r.gap_cap_total:.0%} gap cap")
    if len(open_) >= r.max_open:
        v.append(f"{len(open_)} spreads open (max {r.max_open})")
    same = sum(1 for t in open_ if t.side == cand.side)
    if same >= r.max_same_direction:
        v.append(f"{same} {cand.side} spreads open (max {r.max_same_direction})")
    in_sector = sum(1 for t in open_ if (t.sector or "UNKNOWN") == sector)
    if in_sector >= r.max_per_sector:
        v.append(f"sector {sector} already has {in_sector} spread(s) (max {r.max_per_sector})")
    if any(t.symbol == cand.symbol for t in open_):
        v.append(f"{cand.symbol} already has an open spread")
    used = sum(t.margin or 0.0 for t in open_)
    if lots and used + margin_needed_per_lot * lots > r.max_margin_util * ctx.cfg.capital:
        v.append(f"margin {used + margin_needed_per_lot * lots:,.0f} would exceed "
                 f"{r.max_margin_util:.0%} of capital")
    return lots, v
