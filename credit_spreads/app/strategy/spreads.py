"""Vertical credit spread payoff maths (spec: "Worked example of the payoff maths")."""

from __future__ import annotations

from dataclasses import dataclass

BULL_PUT = "bull_put"
BEAR_CALL = "bear_call"
SIDES = (BULL_PUT, BEAR_CALL)


def option_type(side: str) -> str:
    if side == BULL_PUT:
        return "PE"
    if side == BEAR_CALL:
        return "CE"
    raise ValueError(f"unknown side {side!r}")


@dataclass(frozen=True)
class SpreadMetrics:
    side: str
    short_k: float
    long_k: float
    lot_size: int
    credit: float            # per share, net of whatever costs were passed in
    width: float
    max_loss: float          # rupees per lot
    credit_lot: float        # rupees per lot
    breakeven: float
    return_on_max_loss: float
    breakeven_win_rate: float


def spread_metrics(side: str, short_k: float, long_k: float, short_px: float, long_px: float,
                   lot_size: int, costs_per_share: float = 0.0) -> SpreadMetrics:
    """Credit, width, maximum loss, breakeven and breakeven win rate for one lot.

    Breakeven win rate = max loss / (max loss + credit).
    """
    if side == BULL_PUT and not long_k < short_k:
        raise ValueError("bull put: long strike must be below short strike")
    if side == BEAR_CALL and not long_k > short_k:
        raise ValueError("bear call: long strike must be above short strike")
    option_type(side)
    credit = short_px - long_px - costs_per_share
    width = abs(short_k - long_k)
    max_loss_ps = width - credit
    max_loss = max_loss_ps * lot_size
    credit_lot = credit * lot_size
    breakeven = short_k - credit if side == BULL_PUT else short_k + credit
    rom = credit / max_loss_ps if max_loss_ps > 0 else float("inf")
    denom = max_loss + credit_lot
    be_wr = max_loss / denom if denom > 0 else 1.0
    return SpreadMetrics(side, short_k, long_k, lot_size, credit, width, max_loss, credit_lot,
                         breakeven, rom, be_wr)
