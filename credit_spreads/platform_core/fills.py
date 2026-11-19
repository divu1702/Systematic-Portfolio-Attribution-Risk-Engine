"""Paper fills, deliberately harder than reality (spec: Paper fills): sell at the bid and buy at the ask
at decision time, plus `extra_ticks` of slippage per leg; if the top of the book is too thin, walk the
5 depth levels at their volume-weighted price; if they run out, there is no fill. Every account on the
platform, and the earlier system, fills through this one function."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Fill:
    ok: bool
    qty: int
    natural: float          # best price on the relevant side at decision time
    vwap: float             # volume-weighted across the levels used
    price: float            # vwap plus the extra slippage
    levels: int
    reason: str = ""


def simulate_fill(q, side: str, qty: int, tick: float, extra_ticks: int) -> Fill:
    if q is None:
        return Fill(False, qty, 0, 0, 0, 0, "no quote")
    book = q.asks if side == "BUY" else q.bids
    if not book:
        return Fill(False, qty, 0, 0, 0, 0, f"no {'ask' if side == 'BUY' else 'bid'}")
    natural = book[0].price
    left, notional, used = qty, 0.0, 0
    for lv in book[:5]:
        take = min(left, lv.quantity)
        notional += take * lv.price
        left -= take
        used += 1
        if left == 0:
            break
    if left > 0:
        return Fill(False, qty, natural, 0, 0, used, f"depth: only {qty - left} of {qty} in 5 levels")
    vwap = notional / qty
    price = vwap + extra_ticks * tick if side == "BUY" else vwap - extra_ticks * tick
    if price <= 0:
        return Fill(False, qty, natural, vwap, 0, used, "price after slippage is not positive")
    return Fill(True, qty, natural, vwap, round(price, 4), used)
