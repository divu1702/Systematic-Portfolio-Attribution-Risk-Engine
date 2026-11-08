"""Structure layer: turn one strategy's entry decision into concrete vertical spreads.

Short strikes on the chosen side: beyond the anchoring level by levels.min_buffer_atr (F6, for
level-based direction only), at least strikes.min_dist_atr x ATR from spot and with delta in the band
(F7). The long leg is spread.widths_steps strikes further out, or the listed strike nearest each
spread.wing_pct x spot further out. Then both legs' quotes (F9 liquidity), the natural-price credit
after round-trip costs (F8 credit quality), sizing within the book's risk budget, order-book depth for
that size (F9) and EV under the strategy's own exit rules. Every enumerated spread is returned with its
inputs; accepted ones are ranked by EV / max loss.
"""

from __future__ import annotations

import zlib
from datetime import datetime
from typing import List, Tuple

from .. import market
from ..data.calendar import TradingCalendar
from ..risk.sizing import plan_lots, risk_per_lot
from . import ev as evmod
from .candidates import SpreadCandidate, StockOutcome, quote_dict
from .costs import round_trip_costs
from .rules import EntryDecision
from .signals import StockSignals
from .spreads import BULL_PUT, option_type, spread_metrics


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


def wing_strikes(cfg, chain, short_k: float, side: str, spot: float) -> List[Tuple[float, dict]]:
    """(long strike, inputs) for each wing the strategy tries. Steps: may be unlisted (rejected
    later); %: the nearest listed strike, if within wing_tolerance of the target distance."""
    sc = cfg.spread
    step = chain.strike_step
    out: List[Tuple[float, dict]] = []
    if sc.wing == "steps":
        for w in sc.widths_steps:
            out.append((round(short_k - w * step, 2) if side == BULL_PUT else round(short_k + w * step, 2),
                        {"width_steps": w}))
        return out
    beyond = [k for k in chain.strikes if (k < short_k if side == BULL_PUT else k > short_k)]
    seen = set()
    for pct in sc.wing_pct:
        dist = pct * spot
        target = short_k - dist if side == BULL_PUT else short_k + dist
        if not beyond:
            break
        best = min(beyond, key=lambda k: abs(k - target))
        if abs(best - target) <= sc.wing_tolerance * dist and best not in seen:
            seen.add(best)
            out.append((best, {"wing_pct": pct, "width_steps": round(abs(best - short_k) / step, 2)}))
    return out


def build_verticals(ctx, sig: StockSignals, dec: EntryDecision, equity: float, cal: TradingCalendar) -> StockOutcome:
    cfg, today, symbol = ctx.cfg, sig.today, sig.symbol
    snap = sig.snapshot(dec.trade_exp)
    spot, F = snap.spot, snap.fut_price
    side, cp = dec.side, option_type(dec.side)
    chain = snap.chain
    lot = chain.lot_size
    base_flags = dict(dec.flags)
    base_inputs = {**dec.inputs, "fut": F, "T": snap.T}

    def reject(stage: str, reason: str, flag_n=None) -> StockOutcome:
        c = SpreadCandidate(symbol, side, 0, 0, dec.trade_exp, dec.dte, lot, flags=dict(base_flags),
                            inputs=dict(base_inputs), strategy=ctx.strategy)
        if flag_n:
            c.flags[f"f{flag_n}"] = False
        c.reject_reason = f"{stage}: {reason}"
        return StockOutcome(symbol, stage, reason, rows=[c])

    use_f6 = dec.anchor_kind == "level"
    buf = cfg.levels.min_buffer_atr * sig.atr
    rows: List[SpreadCandidate] = []
    # Path-simulation horizon: from now to the time stop (N trading days before expiry).
    n_days = max(1, cal.trading_days_between(today, dec.trade_exp) - cfg.exit.days_before_expiry)
    stop_day = today
    for _ in range(n_days):
        stop_day = cal.next_trading_day(stop_day)
    T_exit = market.years_to_expiry(dec.trade_exp, datetime.combine(stop_day, market.EXPIRY_TIME))
    any_f6 = not use_f6
    any_f7 = any_liq = any_f8 = any_budget = any_depth = False
    for k in [k for k in chain.strikes if (k < spot if side == BULL_PUT else k > spot)]:
        if use_f6:
            if not ((k <= dec.anchor - buf) if side == BULL_PUT else (k >= dec.anchor + buf)):
                continue
            any_f6 = True
        oiv = snap.iv(k, cp)
        delta = abs(oiv.delta) if oiv and oiv.delta is not None else None
        dist_ok = abs(k - spot) >= cfg.strikes.min_dist_atr * sig.atr
        delta_ok = delta is not None and cfg.strikes.delta_min <= delta <= cfg.strikes.delta_max
        if not (dist_ok and delta_ok):
            continue
        any_f7 = True
        for long_k, wing_inputs in wing_strikes(cfg, chain, k, side, spot):
            short_leg, long_leg = chain.leg(k, cp), chain.leg(long_k, cp)
            c = SpreadCandidate(symbol, side, k, long_k, dec.trade_exp, dec.dte, lot,
                                flags={**base_flags, "f6": True if use_f6 else None, "f7": True},
                                inputs={**base_inputs, "short_delta": delta, "short_iv": oiv.iv, **wing_inputs},
                                strategy=ctx.strategy)
            rows.append(c)
            if long_leg is None:
                c.reject_reason = "long strike not listed"
                continue
            c.short_sym, c.long_sym = short_leg.tradingsymbol, long_leg.tradingsymbol
            qs, ql = snap.quotes.get(short_leg.key), snap.quotes.get(long_leg.key)
            c.inputs.update(short_quote=quote_dict(qs), long_quote=quote_dict(ql))
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
                    side, F, k, long_k, oiv.iv, (liv.iv if liv and liv.iv else oiv.iv), sig.hv20, snap.T, T_exit,
                    n_days, gross, m.credit, m.width, lot, (qs.ask - qs.bid) / 2, (ql.ask - ql.bid) / 2,
                    dec.anchor, cfg, seed=cfg.ev.seed ^ zlib.crc32(f"{symbol}|{k}|{long_k}|{today}".encode()))
                c.inputs.update(exit_mix=r.exit_mix, pnl_p05=r.pnl_p05, sim_days=n_days,
                                ev_per_risk=r.ev / c.risk_per_lot if c.risk_per_lot else None)
            else:
                r = evmod.evaluate(side, F, m.breakeven, snap.T, oiv.iv, sig.hv20, m.credit, m.width, lot,
                                   cfg.ev.hv_weight, cfg.exit.stop_mult)
            c.p_market, c.p_model, c.ev, c.ev_ratio = r.p_market, r.p_model, r.ev, r.ev_ratio
            c.inputs.update(sigma_model=r.sigma_model, avg_loss=r.avg_loss, ev_method=cfg.ev.method)
            if not (r.ev > 0 and r.ev_ratio >= cfg.ev.min_ev_ratio):
                c.reject_reason = f"EV: {r.ev_ratio:.1%} of max loss < {cfg.ev.min_ev_ratio:.1%}"
    accepted = [c for c in rows if c.accepted]
    if accepted:
        return StockOutcome(symbol, "accepted", "", max(accepted, key=lambda c: c.ev_ratio), rows)
    if not any_f6:
        out = reject("F6 level buffer", f"no strike {cfg.levels.min_buffer_atr} x ATR beyond level {dec.anchor}", 6)
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
