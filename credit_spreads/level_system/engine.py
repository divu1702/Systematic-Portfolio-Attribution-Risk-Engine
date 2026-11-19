"""The level system's runs on the shared platform (spec: How the system works, Confirmation, Entries,
Exits, Screener, Schedule).

- `run_close()` at 15:20: snapshot -> exits (kill level, profit target, time stop, event exit) ->
  screener steps 1-2 -> setups in confirmation advance one session -> new setups armed (Day 0) ->
  entries for each book whose session qualifies -> ROI ranking -> marks, account values, chain snapshot.
- `gap_check()` at 09:30: an overnight gap beyond the level exits at once; profit targets.
- `exit_check()` every 30 minutes: profit targets (and overdue time or event exits).
- `official_recheck(d)` at 18:30: the 15:20 decisions against NSE's official close and OI
  (`close_mismatch` when they disagree), then expiry settlement for every account.
Before go_live_date nothing is traded: the run reports what each book would have entered.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from platform_core.broker import SpreadOrder
from platform_core.data_service import DataService
from platform_core.models import CloseCheck, PTrade, SetupRow
from platform_core.parquet_store import write_snapshot

from . import analytics as A
from . import screener, sizing, store
from . import strikes as K
from .analytics import BEAR_CALL, BULL_PUT
from .setups import detect
from .state import ACTIVE, CANCELLED, CONFIRMED, EXPIRED, Machine, Session, beyond, step

log = logging.getLogger(__name__)
BOOK_ACCOUNT = {"strict": "level_strict", "research": "level_research"}
LEVEL_ACCOUNTS = tuple(BOOK_ACCOUNT.values())
EVENT_EXIT_TYPES = ("results", "board_meeting")


class Snapshot:
    """Quotes taken during one run (fetched once, reused), saved as chain_snap Parquet at the end."""

    def __init__(self, ds: DataService, ts: datetime, r: float):
        self.ds, self.ts, self.r = ds, ts, r
        self.q: Dict[str, object] = {}
        self.legs: Dict[str, tuple] = {}            # option key -> (symbol, expiry, strike, cp, future key)

    def get(self, keys: Sequence[str]) -> Dict[str, object]:
        missing = [k for k in dict.fromkeys(keys) if k not in self.q]
        for i in range(0, len(missing), 500):
            self.q.update(self.ds.quotes(missing[i:i + 500]))
        return self.q

    def spots(self, symbols: Sequence[str]) -> Dict[str, object]:
        q = self.get([f"NSE:{s}" for s in symbols])
        return {s: q[f"NSE:{s}"] for s in symbols if f"NSE:{s}" in q}

    def register_chain(self, chain) -> None:
        fut = chain.future.key if chain.future else None
        for (k, cp), leg in chain.legs.items():
            self.legs[leg.key] = (chain.underlying, chain.expiry, k, cp, fut)

    def chain(self, symbol: str, expiry: date):
        ch = self.ds.chain(symbol, expiry)
        self.register_chain(ch)
        keys = [leg.key for leg in ch.legs.values()] + ([ch.future.key] if ch.future else []) + [f"NSE:{symbol}"]
        return ch, self.get(keys)

    def rows(self) -> List[dict]:
        out = []
        for key, (sym, exp, k, cp, fut) in self.legs.items():
            q = self.q.get(key)
            if q is None:
                continue
            fq = self.q.get(fut) if fut else None
            F = (fq.last or fq.mid) if fq else None
            iv = None
            if F and q.mid:
                iv = A.leg_iv(q.mid, F, k, A.years_to_expiry(self.ts, exp), self.r, cp)
            out.append({"symbol": sym, "expiry": exp, "strike": k, "type": cp, "bid": q.bid or None,
                        "ask": q.ask or None, "last_price": q.last, "oi": q.oi, "volume": q.volume, "iv": iv})
        return out


@dataclass
class RunReport:
    date: date
    ts: datetime
    live: bool
    phase: str = "close"
    exits: List[dict] = field(default_factory=list)
    entries: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    armed: List[dict] = field(default_factory=list)
    advanced: List[dict] = field(default_factory=list)
    screener: List[dict] = field(default_factory=list)
    removed: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    mismatches: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self.__dict__, default=str))


def oi_map(chain, quotes) -> Dict[tuple, float]:
    return {(k, cp): float(quotes[leg.key].oi or 0) for (k, cp), leg in chain.legs.items() if leg.key in quotes}


class LevelEngine:
    def __init__(self, ds: DataService, broker, cfg, notifier=None):
        self.ds, self.broker, self.cfg = ds, broker, cfg
        self.notifier = notifier or ds.ctx.notifier
        self.costs = broker.costs
        self.r = ds.ctx.cfg.analytics.risk_free
        self._nifty: Optional[str] = None

    # ------------------------------------------------------------------ data helpers
    def bars_with_session(self, symbol: str, quote, d: date) -> pd.DataFrame:
        bars = self.ds.daily_bars(symbol, end=d - timedelta(days=1))
        return self.ds.with_session(bars, d, quote) if quote is not None else bars

    def nifty_trend(self) -> str:
        if self._nifty is None:
            try:
                bars = self.ds.index_bars(self.cfg.market_filter.index)
                self._nifty = A.trend(bars, self.cfg.market_filter.swing_side_bars) if len(bars) else "neutral"
            except Exception as exc:
                log.warning("Nifty trend unavailable: %s", exc)
                self._nifty = "neutral"
        return self._nifty

    def _alert(self, msg: str, kind: str) -> None:
        try:
            self.notifier.send(msg, kind)
        except Exception as exc:          # alerts never stop trading
            log.warning("alert failed: %s", exc)

    def level_trades(self) -> List[PTrade]:
        return [t for t in self.broker.open_trades() if t.account in LEVEL_ACCOUNTS]

    # ------------------------------------------------------------------ exits
    def time_stop_date(self, expiry: date, cal) -> date:
        d, n = expiry, 0
        while n < self.cfg.exits.time_stop_trading_days_before_expiry:
            d = cal.prev_trading_day(d)
            n += 1
        return d

    def event_exit(self, t: PTrade, today: date, cal):
        """A results date or board meeting announced on or before the trade's time-stop date: (description,
        event date) once today is the last session before it, else None."""
        stop = self.time_stop_date(t.expiry, cal)
        evs = [e for e in self.ds.events_between(t.symbol, today, stop) if e.event_type in EVENT_EXIT_TYPES]
        if not evs:
            return None
        e = evs[0]
        if cal.next_trading_day(today) >= e.event_date:
            return f"{e.event_type} on {e.event_date}", e.event_date
        return None

    def check_exits(self, snap: Snapshot, phase: str, rep: RunReport) -> None:
        """phase: 'gap' (09:30), 'regular' (every 30 minutes) or 'close' (15:20)."""
        trades = self.level_trades()
        if not trades:
            return
        keys = []
        for t in trades:
            legs = json.loads(t.legs_json)
            keys += [f"NFO:{lg['tradingsymbol']}" for lg in legs] + [f"NSE:{t.symbol}"]
        quotes = snap.get(keys)
        cal, today = self.ds.calendar(), self.ds.today()
        capture = self.cfg.exits.profit_target_capture
        for t in trades:
            legs = json.loads(t.legs_json)
            short = next(lg for lg in legs if lg["side"] == "SELL")
            long_ = next(lg for lg in legs if lg["side"] == "BUY")
            sq = quotes.get(f"NSE:{t.symbol}")
            spot = sq.last if sq else None
            qs, ql = quotes.get(f"NFO:{short['tradingsymbol']}"), quotes.get(f"NFO:{long_['tradingsymbol']}")
            ctc = (qs.ask - (ql.bid if ql and ql.bid else 0.0)) if qs and qs.ask else None
            left = cal.trading_days_between(today, t.expiry)
            n_stop = self.cfg.exits.time_stop_trading_days_before_expiry
            reason = None
            if phase in ("close", "gap") and spot and t.level is not None and beyond(t.direction, t.level, spot):
                reason = "kill_level" if phase == "close" else "kill_gap"
            elif ctc is not None and ctc <= (1 - capture) * t.credit + 1e-9:
                reason = "profit_target"
            elif (phase == "close" and left <= n_stop) or left < n_stop:
                reason = "time_stop"
            elif self.cfg.exits.event_exit:
                ev = self.event_exit(t, today, cal)
                # At the 15:20 snapshot of the last session before the event; at once when that has passed.
                if ev and (phase == "close" or ev[1] <= today):
                    reason = f"event_exit: {ev[0]}"
            if not reason:
                continue
            res = self.broker.close_spread(t.id, reason, quotes=quotes)
            info = {"trade_id": t.id, "account": t.account, "symbol": t.symbol, "direction": t.direction,
                    "reason": reason, "spot": spot, "level": t.level, "ok": res.ok, "pnl": res.pnl,
                    "pnl_r": round(res.pnl / t.one_r, 2) if res.ok and t.one_r else None, "note": "" if res.ok else res.reason}
            rep.exits.append(info)
            if res.ok:
                self._alert(f"[{t.account}] EXIT {t.symbol} {t.direction.replace('_', ' ')}: {reason}, "
                            f"P&L {res.pnl:,.0f} ({info['pnl_r']:+.2f}R)" if info["pnl_r"] is not None else
                            f"[{t.account}] EXIT {t.symbol}: {reason}, P&L {res.pnl:,.0f}", "exit")
            else:
                self._alert(f"[{t.account}] EXIT FAILED {t.symbol} ({reason}): {res.reason}", "warning")

    # ------------------------------------------------------------------ setups
    def zone_now(self, snap: Snapshot, row: SetupRow) -> Optional[A.ZoneOI]:
        if not row.expiry or not row.zone_json:
            return None
        try:
            ch, quotes = snap.chain(row.symbol, row.expiry)
        except LookupError:
            return None
        z = json.loads(row.zone_json)
        return A.zone_oi(oi_map(ch, quotes), row.direction, z["protected"], z["other"])

    def advance(self, snap: Snapshot, spots, rep: RunReport, today: date) -> None:
        """Each active setup takes today's session; missed sessions (machine off) are judged from the
        daily candles without OI and never trade after the fact."""
        cal = self.ds.calendar()
        for row in store.active(self.ds.pengine):
            if row.last_session and row.last_session >= today:
                continue
            m = store.machine_from_row(row)
            d = cal.next_trading_day(row.last_session or row.day0_date)
            bars = self.ds.daily_bars(row.symbol, end=today - timedelta(days=1))
            while d < today and m.state in ACTIVE:
                if d in bars.index:
                    b = bars.loc[d]
                    st = step(m, Session(d, float(b["close"]), float(b["high"]), float(b["low"]), None,
                                         A.atr_before(bars, d, self.cfg.atr_period)), self.cfg)
                    st.record["missed"] = True
                    store.save_step(self.ds.pengine, row.id, m, st, d, self.ds.now())
                    if st.strict or st.research:
                        rep.warnings.append(f"{row.symbol} setup {row.setup}: session {d} was missed; no late entry")
                d = cal.next_trading_day(d)
            if m.state not in ACTIVE:
                continue
            q = spots.get(row.symbol)
            if q is None or not q.last:
                rep.warnings.append(f"{row.symbol}: no live quote, setup {row.id} not advanced today")
                continue
            bars_now = self.ds.with_session(bars, today, q)
            sess = Session(today, float(q.last), float(max(q.day_high or q.last, q.last)),
                           float(min(q.day_low or q.last, q.last)), self.zone_now(snap, row),
                           A.atr_before(bars_now, today, self.cfg.atr_period))
            st = step(m, sess, self.cfg)
            store.save_step(self.ds.pengine, row.id, m, st, today, self.ds.now())
            rep.advanced.append({"setup_id": row.id, "symbol": row.symbol, "setup": row.setup, "direction": row.direction,
                                 "level": row.level, "state": st.state, "route": m.route, "record": st.record,
                                 "strict": st.strict, "research": st.research, "note": st.note})
            if st.state == CANCELLED:
                self._alert(f"Setup {row.setup} {row.symbol} cancelled: closed beyond {row.level:g}", "info")
            for book in ("strict", "research"):
                if getattr(st, book):
                    self.enter(row, m, book, st.failed_filters, snap, bars_now, q, rep)

    def arm(self, snap: Snapshot, rows: List[screener.StockRow], rep: RunReport, today: date) -> None:
        """Setup scan on the stocks the screener kept: anything with Day 0 today is armed."""
        for r in rows:
            if not r.remaining or r.bars is None or not len(r.bars):
                continue
            for a in detect(r.bars, today, self.cfg):
                if store.duplicate(self.ds.pengine, r.symbol, a.setup, a.direction, a.level):
                    continue
                expiry = r.expiry                     # the 15-day-rule expiry on Day 0, fixed through confirmation
                try:
                    ch, quotes = snap.chain(r.symbol, expiry)
                except LookupError:
                    continue
                if a.setup == 1:
                    prot, other = A.setup1_zone(a.direction, a.level, a.extreme, ch.strikes,
                                                self.cfg.confirmation.zone_strikes_per_side)
                else:
                    prot, other = A.level_zone(a.direction, a.level, ch.strikes, self.cfg.confirmation.zone_strikes_per_side)
                oi = oi_map(ch, quotes)
                z0 = A.zone_oi(oi, a.direction, prot, other)
                own, opp = A.own_type(a.direction), A.opposite_type(a.direction)
                day0_oi = {f"{k:g}{own}": oi.get((k, own)) for k in prot} | {f"{k:g}{opp}": oi.get((k, opp)) for k in other}
                signals = self.strength_signals(r, a, today)
                signals.update(zone_oi_share=z0.share, local_pcr=z0.local_pcr)
                m = Machine(a.setup, a.direction, a.level, today, a.atr, a.vol_ok, z0)
                sid = store.create(self.ds.pengine, r.symbol, a, m, expiry=expiry,
                                   zone={"protected": prot, "other": other, "own": own, "opp": opp},
                                   day0_oi=day0_oi, signals=signals, now=self.ds.now())
                r.setups.append(f"armed S{a.setup} {a.direction}")
                rep.armed.append({"setup_id": sid, "symbol": r.symbol, "setup": a.setup, "name": a.name,
                                  "direction": a.direction, "level": a.level, "volume_mult": a.volume_mult,
                                  "vol_ok": a.vol_ok, "zone": {"protected": prot, "other": other},
                                  "zone_oi_share": z0.share, "local_pcr": z0.local_pcr, "expiry": expiry,
                                  "signals": signals})

    def strength_signals(self, r: screener.StockRow, a, today: date) -> dict:
        """Logged for every setup, never used by the rules."""
        out = {"volume_mult": a.volume_mult, "atr": a.atr, "breakout_extreme": a.breakout_extreme,
               "breakout_dist_atr": A.dist_atr(a.breakout_extreme, a.level, a.atr), **a.detail}
        try:
            day = self.ds.minute_candles(r.symbol, today, today)
            out["vwap"] = A.vwap(day)
            out["vwap_dist_atr"] = A.dist_atr(out["vwap"], a.level, a.atr)
            cal = self.ds.calendar()
            start = cal.last_n(today, self.cfg.strength_signals.hvn_lookback)[0]
            hist = self.ds.minute_candles(r.symbol, start, today)
            out["hvn"] = A.hvn(hist, self.cfg.strength_signals.hvn_bucket_atr * a.atr)
            out["hvn_dist_atr"] = A.dist_atr(out["hvn"], a.level, a.atr)
        except Exception as exc:
            log.warning("strength signals for %s: %s", r.symbol, exc)
            out["signals_error"] = str(exc)
        return out

    # ------------------------------------------------------------------ entries
    def enter(self, row: SetupRow, m: Machine, book: str, failed: dict, snap: Snapshot, bars, q, rep: RunReport) -> None:
        today, now = self.ds.today(), self.ds.now()
        sym = row.symbol
        base = {"setup_id": row.id, "symbol": sym, "setup": row.setup, "direction": row.direction,
                "level": row.level, "book": book}

        def skip(reason: str, **extra):
            rep.skipped.append({**base, "reason": reason, **extra})
            store.add_note(self.ds.pengine, row.id, f"{book}: skipped ({reason})", now)

        expiry, _ = self.ds.expiry_for(sym, today)
        if expiry is None:
            return skip("no listed expiry")
        if not self.ds.events_fresh():
            return skip("event data is stale")
        evs = self.ds.events_between(sym, today, expiry)
        if evs:
            return skip("event before expiry: " + ", ".join(f"{e.event_date} {e.event_type}" for e in evs))
        try:
            ch, quotes = snap.chain(sym, expiry)
        except LookupError:
            return skip(f"no option chain for {expiry}")
        atr = A.atr_before(bars, today, self.cfg.atr_period)
        spot = float(q.last)
        choice = K.choose(self.ds, self.costs, ch, quotes, level=row.level, direction=row.direction, atr=atr,
                          spot=spot, cfg=self.cfg)
        failed = {k: v for k, v in (failed or {}).items()}
        if choice.best is None:
            if choice.nearest is None:
                return skip(choice.reason, tried=choice.tried[:6])
            if book == "strict":
                skip(choice.reason, tried=choice.tried[:6])
                if not m.research_done:                         # the research book takes the nearest strike
                    m.research_done = True
                    self._save_machine(row.id, m)
                    self.enter(row, m, "research", {**failed, "rom": {"rom": choice.nearest.rom,
                                                                        "needed": self.cfg.strikes.min_rom}},
                               snap, bars, q, rep)
                return
            failed["rom"] = {"rom": choice.nearest.rom, "needed": self.cfg.strikes.min_rom}
        p = choice.best or choice.nearest
        sl, ll = ch.leg(p.short_k, p.cp), ch.leg(p.long_k, p.cp)
        sq, lq = quotes[sl.key], quotes[ll.key]
        tick = ch.tick_size or 0.05
        slip = self.cfg.paper.extra_slip_ticks * tick
        est_credit = (sq.bid - slip) - (lq.ask + slip)
        fq = quotes.get(ch.future.key) if ch.future else None
        fut = (fq.last or fq.mid) if fq else spot
        kill = A.loss_at_kill(level=row.level, spot=spot, future=fut, now=now, expiry=expiry, cp=p.cp,
                              short_k=p.short_k, long_k=p.long_k, short_q=sq, long_q=lq, credit=est_credit,
                              lot_size=ch.lot_size, r=self.r, cushion_points=self.cfg.sizing.iv_cushion_points,
                              exit_charges_per_lot=lambda s, lg: self.costs.close_spread(s, lg, ch.lot_size).total)
        sector = self.ds.sector_of(sym)
        open_ = self.broker.open_trades(BOOK_ACCOUNT[book])
        sz = sizing.size(loss_per_lot=kill.per_lot if kill else None, max_loss_lot=(p.width - est_credit) * ch.lot_size,
                         capital=self.cfg.capital, cfg=self.cfg, book=book, open_trades=open_, sector=sector)
        if sz.lots <= 0:
            return skip(sz.reason, notes=sz.notes)
        nifty = self.nifty_trend()
        entry = {**base, "expiry": expiry, "short": p.short_k, "long": p.long_k, "lots": sz.lots, "lot_size": ch.lot_size,
                 "credit": round(p.credit, 2), "rom": p.rom, "margin_lot": p.margin, "loss_at_kill_lot": round(kill.per_lot, 2),
                 "one_r": sz.one_r, "failed_filters": failed, "nifty_trend": nifty,
                 "nifty_agrees": A.trend_agrees(nifty, row.direction), "sizing": sz.notes}
        if not self.cfg.is_live(today):
            entry["dry_run"] = True
            rep.entries.append(entry)
            store.add_note(self.ds.pengine, row.id, f"{book}: would enter {sz.lots} lot(s) {p.short_k:g}/{p.long_k:g} "
                                                     f"(not live: go_live_date not reached)", now)
            return
        order = SpreadOrder(
            account=BOOK_ACCOUNT[book], symbol=sym, direction=row.direction, expiry=expiry, cp=p.cp,
            short_symbol=sl.tradingsymbol, short_strike=p.short_k, long_symbol=ll.tradingsymbol, long_strike=p.long_k,
            lots=sz.lots, lot_size=ch.lot_size, tick=tick, source="system", setup_tag=str(row.setup), setup_id=row.id,
            sector=sector, level=row.level, loss_at_kill=round(kill.per_lot, 2), one_r=sz.one_r, book=book,
            failed_filters=failed or None, nifty_trend=nifty,
            reason=f"Setup {row.setup} {row.direction.replace('_', ' ')} at {row.level:g}" + (f", route {m.route}" if m.route else ""),
            context={"spot": spot, "atr": atr, "short_iv": kill.short_iv, "long_iv": kill.long_iv, "rom_natural": p.rom,
                     "margin_lot": p.margin, "nifty_agrees": entry["nifty_agrees"], "strikes_tried": choice.tried[:8],
                     "sizing": sz.notes})
        res = self.broker.open_spread(order, quotes)
        if not res.ok:
            rep.skipped.append({**base, "reason": f"broker rejected: {res.reason}"})
            self._alert(f"[{order.account}] REJECTED {sym} {row.direction.replace('_', ' ')}: {res.reason}", "rejection")
            return
        store.link_trade(self.ds.pengine, row.id, book, res.trade_id)
        entry.update(trade_id=res.trade_id, filled_credit=round(res.credit, 2), margin=res.margin, charges=res.charges)
        rep.entries.append(entry)
        self._alert(f"[{order.account}] ENTRY {sym} {row.direction.replace('_', ' ')} {p.short_k:g}/{p.long_k:g} {expiry} "
                    f"x{sz.lots}: credit {res.credit:.2f}, RoM {p.rom:.1%}, 1R {sz.one_r:,.0f}, level {row.level:g}"
                    + (f", failed filters: {', '.join(failed)}" if failed else ""), "entry")

    def _save_machine(self, setup_id: int, m: Machine) -> None:
        with DBSession(self.ds.pengine) as s:
            row = s.get(SetupRow, setup_id)
            row.machine_json = store.machine_to_json(m)
            s.commit()

    # ------------------------------------------------------------------ runs
    def run_close(self) -> RunReport:
        today, now = self.ds.today(), self.ds.now()
        rep = RunReport(today, now, self.cfg.is_live(today))
        snap = Snapshot(self.ds, now, self.r)
        stocks = self.ds.stocks()
        spots = snap.spots(stocks)
        self.check_exits(snap, "close", rep)
        rows = screener.filter_stocks(self, snap, stocks, spots, today)
        for r in rows:
            if not r.event_ok:
                rep.removed["event"] = rep.removed.get("event", 0) + 1
            elif not r.liquidity_ok:
                rep.removed["liquidity"] = rep.removed.get("liquidity", 0) + 1
        self.advance(snap, spots, rep, today)
        self.arm(snap, rows, rep, today)
        ranked = screener.rank(self, snap, rows)
        status = self._setup_status(today)
        for r in rows:
            r.setups = sorted(set(r.setups) | set(status.get(r.symbol, [])))
            r.sector = r.sector or (self.ds.sector_of(r.symbol) if r.remaining else None)
        screener.save(self.ds.pengine, today, rows)
        rep.screener = [{"rank": r.rank, "symbol": r.symbol, "sector": r.sector, "rom_call": r.rom_call,
                         "rom_put": r.rom_put, "setups": r.setups, "spot": r.spot, "atr": r.atr,
                         "expiry": r.expiry, "reference": r.ref} for r in ranked]
        self.finish(snap, rep)
        return rep

    def _setup_status(self, today: date) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for row in store.on_date(self.ds.pengine, today):
            out.setdefault(row.symbol, []).append(f"S{row.setup} {row.direction} {row.state}")
        return out

    def gap_check(self) -> RunReport:
        rep = RunReport(self.ds.today(), self.ds.now(), self.cfg.is_live(self.ds.today()), phase="gap")
        snap = Snapshot(self.ds, rep.ts, self.r)
        self.check_exits(snap, "gap", rep)
        self.finish(snap, rep, save_snapshot=False)
        return rep

    def exit_check(self) -> RunReport:
        rep = RunReport(self.ds.today(), self.ds.now(), self.cfg.is_live(self.ds.today()), phase="regular")
        snap = Snapshot(self.ds, rep.ts, self.r)
        self.check_exits(snap, "regular", rep)
        self.finish(snap, rep, save_snapshot=False)
        return rep

    def finish(self, snap: Snapshot, rep: RunReport, save_snapshot: bool = True) -> None:
        try:
            self.broker.mark()
        except Exception as exc:
            rep.warnings.append(f"marks failed: {exc}")
        if rep.phase == "close":
            try:
                self.broker.record_day(rep.date)
            except Exception as exc:
                rep.warnings.append(f"account values not recorded: {exc}")
        if save_snapshot:
            try:
                write_snapshot(self.cfg.resolve(self.cfg.paths.chain_snap), rep.ts, snap.rows())
            except Exception as exc:
                rep.warnings.append(f"chain snapshot not saved: {exc}")

    # ------------------------------------------------------------------ 18:30
    def official_recheck(self, d: Optional[date] = None) -> RunReport:
        """Compare the 15:20 decisions of `d` with NSE's official close and OI; log `close_mismatch` rows."""
        d = d or self.ds.today()
        rep = RunReport(d, self.ds.now(), self.cfg.is_live(d), phase="official")
        with DBSession(self.ds.pengine) as s:
            setups = s.scalars(select(SetupRow).where((SetupRow.last_session == d) | (SetupRow.day0_date == d))).all()
            killed = s.scalars(select(PTrade).where(PTrade.account.in_(LEVEL_ACCOUNTS),
                                                    PTrade.exit_reason.in_(("kill_level", "kill_gap")))).all()
            killed = [t for t in killed if t.exit_ts and t.exit_ts.date() == d]
            expiring = s.scalars(select(PTrade).where(PTrade.status == "open", PTrade.expiry <= d)).all()
        symbols = sorted({r.symbol for r in setups} | {t.symbol for t in killed} | {t.symbol for t in expiring})
        eod = self.ds.official_eod(d, symbols) if symbols else None
        checks: List[CloseCheck] = []
        closes = {s_: v["close"] for s_, v in (eod.closes.items() if eod else []) if v.get("close")}
        for row in setups:
            off = closes.get(row.symbol)
            sessions = json.loads(row.sessions_json or "[]")
            rec = next((x for x in reversed(sessions) if x.get("date") == d.isoformat()), None)
            if rec is not None and off is not None:
                snap_beyond, off_beyond = rec["beyond"], beyond(row.direction, row.level, off)
                checks.append(CloseCheck(date=d, kind="setup_close", ref=str(row.id), symbol=row.symbol,
                                         snapshot_value=rec["close"], official_value=off,
                                         mismatch=snap_beyond != off_beyond,
                                         note=f"level {row.level:g}: 15:20 {'beyond' if snap_beyond else 'held'}, "
                                              f"official {'beyond' if off_beyond else 'held'}"))
                if eod and eod.oi and rec.get("oi") is not None and row.zone_json:
                    z = json.loads(row.zone_json)
                    oi = {(k, cp): v for (sym_, exp, k, cp), v in eod.oi.items() if sym_ == row.symbol and exp == row.expiry}
                    m = store.machine_from_row(row)
                    if oi and m.day0_zone is not None:
                        t = A.oi_test(m.day0_zone, A.zone_oi(oi, row.direction, z["protected"], z["other"]),
                                      self.cfg.confirmation.oi_tolerance)
                        checks.append(CloseCheck(date=d, kind="setup_oi", ref=str(row.id), symbol=row.symbol,
                                                 snapshot_value=rec["oi"].get("protected_change"),
                                                 official_value=t.protected_change,
                                                 mismatch=bool(rec["oi"]["passed"]) != t.passed,
                                                 note=f"zone OI test: 15:20 {'pass' if rec['oi']['passed'] else 'fail'}, "
                                                      f"official {'pass' if t.passed else 'fail'} ({t.reason or 'ok'})"))
            if row.day0_date == d and off is not None:
                bars = self.ds.daily_bars(row.symbol, end=d - timedelta(days=1))
                v = eod.closes[row.symbol]
                official = pd.concat([bars, pd.DataFrame({k: [v.get(k) or 0] for k in ("open", "high", "low", "close", "volume")},
                                                         index=[d])]) if len(bars) else None
                if official is not None:
                    still = any(a.setup == row.setup and a.direction == row.direction and abs(a.level - row.level) < 0.01
                                for a in detect(official, d, self.cfg))
                    checks.append(CloseCheck(date=d, kind="day0", ref=str(row.id), symbol=row.symbol,
                                             snapshot_value=row.level, official_value=off, mismatch=not still,
                                             note="Day 0 " + ("confirmed" if still else "not armed") + " on official values"))
        for t in killed:
            off = closes.get(t.symbol)
            if off is not None:
                checks.append(CloseCheck(date=d, kind="kill", ref=t.id, symbol=t.symbol, snapshot_value=None,
                                         official_value=off, mismatch=not beyond(t.direction, t.level, off),
                                         note=f"killed at the snapshot; official close {'beyond' if beyond(t.direction, t.level, off) else 'inside'} {t.level:g}"))
        rep.mismatches = [{"kind": c.kind, "symbol": c.symbol, "ref": c.ref, "note": c.note} for c in checks if c.mismatch]
        if checks:
            with DBSession(self.ds.pengine) as s:
                s.add_all(checks)
                s.commit()
        for c in rep.mismatches:
            self._alert(f"close_mismatch {c['symbol']} ({c['kind']}): {c['note']}", "warning")
        settle = self.broker.settle_expired(d, closes)
        rep.warnings += settle
        return rep
