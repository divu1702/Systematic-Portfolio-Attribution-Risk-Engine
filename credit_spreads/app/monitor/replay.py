"""Missed-mark replay: if the machine slept or lost its connection, rebuild the missing 5-minute
marks for open spreads from Kite's 5-minute candles and apply the exit rules to them.

Candles carry trade prices, not quotes, so the cost to close is estimated pessimistically:
short leg at close x (1 + h), long leg at close x (1 - h), with h = half the maximum bid-ask
spread allowed at entry (liquidity.max_spread_pct / 2). Exits found this way are flagged
`replayed` so their effect on results can be measured. Event exits are not replayed (event
data is checked live); the thesis stop is checked on the 15:15 candle.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, select

from ..clock import MARKET_CLOSE, MARKET_OPEN, parse_hhmm
from ..data import instruments as inst
from ..data.calendar import TradingCalendar
from ..data.models import Mark
from ..execution.paper import legs_of
from ..fmt import inr
from ..risk.limits import open_trades
from .marks import MarkResult, mark_trade

log = logging.getLogger(__name__)
BAR = timedelta(minutes=5)


def _closes(ctx, tradingsymbol: str, exchange: str, start: datetime, end: datetime) -> Dict[datetime, float]:
    tok = inst.token_for(ctx.engine, tradingsymbol, exchange)
    if not tok:
        return {}
    rows = ctx.broker.historical(tok, start, end, "5minute")
    return {r["date"] + BAR: float(r["close"]) for r in rows}


def catch_up(ctx, executor) -> List[str]:
    from .exits import REGULAR, THESIS, decide_exit

    now = ctx.clock.now()
    interval = timedelta(minutes=ctx.cfg.schedule.monitor_every_min)
    thesis_t = parse_hhmm(ctx.cfg.schedule.thesis_exit)
    h = ctx.cfg.liquidity.max_spread_pct / 2
    cal = TradingCalendar.load(ctx.engine)
    msgs = []
    for t in open_trades(ctx.engine):
        with ctx.engine.connect() as conn:
            last = conn.execute(select(func.max(Mark.ts)).where(Mark.trade_id == t.trade_id)).scalar()
        last = last or t.entry_ts
        if now - last <= 2 * interval:
            continue
        s, lg = legs_of(t)
        start = last
        short_c = _closes(ctx, s["tradingsymbol"], "NFO", start, now)
        long_c = _closes(ctx, lg["tradingsymbol"], "NFO", start, now)
        spot_c = _closes(ctx, t.symbol, "NSE", start, now)
        times = sorted(ts for ts in short_c if last < ts < now - interval / 2
                       and MARKET_OPEN < ts.time() <= MARKET_CLOSE and cal.is_trading_day(ts.date()))
        if not times:
            continue
        log.info("replaying %d missed marks for %s", len(times), t.trade_id)
        for ts in times:
            sp = short_c[ts] * (1 + h)
            lp = long_c.get(ts, 0.0) * (1 - h)
            m = mark_trade(ctx, t, {}, ts=ts, source="replay", prices=(sp, lp), spot=spot_c.get(ts))
            phase = THESIS if ts.time() == thesis_t else REGULAR
            decision = decide_exit(ctx, t, m, phase, cal, check_events=False)
            if decision:
                reason, gap = decision
                res = executor.close_spread(t, {}, reason, ts=ts, gap=gap, replayed=True, prices=(sp, lp))
                msg = (f"EXIT (replayed) {t.symbol} {t.short_k:g}/{t.long_k:g}: {reason} at {ts:%Y-%m-%d %H:%M}, "
                       f"net P&L {inr(res.pnl_net)}")
                msgs.append(msg)
                ctx.notifier.send(msg, "exit")
                break
    return msgs
