"""Zerodha + exchange cost stack for option orders. Rates come from config `costs`.

Per order: brokerage (flat), STT on sold premium, exchange transaction charge on premium,
SEBI fee, stamp duty on buys, and GST on brokerage + exchange charge + SEBI fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import CostsCfg


class CostModelIncomplete(RuntimeError):
    """A cost rate in config.yaml is unset, so trades cannot be priced."""


@dataclass(frozen=True)
class OrderCost:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst


def order_cost(c: CostsCfg, side: str, premium: float, qty: int) -> OrderCost:
    if c.missing:
        raise CostModelIncomplete(f"cost rates not set in config.yaml: {', '.join(c.missing)}")
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    turnover = max(premium, 0.0) * qty
    if turnover <= 0:
        return OrderCost(0, 0, 0, 0, 0, 0)
    brokerage = c.brokerage_per_order
    stt = c.stt_sell_premium * turnover if side == "SELL" else 0.0
    exchange = c.exchange_txn_pct * turnover
    sebi = c.sebi_fee_per_crore * turnover / 1e7
    stamp = c.stamp_buy_pct * turnover if side == "BUY" else 0.0
    gst = c.gst * (brokerage + exchange + sebi)
    return OrderCost(brokerage, stt, exchange, sebi, stamp, gst)


def entry_costs(c: CostsCfg, short_px: float, long_px: float, qty: int) -> float:
    """Open a credit spread: sell the short leg, buy the long leg."""
    return order_cost(c, "SELL", short_px, qty).total + order_cost(c, "BUY", long_px, qty).total


def exit_costs(c: CostsCfg, short_px: float, long_px: float, qty: int) -> float:
    """Close a credit spread: buy back the short leg, sell the long leg."""
    return order_cost(c, "BUY", short_px, qty).total + order_cost(c, "SELL", long_px, qty).total


def round_trip_costs(c: CostsCfg, short_px: float, long_px: float, qty: int,
                     exit_short_px: Optional[float] = None, exit_long_px: Optional[float] = None) -> float:
    """Entry plus exit costs. Exit premiums default to the entry premiums, which overstates
    exit STT and charges for a winning trade, so the estimate errs on the costly side."""
    xs = short_px if exit_short_px is None else exit_short_px
    xl = long_px if exit_long_px is None else exit_long_px
    return entry_costs(c, short_px, long_px, qty) + exit_costs(c, xs, xl, qty)
