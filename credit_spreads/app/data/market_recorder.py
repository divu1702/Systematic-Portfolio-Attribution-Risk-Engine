"""Record the live market into a backtest store while paper trading.

Free Indian option history is not available to automated clients, so the system keeps its own:
every 15:00 scan already fetches every quote it evaluates (stocks, futures and options within the
scan band). Those quotes are saved with their real bid/ask, top-of-book and 5-level sizes, OI,
volume, lot size and tick, plus each day's bars, sectors and the date each corporate event was
first seen. `python -m app backtest run --market nse` replays the store through the same code.

Recording never interrupts trading: failures are logged and skipped.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Tuple

from sqlalchemy import select

from ..analytics import black76
from ..backtest.store import connect
from ..broker.base import Quote
from .models import Event, Instrument, Sector, UnderlyingDaily

log = logging.getLogger(__name__)


def _enabled(ctx) -> bool:
    return bool(ctx.cfg.ops.record_market) and not ctx.is_mock and not getattr(ctx, "banner", "")


def _meta(ctx, d: date) -> Dict[str, Tuple]:
    with ctx.engine.connect() as conn:
        rows = conn.execute(select(Instrument.exchange, Instrument.tradingsymbol, Instrument.underlying,
                                   Instrument.expiry, Instrument.strike, Instrument.instrument_type,
                                   Instrument.lot_size, Instrument.tick_size).where(Instrument.date == d)).all()
    return {f"{ex}:{ts}": (und, exp, k, typ, lot, tick) for ex, ts, und, exp, k, typ, lot, tick in rows}


def record_scan_quotes(ctx, quotes: Dict[str, Quote]) -> int:
    """Save one scan's quotes. Returns the number of option rows written."""
    if not _enabled(ctx) or not quotes:
        return 0
    d = ctx.clock.today()
    now = ctx.clock.now()
    meta = _meta(ctx, d)
    fut_mid: Dict[Tuple[str, date], float] = {}
    rows_fut = []
    for key, q in quotes.items():
        m = meta.get(key)
        if m and m[3] == "FUT":
            sym, exp = m[0], m[1]
            mid = q.mid or q.last
            if mid:
                fut_mid[(sym, exp)] = mid
            rows_fut.append((str(d), sym, str(exp), q.bid or None, q.ask or None, q.last or None))
    r = ctx.cfg.analytics.risk_free
    rows_chain, rows_extra, per_symbol = [], [], {}
    for key, q in quotes.items():
        m = meta.get(key)
        if not m or m[3] not in ("CE", "PE"):
            continue
        sym, exp, k, cp, lot, tick = m
        F = fut_mid.get((sym, exp))
        iv = None
        if F and q.bid > 0 and q.ask > 0:
            T = black76.year_fraction((black76_expiry(exp) - now).total_seconds())
            iv = black76.implied_vol(q.mid, F, k, T, r, cp) if T > 0 else None
        rows_chain.append((str(d), sym, str(exp), float(k), cp, q.bid or 0.0, q.ask or 0.0, iv, None))
        rows_extra.append((str(d), sym, str(exp), float(k), cp, lot, tick,
                           q.bids[0].quantity if q.bids else 0, q.asks[0].quantity if q.asks else 0,
                           sum(lv.quantity for lv in q.bids[:5]), sum(lv.quantity for lv in q.asks[:5]),
                           q.oi, q.volume))
        per_symbol[sym] = per_symbol.get(sym, 0) + 1
    store = connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))
    try:
        store.executemany("INSERT OR REPLACE INTO chains VALUES (?,?,?,?,?,?,?,?,?)", rows_chain)
        store.executemany("INSERT OR REPLACE INTO chain_extras VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_extra)
        store.executemany("INSERT OR REPLACE INTO futures VALUES (?,?,?,?,?,?)", rows_fut)
        store.executemany("INSERT OR REPLACE INTO chain_days_loaded VALUES (?,?,?)",
                          [(sym, str(d), n) for sym, n in per_symbol.items()])
        store.commit()
    finally:
        store.close()
    log.info("recorded %d option quotes for %d stocks", len(rows_chain), len(per_symbol))
    return len(rows_chain)


def black76_expiry(exp: date):
    from datetime import datetime

    from ..market import EXPIRY_TIME

    return datetime.combine(exp, EXPIRY_TIME)


def record_eod(ctx) -> None:
    """Today's bars, sectors and first-seen dates of corporate events."""
    if not _enabled(ctx):
        return
    d = ctx.clock.today()
    with ctx.engine.connect() as conn:
        bars = conn.execute(select(UnderlyingDaily.symbol, UnderlyingDaily.date, UnderlyingDaily.open,
                                   UnderlyingDaily.high, UnderlyingDaily.low, UnderlyingDaily.close,
                                   UnderlyingDaily.volume).where(UnderlyingDaily.date == d)).all()
        evs = conn.execute(select(Event.symbol, Event.event_date, Event.event_type, Event.fetched_at)).all()
        secs = conn.execute(select(Sector.symbol, Sector.sector)).all()
    store = connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))
    try:
        store.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                          [(s, str(dd), o, h, lo, c, v) for s, dd, o, h, lo, c, v in bars])
        store.executemany("INSERT OR IGNORE INTO events_seen VALUES (?,?,?,?)",
                          [(s, str(ed), t, str(fa)) for s, ed, t, fa in evs])
        store.executemany("INSERT OR REPLACE INTO sectors VALUES (?,?)", list(secs))
        store.commit()
    finally:
        store.close()
