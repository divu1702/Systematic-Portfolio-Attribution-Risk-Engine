"""Daily scanner (spec: Entry filters, Strike selection and spread construction), in layers:

1. Signals (signals.py), once per stock and shared by every strategy: daily bars and indicators,
   the ATM IV snapshot (stored for every universe stock), IV percentile, levels, averages, recent
   moves, chain snapshots from one batched quote pass.
2. Entry rules (rules.py), per strategy: DTE window, F2 events, F3 IV/HV, F4 IV percentile, F5
   timing, then the side to sell and the price behind it.
3. Structures (structures.py), per strategy: short strikes (F6, F7), wings, F9 liquidity, F8
   credit quality, sizing within that strategy's book, depth, EV under its exit rules.
4. Risk (app/risk), per book: portfolio limits and kill switches at entry (execution/entries.py).

The main rules form the main book; each entry under `strategies:` in config.yaml is a paper sleeve
evaluated on the same signals in its own book. Every stock-level rejection and every enumerated
spread is saved to `candidates` with its strategy and inputs. Accepted spreads are ranked by EV / max
loss within each book.
"""

from __future__ import annotations

import json
import logging
import math
import time as _time
from typing import Dict, List, Optional

from sqlalchemy import insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .. import market
from ..config import MAIN
from ..data import instruments as inst
from ..data import refdata
from ..data.calendar import TradingCalendar
from ..data.models import Candidate, IVSnapshot
from ..risk.sizing import current_equity
from .candidates import (STAGES, BookResult, ScanResult, SpreadCandidate, StockOutcome,  # noqa: F401
                         flags_upto, quote_dict)
from .rules import choose_direction, decide_entry, f4_pass, f5_check  # noqa: F401  (re-exported)
from .signals import SCAN_BAND, StockSignals, compute_signals, iv_history, iv_percentile  # noqa: F401
from .structures import build_verticals, credit_quality, depth_ok, liquidity_ok  # noqa: F401

log = logging.getLogger(__name__)

_flags_upto = flags_upto          # older name, kept for callers
_q = quote_dict


def _row(ctx, symbol: str, stage: str, reason: str, flag_ok: bool = True, trade_exp=None, dte=None,
         inputs: Optional[dict] = None) -> StockOutcome:
    c = SpreadCandidate(symbol, "", 0, 0, trade_exp, dte, 0, flags=flags_upto(1, flag_ok), inputs=inputs or {},
                        strategy=ctx.strategy)
    c.reject_reason = f"{stage}: {reason}"
    return StockOutcome(symbol, stage, reason, rows=[c])


def evaluate_book(ctx, sig: Optional[StockSignals], data_reason: str, symbol: str, today, cal: TradingCalendar,
                  equity: float) -> StockOutcome:
    """One strategy's verdict on one stock, from the shared signals."""
    if sig is None:
        exps = inst.expiries(ctx.engine, today, symbol)
        trade_exp = market.pick_expiry(exps, today, ctx.cfg.dte_min, ctx.cfg.dte_max)
        return _row(ctx, symbol, "data", data_reason, trade_exp=trade_exp,
                    dte=market.dte(trade_exp, today) if trade_exp else None)
    dec = decide_entry(ctx, sig, cal)
    if not dec.ok:
        c = SpreadCandidate(symbol, dec.side or "", 0, 0, dec.trade_exp, dec.dte, 0, flags=dict(dec.flags),
                            inputs=dict(dec.inputs), strategy=ctx.strategy)
        c.reject_reason = f"{dec.stage}: {dec.reason}"
        return StockOutcome(symbol, dec.stage, dec.reason, rows=[c])
    return build_verticals(ctx, sig, dec, equity, cal)


def evaluate_stock(ctx, symbol: str, today, cal: TradingCalendar, equity: float, snapshots: List[dict]) -> StockOutcome:
    """Signals plus this context's strategy for one stock (single-book use)."""
    main = ctx.parent or ctx
    sig, why = compute_signals(main, symbol, today, cal, snapshots)
    return evaluate_book(ctx, sig, why, symbol, today, cal, equity)


def run_scan(ctx, persist: bool = True) -> ScanResult:
    main = ctx.parent or ctx
    books = main.books()
    now = main.clock.now()
    today = market.ensure_instruments(main)
    cal = TradingCalendar.load(main.engine)
    scan_id = now.strftime("%Y%m%d-%H%M%S")
    stocks = inst.fno_stocks(main.engine, today)
    prev = cal.prev_trading_day(today)
    universes = {}
    for b in books:
        u = b.cfg.universe
        universes[b.strategy] = refdata.universe(b.engine, stocks, prev, u.top_n, u.turnover_window, cal,
                                                 u.min_avg_turnover)
    in_any = set().union(*(set(v) for v in universes.values()))
    equity = {b.strategy: current_equity(b) for b in books}
    outcomes: Dict[str, List[StockOutcome]] = {b.strategy: [] for b in books}
    snapshots: List[dict] = []

    def _expiries(sym):                       # every book's trade expiry plus the ATM expiry
        exps = inst.expiries(main.engine, today, sym)
        picks = {market.pick_expiry(exps, today, main.cfg.iv.atm_min_dte)}
        picks |= {market.pick_expiry(exps, today, b.cfg.dte_min, b.cfg.dte_max) for b in books}
        return [e for e in picks if e]

    t_q = _time.time()
    main.cache["scan_quotes"] = market.prefetch_scan_quotes(main, [s for s in stocks if s in in_any], today,
                                                            _expiries, SCAN_BAND)
    log.info("scan quotes: %d instruments in %.1fs", len(main.cache["scan_quotes"]), _time.time() - t_q)
    for sym in stocks:
        sig, why = None, ""
        if sym in in_any:
            try:
                sig, why = compute_signals(main, sym, today, cal, snapshots)
            except Exception as exc:          # one bad stock must not stop the scan
                log.exception("signals failed for %s", sym)
                why = f"error {exc}"
        for b in books:
            if sym not in set(universes[b.strategy]):
                u = b.cfg.universe
                if not universes[b.strategy]:
                    reason = "no option turnover data"
                elif u.top_n:
                    reason = f"not in top {u.top_n} by option turnover"
                else:
                    reason = f"average option turnover below {u.min_avg_turnover:,.0f}"
                outcomes[b.strategy].append(_row(b, sym, "F1 universe", reason, flag_ok=False))
                continue
            try:
                outcomes[b.strategy].append(evaluate_book(b, sig, why, sym, today, cal, equity[b.strategy]))
            except Exception as exc:          # one bad stock must not stop the scan
                log.exception("scan failed for %s (%s)", sym, b.strategy)
                outcomes[b.strategy].append(_row(b, sym, "data", f"error {exc}"))
    try:
        from ..data.market_recorder import record_scan_quotes

        record_scan_quotes(main, main.cache.get("scan_quotes") or {})
    except Exception as exc:                    # recording must never break a scan
        log.warning("could not record scan quotes: %s", exc)
    main.cache.pop("scan_quotes", None)         # never reuse a scan's quotes later
    results = {}
    for b in books:
        ranked = sorted([o.best for o in outcomes[b.strategy] if o.best], key=lambda c: c.ev_ratio, reverse=True)
        results[b.strategy] = BookResult(b.strategy, outcomes[b.strategy], ranked, ranked[: b.cfg.scan.top_n])
    m = results[MAIN]
    result = ScanResult(scan_id, now, m.outcomes, m.ranked, m.selected, books=results)
    if persist:
        _persist(main, result, snapshots)
    log.info("scan %s: %d stocks; accepted %s", scan_id, len(stocks),
             ", ".join(f"{name} {len(r.ranked)}" for name, r in results.items()))
    return result


def _persist(ctx, result: ScanResult, snapshots: List[dict]) -> None:
    from ..data.configlog import record

    rows = []
    for name, book in result.books.items():
        chash = record(ctx.for_strategy(name))
        best_ids = {id(c) for c in book.ranked}
        for o in book.outcomes:
            for c in o.rows:
                rows.append(dict(
                    ts=result.ts, scan_id=result.scan_id, symbol=c.symbol, side=c.side or None,
                    short_k=c.short_k or None, long_k=c.long_k or None, expiry=c.expiry, dte=c.dte,
                    lot_size=c.lot_size or None, credit_nat=c.credit_nat or None, width=c.width or None,
                    max_loss=c.max_loss or None, p_market=c.p_market, p_model=c.p_model, ev=c.ev,
                    ev_ratio=c.ev_ratio if c.ev_ratio is None or math.isfinite(c.ev_ratio) else None,
                    **{f"{k}_pass": v for k, v in c.flags.items()},
                    accepted=id(c) in best_ids, reject_reason=c.reject_reason, config_hash=chash, strategy=name,
                    inputs_json=json.dumps({**c.inputs, "short_sym": c.short_sym, "long_sym": c.long_sym,
                                            "best_of_stock": id(c) in best_ids}, default=str)))
    with ctx.engine.begin() as conn:
        if rows:
            conn.execute(insert(Candidate), rows)
        for s in snapshots:
            stmt = sqlite_insert(IVSnapshot).values(**s)
            conn.execute(stmt.on_conflict_do_update(index_elements=["date", "symbol"], set_={
                k: getattr(stmt.excluded, k) for k in s if k not in ("date", "symbol")}))
