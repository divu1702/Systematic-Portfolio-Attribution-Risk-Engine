"""Mark open spreads at natural prices: cost to close = short ask - long bid (per share)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import insert, update

from ..broker.base import Quote
from ..data.models import Mark, Trade
from ..execution.paper import legs_of


@dataclass
class MarkResult:
    trade: Trade
    ts: datetime
    spot: Optional[float]
    cost_to_close: Optional[float]
    unrealised: Optional[float]
    ok: bool
    reason: str = ""


def quote_keys(trades: List[Trade]) -> List[str]:
    keys = []
    for t in trades:
        s, lg = legs_of(t)
        keys += [f"NFO:{s['tradingsymbol']}", f"NFO:{lg['tradingsymbol']}", f"NSE:{t.symbol}"]
    return keys


def mark_trade(ctx, t: Trade, quotes: Dict[str, Quote], ts: Optional[datetime] = None, source: str = "live",
               prices: Optional[tuple] = None, spot: Optional[float] = None) -> MarkResult:
    ts = ts or ctx.clock.now()
    s, lg = legs_of(t)
    qty = s["qty"]
    if prices is None:
        sq, lq = quotes.get(f"NFO:{s['tradingsymbol']}"), quotes.get(f"NFO:{lg['tradingsymbol']}")
        spot_q = quotes.get(f"NSE:{t.symbol}")
        spot = (spot_q.mid or spot_q.last) if spot_q else None
        if sq is None or sq.ask <= 0:
            return MarkResult(t, ts, spot, None, None, False, "no ask on the short leg")
        short_ask, long_bid = sq.ask, (lq.bid if lq else 0.0)
        legs = {"short": {"bid": sq.bid, "ask": sq.ask}, "long": {"bid": lq.bid if lq else None,
                                                                     "ask": lq.ask if lq else None}}
    else:
        short_ask, long_bid = prices
        legs = {"short": {"ask_est": short_ask}, "long": {"bid_est": long_bid}}
    ctc = short_ask - long_bid
    unreal = (t.entry_credit - ctc) * qty
    with ctx.engine.begin() as conn:
        conn.execute(insert(Mark).values(ts=ts, trade_id=t.trade_id, spot=spot, legs_json=json.dumps(legs),
                                         cost_to_close=round(ctc, 4), unrealised_pnl=round(unreal, 2), source=source))
        mae, mfe = min(t.mae or 0.0, unreal), max(t.mfe or 0.0, unreal)
        conn.execute(update(Trade).where(Trade.trade_id == t.trade_id).values(mae=round(mae, 2), mfe=round(mfe, 2)))
    t.mae, t.mfe = mae, mfe
    return MarkResult(t, ts, spot, ctc, unreal, True)
