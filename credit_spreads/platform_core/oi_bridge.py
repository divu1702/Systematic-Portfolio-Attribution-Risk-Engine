"""The earlier OI-wall system on the platform, as accounts oi_tight and oi_loose (spec: Integrating the
earlier OI-wall system).

Its setup detection, strikes, sizing and exit rules run unchanged. Only its order filling and money
records change: `attach()` installs an order adapter that fills each order through the shared paper
broker under the book's account (main -> oi_tight, v2 -> oi_loose, from platform.yaml `oi_system.books`)
and then writes the earlier system's own trade and order rows from those fills, under the same trade id,
so its monitor, exits, equity and reports keep reading what they always read. Money is the broker's:
cash, charges, margin and the ledger live in the platform database.

1R for these accounts is the earlier system's planned loss at its own stop (its `risk` field).
Trades the earlier system opened before the platform was attached keep closing through its own
paper executor. A position the broker settles at expiry is mirrored into the earlier system's records.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.execution.paper import EntryResult, ExitResult, Fill, PaperExecutor, legs_of
from app.risk.limits import open_trades
from app.risk.sizing import risk_per_lot

from .broker import PaperBroker, SpreadOrder
from .models import POrder, PTrade

log = logging.getLogger(__name__)

FACTORY_KEY = "platform_executor"


class OIExecutor:
    mode = "paper"

    def __init__(self, ctx, broker: PaperBroker, account: str):
        self.ctx, self.broker, self.account = ctx, broker, account
        self.legacy = PaperExecutor(ctx)

    def open_spread(self, cand, lots: int, quotes, meta: dict) -> EntryResult:
        ctx = self.ctx
        ts = ctx.clock.now()
        tid = self.legacy.new_trade_id(cand, ts)
        order = SpreadOrder(
            account=self.account, symbol=cand.symbol, direction=cand.side, expiry=cand.expiry, cp=meta["cp"],
            short_symbol=cand.short_sym, short_strike=cand.short_k, long_symbol=cand.long_sym, long_strike=cand.long_k,
            lots=lots, lot_size=cand.lot_size, tick=meta.get("tick", 0.05), source="oi_system", trade_id=tid,
            sector=meta.get("sector"), level=cand.inputs.get("level"), book=ctx.strategy,
            reason=f"OI-wall system, {ctx.strategy} rules",
            context={k: cand.inputs.get(k) for k in ("short_iv", "hv20", "ivp", "iv_hv", "level") if k in cand.inputs})
        res = self.broker.open_spread(order, quotes)
        if not res.ok:                                   # the earlier system counts rejections for its kill switch
            qty = lots * cand.lot_size
            miss = Fill(False, qty, 0, 0, 0, 0, res.reason[:120])
            self.legacy.record_rejection(cand, qty, miss, miss, ts)
            return EntryResult(False, reason=res.reason)
        risk = risk_per_lot(ctx.cfg, res.credit, cand.width, cand.lot_size) * lots
        self.broker.update_trade(tid, one_r=round(risk, 2), loss_at_kill=round(risk / lots, 2))
        self.legacy.record_entry(tid, cand, lots, meta, res.short_fill, res.long_fill, res.charges, res.margin,
                                 res.margin_fallback, ts)
        return EntryResult(True, tid, credit=res.credit, costs=res.charges, margin=res.margin,
                           margin_fallback=res.margin_fallback)

    def close_spread(self, trade, quotes, reason: str, ts=None, gap: bool = False, replayed: bool = False,
                     prices=None, tick: float = 0.05) -> ExitResult:
        pt = self.broker.get_trade(trade.trade_id)
        if pt is None:                                   # opened before the platform: the earlier executor closes it
            return self.legacy.close_spread(trade, quotes, reason, ts=ts, gap=gap, replayed=replayed, prices=prices,
                                            tick=tick)
        if pt.status != "open":                          # already closed by the broker (expiry settlement)
            return mirror_close(self.broker, self.legacy, trade, pt)
        ts = ts or self.ctx.clock.now()
        res = self.broker.close_spread(trade.trade_id, reason, quotes=quotes, prices=prices, ts=ts, tick=tick)
        if not res.ok:
            self.legacy.record_exit_failure(trade, Fill(False, 0, 0, 0, 0, 0, res.reason[:120]), ts)
            return ExitResult(False, res.reason)
        return self.legacy.record_exit(trade, res.short_fill, res.long_fill, res.charges, reason, ts, gap, replayed)


def mirror_close(broker: PaperBroker, legacy: PaperExecutor, trade, pt: PTrade) -> ExitResult:
    """Write a close the broker made on its own (expiry settlement) into the earlier system's records:
    each leg at its settlement value, the broker's settlement charges, the same P&L."""
    with Session(broker.pengine) as s:
        o = s.scalars(select(POrder).where(POrder.trade_id == pt.id, POrder.purpose == "settlement")
                      .order_by(POrder.ts.desc()).limit(1)).first()
    vals = {x.get("symbol"): x.get("intrinsic") for x in json.loads(o.legs_json)} if o else {}
    short, long_ = legs_of(trade)
    qty = short["qty"]
    sp = float(vals.get(short["tradingsymbol"]) or (pt.exit_cost or 0.0))
    lp = float(vals.get(long_["tradingsymbol"]) or 0.0)
    s_fill = Fill(True, qty, sp, sp, sp, 0, "expiry settlement")
    l_fill = Fill(True, qty, lp, lp, lp, 0, "expiry settlement")
    return legacy.record_exit(trade, s_fill, l_fill, pt.exit_charges or 0.0, pt.exit_reason or "expiry_settlement",
                              pt.exit_ts)


def sync_closed(ctx, broker: PaperBroker) -> List[str]:
    """Mirror every broker-side close (expiry settlement) into the earlier system's open trades."""
    msgs = []
    for t in open_trades(ctx.engine):
        pt = broker.get_trade(t.trade_id)
        if pt is not None and pt.status == "closed":
            res = mirror_close(broker, PaperExecutor(ctx.for_strategy(t.strategy, missing_ok=True)), t, pt)
            msgs.append(f"[{pt.account}] {t.symbol}: {pt.exit_reason}, net P&L {res.pnl_net:,.0f}")
    return msgs


def account_for_book(pcfg) -> Dict[str, str]:
    """Earlier-system book -> platform account, e.g. {"main": "oi_tight", "v2": "oi_loose"}."""
    return {book: acc for acc, book in pcfg.oi_system.books.items()}


def attach(ctx, broker: PaperBroker, pcfg) -> Dict[str, str]:
    """Route the earlier system's mapped books through the shared broker. The platform is paper only,
    so a config in live mode is refused."""
    if ctx.cfg.mode != "paper":
        raise RuntimeError("the platform is paper trading only: config.yaml must say mode: paper")
    books = account_for_book(pcfg)
    missing = [b for b in books if b not in ctx.cfg.strategy_names]
    if missing:
        raise RuntimeError(f"platform.yaml oi_system.books names books config.yaml does not define: {missing}")
    unmapped = [b for b in ctx.cfg.strategy_names if b not in books]
    if unmapped:
        log.warning("earlier-system books with no platform account keep trading in its own records only: %s",
                    ", ".join(unmapped))

    def factory(c) -> Optional[OIExecutor]:
        acc = books.get(c.strategy)
        return OIExecutor(c, broker, acc) if acc else None

    (ctx.parent or ctx).cache[FACTORY_KEY] = factory
    return books
