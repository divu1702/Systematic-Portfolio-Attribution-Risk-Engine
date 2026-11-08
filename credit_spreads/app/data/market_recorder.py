"""Record the live market into a backtest store while paper trading.

Free Indian option history is not available to automated clients, so the system keeps its own:
every 15:00 scan already fetches every quote it evaluates (stocks, futures and options within the
scan band). Those quotes are saved with their real bid/ask, top-of-book and 5-level sizes, OI,
volume, lot size and tick, plus each day's bars, sectors and the date each corporate event was
first seen. `python -m app backtest run --market nse` replays the store through the same code.

During the session, `snapshot_quotes` also saves bid/ask through the day (every
`snapshots.every_min` minutes) for the strikes the strategy trades; the end-of-day step compacts
those into one compressed blob per contract-day.

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
    compact_snapshots(ctx)


# ----------------------------------------------------------------------------- intraday quote snapshots

def snapshot_quotes(ctx) -> int:
    """Bid/ask, sizes, volume, OI, all pending buy and sell quantity (order-book imbalance, a gauge of
    who is pressing) and the last trade's size, through the day, for the strikes the strategy trades: options of
    each stock's nearest `snapshots.expiries` monthly series within +-`snapshots.band` of the futures
    price, plus those futures and the stock. Two quote requests for stocks and futures, then about
    one per 500 options (Kite: one request a second). Rows are staged and compacted after the close
    (compact_snapshots). Returns the number of quotes saved."""
    from ..clock import parse_hhmm
    from .. import market

    sc = ctx.cfg.snapshots
    if not sc.every_min or not _enabled(ctx):
        return 0
    now = ctx.clock.now().replace(second=0, microsecond=0)
    if not parse_hhmm(sc.start) <= now.time() <= parse_hhmm(sc.end):
        return 0
    d = market.ensure_instruments(ctx)
    with ctx.engine.connect() as conn:
        rows = conn.execute(select(Instrument.exchange, Instrument.tradingsymbol, Instrument.underlying,
                                   Instrument.expiry, Instrument.strike, Instrument.instrument_type)
                            .where(Instrument.date == d)).all()
    fut_exp: Dict[str, list] = {}
    for ex, ts, und, exp, k, typ in rows:
        if typ == "FUT":
            fut_exp.setdefault(und, []).append(exp)
    keep = {und: set(sorted(es)[: sc.expiries]) for und, es in fut_exp.items()}
    meta = {f"{ex}:{ts}": (und, exp, k, typ) for ex, ts, und, exp, k, typ in rows
            if (typ == "EQ" and und in keep) or (typ != "EQ" and exp in keep.get(und, ()))}
    head = [key for key, m in meta.items() if m[3] in ("EQ", "FUT")]
    quotes = ctx.broker.quotes(head)
    ref: Dict[Tuple[str, date], float] = {}
    for key in head:
        q, (und, exp, _, typ) = quotes.get(key), meta[key]
        if q and typ == "FUT" and (q.mid or q.last):
            ref[(und, exp)] = q.mid or q.last
    legs = [key for key, (und, exp, k, typ) in meta.items() if typ in ("CE", "PE") and ref.get((und, exp))
            and abs(k / ref[(und, exp)] - 1) <= sc.band]
    quotes.update(ctx.broker.quotes(legs))
    out = []
    for key, q in quotes.items():
        if key not in meta:
            continue
        und, exp, k, typ = meta[key]
        out.append((now.isoformat(sep=" "), und, str(exp) if typ != "EQ" else "", float(k or 0) if typ in ("CE", "PE")
                    else 0.0, typ, q.bid or None, q.ask or None, q.last or None,
                    q.bids[0].quantity if q.bids else 0, q.asks[0].quantity if q.asks else 0,
                    sum(lv.quantity for lv in q.bids[:5]), sum(lv.quantity for lv in q.asks[:5]),
                    int(q.volume or 0), int(q.oi or 0), int(q.buy_total or 0), int(q.sell_total or 0),
                    int(q.last_qty or 0)))
    store = connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))
    try:
        store.executemany("INSERT OR REPLACE INTO quote_snap_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        store.commit()
    finally:
        store.close()
    log.info("quote snapshot: %d quotes (%d options)", len(out), len(legs))
    return len(out)


def compact_snapshots(ctx) -> int:
    """Move staged snapshot rows into one compressed blob per contract-day (merging with any blob
    already there). Returns the number of quotes compacted."""
    from . import barcodec

    store = connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))
    try:
        rows = store.execute("SELECT ts, symbol, expiration, strike, cp, bid, ask, last, bid_qty, ask_qty, "
                             "bid_depth, ask_depth, volume, oi, buy_total, sell_total, last_qty FROM quote_snap_rows "
                             "ORDER BY symbol, expiration, strike, cp, ts").fetchall()
        groups: Dict[tuple, list] = {}
        for r in rows:
            groups.setdefault((r[1], r[2], r[3], r[4], r[0][:10]), []).append(r)
        names = [c for c, _, _ in barcodec.QUOTE_COLS]
        for (sym, exp, k, cp, d), rs in groups.items():
            cols = {"minute": [int(r[0][11:13]) * 60 + int(r[0][14:16]) for r in rs],
                    "bid": [r[5] or 0.0 for r in rs], "ask": [r[6] or 0.0 for r in rs], "last": [r[7] or 0.0 for r in rs],
                    "bid_qty": [r[8] or 0 for r in rs], "ask_qty": [r[9] or 0 for r in rs],
                    "bid_depth": [r[10] or 0 for r in rs], "ask_depth": [r[11] or 0 for r in rs],
                    "volume": [r[12] or 0 for r in rs], "oi": [r[13] or 0 for r in rs],
                    "buy_total": [r[14] or 0 for r in rs], "sell_total": [r[15] or 0 for r in rs],
                    "last_qty": [r[16] or 0 for r in rs]}
            old = store.execute("SELECT data FROM quote_snaps WHERE symbol=? AND expiration=? AND strike=? AND cp=? "
                                "AND date=?", (sym, exp, k, cp, d)).fetchone()
            if old:
                prev = barcodec.decode(old[0])
                merged = {int(m): [prev[c][i] for c in names] for i, m in enumerate(prev["minute"])}
                for i, m in enumerate(cols["minute"]):
                    merged[m] = [cols[c][i] for c in names]
                order = sorted(merged)
                cols = {c: [merged[m][j] for m in order] for j, c in enumerate(names)}
            store.execute("INSERT OR REPLACE INTO quote_snaps VALUES (?,?,?,?,?,?,?)",
                          (sym, exp, k, cp, d, len(cols["minute"]), barcodec.encode(cols, b"Q")))
        store.execute("DELETE FROM quote_snap_rows")
        store.commit()
    finally:
        store.close()
    if rows:
        log.info("compacted %d snapshot quotes into %d contract-days", len(rows), len(groups))
    return len(rows)
