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


def risk_at_stop_per_share(cfg, credit: float, width: float) -> float:
    """Planned loss per share if the hard stop fires: (stop_mult - 1) x credit, plus an overshoot
    allowance because stops fill at the first mark past the trigger; never more than max loss."""
    return min((cfg.exit.stop_mult - 1.0) * credit * cfg.risk.stop_overshoot, width - credit)


def risk_per_lot(cfg, credit: float, width: float, lot: int) -> float:
    """Rupees per lot on the configured sizing basis."""
    if cfg.risk.sizing_basis == "stop":
        return risk_at_stop_per_share(cfg, credit, width) * lot
    return (width - credit) * lot


def plan_lots(cfg, equity: float, risk_lot: float, max_loss_lot: float):
    """(lots, reason if zero): floor of the per-trade budget over risk per lot, further limited by
    the per-trade gap cap on full max loss."""
    lots = lots_for(cfg.risk.per_trade * equity, risk_lot)
    if lots == 0:
        return 0, f"risk per lot {risk_lot:,.0f} exceeds the per-trade budget {cfg.risk.per_trade * equity:,.0f}"
    if cfg.risk.gap_cap_per_trade:
        cap = lots_for(cfg.risk.gap_cap_per_trade * equity, max_loss_lot)
        if cap == 0:
            return 0, (f"max loss per lot {max_loss_lot:,.0f} exceeds the gap cap "
                       f"{cfg.risk.gap_cap_per_trade * equity:,.0f}")
        lots = min(lots, cap)
    if cfg.risk.max_lots_per_trade:
        lots = min(lots, cfg.risk.max_lots_per_trade)
    return lots, ""


def lots_for(budget: float, max_loss_per_lot: float) -> int:
    """floor(budget / max loss per lot); zero means skip, never round up."""
    if max_loss_per_lot <= 0:
        return 0
    return max(0, math.floor(budget / max_loss_per_lot + 1e-9))
