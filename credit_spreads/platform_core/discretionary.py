"""The owner's discretionary trades (spec: Discretionary journal; Order ticket).

The owner enters stock, direction, strikes, expiry, lots, a stop level, a setup tag (1-4 or none) and a
one-line reason. The trade fills at live natural prices through the shared paper broker, exactly like a
system trade, and the system fills in the rest: fills, charges, margin, return on margin, 1R, spot, ATR,
each leg's IV and OI, and whether the system armed or confirmed a setup on that stock that day (and the
level it used). 1R is the loss at the owner's stop level priced exactly as the system prices its kill
level (Black-76, IV plus the cushion, one day less); with no stop level it is the spread's maximum loss.
The broker rejects trades before go_live_date.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from level_system import analytics as A
from level_system.analytics import BEAR_CALL, BULL_PUT

from .broker import SpreadOrder
from .data_service import Leg
from .models import PTrade, SetupRow

ACCOUNT = "discretionary"
TAGS = ("1", "2", "3", "4", "none")


class TicketError(ValueError):
    """The order ticket is incomplete or inconsistent."""


def chain_view(p, symbol: str, expiry: date) -> dict:
    """Strikes of one expiry with each side's bid, ask, OI and IV, for the order ticket."""
    ch = p.ds.chain(symbol, expiry)
    keys = [leg.key for leg in ch.legs.values()] + ([ch.future.key] if ch.future else []) + [f"NSE:{symbol}"]
    q = p.ds.quotes(keys)
    spot_q = q.get(f"NSE:{symbol}")
    fq = q.get(ch.future.key) if ch.future else None
    F = (fq.last or fq.mid) if fq else (spot_q.last if spot_q else None)
    T = A.years_to_expiry(p.now(), expiry)
    rows = []
    for k in ch.strikes:
        row = {"strike": k}
        for cp in ("CE", "PE"):
            leg = ch.leg(k, cp)
            x = q.get(leg.key) if leg else None
            if x is None:
                continue
            iv = A.leg_iv(x.mid, F, k, T, p.ds.ctx.cfg.analytics.risk_free, cp) if F and x.mid else None
            row[cp] = {"bid": x.bid, "ask": x.ask, "oi": x.oi, "iv": iv}
        rows.append(row)
    return {"symbol": symbol, "expiry": expiry.isoformat(), "spot": spot_q.last if spot_q else None, "future": F,
            "lot_size": ch.lot_size, "tick": ch.tick_size, "rows": rows}


def _setups_today(p, symbol: str, d: date) -> List[dict]:
    with Session(p.pengine) as s:
        rows = s.scalars(select(SetupRow).where(SetupRow.symbol == symbol).where(
            (SetupRow.day0_date == d) | (SetupRow.last_session == d) |
            (SetupRow.state.in_(("armed", "confirming"))))).all()
    return [{"setup_id": r.id, "setup": r.setup, "direction": r.direction, "state": r.state, "level": r.level,
             "day0": r.day0_date.isoformat()} for r in rows]


def _validate(direction: str, short_k: float, long_k: float, lots: int, tag: str) -> str:
    if direction not in (BEAR_CALL, BULL_PUT):
        raise TicketError("direction must be bear_call or bull_put")
    if lots is None or int(lots) < 1:
        raise TicketError("lots must be at least 1")
    if direction == BEAR_CALL and not long_k > short_k:
        raise TicketError("a bear call spread buys a higher strike than it sells")
    if direction == BULL_PUT and not long_k < short_k:
        raise TicketError("a bull put spread buys a lower strike than it sells")
    tag = (tag or "none").strip().lower()
    if tag not in TAGS:
        raise TicketError("setup tag must be 1, 2, 3, 4 or none")
    return tag


def preview(p, *, symbol: str, expiry: date, direction: str, short_k: float, long_k: float, lots: int,
            stop: Optional[float] = None, tag: str = "none") -> dict:
    """Everything the ticket shows before the order: bid and ask of each leg, natural credit, basket margin,
    return on margin, maximum loss and 1R."""
    _validate(direction, short_k, long_k, lots, tag)
    cp = A.own_type(direction)
    ch = p.ds.chain(symbol, expiry)
    sl, ll = ch.leg(short_k, cp), ch.leg(long_k, cp)
    if sl is None or ll is None:
        raise TicketError(f"{symbol} {expiry} has no {short_k:g}{cp} or {long_k:g}{cp}")
    keys = [sl.key, ll.key, f"NSE:{symbol}"] + ([ch.future.key] if ch.future else [])
    q = p.ds.quotes(keys)
    sq, lq, spot_q = q.get(sl.key), q.get(ll.key), q.get(f"NSE:{symbol}")
    if sq is None or lq is None or not sq.bid or not lq.ask:
        raise TicketError("no two-sided quotes for these strikes right now")
    lot, lots = ch.lot_size, int(lots)
    qty = lot * lots
    tick = ch.tick_size or 0.05
    slip = p.cfg.paper.extra_slip_ticks * tick
    natural = sq.bid - lq.ask
    est = (sq.bid - slip) - (lq.ask + slip)
    width = abs(long_k - short_k)
    costs = p.broker.costs
    try:
        margin = p.ds.margin([Leg(sl.tradingsymbol, short_k, cp, "SELL", qty), Leg(ll.tradingsymbol, long_k, cp, "BUY", qty)],
                             {sl.tradingsymbol: sq.bid, ll.tradingsymbol: lq.ask})
        margin_note = ""
    except Exception as exc:
        margin, margin_note = (width - est) * qty, f"basket margin unavailable ({exc}); showing the maximum loss"
    rt = costs.round_trip(sq.bid, lq.ask, qty)
    max_loss = (width - est) * qty
    spot = spot_q.last if spot_q else None
    fq = q.get(ch.future.key) if ch.future else None
    fut = (fq.last or fq.mid) if fq else spot
    one_r, kill = max_loss, None
    if stop is not None and spot:
        kill = A.loss_at_kill(level=float(stop), spot=spot, future=fut, now=p.now(), expiry=expiry, cp=cp,
                              short_k=short_k, long_k=long_k, short_q=sq, long_q=lq, credit=est, lot_size=lot,
                              r=p.ds.ctx.cfg.analytics.risk_free, cushion_points=p.cfg.sizing.iv_cushion_points,
                              exit_charges_per_lot=lambda s, lg: costs.close_spread(s, lg, lot).total)
        if kill is not None and kill.per_lot > 0:
            one_r = kill.per_lot * lots
    bars = p.ds.daily_bars(symbol, end=p.today())
    atr = A.atr_before(bars, p.today(), p.cfg.atr_period) if len(bars) else None
    T = A.years_to_expiry(p.now(), expiry)
    r_free = p.ds.ctx.cfg.analytics.risk_free
    s_iv = kill.short_iv if kill else (A.leg_iv(sq.mid, fut, short_k, T, r_free, cp) if fut else None)
    l_iv = kill.long_iv if kill else (A.leg_iv(lq.mid, fut, long_k, T, r_free, cp) if fut else None)
    return {
        "symbol": symbol, "expiry": expiry.isoformat(), "direction": direction, "cp": cp, "short": short_k, "long": long_k,
        "lots": lots, "lot_size": lot, "qty": qty, "spot": spot, "future": fut, "atr": atr,
        "short_leg": {"symbol": sl.tradingsymbol, "bid": sq.bid, "ask": sq.ask, "oi": sq.oi},
        "long_leg": {"symbol": ll.tradingsymbol, "bid": lq.bid, "ask": lq.ask, "oi": lq.oi},
        "credit_natural": round(natural, 2), "credit_expected": round(est, 2), "credit_total": round(est * qty, 2),
        "margin": round(margin, 2), "margin_note": margin_note, "round_trip_charges": round(rt, 2),
        "rom": (natural * qty - rt) / margin if margin > 0 else None, "max_loss": round(max_loss, 2),
        "one_r": round(one_r, 2), "one_r_basis": "stop level" if kill is not None and kill.per_lot > 0 else "maximum loss",
        "loss_at_stop_lot": round(kill.per_lot, 2) if kill else None, "stop": stop,
        "short_iv": s_iv, "long_iv": l_iv,
        "breakeven": short_k + est if direction == BEAR_CALL else short_k - est,
        "system_setups": _setups_today(p, symbol, p.today()), "live": p.cfg.is_live(p.today()),
    }


def place(p, *, symbol: str, expiry: date, direction: str, short_k: float, long_k: float, lots: int,
          stop: Optional[float] = None, tag: str = "none", reason: str = "") -> dict:
    tag = _validate(direction, short_k, long_k, lots, tag)
    pv = preview(p, symbol=symbol, expiry=expiry, direction=direction, short_k=short_k, long_k=long_k, lots=lots,
                 stop=stop, tag=tag)
    ch = p.ds.chain(symbol, expiry)
    order = SpreadOrder(
        account=ACCOUNT, symbol=symbol, direction=direction, expiry=expiry, cp=pv["cp"],
        short_symbol=pv["short_leg"]["symbol"], short_strike=short_k, long_symbol=pv["long_leg"]["symbol"],
        long_strike=long_k, lots=int(lots), lot_size=ch.lot_size, tick=ch.tick_size or 0.05, source="discretionary",
        setup_tag=tag, sector=p.ds.sector_of(symbol), level=stop, owner_stop=stop,
        loss_at_kill=pv["loss_at_stop_lot"], one_r=pv["one_r"], book="discretionary", reason=(reason or "")[:300],
        context={"entry": {k: pv[k] for k in ("spot", "future", "atr", "short_iv", "long_iv", "credit_natural",
                                               "credit_expected", "margin", "rom", "one_r_basis")},
                 "oi": {"short": pv["short_leg"]["oi"], "long": pv["long_leg"]["oi"]},
                 "system_setups": pv["system_setups"]})
    res = p.broker.open_spread(order)
    out = {"ok": res.ok, "reason": res.reason, "trade_id": res.trade_id, "preview": pv}
    if res.ok:
        out.update(credit=res.credit, charges=res.charges, margin=res.margin, rom=res.rom)
        p.notifier.send(f"[discretionary] ENTRY {symbol} {direction.replace('_', ' ')} {short_k:g}/{long_k:g} {expiry} "
                        f"x{lots}: credit {res.credit:.2f}, 1R {pv['one_r']:,.0f} ({pv['one_r_basis']})", "entry")
    else:
        p.notifier.send(f"[discretionary] REJECTED {symbol}: {res.reason}", "rejection")
    return out


def close(p, trade_id: str) -> dict:
    t = p.broker.get_trade(trade_id)
    if t is None or t.account != ACCOUNT:
        raise TicketError("only discretionary positions can be closed from the ticket")
    if t.status != "open":
        raise TicketError("this position is already closed")
    spot_q = p.ds.spot_quotes([t.symbol]).get(t.symbol)
    res = p.broker.close_spread(trade_id, "owner_close")
    if res.ok:
        ctx = json.loads(t.context_json or "{}")
        ctx["exit"] = {"spot": spot_q.last if spot_q else None, "ts": p.now().isoformat(timespec="seconds")}
        p.broker.update_trade(trade_id, context_json=json.dumps(ctx, default=str))
        t2 = p.broker.get_trade(trade_id)
        p.notifier.send(f"[discretionary] EXIT {t.symbol}: P&L {t2.pnl_inr:,.0f} ({(t2.pnl_r or 0):+.2f}R)", "exit")
        return {"ok": True, "pnl": t2.pnl_inr, "pnl_r": t2.pnl_r, "holding_days": t2.holding_days}
    return {"ok": False, "reason": res.reason}


def edit(p, trade_id: str, *, tag: Optional[str] = None, reason: Optional[str] = None,
         stop: Optional[float] = None, clear_stop: bool = False) -> dict:
    """The journal's editable fields. A new stop level re-prices 1R with the values saved at entry
    (spot, implied volatilities, time to expiry), so 1R stays the estimate made at entry."""
    t = p.broker.get_trade(trade_id)
    if t is None or t.account != ACCOUNT:
        raise TicketError("only discretionary trades are edited in the journal")
    fields: Dict[str, object] = {}
    if tag is not None:
        tag = tag.strip().lower()
        if tag not in TAGS:
            raise TicketError("setup tag must be 1, 2, 3, 4 or none")
        fields["setup_tag"] = tag
    if reason is not None:
        fields["reason"] = reason[:300]
    if clear_stop or stop is not None:
        new_stop = None if clear_stop else float(stop)
        fields["owner_stop"] = new_stop
        fields["one_r"] = one_r_at_entry(p, t, new_stop)
        fields["loss_at_kill"] = None if new_stop is None else round(fields["one_r"] / t.lots, 2)
    if fields:
        p.broker.update_trade(trade_id, **fields)
    return {"ok": True, **{k: v for k, v in fields.items()}}


def one_r_at_entry(p, t: PTrade, stop: Optional[float]) -> float:
    """1R for `stop` priced with the trade's entry values; the maximum loss when there is no stop."""
    if stop is None:
        return round(t.max_loss, 2)
    ctx = json.loads(t.context_json or "{}").get("entry") or {}
    legs = json.loads(t.legs_json)
    short = next(lg for lg in legs if lg["side"] == "SELL")
    long_ = next(lg for lg in legs if lg["side"] == "BUY")
    spot, s_iv, l_iv = ctx.get("spot"), ctx.get("short_iv"), ctx.get("long_iv")
    if not (spot and s_iv and l_iv):
        return round(t.max_loss, 2)
    from app.analytics import black76

    r = p.ds.ctx.cfg.analytics.risk_free
    T = max(A.years_to_expiry(t.entry_ts, t.expiry) - 1 / 365.0, 1e-6)
    F = float(stop) * (ctx.get("future") or spot) / spot          # the entry future/spot basis, moved to the stop
    bump = p.cfg.sizing.iv_cushion_points / 100.0
    cp = short["cp"]
    s_px = black76.price(F, short["strike"], T, s_iv + bump, r, cp)
    l_px = black76.price(F, long_["strike"], T, l_iv + bump, r, cp)
    per_share = s_px - l_px - t.credit
    charges = p.broker.costs.close_spread(s_px, l_px, short["qty"]).total
    loss = per_share * short["qty"] + charges
    return round(loss, 2) if loss > 0 else round(t.max_loss, 2)
