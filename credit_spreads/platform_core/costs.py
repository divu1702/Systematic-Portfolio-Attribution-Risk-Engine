"""The full Zerodha cost stack, from platform.yaml `costs` and `settlement`.

Per option order: brokerage (flat), STT on sold premium, the exchange transaction charge on premium
(each side), SEBI fee, stamp duty on buys, GST on brokerage + exchange charge + SEBI fee.
At expiry, legs settled in the money also pay STT on exercise (bought legs, on intrinsic value) and
Zerodha's physical-delivery brokerage (on the settled value, strike x quantity), plus GST on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class Charges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def __add__(self, other: "Charges") -> "Charges":
        return Charges(*(a + b for a, b in zip(self.astuple(), other.astuple())))

    def astuple(self) -> Tuple[float, ...]:
        return self.brokerage, self.stt, self.exchange, self.sebi, self.stamp, self.gst


class CostModel:
    def __init__(self, costs, settlement):
        self.c, self.s = costs, settlement

    def order(self, side: str, premium: float, qty: int) -> Charges:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        turnover = max(premium, 0.0) * qty
        if turnover <= 0:
            return Charges()
        c = self.c
        brokerage = c.brokerage_per_order
        exchange = c.exchange_txn_pct * turnover
        sebi = c.sebi_fee_per_crore * turnover / 1e7
        return Charges(brokerage=brokerage, stt=c.stt_sell_premium * turnover if side == "SELL" else 0.0,
                       exchange=exchange, sebi=sebi, stamp=c.stamp_buy_pct * turnover if side == "BUY" else 0.0,
                       gst=c.gst * (brokerage + exchange + sebi))

    def open_spread(self, short_px: float, long_px: float, qty: int) -> Charges:
        """Sell the short leg, buy the long leg."""
        return self.order("SELL", short_px, qty) + self.order("BUY", long_px, qty)

    def close_spread(self, short_px: float, long_px: float, qty: int) -> Charges:
        """Buy back the short leg, sell the long leg."""
        return self.order("BUY", short_px, qty) + self.order("SELL", long_px, qty)

    def round_trip(self, short_px: float, long_px: float, qty: int) -> float:
        """Entry plus an exit at the same premiums (errs on the costly side for a winning trade)."""
        return self.open_spread(short_px, long_px, qty).total + self.close_spread(short_px, long_px, qty).total

    def settlement(self, legs: Iterable[Tuple[str, float, str, int]], spot: float) -> Tuple[float, Charges]:
        """Cash value and charges of settling legs at expiry against `spot`.
        legs: (side, strike, cp, qty) with side SELL for the short leg. Returns (net intrinsic received, charges)."""
        net, stt, brokerage = 0.0, 0.0, 0.0
        for side, strike, cp, qty in legs:
            intrinsic = max(spot - strike, 0.0) if cp == "CE" else max(strike - spot, 0.0)
            if intrinsic <= 0:
                continue                                  # expires worthless
            net += intrinsic * qty * (1 if side == "BUY" else -1)
            if side == "BUY":
                stt += self.s.stt_exercise * intrinsic * qty
            brokerage += self.s.physical_delivery_brokerage_pct * strike * qty
        return net, Charges(brokerage=brokerage, stt=stt, gst=self.c.gst * brokerage)
