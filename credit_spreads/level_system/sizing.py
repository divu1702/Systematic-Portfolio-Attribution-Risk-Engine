"""Sizing on the stop and the portfolio limits (spec: Sizing and portfolio limits).

1. Loss at the kill level per lot (analytics.loss_at_kill).
2. Lots = floor(risk_at_stop x capital / loss per lot); zero lots: skip.
3. Gap cap per position: lots x maximum loss per lot <= gap_cap_position x capital; cut lots until it fits.
4. Strict book only: at most max_positions open, one per sector, combined maximum loss of all open
   positions <= gap_cap_portfolio x capital (cut lots, or skip at zero). The research book is sized by
   the same per-trade rules and is exempt from the portfolio limits.
1R = lots x loss per lot at the kill level, including the IV cushion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass
class Size:
    lots: int
    one_r: float = 0.0
    notes: List[str] = field(default_factory=list)
    reason: str = ""                     # why the trade is skipped (lots == 0)


def size(*, loss_per_lot: Optional[float], max_loss_lot: float, capital: float, cfg, book: str,
         open_trades: Sequence = (), sector: Optional[str] = None) -> Size:
    s = cfg.sizing
    if loss_per_lot is None:
        return Size(0, reason="loss at the kill level could not be priced (no implied volatility)")
    if loss_per_lot <= 0:
        return Size(0, reason=f"loss at the kill level is not positive ({loss_per_lot:,.0f} per lot): quotes look wrong")
    lots = math.floor(s.risk_at_stop * capital / loss_per_lot + 1e-9)
    notes = [f"{s.risk_at_stop:.0%} of capital / loss per lot {loss_per_lot:,.0f} -> {lots} lot(s)"]
    if lots <= 0:
        return Size(0, notes=notes, reason=f"zero lots: one lot loses {loss_per_lot:,.0f} at the kill level, "
                                           f"more than {s.risk_at_stop:.0%} of capital")
    cap = s.gap_cap_position * capital
    if max_loss_lot > 0 and lots * max_loss_lot > cap:
        lots = math.floor(cap / max_loss_lot + 1e-9)
        notes.append(f"gap cap {s.gap_cap_position:.0%}: cut to {lots} lot(s)")
        if lots <= 0:
            return Size(0, notes=notes, reason=f"one lot's maximum loss {max_loss_lot:,.0f} exceeds the "
                                               f"{s.gap_cap_position:.0%} per-position gap cap")
    if book == "strict" or not cfg.books.research_exempt_from_portfolio_limits:
        if len(open_trades) >= s.max_positions:
            return Size(0, notes=notes, reason=f"{len(open_trades)} positions open (limit {s.max_positions})")
        sec = sector or "UNKNOWN"
        if s.one_per_sector and any((t.sector or "UNKNOWN") == sec for t in open_trades):
            return Size(0, notes=notes, reason=f"a position in sector {sec} is already open"
                        + (" (unknown sectors count as one sector)" if sec == "UNKNOWN" else ""))
        if s.one_per_sector and sec == "UNKNOWN":
            notes.append("sector unknown: treated as one sector of its own")
        room = s.gap_cap_portfolio * capital - sum(t.max_loss or 0 for t in open_trades)
        if max_loss_lot > 0 and lots * max_loss_lot > room:
            lots = math.floor(max(room, 0) / max_loss_lot + 1e-9)
            notes.append(f"portfolio gap cap {s.gap_cap_portfolio:.0%}: cut to {lots} lot(s)")
            if lots <= 0:
                return Size(0, notes=notes, reason=f"combined maximum loss would exceed {s.gap_cap_portfolio:.0%} of capital")
    return Size(lots, round(lots * loss_per_lot, 2), notes)
