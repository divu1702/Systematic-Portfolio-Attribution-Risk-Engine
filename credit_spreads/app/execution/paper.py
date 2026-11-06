"""Paper fill simulator (spec: Paper trading engine). Fills are harder than reality:

- Sell at the bid, buy at the ask, at decision time; then 1 extra tick of slippage per leg.
- If top-of-book size is short, walk the 5-level book and use the volume-weighted price;
  reject the fill if 5 levels are not enough.
- Entry legs: buy the hedge first, then sell the short (same order live will use).
- Exit legs: buy back the short first, then sell the long. If the short cannot be closed, the
  long is left alone and the position stays open (never a naked short); the next mark retries.
- Margin from the broker's basket-margin API; on failure max loss x fallback multiplier, flagged.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import insert, select, update

from ..broker.base import Quote
from ..data.models import Order, Trade
from ..risk.sizing import risk_per_lot
from ..strategy.costs import entry_costs, exit_costs

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fill:
    ok: bool
    qty: int
    natural: float          # best price on the relevant side at decision time
    vwap: float             # volume-weighted across the levels used
    price: float            # vwap plus the extra slippage
    levels: int
    reason: str = ""


def simulate_fill(q: Optional[Quote], side: str, qty: int, tick: float, extra_ticks: int) -> Fill:
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


def _order_row(trade_id: Optional[str], purpose: str, leg: str, side: str, qty: int, fill: Fill,
               ts: datetime, symbol: str) -> dict:
    return dict(order_id=f"P-{uuid.uuid4().hex[:12]}", trade_id=trade_id, purpose=purpose, leg=leg, side=side,
                qty=qty, limit_px=fill.natural or None, status="filled" if fill.ok else "rejected",
                fill_px=fill.price if fill.ok else None, broker_ref=f"paper:{symbol}:{fill.levels}lv"
                if fill.ok else f"paper:{fill.reason}", ts=ts)


def legs_of(trade: Trade) -> Tuple[dict, dict]:
    legs = json.loads(trade.legs_json)
    short = next(lg for lg in legs if lg["leg"] == "short")
    long_ = next(lg for lg in legs if lg["leg"] == "long")
    return short, long_


@dataclass
class EntryResult:
    ok: bool
    trade_id: Optional[str] = None
    reason: str = ""
    credit: float = 0.0
    costs: float = 0.0
    margin: float = 0.0
    margin_fallback: bool = False


@dataclass
class ExitResult:
    ok: bool
    reason: str = ""
    exit_cost: float = 0.0
    pnl_net: float = 0.0


class PaperExecutor:
    mode = "paper"

    def __init__(self, ctx):
        self.ctx = ctx

    # ------------------------------------------------------------------ entry
    def open_spread(self, cand, lots: int, quotes: Dict[str, Quote], meta: dict) -> EntryResult:
        ctx, cfg = self.ctx, self.ctx.cfg
        ts = ctx.clock.now()
        qty = lots * cand.lot_size
        tick = meta.get("tick", 0.05)
        short_key, long_key = f"NFO:{cand.short_sym}", f"NFO:{cand.long_sym}"
        long_fill = simulate_fill(quotes.get(long_key), "BUY", qty, tick, cfg.paper.extra_slip_ticks)
        short_fill = simulate_fill(quotes.get(short_key), "SELL", qty, tick, cfg.paper.extra_slip_ticks)
        if not (long_fill.ok and short_fill.ok):
            reason = f"fill rejected: long {long_fill.reason or 'ok'}, short {short_fill.reason or 'ok'}"
            with ctx.engine.begin() as conn:
                conn.execute(insert(Order), [_order_row(None, "entry", "long", "BUY", qty, long_fill, ts, cand.long_sym),
                                             _order_row(None, "entry", "short", "SELL", qty, short_fill, ts,
                                                        cand.short_sym)])
            return EntryResult(False, reason=reason)
        credit = short_fill.price - long_fill.price
        if credit <= 0:
            return EntryResult(False, reason="no credit after fills")
        costs = entry_costs(cfg.costs, short_fill.price, long_fill.price, qty)
        max_loss = (cand.width - credit) * qty
        risk = risk_per_lot(cfg, credit, cand.width, cand.lot_size) * lots
        margin, fallback = self._margin(cand, qty, short_fill, long_fill, max_loss)
        trade_id = f"T{ts:%Y%m%d%H%M}-{cand.symbol}-{uuid.uuid4().hex[:6]}"
        legs = [
            {"leg": "short", "tradingsymbol": cand.short_sym, "strike": cand.short_k, "cp": meta["cp"],
             "qty": qty, "fill": short_fill.price, "natural": short_fill.natural},
            {"leg": "long", "tradingsymbol": cand.long_sym, "strike": cand.long_k, "cp": meta["cp"],
             "qty": qty, "fill": long_fill.price, "natural": long_fill.natural},
        ]
        i = cand.inputs
        row = dict(
            trade_id=trade_id, mode="paper", status="open", candidate_id=None, symbol=cand.symbol,
            sector=meta.get("sector"), side=cand.side, expiry=cand.expiry, legs_json=json.dumps(legs), lots=lots,
            lot_size=cand.lot_size, entry_ts=ts, entry_credit=round(credit, 4), costs=round(costs, 2),
            max_loss=round(max_loss, 2), risk=round(risk, 2), margin=margin, margin_fallback=fallback, iv_at_entry=i.get("short_iv"),
            hv20=i.get("hv20"), ivp=i.get("ivp"), ivp_method=i.get("ivp_method"), level_ref=i.get("level"),
            p_market=cand.p_market, p_model=cand.p_model, breakeven_wr=i.get("breakeven_win_rate"),
            iv_hv=i.get("iv_hv"), short_k=cand.short_k, long_k=cand.long_k, mae=0.0, mfe=0.0,
        )
        with ctx.engine.begin() as conn:
            conn.execute(insert(Trade).values(**row))
            # Hedge first, then the short.
            conn.execute(insert(Order), [_order_row(trade_id, "entry", "long", "BUY", qty, long_fill, ts, cand.long_sym),
                                         _order_row(trade_id, "entry", "short", "SELL", qty, short_fill, ts,
                                                    cand.short_sym)])
        return EntryResult(True, trade_id, credit=credit, costs=costs, margin=margin, margin_fallback=fallback)

    def _margin(self, cand, qty: int, short_fill: Fill, long_fill: Fill, max_loss: float) -> Tuple[float, bool]:
        orders = [
            {"exchange": "NFO", "tradingsymbol": cand.long_sym, "transaction_type": "BUY", "variety": "regular",
             "product": "NRML", "order_type": "LIMIT", "quantity": qty, "price": long_fill.price},
            {"exchange": "NFO", "tradingsymbol": cand.short_sym, "transaction_type": "SELL", "variety": "regular",
             "product": "NRML", "order_type": "LIMIT", "quantity": qty, "price": short_fill.price},
        ]
        try:
            return float(self.ctx.broker.basket_margin(orders)), False
        except Exception as exc:
            log.warning("basket margin failed for %s (%s); using max loss x %.1f", cand.symbol, exc,
                        self.ctx.cfg.paper.margin_fallback_mult)
            return round(max_loss * self.ctx.cfg.paper.margin_fallback_mult, 2), True

    # ------------------------------------------------------------------ exit
    def close_spread(self, trade: Trade, quotes: Dict[str, Quote], reason: str, ts: Optional[datetime] = None,
                     gap: bool = False, replayed: bool = False, prices: Optional[Tuple[float, float]] = None,
                     tick: float = 0.05) -> ExitResult:
        """Close at natural prices: buy back the short first, then sell the long. `prices`
        (short, long) replaces book fills when an exit is reconstructed from candles."""
        ctx, cfg = self.ctx, self.ctx.cfg
        ts = ts or ctx.clock.now()
        short, long_ = legs_of(trade)
        qty = short["qty"]
        slip = cfg.paper.extra_slip_ticks
        if prices:
            sp, lp = prices
            s_fill = Fill(True, qty, sp, sp, round(sp + slip * tick, 4), 0)
            l_fill = Fill(True, qty, lp, lp, round(max(lp - slip * tick, 0.0), 4), 0)
        else:
            s_fill = simulate_fill(quotes.get(f"NFO:{short['tradingsymbol']}"), "BUY", qty, tick, slip)
            if not s_fill.ok:
                with ctx.engine.begin() as conn:
                    conn.execute(insert(Order).values(**_order_row(trade.trade_id, "exit", "short", "BUY", qty, s_fill,
                                                                   ts, short["tradingsymbol"])))
                return ExitResult(False, f"short-leg close failed ({s_fill.reason}); long kept, will retry")
            l_fill = simulate_fill(quotes.get(f"NFO:{long_['tradingsymbol']}"), "SELL", qty, tick, slip)
            if not l_fill.ok:
                # Short is already closed, so the long can be sold at whatever the book gives
                # (a bought option cannot hurt); value it at zero if there is no bid at all.
                l_fill = Fill(True, qty, 0.0, 0.0, 0.0, 0, "no bid: long valued at 0")
        exit_cost = s_fill.price - l_fill.price
        x_costs = exit_costs(cfg.costs, s_fill.price, l_fill.price, qty)
        pnl_gross = (trade.entry_credit - exit_cost) * qty
        pnl_net = pnl_gross - trade.costs - x_costs
        with ctx.engine.begin() as conn:
            conn.execute(insert(Order), [
                _order_row(trade.trade_id, "exit", "short", "BUY", qty, s_fill, ts, short["tradingsymbol"]),
                _order_row(trade.trade_id, "exit", "long", "SELL", qty, l_fill, ts, long_["tradingsymbol"])])
            conn.execute(update(Trade).where(Trade.trade_id == trade.trade_id).values(
                status="closed", exit_ts=ts, exit_cost=round(exit_cost, 4), exit_costs=round(x_costs, 2),
                exit_reason=reason, pnl_gross=round(pnl_gross, 2), pnl_net=round(pnl_net, 2),
                gap_flag=bool(trade.gap_flag or gap), replayed=replayed))
        return ExitResult(True, reason, exit_cost, pnl_net)
