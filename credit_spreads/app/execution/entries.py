"""Turn the scan's top candidates into paper trades, through sizing, portfolio limits and kill
switches. Every rejection is alerted and logged."""

from __future__ import annotations

import logging
from typing import List

from ..broker.base import BrokerError
from ..data import refdata
from ..data.instruments import load_chain
from ..fmt import inr
from ..risk.limits import kill_switches, open_trades, size_and_check, stale_quotes
from ..risk.sizing import current_equity
from ..strategy.spreads import option_type

log = logging.getLogger(__name__)


def enter_from_scan(ctx, executor, result) -> List[str]:
    msgs: List[str] = []
    if not result.selected:
        return msgs
    equity = current_equity(ctx)
    blocks = kill_switches(ctx, equity)
    if blocks:
        msg = f"Entries blocked ({len(result.selected)} candidate(s)): " + "; ".join(blocks)
        ctx.notifier.send(msg, "risk")
        return [msg]
    for cand in result.selected:
        open_ = open_trades(ctx.engine)
        sector = refdata.sector_of(ctx.engine, cand.symbol)
        keys = [f"NFO:{cand.short_sym}", f"NFO:{cand.long_sym}"]
        quotes = ctx.broker.quotes(keys)
        stale = stale_quotes(ctx, quotes) if len(quotes) == 2 else keys
        if stale:
            msgs.append(f"REJECTED {cand.symbol}: stale or missing quotes for {', '.join(stale)}")
            continue
        margin_lot = _margin_per_lot(ctx, cand, quotes)
        lots, violations = size_and_check(ctx, cand, sector, margin_lot, open_, equity)
        if violations or lots <= 0:
            msgs.append(f"REJECTED {cand.symbol} {cand.side.replace('_', ' ')}: " + "; ".join(violations))
            continue
        chain = load_chain(ctx.engine, ctx.clock.today(), cand.symbol, cand.expiry)
        res = executor.open_spread(cand, lots, quotes, {"cp": option_type(cand.side), "tick": chain.tick_size,
                                                        "sector": sector})
        if not res.ok:
            msgs.append(f"REJECTED {cand.symbol}: {res.reason}")
            continue
        max_loss = (cand.width - res.credit) * lots * cand.lot_size
        msgs.append(
            f"ENTRY {cand.symbol} {cand.side.replace('_', ' ')} {cand.short_k:g}/{cand.long_k:g} {cand.expiry} "
            f"x{lots} lot(s) of {cand.lot_size}: credit {res.credit:.2f}/share "
            f"({inr(res.credit * lots * cand.lot_size)}), max loss {inr(max_loss)}, margin {inr(res.margin)}"
            f"{' [margin fallback]' if res.margin_fallback else ''}, p_model {cand.p_model:.0%}")
    for m in msgs:
        ctx.notifier.send(m, "entry" if m.startswith("ENTRY") else "rejection")
    return msgs


def _margin_per_lot(ctx, cand, quotes) -> float:
    s, lg = quotes[f"NFO:{cand.short_sym}"], quotes[f"NFO:{cand.long_sym}"]
    orders = [
        {"exchange": "NFO", "tradingsymbol": cand.long_sym, "transaction_type": "BUY", "variety": "regular",
         "product": "NRML", "order_type": "LIMIT", "quantity": cand.lot_size, "price": lg.ask},
        {"exchange": "NFO", "tradingsymbol": cand.short_sym, "transaction_type": "SELL", "variety": "regular",
         "product": "NRML", "order_type": "LIMIT", "quantity": cand.lot_size, "price": s.bid},
    ]
    try:
        return float(ctx.broker.basket_margin(orders))
    except (BrokerError, Exception):
        return cand.max_loss * ctx.cfg.paper.margin_fallback_mult
