"""The shared paper broker: every account on the platform trades through it (spec: Paper broker).

1. Fills at natural prices from live quotes plus one tick per leg, walking the 5 depth levels
   (fills.py), and applies the full Zerodha cost stack (costs.py). Entry buys the hedge first, then sells
   the short; exit buys back the short first, then sells the long. If the short cannot be bought back the
   position stays open (never a naked short) and the next check retries.
2. Blocks margin from Zerodha's basket-margin API for the exact legs held, recomputed daily. From the
   Wednesday before expiry it adds the physical-delivery margin step-up (`settlement` in platform.yaml).
3. Rejects an order when the account lacks available margin (cash balance - used margin) or has not
   started yet (go_live_date), and logs the rejection, as a real broker would.
4. Keeps a ledger per account: opening capital, premium in and out, charges, settlement, realised P&L,
   margin blocks and releases, each with the running cash balance and used margin.
Positions still open at expiry are settled at intrinsic value against the stock's official close, with
STT on exercise and Zerodha's physical-delivery brokerage.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from .costs import CostModel
from .data_service import Leg
from .fills import Fill, simulate_fill
from .models import Account, AccountDay, LedgerEntry, PMark, POrder, PTrade

log = logging.getLogger(__name__)

STRATEGIES = {"oi_tight": "oi_system", "oi_loose": "oi_system", "level_strict": "level_strict",
              "level_research": "level_research", "discretionary": "discretionary"}


@dataclass
class SpreadOrder:
    account: str
    symbol: str
    direction: str                     # bear_call | bull_put
    expiry: date
    cp: str                            # CE | PE
    short_symbol: str
    short_strike: float
    long_symbol: str
    long_strike: float
    lots: int
    lot_size: int
    tick: float = 0.05
    source: str = "system"             # system | discretionary | oi_system
    trade_id: Optional[str] = None
    setup_tag: Optional[str] = None
    setup_id: Optional[int] = None
    sector: Optional[str] = None
    level: Optional[float] = None
    loss_at_kill: Optional[float] = None     # per lot
    one_r: Optional[float] = None            # rupees
    book: Optional[str] = None
    failed_filters: Optional[dict] = None
    nifty_trend: Optional[str] = None
    owner_stop: Optional[float] = None
    reason: Optional[str] = None
    context: dict = field(default_factory=dict)

    @property
    def qty(self) -> int:
        return self.lots * self.lot_size

    def legs(self) -> List[Leg]:
        return [Leg(self.short_symbol, self.short_strike, self.cp, "SELL", self.qty),
                Leg(self.long_symbol, self.long_strike, self.cp, "BUY", self.qty)]


@dataclass
class OrderResult:
    ok: bool
    reason: str = ""
    trade_id: Optional[str] = None
    short_fill: Optional[Fill] = None
    long_fill: Optional[Fill] = None
    credit: float = 0.0                # per share
    charges: float = 0.0               # rupees, this order
    margin: float = 0.0
    margin_fallback: bool = False
    rom: Optional[float] = None
    exit_cost: Optional[float] = None
    pnl: Optional[float] = None


@dataclass
class AccountState:
    account: str
    started: bool
    cash_balance: float
    used_margin: float

    @property
    def available(self) -> float:
        return self.cash_balance - self.used_margin


def _legs_of(t: PTrade) -> Tuple[dict, dict]:
    legs = json.loads(t.legs_json)
    return next(lg for lg in legs if lg["side"] == "SELL"), next(lg for lg in legs if lg["side"] == "BUY")


class PaperBroker:
    def __init__(self, pengine, cfg, ds, notifier=None):
        self.pengine, self.cfg, self.ds, self.notifier = pengine, cfg, ds, notifier
        self.costs = CostModel(cfg.costs, cfg.settlement)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ accounts and ledger
    def ensure_accounts(self) -> None:
        """Create the five accounts; open each one (ledger entry with its capital) once go_live_date arrives."""
        today = self.ds.today()
        with self._lock, Session(self.pengine) as s:
            for name in self.cfg.accounts:
                acc = s.get(Account, name)
                opened = s.scalar(select(func.count()).select_from(LedgerEntry).where(
                    LedgerEntry.account == name, LedgerEntry.entry_type == "opening"))
                if acc is None:
                    s.add(Account(id=name, strategy=STRATEGIES[name], opening_capital=self.cfg.capital,
                                  start_date=self.cfg.go_live_date))
                elif not opened:
                    acc.start_date, acc.opening_capital = self.cfg.go_live_date, self.cfg.capital
                s.flush()
                if not opened and self.cfg.is_live(today):
                    ts = datetime.combine(self.cfg.go_live_date, time(9, 0))
                    s.add(LedgerEntry(account=name, ts=ts, entry_type="opening", amount=self.cfg.capital,
                                      cash_balance=self.cfg.capital, used_margin=0.0, note="go_live_date"))
            s.commit()

    def state(self, account: str) -> AccountState:
        with Session(self.pengine) as s:
            last = s.scalars(select(LedgerEntry).where(LedgerEntry.account == account)
                             .order_by(LedgerEntry.id.desc()).limit(1)).first()
        if last is None:
            return AccountState(account, False, 0.0, 0.0)
        return AccountState(account, True, last.cash_balance, last.used_margin)

    def _post(self, s: Session, account: str, ts: datetime, kind: str, amount: float, trade_id: Optional[str] = None,
              note: Optional[str] = None) -> None:
        last = s.scalars(select(LedgerEntry).where(LedgerEntry.account == account)
                         .order_by(LedgerEntry.id.desc()).limit(1)).first()
        cash, used = (last.cash_balance, last.used_margin) if last else (0.0, 0.0)
        if kind in ("premium", "charges", "settlement", "opening"):
            cash += amount
        elif kind in ("margin_block", "margin_release", "margin_adjust"):
            used = max(0.0, used + amount)
        s.add(LedgerEntry(account=account, ts=ts, entry_type=kind, amount=round(amount, 2),
                          cash_balance=round(cash, 2), used_margin=round(used, 2), trade_id=trade_id, note=note))
        s.flush()

    def _order(self, s: Session, account: str, trade_id: Optional[str], purpose: str, legs: List[dict], ok: bool,
               reason: str, ts: datetime) -> None:
        s.add(POrder(id=f"O-{uuid.uuid4().hex[:12]}", account=account, trade_id=trade_id, purpose=purpose,
                     legs_json=json.dumps(legs), status="filled" if ok else "rejected",
                     rejection_reason=None if ok else reason, ts=ts))

    def _reject(self, order: SpreadOrder, reason: str, legs: List[dict]) -> OrderResult:
        with Session(self.pengine) as s:
            self._order(s, order.account, None, "entry", legs, False, reason, self.ds.now())
            s.commit()
        log.info("rejected %s %s: %s", order.account, order.symbol, reason)
        return OrderResult(False, reason)

    # ------------------------------------------------------------------ entries
    def open_spread(self, order: SpreadOrder, quotes=None) -> OrderResult:
        with self._lock:
            now, qty = self.ds.now(), order.qty
            st = self.state(order.account)
            leg_info = [{"symbol": order.short_symbol, "side": "SELL", "qty": qty},
                        {"symbol": order.long_symbol, "side": "BUY", "qty": qty}]
            if not st.started:
                when = self.cfg.go_live_date or "not set"
                return self._reject(order, f"account {order.account} has not started (go_live_date {when})", leg_info)
            if order.lots < 1:
                return self._reject(order, "zero lots", leg_info)
            quotes = quotes if quotes is not None else self.ds.quotes([f"NFO:{order.short_symbol}", f"NFO:{order.long_symbol}"])
            slip = self.cfg.paper.extra_slip_ticks
            lf = simulate_fill(quotes.get(f"NFO:{order.long_symbol}"), "BUY", qty, order.tick, slip)
            sf = simulate_fill(quotes.get(f"NFO:{order.short_symbol}"), "SELL", qty, order.tick, slip)
            leg_info = [{"symbol": order.short_symbol, "side": "SELL", "qty": qty, "natural": sf.natural,
                         "filled": sf.price if sf.ok else None, "levels": sf.levels, "note": sf.reason},
                        {"symbol": order.long_symbol, "side": "BUY", "qty": qty, "natural": lf.natural,
                         "filled": lf.price if lf.ok else None, "levels": lf.levels, "note": lf.reason}]
            if not (lf.ok and sf.ok):
                return self._reject(order, f"no fill: long {lf.reason or 'ok'}, short {sf.reason or 'ok'}", leg_info)
            credit = sf.price - lf.price
            if credit <= 0:
                return self._reject(order, "no credit after fills", leg_info)
            charges = self.costs.open_spread(sf.price, lf.price, qty)
            width = abs(order.long_strike - order.short_strike)
            max_loss = (width - credit) * qty
            fallback = False
            try:
                margin = self.ds.margin(order.legs(), {order.short_symbol: sf.price, order.long_symbol: lf.price})
            except Exception as exc:
                log.warning("basket margin failed for %s (%s): blocking the full maximum loss", order.symbol, exc)
                margin, fallback = max_loss, True
            if margin + charges.total > st.available + 1e-6:
                return self._reject(order, f"insufficient margin: needs {margin + charges.total:,.0f} including charges, "
                                           f"available {st.available:,.0f}", leg_info)
            rom = (credit * qty - self.costs.round_trip(sf.price, lf.price, qty)) / margin if margin > 0 else None
            tid = order.trade_id or f"{order.account}-{now:%Y%m%d%H%M%S}-{order.symbol}-{uuid.uuid4().hex[:4]}"
            legs = [{"side": "SELL", "tradingsymbol": order.short_symbol, "strike": order.short_strike, "cp": order.cp,
                     "qty": qty, "fill": sf.price, "natural": sf.natural},
                    {"side": "BUY", "tradingsymbol": order.long_symbol, "strike": order.long_strike, "cp": order.cp,
                     "qty": qty, "fill": lf.price, "natural": lf.natural}]
            with Session(self.pengine) as s:
                s.add(PTrade(
                    id=tid, account=order.account, source=order.source, status="open", symbol=order.symbol,
                    sector=order.sector, setup_tag=order.setup_tag, setup_id=order.setup_id, direction=order.direction,
                    legs_json=json.dumps(legs), expiry=order.expiry, lots=order.lots, lot_size=order.lot_size,
                    credit=round(credit, 4), entry_charges=round(charges.total, 2), margin=round(margin, 2),
                    rom=rom, level=order.level, loss_at_kill=order.loss_at_kill,
                    one_r=order.one_r if order.one_r else round(max_loss, 2), max_loss=round(max_loss, 2),
                    entry_ts=now, book=order.book,
                    failed_filters=json.dumps(order.failed_filters) if order.failed_filters else None,
                    nifty_trend=order.nifty_trend, owner_stop=order.owner_stop, reason=order.reason,
                    context_json=json.dumps(order.context, default=str) if order.context else None))
                s.flush()
                self._order(s, order.account, tid, "entry", leg_info, True, "", now)
                self._post(s, order.account, now, "premium", credit * qty, tid, "credit received")
                self._post(s, order.account, now, "charges", -charges.total, tid, "entry charges")
                self._post(s, order.account, now, "margin_block", margin, tid,
                           "margin (fallback: maximum loss)" if fallback else "basket margin")
                s.commit()
            return OrderResult(True, "", tid, sf, lf, credit, charges.total, margin, fallback, rom)

    # ------------------------------------------------------------------ exits
    def close_spread(self, trade_id: str, reason: str, quotes=None, prices: Optional[Tuple[float, float]] = None,
                     ts: Optional[datetime] = None, tick: float = 0.05) -> OrderResult:
        """Close at natural prices: buy back the short first, then sell the long. `prices` (short, long)
        replaces book fills when an exit is reconstructed from candles."""
        with self._lock:
            ts = ts or self.ds.now()
            with Session(self.pengine) as s:
                t = s.get(PTrade, trade_id)
                if t is None or t.status != "open":
                    return OrderResult(False, f"no open trade {trade_id}")
                short, long_ = _legs_of(t)
                qty, slip = short["qty"], self.cfg.paper.extra_slip_ticks
                if prices:
                    sp, lp = prices
                    sf = Fill(True, qty, sp, sp, round(sp + slip * tick, 4), 0)
                    lf = Fill(True, qty, lp, lp, round(max(lp - slip * tick, 0.0), 4), 0)
                else:
                    quotes = quotes if quotes is not None else self.ds.quotes(
                        [f"NFO:{short['tradingsymbol']}", f"NFO:{long_['tradingsymbol']}"])
                    sf = simulate_fill(quotes.get(f"NFO:{short['tradingsymbol']}"), "BUY", qty, tick, slip)
                    if not sf.ok:
                        self._order(s, t.account, t.id, "exit", [{"symbol": short["tradingsymbol"], "side": "BUY",
                                                                   "qty": qty, "note": sf.reason}], False,
                                    f"short-leg close failed ({sf.reason}); long kept, will retry", ts)
                        s.commit()
                        return OrderResult(False, f"short-leg close failed ({sf.reason}); long kept, will retry")
                    lf = simulate_fill(quotes.get(f"NFO:{long_['tradingsymbol']}"), "SELL", qty, tick, slip)
                    if not lf.ok:                 # the short is closed: a bought option with no bid is worth 0
                        lf = Fill(True, qty, 0.0, 0.0, 0.0, 0, "no bid: long valued at 0")
                exit_cost = sf.price - lf.price
                charges = self.costs.close_spread(sf.price, lf.price, qty)
                pnl = (t.credit - exit_cost) * qty - t.entry_charges - charges.total
                self._order(s, t.account, t.id, "exit", [
                    {"symbol": short["tradingsymbol"], "side": "BUY", "qty": qty, "natural": sf.natural, "filled": sf.price},
                    {"symbol": long_["tradingsymbol"], "side": "SELL", "qty": qty, "natural": lf.natural,
                     "filled": lf.price, "note": lf.reason}], True, "", ts)
                self._post(s, t.account, ts, "premium", -exit_cost * qty, t.id, "paid to close")
                self._post(s, t.account, ts, "charges", -charges.total, t.id, "exit charges")
                self._post(s, t.account, ts, "margin_release", -(t.margin or 0.0), t.id, "margin released")
                self._post(s, t.account, ts, "realised_pnl", pnl, t.id, reason)
                self._finish(t, ts, reason, exit_cost, charges.total, pnl)
                s.commit()
                return OrderResult(True, reason, t.id, sf, lf, t.credit, charges.total, 0.0, exit_cost=exit_cost, pnl=pnl)

    @staticmethod
    def _finish(t: PTrade, ts: datetime, reason: str, exit_cost: float, charges: float, pnl: float) -> None:
        t.status, t.exit_ts, t.exit_reason = "closed", ts, reason
        t.exit_cost, t.exit_charges, t.pnl_inr = round(exit_cost, 4), round(charges, 2), round(pnl, 2)
        t.pnl_r = round(pnl / t.one_r, 4) if t.one_r else None
        t.holding_days = (ts.date() - t.entry_ts.date()).days

    # ------------------------------------------------------------------ trade rows
    EDITABLE = {"one_r", "loss_at_kill", "setup_tag", "reason", "owner_stop", "context_json", "failed_filters"}

    def get_trade(self, trade_id: str) -> Optional[PTrade]:
        with Session(self.pengine) as s:
            return s.get(PTrade, trade_id)

    def update_trade(self, trade_id: str, **fields) -> None:
        """Change descriptive fields of a trade (1R once the fill is known, the journal's tag, reason and
        stop). Money fields are never edited: they come only from fills, charges and settlement."""
        bad = set(fields) - self.EDITABLE
        if bad:
            raise ValueError(f"not editable: {', '.join(sorted(bad))}")
        with self._lock, Session(self.pengine) as s:
            t = s.get(PTrade, trade_id)
            if t is None:
                raise KeyError(trade_id)
            for k, v in fields.items():
                setattr(t, k, v)
            if "one_r" in fields and t.status == "closed" and t.pnl_inr is not None:
                t.pnl_r = round(t.pnl_inr / t.one_r, 4) if t.one_r else None
            s.commit()

    # ------------------------------------------------------------------ margin, marks, settlement
    def open_trades(self, account: Optional[str] = None) -> List[PTrade]:
        q = select(PTrade).where(PTrade.status == "open")
        if account:
            q = q.where(PTrade.account == account)
        with Session(self.pengine) as s:
            return list(s.scalars(q.order_by(PTrade.entry_ts)))

    def in_delivery_window(self, t: PTrade, d: date) -> bool:
        return self.ds.calendar().trading_days_between(d, t.expiry) <= len(self.cfg.settlement.delivery_margin_steps)

    def delivery_addon(self, t: PTrade, d: date, spot: Optional[float]) -> float:
        """Physical-delivery margin from the Wednesday before expiry: a share of the full delivery margin
        (settlement.delivery_margin_steps, by sessions left) on the net in-the-money delivery value."""
        if spot is None:
            return 0.0
        k = self.ds.calendar().trading_days_between(d, t.expiry)
        steps = self.cfg.settlement.delivery_margin_steps
        if k > len(steps):
            return 0.0
        share = 1.0 if k <= 0 else steps[len(steps) - k]
        net = 0
        for lg in json.loads(t.legs_json):
            itm = spot > lg["strike"] if lg["cp"] == "CE" else spot < lg["strike"]
            if not itm:
                continue
            sign = (-1 if lg["side"] == "SELL" else 1) * (1 if lg["cp"] == "CE" else -1)
            net += sign * lg["qty"]
        return round(share * self.cfg.settlement.delivery_margin_full_pct * abs(net) * spot, 2)

    def recompute_margins(self, d: Optional[date] = None) -> List[str]:
        """Re-price every open position's margin with the basket-margin API (plus any delivery step-up)."""
        d = d or self.ds.today()
        msgs = []
        trades = self.open_trades()
        try:
            spots = {k: (q.last or q.mid) for k, q in self.ds.spot_quotes({t.symbol for t in trades}).items()} if trades else {}
        except Exception as exc:                      # no quotes: keep the base margin, try again tomorrow
            log.warning("spot quotes for the margin recompute failed: %s", exc)
            spots = {}
        with self._lock, Session(self.pengine) as s:
            for t0 in trades:
                t = s.get(PTrade, t0.id)
                short, long_ = _legs_of(t)
                legs = [Leg(lg["tradingsymbol"], lg["strike"], lg["cp"], lg["side"], lg["qty"]) for lg in (short, long_)]
                try:
                    base = self.ds.margin(legs)
                except Exception as exc:
                    log.warning("margin recompute failed for %s: %s", t.id, exc)
                    continue
                spot = spots.get(t.symbol)
                if spot is None and self.in_delivery_window(t, d):
                    log.warning("no spot for %s in the delivery-margin window: margin left at %s", t.id, t.margin)
                    continue
                new = base + self.delivery_addon(t, d, spot)
                delta = new - (t.margin or 0.0)
                if abs(delta) >= 1.0:
                    self._post(s, t.account, self.ds.now(), "margin_adjust", delta, t.id, "daily margin recompute")
                    t.margin = round(new, 2)
            s.flush()
            for acc in self.cfg.accounts:
                st_last = s.scalars(select(LedgerEntry).where(LedgerEntry.account == acc)
                                    .order_by(LedgerEntry.id.desc()).limit(1)).first()
                if st_last and st_last.cash_balance - st_last.used_margin < 0:
                    msgs.append(f"{acc}: margin shortfall, available {st_last.cash_balance - st_last.used_margin:,.0f}")
            s.commit()
        for m in msgs:
            if self.notifier:
                self.notifier.send(m, "risk")
        return msgs

    def mark(self, trades: Optional[Sequence[PTrade]] = None, quotes=None, persist: bool = True) -> Dict[str, dict]:
        """Cost to close at natural prices (buy the short at the ask, sell the long at the bid) and
        unrealised P&L for open positions."""
        trades = list(trades) if trades is not None else self.open_trades()
        if not trades:
            return {}
        keys = []
        for t in trades:
            short, long_ = _legs_of(t)
            keys += [f"NFO:{short['tradingsymbol']}", f"NFO:{long_['tradingsymbol']}", f"NSE:{t.symbol}"]
        quotes = quotes if quotes is not None else self.ds.quotes(keys)
        out, now = {}, self.ds.now()
        rows = []
        for t in trades:
            short, long_ = _legs_of(t)
            qs, ql = quotes.get(f"NFO:{short['tradingsymbol']}"), quotes.get(f"NFO:{long_['tradingsymbol']}")
            sq = quotes.get(f"NSE:{t.symbol}")
            if qs is None or not qs.ask:
                continue
            ctc = qs.ask - (ql.bid if ql and ql.bid else 0.0)
            info = {"cost_to_close": round(ctc, 4), "unrealised": round((t.credit - ctc) * short["qty"], 2),
                    "spot": sq.last if sq else None, "short": (qs.bid, qs.ask), "long": (ql.bid, ql.ask) if ql else None}
            out[t.id] = info
            if persist:
                rows.append(dict(ts=now, trade_id=t.id, spot=info["spot"], cost_to_close=info["cost_to_close"],
                                 legs_json=json.dumps({"short": info["short"], "long": info["long"]})))
        if rows:
            with Session(self.pengine) as s:
                s.execute(insert(PMark), rows)
                s.commit()
        return out

    def settle_expired(self, d: date, spots: Dict[str, float]) -> List[str]:
        """Settle every position still open on its expiry day at intrinsic value against `spots` (official close)."""
        msgs = []
        with self._lock, Session(self.pengine) as s:
            for t in s.scalars(select(PTrade).where(PTrade.status == "open", PTrade.expiry <= d)).all():
                S = spots.get(t.symbol)
                if S is None:
                    msgs.append(f"{t.id}: no official close for {t.symbol}; settlement waits")
                    continue
                legs = [(lg["side"], lg["strike"], lg["cp"], lg["qty"]) for lg in json.loads(t.legs_json)]
                net, charges = self.costs.settlement(legs, S)
                qty = legs[0][3]
                ts = datetime.combine(d, time(15, 30))
                pnl = t.credit * qty + net - t.entry_charges - charges.total
                self._order(s, t.account, t.id, "settlement",
                            [{"symbol": t.symbol, "spot": S, "net_intrinsic": round(net, 2)}], True, "", ts)
                self._post(s, t.account, ts, "settlement", net, t.id, f"expiry settlement at {S:g}")
                if charges.total:
                    self._post(s, t.account, ts, "charges", -charges.total, t.id,
                               "STT on exercise and physical-delivery brokerage")
                self._post(s, t.account, ts, "margin_release", -(t.margin or 0.0), t.id, "margin released")
                self._post(s, t.account, ts, "realised_pnl", pnl, t.id, "expiry settlement")
                self._finish(t, ts, "expiry_settlement", -net / qty, charges.total, pnl)
                msgs.append(f"{t.account}: {t.symbol} settled at expiry against {S:g}, net P&L {pnl:,.0f}")
            s.commit()
        return msgs

    # ------------------------------------------------------------------ account values
    def last_marks(self, trade_ids: Sequence[str]) -> Dict[str, dict]:
        """The latest saved mark of each trade (after the close, live quotes have no depth)."""
        if not trade_ids:
            return {}
        with Session(self.pengine) as s:
            latest = (select(PMark.trade_id, func.max(PMark.id).label("mid")).where(PMark.trade_id.in_(list(trade_ids)))
                      .group_by(PMark.trade_id).subquery())
            rows = s.scalars(select(PMark).join(latest, PMark.id == latest.c.mid)).all()
        return {r.trade_id: {"cost_to_close": r.cost_to_close, "spot": r.spot, "ts": r.ts, "saved": True} for r in rows}

    def account_value(self, account: str, marks: Optional[Dict[str, dict]] = None) -> dict:
        """Cash, margin and value of one account. Value = cash balance - cost to close every open
        position at natural prices (live marks, else the latest saved mark, else the entry credit)."""
        st = self.state(account)
        trades = self.open_trades(account)
        marks = dict(marks) if marks is not None else self.mark(trades, persist=False)
        missing = [t.id for t in trades if t.id not in marks]
        marks.update({k: v for k, v in self.last_marks(missing).items() if v["cost_to_close"] is not None})
        liability = 0.0
        unreal = 0.0
        for t in trades:
            m = marks.get(t.id)
            ctc = m["cost_to_close"] if m else t.credit
            qty = t.lots * t.lot_size
            liability += ctc * qty
            unreal += (t.credit - ctc) * qty
        with Session(self.pengine) as s:
            realised = s.scalar(select(func.coalesce(func.sum(PTrade.pnl_inr), 0.0)).where(
                PTrade.account == account, PTrade.status == "closed")) or 0.0
        return {"account": account, "started": st.started, "cash_balance": st.cash_balance, "used_margin": st.used_margin,
                "available": st.available, "value": st.cash_balance - liability, "unrealised": unreal,
                "realised": realised, "open_positions": len(trades)}

    def record_day(self, d: date) -> None:
        """Each started account's end-of-day value, for today's P&L and the equity curves."""
        marks = self.mark(persist=False)
        from sqlalchemy.dialects.sqlite import insert as sq_insert

        with Session(self.pengine) as s:
            for acc in self.cfg.accounts:
                v = self.account_value(acc, marks)
                if not v["started"]:
                    continue
                stmt = sq_insert(AccountDay).values(date=d, account=acc, value=round(v["value"], 2),
                                                    cash_balance=v["cash_balance"], used_margin=v["used_margin"],
                                                    realised=round(v["realised"], 2), unrealised=round(v["unrealised"], 2))
                s.execute(stmt.on_conflict_do_update(index_elements=["date", "account"], set_={
                    c: getattr(stmt.excluded, c) for c in ("value", "cash_balance", "used_margin", "realised", "unrealised")}))
            s.commit()
