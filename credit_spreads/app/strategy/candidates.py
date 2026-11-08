"""What the scan produces: candidate spreads and each stock's outcome, per strategy."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

# Funnel stages in evaluation order (stock level). Each stock is counted at its first failure.
STAGES = ["F1 universe", "DTE window", "data", "F2 events", "F3 IV/HV", "F4 IV percentile",
          "F5 cause passed", "F5 shock passed", "direction", "F6 level buffer", "F7 distance/delta", "F9 liquidity",
          "F8 credit quality", "risk budget", "EV", "accepted"]


@dataclass
class SpreadCandidate:
    symbol: str
    side: str
    short_k: float
    long_k: float
    expiry: Optional[date]
    dte: Optional[int]
    lot_size: int
    credit_nat: float = 0.0          # per share, after round-trip costs
    credit_gross: float = 0.0
    width: float = 0.0
    max_loss: float = 0.0            # rupees per lot
    risk_per_lot: float = 0.0        # rupees per lot on the sizing basis (loss at the stop by default)
    breakeven: float = 0.0
    p_market: Optional[float] = None
    p_model: Optional[float] = None
    ev: Optional[float] = None
    ev_ratio: Optional[float] = None
    flags: Dict[str, Optional[bool]] = field(default_factory=dict)   # "f1".."f9"
    reject_reason: Optional[str] = None
    inputs: Dict = field(default_factory=dict)
    short_sym: str = ""
    long_sym: str = ""
    strategy: str = "main"

    @property
    def accepted(self) -> bool:
        return self.reject_reason is None


@dataclass
class StockOutcome:
    symbol: str
    stage: str                       # first failing stage, or "accepted"
    reason: str
    best: Optional[SpreadCandidate] = None
    rows: List[SpreadCandidate] = field(default_factory=list)


def funnel_of(outcomes: List[StockOutcome]) -> "OrderedDict[str, int]":
    counts = OrderedDict((s, 0) for s in STAGES)
    for o in outcomes:
        counts[o.stage] += 1
    return counts


@dataclass
class BookResult:
    """One strategy's part of a scan."""
    strategy: str
    outcomes: List[StockOutcome]
    ranked: List[SpreadCandidate]
    selected: List[SpreadCandidate]
    entry_messages: List[str] = field(default_factory=list)

    @property
    def funnel(self) -> "OrderedDict[str, int]":
        return funnel_of(self.outcomes)


@dataclass
class ScanResult:
    """A scan: the main book's results at the top level (as before), every book in `books`."""
    scan_id: str
    ts: datetime
    outcomes: List[StockOutcome]
    ranked: List[SpreadCandidate]
    selected: List[SpreadCandidate]
    entry_messages: List[str] = field(default_factory=list)
    books: Dict[str, BookResult] = field(default_factory=dict)

    @property
    def funnel(self) -> "OrderedDict[str, int]":
        return funnel_of(self.outcomes)


def flags_upto(n: int, last_ok: bool = True) -> Dict[str, Optional[bool]]:
    """f1..f(n-1) True, f(n) = last_ok, rest None."""
    out: Dict[str, Optional[bool]] = {f"f{i}": None for i in range(1, 10)}
    for i in range(1, n):
        out[f"f{i}"] = True
    out[f"f{n}"] = last_ok
    return out


def quote_dict(qt) -> Optional[dict]:
    if qt is None:
        return None
    return {"bid": qt.bid, "ask": qt.ask, "bid_qty": qt.bids[0].quantity if qt.bids else 0,
            "ask_qty": qt.asks[0].quantity if qt.asks else 0, "oi": qt.oi, "volume": qt.volume, "last": qt.last}
