"""Daily scanner (spec: Entry filters, Strike selection and spread construction).

For each F&O stock: F1 universe -> DTE window -> IV snapshot (stored for every universe stock)
-> F2 events -> F3 IV/HV -> F4 IV percentile (true or HV-percentile proxy) -> F5 cause passed
-> direction from levels -> enumerate short strikes (F6 level buffer, F7 distance and delta)
-> long strikes 1..4 steps -> F9 liquidity -> natural-price credit after round-trip costs (F8)
-> sizing within the risk budget -> F9 order-book depth for that size -> EV. Every stock-level rejection and every enumerated spread is saved
to `candidates` with its inputs. Accepted spreads are ranked by EV / max loss.
"""

from __future__ import annotations

import json
import logging
import math
import time as _time
import zlib
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .. import market
from ..analytics import vol
from ..analytics.vol import delta_iv
from ..data import events as ev_data
from ..data import instruments as inst
from ..data import refdata
from ..data.calendar import TradingCalendar
from ..data.daily import load_daily
from ..data.levels_store import StoredLevel, load_levels
from ..data.models import Candidate, IVSnapshot
from ..risk.sizing import current_equity, plan_lots, risk_per_lot
from . import ev as evmod
from .costs import round_trip_costs
from .spreads import BEAR_CALL, BULL_PUT, option_type, spread_metrics

log = logging.getLogger(__name__)

# Funnel stages in evaluation order (stock level). Each stock is counted at its first failure.
STAGES = ["F1 universe", "DTE window", "data", "F2 events", "F3 IV/HV", "F4 IV percentile",
          "F5 cause passed", "direction", "F6 level buffer", "F7 distance/delta", "F9 liquidity",
          "F8 credit quality", "risk budget", "EV", "accepted"]
SCAN_BAND = 0.30           # quote strikes within 30% of the futures price (performance, not a rule)


@dataclass
class SpreadCandidate:
    symbol: str
    side: str
    short_k: float
    long_k: float
    expiry: date
    dte: int
    lot_size: int
    credit_nat: float = 0.0          # per share, after round-trip costs
    credit_gross: float = 0.0
    width: float = 0.0
    max_loss: float = 0.0            # rupees per lot
    risk_per_lot: float = 0.0        # rupees per lot on the sizing basis (loss at the stop by default)
    breakeven: float = 0.0
    p_market: Optional[float] = None
    p_model: Optional[float] = None
    ev: Optional[float] = None
    ev_ratio: Optional[float] = None
    flags: Dict[str, Optional[bool]] = field(default_factory=dict)   # "f1".."f9"
    reject_reason: Optional[str] = None
    inputs: Dict = field(default_factory=dict)
    short_sym: str = ""
    long_sym: str = ""

    @property
    def accepted(self) -> bool:
        return self.reject_reason is None


@dataclass
class StockOutcome:
    symbol: str
    stage: str                       # first failing stage, or "accepted"
    reason: str
    best: Optional[SpreadCandidate] = None
    rows: List[SpreadCandidate] = field(default_factory=list)


@dataclass
class ScanResult:
    scan_id: str
    ts: datetime
    outcomes: List[StockOutcome]
    ranked: List[SpreadCandidate]
    selected: List[SpreadCandidate]
    entry_messages: List[str] = field(default_factory=list)

    @property
    def funnel(self) -> "OrderedDict[str, int]":
        counts = OrderedDict((s, 0) for s in STAGES)
        for o in self.outcomes:
            counts[o.stage] += 1
        return counts


# ------------------------------------------------------------------------------- helpers

def _flags_upto(n: int, last_ok: bool = True) -> Dict[str, Optional[bool]]:
    """f1..f(n-1) True, f(n) = last_ok, rest None."""
    out: Dict[str, Optional[bool]] = {f"f{i}": None for i in range(1, 10)}
    for i in range(1, n):
        out[f"f{i}"] = True
    out[f"f{n}"] = last_ok
    return out


def _q(qt) -> Optional[dict]:
    if qt is None:
        return None
    return {"bid": qt.bid, "ask": qt.ask, "bid_qty": qt.bids[0].quantity if qt.bids else 0,
            "ask_qty": qt.asks[0].quantity if qt.asks else 0, "oi": qt.oi, "volume": qt.volume, "last": qt.last}


def liquidity_ok(ctx, qt, lot: int) -> Tuple[bool, str]:
    """Per leg: a two-sided quote, a tight enough bid-ask, and (only if configured above zero)
    minimum OI and volume."""
    lc = ctx.cfg.liquidity
    if qt is None or qt.bid <= 0 or qt.ask <= 0:
        return False, "no two-sided quote"
    sp = qt.spread_pct
    if sp is None or sp > lc.max_spread_pct:
        return False, f"bid-ask {sp * 100 if sp else 0:.1f}% of mid"
    if lc.min_oi_lots and qt.oi / lot < lc.min_oi_lots:
        return False, f"OI {qt.oi // lot} lots"
    if lc.min_vol_lots and qt.volume / lot < lc.min_vol_lots:
        return False, f"volume {qt.volume // lot} lots"
    return True, ""


def credit_quality(cfg, credit: float, width: float, qs, ql, tick: float) -> Tuple[bool, str]:
    """F8. Friction mode: net credit (after all charges) >= k x the cost of crossing both legs'
    bid-ask to close, and >= a minimum number of ticks. Width mode: the spec's credit/width floor."""
    sc = cfg.spread
    if sc.f8_mode == "width":
        if credit < sc.min_credit_width * width:
            return False, f"{credit / width:.0%} of width < {sc.min_credit_width:.0%}"
        return True, ""
    friction = ((qs.ask - qs.bid) + (ql.ask - ql.bid)) / 2
    need = max(sc.min_credit_friction * friction, sc.min_credit_ticks * (tick or 0.05))
    if credit < need:
        return False, (f"credit {credit:.2f} < {need:.2f} (x{sc.min_credit_friction:g} the {friction:.2f} it costs to "
                       f"cross both bid-asks, min {sc.min_credit_ticks} ticks)")
    return True, ""


def depth_ok(ctx, qs, ql, qty: int) -> Tuple[bool, str]:
    """Absolute liquidity: the 5-level book must hold at least `min_depth_cover` x the order
    size on both bid and ask of both legs (enough to get in and to get out)."""
    need = qty * ctx.cfg.liquidity.min_depth_cover
    for name, q in (("short", qs), ("long", ql)):
        bids, asks = sum(lv.quantity for lv in q.bids[:5]), sum(lv.quantity for lv in q.asks[:5])
        if bids < need or asks < need:
            return False, f"{name} leg depth {bids}/{asks} (bid/ask qty) < {need:g} needed"
    return True, ""


def iv_history(ctx, symbol: str, before: date, n: int) -> List[IVSnapshot]:
    with ctx.engine.connect() as conn:
        rows = conn.execute(select(IVSnapshot.date, IVSnapshot.atm_iv, IVSnapshot.ivp_252, IVSnapshot.method)
                            .where(IVSnapshot.symbol == symbol, IVSnapshot.date < before)
                            .order_by(IVSnapshot.date.desc()).limit(n)).all()
    return list(reversed(rows))


def iv_percentile(ctx, symbol: str, today: date, atm_iv: float, hv20_hist: List[float]) -> Tuple[Optional[float], str, Optional[float]]:
    """(percentile, method, proxy_threshold). True IV percentile once enough snapshots exist,
    else the rank of IV within the stock's rolling HV20 history (proxy)."""
    ic = ctx.cfg.iv
    hist = iv_history(ctx, symbol, today, ic.ivp_history_days)
    if len(hist) >= ic.ivp_min_history:
        return vol.percentile_rank([h.atm_iv for h in hist], atm_iv), "true", None
    hv_hist = hv20_hist[-ic.ivp_history_days:]
    return vol.percentile_rank(hv_hist, atm_iv), "proxy", vol.percentile_value(hv_hist, ic.proxy_hv_pctile)


def f4_pass(ctx, ivp: Optional[float], method: str) -> bool:
    ic = ctx.cfg.iv
    if ivp is None:
        return False
    return ivp >= (ic.min_ivp if method == "true" else ic.proxy_hv_pctile)


def f5_check(ctx, symbol: str, today: date, daily, cal: TradingCalendar) -> Tuple[bool, str, Dict]:
    """IV was below its F4 threshold within the lookback and a cause (price move >= k x ATR, or a
    results/news date) happened on or after that low-IV day and before today."""
    ic = ctx.cfg.iv
    hist = iv_history(ctx, symbol, today, ic.spike_lookback)
    window_days = set(cal.last_n(cal.prev_trading_day(today), ic.spike_lookback))
    hist = [h for h in hist if h.date in window_days]
    if not hist:
        return False, "no IV snapshots in look-back (history still building)", {}
    lows = [h.date for h in hist
            if h.ivp_252 is not None and h.ivp_252 < (ic.min_ivp if h.method == "true" else ic.proxy_hv_pctile)]
    if not lows:
        return False, f"IV not below threshold in last {ic.spike_lookback} sessions", {}
    first_low = min(lows)
    causes = []
    d = daily[daily.index >= first_low]
    prev_close = daily["close"].shift(1)
    prev_atr = daily["atr14"].shift(1)
    for dt_, row in d.iterrows():
        pc, pa = prev_close.get(dt_), prev_atr.get(dt_)
        if pc and pa and not math.isnan(pa) and abs(row["close"] - pc) >= ic.spike_move_atr * pa:
            causes.append(f"{dt_} move {abs(row['close'] - pc) / pa:.1f}xATR")
    for e in ev_data.events_between(ctx.engine, symbol, first_low, today - timedelta(days=1)):
        causes.append(f"{e.event_date} {e.event_type}")
    info = {"low_iv_days": [str(x) for x in lows], "causes": causes}
    if not causes:
        return False, "IV rose without a qualifying price move or event", info
    return True, "", info


def choose_direction(levels: List[StoredLevel], spot: float, atr: float, max_dist_atr: float):
    """Bull put if spot is within max_dist_atr x ATR above a support; bear call if within that
    distance below a resistance; if both, the higher-scored level wins. Returns (side, level)."""
    lim = max_dist_atr * atr
    sup = [lv for lv in levels if 0 <= spot - lv.price <= lim]
    res = [lv for lv in levels if 0 <= lv.price - spot <= lim]
    best_sup = max(sup, key=lambda lv: lv.price) if sup else None      # nearest support
    best_res = min(res, key=lambda lv: lv.price) if res else None      # nearest resistance
    if best_sup and best_res:
        return (BULL_PUT, best_sup) if best_sup.score >= best_res.score else (BEAR_CALL, best_res)
    if best_sup:
        return BULL_PUT, best_sup
    if best_res:
        return BEAR_CALL, best_res
    return None, None


# ------------------------------------------------------------------------------- per stock

def evaluate_stock(ctx, symbol: str, today: date, cal: TradingCalendar, equity: float,
                   snapshots: List[dict]) -> StockOutcome:
    cfg = ctx.cfg
    exps = inst.expiries(ctx.engine, today, symbol)
    trade_exp = market.pick_expiry(exps, today, cfg.dte_min, cfg.dte_max)
    atm_exp = market.pick_expiry(exps, today, cfg.iv.atm_min_dte)
    base = SpreadCandidate(symbol, "", 0, 0, trade_exp, market.dte(trade_exp, today) if trade_exp else None, 0,
                           flags=_flags_upto(1, True))

    def reject(stage: str, reason: str, flag_n: Optional[int] = None, **inputs) -> StockOutcome:
        c = SpreadCandidate(**{**asdict(base), "flags": dict(base.flags), "inputs": {**base.inputs, **inputs}})
        if flag_n:
            c.flags = {**c.flags, f"f{flag_n}": False}
        c.reject_reason = f"{stage}: {reason}"
        return StockOutcome(symbol, stage, reason, rows=[c])

    if atm_exp is None:
        return reject("data", "no expiry for ATM IV")
    daily = load_daily(ctx.engine, symbol, end=cal.prev_trading_day(today))
    if len(daily) < cfg.analytics.hv_window + 2 or daily["atr14"].dropna().empty:
        return reject("data", f"only {len(daily)} daily bars stored")
    atr = float(daily["atr14"].dropna().iloc[-1])
    hv20 = float(daily["hv20"].dropna().iloc[-1]) if daily["hv20"].notna().any() else None
    if not hv20:
        return reject("data", "HV20 unavailable")

    # IV snapshot for every universe stock, whatever happens next.
    qcache = ctx.cache.get("scan_quotes")
    atm_snap = market.snapshot_chain(ctx, symbol, atm_exp, band=SCAN_BAND, quotes=qcache)
    atm_iv = atm_snap.atm_iv
    if atm_iv is None:
        return reject("data", "could not compute ATM IV")
    ivp, method, proxy_thr = iv_percentile(ctx, symbol, today, atm_iv, list(daily["hv20"].dropna()))
    snapshots.append(dict(ts=ctx.clock.now(), date=today, symbol=symbol, expiry=atm_exp,
                          fut_price=atm_snap.fut_price, atm_iv=atm_iv,
                          iv_25d_put=delta_iv(atm_snap.ivs, 0.25, "PE"),
                          iv_25d_call=delta_iv(atm_snap.ivs, 0.25, "CE"), ivp_252=ivp, method=method))
    base.inputs.update(spot=atm_snap.spot, fut_atm=atm_snap.fut_price, atr14=atr, hv20=hv20, atm_iv=atm_iv,
                       atm_expiry=str(atm_exp), ivp=ivp, ivp_method=method, proxy_threshold=proxy_thr)

    if trade_exp is None:
        return reject("DTE window", f"no expiry {cfg.dte_min}-{cfg.dte_max} days out")
    f2 = ev_data.check_f2(ctx, symbol, trade_exp)
    if not f2.passed:
        return reject("F2 events", f2.reason, 2, events=f2.events)
    base.flags["f2"] = True
    ratio = atm_iv / hv20
    base.inputs["iv_hv"] = ratio
    if ratio < cfg.iv.min_iv_hv_ratio:
        return reject("F3 IV/HV", f"IV/HV {ratio:.2f} < {cfg.iv.min_iv_hv_ratio}", 3)
    base.flags["f3"] = True
    if not f4_pass(ctx, ivp, method):
        thr = cfg.iv.min_ivp if method == "true" else cfg.iv.proxy_hv_pctile
        return reject("F4 IV percentile", f"{method} IVP {ivp or 0:.0f} < {thr:g}", 4)
    base.flags["f4"] = True
    ok5, why5, info5 = f5_check(ctx, symbol, today, daily, cal)
    base.inputs.update(f5=info5)
    if not ok5:
        return reject("F5 cause passed", why5, 5)
    base.flags["f5"] = True

    snap = atm_snap if trade_exp == atm_exp else market.snapshot_chain(ctx, symbol, trade_exp, band=SCAN_BAND,
                                                                       quotes=qcache)
    spot, F = snap.spot, snap.fut_price
    levels = load_levels(ctx.engine, symbol, today)
    side, level = choose_direction(levels, spot, atr, cfg.direction.max_dist_atr)
    if side is None:
        return reject("direction", f"no scored level within {cfg.direction.max_dist_atr} x ATR of spot")
    base.side = side
    base.inputs.update(level=level.price, level_score=level.score, level_source=level.source,
                       fut=F, T=snap.T)
    cp = option_type(side)
    chain = snap.chain
    step = chain.strike_step
    lot = chain.lot_size
    base.lot_size = lot
    buf = cfg.levels.min_buffer_atr * atr
    rows: List[SpreadCandidate] = []
    # Path-simulation horizon: from now to the time stop (N trading days before expiry).
    n_days = max(1, cal.trading_days_between(today, trade_exp) - cfg.exit.days_before_expiry)
    stop_day = today
    for _ in range(n_days):
        stop_day = cal.next_trading_day(stop_day)
    T_exit = market.years_to_expiry(trade_exp, datetime.combine(stop_day, market.EXPIRY_TIME))
    any_f6 = any_f7 = any_liq = any_f8 = any_budget = any_depth = False
    strikes = [k for k in chain.strikes if (k < spot if side == BULL_PUT else k > spot)]
    for k in strikes:
        f6 = (k <= level.price - buf) if side == BULL_PUT else (k >= level.price + buf)
        if not f6:
            continue
        any_f6 = True
        oiv = snap.iv(k, cp)
        delta = abs(oiv.delta) if oiv and oiv.delta is not None else None
        dist_ok = abs(k - spot) >= cfg.strikes.min_dist_atr * atr
        delta_ok = delta is not None and cfg.strikes.delta_min <= delta <= cfg.strikes.delta_max
        if not (dist_ok and delta_ok):
            continue
        any_f7 = True
        for w in cfg.spread.widths_steps:
            long_k = round(k - w * step, 2) if side == BULL_PUT else round(k + w * step, 2)
            short_leg, long_leg = chain.leg(k, cp), chain.leg(long_k, cp)
            c = SpreadCandidate(symbol, side, k, long_k, trade_exp, market.dte(trade_exp, today), lot,
                                flags={**base.flags, "f6": True, "f7": True},
                                inputs={**base.inputs, "short_delta": delta, "short_iv": oiv.iv, "width_steps": w})
            rows.append(c)
            if long_leg is None:
                c.reject_reason = "long strike not listed"
                continue
            c.short_sym, c.long_sym = short_leg.tradingsymbol, long_leg.tradingsymbol
            qs, ql = snap.quotes.get(short_leg.key), snap.quotes.get(long_leg.key)
            c.inputs.update(short_quote=_q(qs), long_quote=_q(ql))
            ok_s, why_s = liquidity_ok(ctx, qs, lot)
            ok_l, why_l = liquidity_ok(ctx, ql, lot)
            if not (ok_s and ok_l):
                c.flags["f9"] = False
                c.reject_reason = f"F9 liquidity: {'short ' + why_s if not ok_s else 'long ' + why_l}"
                continue
            any_liq = True
            gross = qs.bid - ql.ask
            c.credit_gross = gross
            if gross <= 0:
                c.flags["f8"] = False
                c.reject_reason = "F8 credit quality: no credit at natural prices"
                continue
            costs = round_trip_costs(cfg.costs, qs.bid, ql.ask, lot, lot=lot)
            m = spread_metrics(side, k, long_k, qs.bid, ql.ask, lot, costs_per_share=costs / lot)
            c.credit_nat, c.width, c.max_loss, c.breakeven = m.credit, m.width, m.max_loss, m.breakeven
            c.inputs.update(round_trip_costs=costs, breakeven_win_rate=m.breakeven_win_rate)
            ok8, why8 = credit_quality(cfg, m.credit, m.width, qs, ql, chain.tick_size)
            c.inputs.update(credit_width=m.credit / m.width, exit_friction=((qs.ask - qs.bid) + (ql.ask - ql.bid)) / 2)
            if not ok8:
                c.flags["f8"] = False
                c.reject_reason = f"F8 credit quality: {why8}"
                continue
            c.flags["f8"] = True
            any_f8 = True
            c.risk_per_lot = risk_per_lot(cfg, m.credit, m.width, lot)
            lots, why_lots = plan_lots(cfg, equity, c.risk_per_lot, m.max_loss)
            c.inputs.update(risk_per_lot=c.risk_per_lot, planned_lots=lots, sizing_basis=cfg.risk.sizing_basis)
            if lots == 0:
                c.reject_reason = f"risk budget: {why_lots}"
                continue
            any_budget = True
            ok_d, why_d = depth_ok(ctx, qs, ql, lots * lot)
            if not ok_d:
                c.flags["f9"] = False
                c.reject_reason = f"F9 liquidity: {why_d}"
                continue
            c.flags["f9"] = True
            any_depth = True
            if cfg.ev.method == "paths":
                liv = snap.iv(long_k, cp)
                r = evmod.evaluate_paths(
                    side, F, k, long_k, oiv.iv, (liv.iv if liv and liv.iv else oiv.iv), hv20, snap.T, T_exit,
                    n_days, gross, m.credit, m.width, lot, (qs.ask - qs.bid) / 2, (ql.ask - ql.bid) / 2,
                    level.price, cfg, seed=cfg.ev.seed ^ zlib.crc32(f"{symbol}|{k}|{long_k}|{today}".encode()))
                c.inputs.update(exit_mix=r.exit_mix, pnl_p05=r.pnl_p05, sim_days=n_days,
                                ev_per_risk=r.ev / c.risk_per_lot if c.risk_per_lot else None)
            else:
                r = evmod.evaluate(side, F, m.breakeven, snap.T, oiv.iv, hv20, m.credit, m.width, lot,
                                   cfg.ev.hv_weight, cfg.exit.stop_mult)
            c.p_market, c.p_model, c.ev, c.ev_ratio = r.p_market, r.p_model, r.ev, r.ev_ratio
            c.inputs.update(sigma_model=r.sigma_model, avg_loss=r.avg_loss, ev_method=cfg.ev.method)
            if not (r.ev > 0 and r.ev_ratio >= cfg.ev.min_ev_ratio):
                c.reject_reason = f"EV: {r.ev_ratio:.1%} of max loss < {cfg.ev.min_ev_ratio:.1%}"
    accepted = [c for c in rows if c.accepted]
    if accepted:
        best = max(accepted, key=lambda c: c.ev_ratio)
        return StockOutcome(symbol, "accepted", "", best, rows)
    if not any_f6:
        out = reject("F6 level buffer", f"no strike {cfg.levels.min_buffer_atr} x ATR beyond level {level.price}", 6)
    elif not any_f7:
        out = reject("F7 distance/delta", "no short strike with delta in band and >= ATR from spot", 7)
    elif not any_liq:
        out = StockOutcome(symbol, "F9 liquidity", "no spread with both legs liquid", rows=rows)
    elif not any_f8:
        out = StockOutcome(symbol, "F8 credit quality", "no spread with enough credit", rows=rows)
    elif not any_budget:
        out = StockOutcome(symbol, "risk budget", "no width fits the per-trade risk budget", rows=rows)
    elif not any_depth:
        out = StockOutcome(symbol, "F9 liquidity", "order book too thin for the planned size", rows=rows)
    else:
        out = StockOutcome(symbol, "EV", "no spread with positive EV above the minimum", rows=rows)
    if out.stage in ("F6 level buffer", "F7 distance/delta"):
        out.rows = out.rows + rows
    return out


# ------------------------------------------------------------------------------- scan

def run_scan(ctx, persist: bool = True) -> ScanResult:
    cfg = ctx.cfg
    now = ctx.clock.now()
    today = market.ensure_instruments(ctx)
    cal = TradingCalendar.load(ctx.engine)
    scan_id = now.strftime("%Y%m%d-%H%M%S")
    stocks = inst.fno_stocks(ctx.engine, today)
    universe = refdata.universe(ctx.engine, stocks, cal.prev_trading_day(today), cfg.universe.top_n,
                                cfg.universe.turnover_window, cal, cfg.universe.min_avg_turnover)
    equity = current_equity(ctx)
    outcomes: List[StockOutcome] = []
    snapshots: List[dict] = []
    in_universe = set(universe)
    # One batched quote pass for the whole scan (see market.prefetch_scan_quotes).
    def _expiries(sym):
        exps = inst.expiries(ctx.engine, today, sym)
        picks = {market.pick_expiry(exps, today, cfg.dte_min, cfg.dte_max),
                 market.pick_expiry(exps, today, cfg.iv.atm_min_dte)}
        return [e for e in picks if e]
    t_q = _time.time()
    ctx.cache["scan_quotes"] = market.prefetch_scan_quotes(ctx, [s for s in stocks if s in in_universe], today,
                                                           _expiries, SCAN_BAND)
    log.info("scan quotes: %d instruments in %.1fs", len(ctx.cache["scan_quotes"]), _time.time() - t_q)
    for sym in stocks:
        if sym not in in_universe:
            c = SpreadCandidate(sym, "", 0, 0, None, None, 0, flags=_flags_upto(1, False))
            if not universe:
                reason = "no option turnover data"
            elif cfg.universe.top_n:
                reason = f"not in top {cfg.universe.top_n} by option turnover"
            else:
                reason = f"average option turnover below {cfg.universe.min_avg_turnover:,.0f}"
            c.reject_reason = f"F1 universe: {reason}"
            outcomes.append(StockOutcome(sym, "F1 universe", reason, rows=[c]))
            continue
        try:
            outcomes.append(evaluate_stock(ctx, sym, today, cal, equity, snapshots))
        except Exception as exc:          # one bad stock must not stop the scan
            log.exception("scan failed for %s", sym)
            c = SpreadCandidate(sym, "", 0, 0, None, None, 0, flags=_flags_upto(1, True))
            c.reject_reason = f"data: error {exc}"
            outcomes.append(StockOutcome(sym, "data", f"error: {exc}", rows=[c]))
    try:
        from ..data.market_recorder import record_scan_quotes

        record_scan_quotes(ctx, ctx.cache.get("scan_quotes") or {})
    except Exception as exc:                    # recording must never break a scan
        log.warning("could not record scan quotes: %s", exc)
    ctx.cache.pop("scan_quotes", None)          # never reuse a scan's quotes later
    ranked = sorted([o.best for o in outcomes if o.best], key=lambda c: c.ev_ratio, reverse=True)
    selected = ranked[: cfg.scan.top_n]
    result = ScanResult(scan_id, now, outcomes, ranked, selected)
    if persist:
        _persist(ctx, result, snapshots)
    log.info("scan %s: %d stocks, %d universe, %d accepted", scan_id, len(stocks), len(universe), len(ranked))
    return result


def _persist(ctx, result: ScanResult, snapshots: List[dict]) -> None:
    from ..data.configlog import record

    chash = record(ctx)
    rows = []
    best_ids = {id(c) for c in result.ranked}
    for o in result.outcomes:
        for c in o.rows:
            rows.append(dict(
                ts=result.ts, scan_id=result.scan_id, symbol=c.symbol, side=c.side or None,
                short_k=c.short_k or None, long_k=c.long_k or None, expiry=c.expiry, dte=c.dte,
                lot_size=c.lot_size or None, credit_nat=c.credit_nat or None, width=c.width or None,
                max_loss=c.max_loss or None, p_market=c.p_market, p_model=c.p_model, ev=c.ev,
                ev_ratio=c.ev_ratio if c.ev_ratio is None or math.isfinite(c.ev_ratio) else None,
                **{f"{k}_pass": v for k, v in c.flags.items()},
                accepted=id(c) in best_ids, reject_reason=c.reject_reason, config_hash=chash,
                inputs_json=json.dumps({**c.inputs, "short_sym": c.short_sym, "long_sym": c.long_sym,
                                        "best_of_stock": id(c) in best_ids}, default=str)))
    with ctx.engine.begin() as conn:
        if rows:
            conn.execute(insert(Candidate), rows)
        for s in snapshots:
            stmt = sqlite_insert(IVSnapshot).values(**s)
            conn.execute(stmt.on_conflict_do_update(index_elements=["date", "symbol"], set_={
                k: getattr(stmt.excluded, k) for k in s if k not in ("date", "symbol")}))
