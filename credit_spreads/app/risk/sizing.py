"""Position sizing by maximum loss, never by margin."""

from __future__ import annotations

import math

from sqlalchemy import select

from ..data.models import Equity


def current_equity(ctx) -> float:
    """Latest end-of-day equity, or starting capital before the first EOD."""
    with ctx.engine.connect() as conn:
        eq = conn.execute(select(Equity.equity).order_by(Equity.date.desc()).limit(1)).scalar()
    return float(eq) if eq is not None else float(ctx.cfg.capital)


def risk_budget(ctx, equity: float = None) -> float:
    return ctx.cfg.risk.per_trade * (current_equity(ctx) if equity is None else equity)


def lots_for(budget: float, max_loss_per_lot: float) -> int:
    """floor(budget / max loss per lot); zero means skip, never round up."""
    if max_loss_per_lot <= 0:
        return 0
    return max(0, math.floor(budget / max_loss_per_lot + 1e-9))
