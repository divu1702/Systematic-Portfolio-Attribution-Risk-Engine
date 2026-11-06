"""Exit rules, checked on every mark; the first rule that fires closes the position.

1 event appears before expiry   2 hard stop: cost to close >= stop_mult x credit
3 thesis stop (15:15 check): spot beyond the level behind the trade
4 profit target: cost to close <= tp_frac x credit   5 time stop: <= N trading days to expiry
At the 09:20 gap check, a spot beyond the long strike forces an exit at actual prices and the
trade is flagged as a gap loss.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from ..data import events as ev_data
from ..data.calendar import TradingCalendar
from ..data.models import Trade
from ..execution.paper import legs_of
from ..fmt import inr
from ..risk.limits import open_trades
from ..strategy.spreads import BULL_PUT
from .marks import MarkResult, mark_trade, quote_keys

log = logging.getLogger(__name__)

REGULAR, GAP, THESIS = "regular", "gap", "thesis"


def decide_exit(ctx, t: Trade, m: MarkResult, phase: str, cal: TradingCalendar,
                check_events: bool = True) -> Optional[Tuple[str, bool]]:
    """(reason, gap_flag) for the first rule that fires, or None."""
    x = ctx.cfg.exit
    beyond_long = m.spot is not None and t.long_k is not None and (
        m.spot < t.long_k if t.side == BULL_PUT else m.spot > t.long_k)
    if phase == GAP and beyond_long:
        return "gap_through_long", True
    if check_events and x.on_new_event:
        evs = ev_data.new_events_for_position(ctx, t.symbol, t.expiry)
        if evs:
            return f"event: {evs[0]}", False
    if m.cost_to_close is not None and m.cost_to_close >= x.stop_mult * t.entry_credit:
        return "hard_stop", phase == GAP
    if phase == THESIS and x.thesis_stop and m.spot is not None and t.level_ref is not None:
        if (t.side == BULL_PUT and m.spot < t.level_ref) or (t.side != BULL_PUT and m.spot > t.level_ref):
            return "thesis_stop", False
    if m.cost_to_close is not None and m.cost_to_close <= x.tp_frac * t.entry_credit:
        return "profit_target", False
    if cal.trading_days_between(m.ts.date(), t.expiry) <= x.days_before_expiry:
        return "time_stop", False
    return None


def run_monitor(ctx, executor, phase: str = REGULAR) -> List[str]:
    """Mark every open spread and close those whose exit rule fires. Returns alert texts."""
    if ctx.cfg.monitor.replay_missed:
        from .replay import catch_up

        catch_up(ctx, executor)
    trades = open_trades(ctx.engine)
    if not trades:
        return []
    cal = TradingCalendar.load(ctx.engine)
    quotes = ctx.broker.quotes(quote_keys(trades))
    msgs = []
    for t in trades:
        m = mark_trade(ctx, t, quotes)
        if not m.ok:
            msgs.append(f"{t.symbol}: could not mark ({m.reason})")
            continue
        decision = decide_exit(ctx, t, m, phase, cal)
        if not decision:
            continue
        reason, gap = decision
        res = executor.close_spread(t, quotes, reason, gap=gap)
        if res.ok:
            msgs.append(f"EXIT {t.symbol} {t.side.replace('_', ' ')} {t.short_k:g}/{t.long_k:g}: {reason}, "
                        f"net P&L {inr(res.pnl_net)}{' (gap)' if gap else ''}")
        else:
            msgs.append(f"EXIT FAILED {t.symbol}: {res.reason}")
    for msg in msgs:
        ctx.notifier.send(msg, "exit" if msg.startswith("EXIT") else "warning")
    return msgs
