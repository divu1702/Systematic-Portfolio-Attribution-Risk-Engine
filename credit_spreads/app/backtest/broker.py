"""Broker and exchange-data source that replay a local market store day by day.

The backtest drives the same jobs as live trading (instrument refresh, scan, entries, monitor,
end of day) with a SimClock. This broker answers every call from the store for the clock's date
only, so nothing after the simulated day can leak in.

Data facts and assumptions (DoltHub US end-of-day chains):
- Bid and ask are real end-of-day quotes. Size at the bid and ask is not in the data, so the order
  book is given `depth_lots` lots per level (depth never binds in a backtest); OI and volume are 0.
- The forward for each expiry is implied from put-call parity at the strikes nearest the money,
  which also absorbs dividends and borrow; without both a call and a put it falls back to spot x e^(rT).
- The data samples ~25 strikes around the money for ~3 expiries a day. When an open position's
  leg is missing from a day's sample (it moved away from the money, or the expiry left the sample
  in its last two weeks), the leg is priced with Black-76 at its last observed IV and spread.
  Such quotes are counted in `modelled_quotes` and flagged in the quote dict.
- Internal segment labels stay "NSE" (stocks) and "NFO" (options) so the engine is unchanged.
"""

from __future__ import annotations

import csv
import math
import re
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..analytics import black76
from ..broker.base import Broker
from ..clock import Clock

EXPIRY_TIME = time(16, 0)
# Internal contract names use "_" separators so any ticker parses, including NSE names with digits,
# "&" or "-" (M&M, BAJAJ-AUTO, 360ONE): SYMBOL_YYMMDD_STRIKE_CE / SYMBOL_YYMMDD_FUT.
_KEY = re.compile(r"^(?P<sym>.+)_(?P<ymd>\d{6})_(?P<rest>FUT|\d+(?:\.\d+)?_(?:CE|PE))$")


def fmt_strike(k: float) -> str:
    return str(int(k)) if float(k).is_integer() else f"{k:g}"


def option_symbol(sym: str, expiry: date, strike: float, cp: str) -> str:
    return f"{sym}_{expiry:%y%m%d}_{fmt_strike(strike)}_{cp}"


def future_symbol(sym: str, expiry: date) -> str:
    return f"{sym}_{expiry:%y%m%d}_FUT"


def _token(*parts) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode()) % 10**9


@dataclass
class _Day:
    d: date
    spot: Dict[str, float]
    chain: Dict[str, Dict[Tuple[date, float, str], Tuple[float, float, float]]]   # sym -> (exp, K, cp) -> (bid, ask, iv)
    expiries: Dict[str, List[date]]
    extras: Dict[Tuple[str, date, float, str], tuple] = None    # recorded lot, tick, sizes, OI, volume
    futures: Dict[Tuple[str, date], float] = None                # recorded futures mid
    lots: Dict[str, Tuple[int, float]] = None                    # recorded lot size and tick per symbol


class HistoricalBroker(Broker):
    name = "historical"

    def __init__(self, store_path: Path, clock: Clock, symbols: Sequence[str], r: float = 0.045,
                 lot: int = 100, tick: float = 0.01, depth_lots: int = 500):
        self.conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True, check_same_thread=False)
        self.clock, self.r, self.lot, self.tick, self.depth_lots = clock, r, lot, tick, depth_lots
        self.symbols = list(symbols)
        self._days: Dict[date, _Day] = {}
        self._fwd: Dict[Tuple[date, str, date], float] = {}
        self._splits = self._load_splits()
        self.modelled_quotes = 0
        self.real_quotes = 0

    # ------------------------------------------------------------------ data access
    def _load_splits(self) -> Dict[str, List[Tuple[date, float]]]:
        out: Dict[str, List[Tuple[date, float]]] = {}
        for sym, ex, to_f, for_f in self.conn.execute("SELECT symbol, ex_date, to_factor, for_factor FROM splits"):
            if to_f and for_f and to_f != for_f:
                out.setdefault(sym, []).append((date.fromisoformat(ex), to_f / for_f))
        return out

    def splits_on(self, d: date) -> List[str]:
        return [s for s, lst in self._splits.items() if any(ex == d for ex, _ in lst) and s in self.symbols]

    def _day(self, d: date) -> _Day:
        if d not in self._days:
            if len(self._days) > 5:
                self._days.clear()
                self._fwd.clear()
            spot = {s: c for s, c in self.conn.execute("SELECT symbol, close FROM bars WHERE date=?", (str(d),))
                    if s in self.symbols and c}
            chain: Dict[str, dict] = {}
            exps: Dict[str, set] = {}
            for sym, exp, k, cp, bid, ask, iv in self.conn.execute(
                    "SELECT symbol, expiration, strike, cp, bid, ask, iv FROM chains WHERE date=?", (str(d),)):
                if sym not in spot:
                    continue
                e = date.fromisoformat(exp)
                chain.setdefault(sym, {})[(e, float(k), cp)] = (bid or 0.0, ask or 0.0, iv or 0.0)
                exps.setdefault(sym, set()).add(e)
            extras = {(sym, date.fromisoformat(exp), float(k), cp): rest for sym, exp, k, cp, *rest in self.conn.execute(
                "SELECT symbol, expiration, strike, cp, lot, tick, bid_qty, ask_qty, bid_depth, ask_depth, oi, volume "
                "FROM chain_extras WHERE date=?", (str(d),))} if self._has("chain_extras") else {}
            futs = {(sym, date.fromisoformat(exp)): ((b + a) / 2 if b and a else last)
                    for sym, exp, b, a, last in self.conn.execute(
                        "SELECT symbol, expiration, bid, ask, last FROM futures WHERE date=?", (str(d),))
                    } if self._has("futures") else {}
            lots = {}
            for (sym, _e, _k, _cp), ex in extras.items():
                if ex[0] and sym not in lots:
                    lots[sym] = (int(ex[0]), float(ex[1] or self.tick))
            self._days[d] = _Day(d, spot, chain, {s: sorted(v) for s, v in exps.items()}, extras, futs, lots)
        return self._days[d]

    def _has(self, table: str) -> bool:
        if not hasattr(self, "_tables"):
            self._tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return table in self._tables

    def _lot_tick(self, sym: str) -> Tuple[int, float]:
        """Recorded lot size and tick for a symbol on the clock's day, else the defaults."""
        return (self._day(self.clock.today()).lots or {}).get(sym, (self.lot, self.tick))

    def _T(self, expiry: date) -> float:
        return black76.year_fraction((datetime.combine(expiry, EXPIRY_TIME) - self.clock.now()).total_seconds())

    def forward(self, sym: str, expiry: date) -> Optional[float]:
        day = self._day(self.clock.today())
        key = (day.d, sym, expiry)
        if key in self._fwd:
            return self._fwd[key]
        if day.futures and day.futures.get((sym, expiry)):
            self._fwd[key] = day.futures[(sym, expiry)]          # a recorded futures price is the forward
            return self._fwd[key]
        S = day.spot.get(sym)
        if S is None:
            return None
        T = self._T(expiry)
        pairs = []
        rows = day.chain.get(sym, {})
        for (e, k, cp), (bid, ask, _iv) in rows.items():
            if e == expiry and cp == "CE" and bid > 0 and ask > 0:
                put = rows.get((e, k, "PE"))
                if put and put[0] > 0 and put[1] > 0:
                    pairs.append((abs(k - S), k, (bid + ask) / 2, (put[0] + put[1]) / 2))
        if pairs:
            pairs.sort()
            ests = [k + math.exp(self.r * T) * (c - p) for _, k, c, p in pairs[:2]]
            F = sum(ests) / len(ests)
        else:
            F = S * math.exp(self.r * T)
        self._fwd[key] = F
        return F

    # ------------------------------------------------------------------ Broker API
    def login_url(self) -> str:
        return "backtest://login"

    def complete_login(self, request_token: str) -> None:
        return None

    def session_valid(self) -> bool:
        return True

    def instruments(self, exchange: str) -> List[dict]:
        day = self._day(self.clock.today())
        rows = []
        for sym in sorted(day.spot):
            if exchange == "NSE":
                rows.append(self._row(_token("EQ", sym), sym, sym, "EQ", None, 0.0, 1, "NSE", "NSE"))
                continue
            lot, tick = self._lot_tick(sym)
            for e in day.expiries.get(sym, []):
                rows.append(self._row(_token("FUT", sym, e), future_symbol(sym, e), sym, "FUT", e, 0.0, lot,
                                      "NFO-FUT", "NFO", tick))
            for (e, k, cp) in day.chain.get(sym, {}):
                rows.append(self._row(_token("OPT", sym, e, fmt_strike(k), cp), option_symbol(sym, e, k, cp), sym,
                                      cp, e, k, lot, "NFO-OPT", "NFO", tick))
        return rows

    def _row(self, token, tsym, name, itype, expiry, strike, lot, segment, exchange, tick=None) -> dict:
        return {"instrument_token": token, "exchange_token": str(token % 100000), "tradingsymbol": tsym,
                "name": name, "last_price": 0.0, "expiry": expiry, "strike": float(strike),
                "tick_size": tick or self.tick, "lot_size": lot, "instrument_type": itype, "segment": segment,
                "exchange": exchange}

    def _book(self, bid: float, ask: float, qty: int) -> dict:
        lv = lambda px, i, sign: {"price": round(px + sign * i * self.tick, 4), "quantity": qty, "orders": 1}  # noqa: E731
        buy = [lv(bid, i, -1) for i in range(5) if bid > 0 and bid - i * self.tick > 0]
        sell = [lv(ask, i, +1) for i in range(5)] if ask > 0 else []
        return {"buy": buy, "sell": sell}

    def _quote(self, last: float, bid: float, ask: float, qty: int, modelled: bool = False) -> dict:
        now = self.clock.now()
        return {"instrument_token": 0, "timestamp": now, "last_trade_time": now, "last_price": last,
                "volume": 0, "oi": 0, "depth": self._book(bid, ask, qty), "modelled": modelled}

    def _recorded_quote(self, bid: float, ask: float, ex: tuple) -> dict:
        lot, tick, bq, aq, bd, ad, oi, vol = ex
        tick = tick or self.tick
        now = self.clock.now()
        def side(px, top, total, sign):
            if not px or px <= 0 or not top:
                return []
            levels = [{"price": px, "quantity": int(top), "orders": 1}]
            rest = int((total or top) - top)
            if rest > 0:
                levels.append({"price": round(px + sign * tick, 4), "quantity": rest, "orders": 1})
            return levels
        return {"instrument_token": 0, "timestamp": now, "last_trade_time": now, "last_price": round((bid + ask) / 2, 2),
                "volume": int(vol or 0), "oi": int(oi or 0), "modelled": False,
                "depth": {"buy": side(bid, bq, bd, -1), "sell": side(ask, aq, ad, +1)}}

    def _last_seen(self, sym: str, e: date, k: float, cp: str) -> Optional[Tuple[float, float, float]]:
        row = self.conn.execute(
            "SELECT bid, ask, iv FROM chains WHERE symbol=? AND expiration=? AND strike=? AND cp=? AND date<=? "
            "AND iv > 0 ORDER BY date DESC LIMIT 1", (sym, str(e), k, cp, str(self.clock.today()))).fetchone()
        return tuple(row) if row else None

    def _model_option(self, sym: str, e: date, k: float, cp: str) -> Optional[dict]:
        day = self._day(self.clock.today())
        if sym not in day.spot:
            return None
        T = self._T(e)
        F = self.forward(sym, e) if e in day.expiries.get(sym, []) else day.spot[sym] * math.exp(self.r * max(T, 0))
        seen = self._last_seen(sym, e, k, cp)
        if seen:
            bid0, ask0, iv = seen
            mid0 = (bid0 + ask0) / 2
            half_frac = ((ask0 - bid0) / 2 / mid0) if mid0 > 0 else 0.05
        else:
            ivs = [v[2] for v in day.chain.get(sym, {}).values() if v[2] > 0]
            if not ivs:
                return None
            iv, half_frac = sorted(ivs)[len(ivs) // 2], 0.05
        px = black76.price(F, k, max(T, 0.0), iv, self.r, cp)
        half = max(self.tick, px * min(max(half_frac, 0.01), 0.25))
        bid = max(0.0, round(px - half, 2))
        ask = round(px + half, 2)
        self.modelled_quotes += 1
        return self._quote(round(px, 2), bid, ask, self.depth_lots * self.lot, modelled=True)

    def quote_raw(self, keys: Sequence[str]) -> Dict[str, dict]:
        day = self._day(self.clock.today())
        out: Dict[str, dict] = {}
        for key in keys:
            exch, _, tsym = key.partition(":")
            if exch == "NSE":
                S = day.spot.get(tsym)
                if S:
                    out[key] = self._quote(S, S, S, 10**7)
                continue
            m = _KEY.match(tsym)
            if not m:
                continue
            sym = m.group("sym")
            e = datetime.strptime(m.group("ymd"), "%y%m%d").date()
            rest = m.group("rest")
            if e < day.d:
                continue                                         # expired: not quotable
            if rest == "FUT":
                F = self.forward(sym, e)
                if F:
                    out[key] = self._quote(F, F, F, 10**7)
                continue
            k, cp = float(rest[:-3]), rest[-2:]
            hit = day.chain.get(sym, {}).get((e, k, cp))
            if hit and (hit[0] > 0 or hit[1] > 0):
                bid, ask, _iv = hit
                self.real_quotes += 1
                ex = (day.extras or {}).get((sym, e, k, cp))
                if ex:                       # recorded size: top of book, rest of the 5-level depth one tick out
                    out[key] = self._recorded_quote(bid, ask, ex)
                else:
                    out[key] = self._quote(round((bid + ask) / 2, 2), bid, ask, self.depth_lots * self.lot)
            else:
                q = self._model_option(sym, e, k, cp)
                if q:
                    out[key] = q
        return out

    def historical(self, instrument_token: int, from_dt: datetime, to_dt: datetime, interval: str) -> List[dict]:
        if interval != "day":
            return []
        sym = next((s for s in self.symbols if _token("EQ", s) == instrument_token), None)
        if sym is None:
            return []
        end = min(to_dt.date(), self.clock.today())
        rows = self.conn.execute("SELECT date, open, high, low, close, volume FROM bars WHERE symbol=? AND date BETWEEN ? "
                                 "AND ? ORDER BY date", (sym, str(from_dt.date()), str(end))).fetchall()
        today = self.clock.today()
        out = []
        for d, o, h, lo, c, v in rows:
            dd = date.fromisoformat(d)
            f = 1.0
            for ex, ratio in self._splits.get(sym, []):
                if dd < ex <= today:                     # adjust only for splits already happened
                    f *= ratio
            out.append({"date": datetime.combine(dd, time(0)), "open": o / f, "high": h / f, "low": lo / f,
                        "close": c / f, "volume": int((v or 0) * f), "oi": 0})
        return out

    def basket_margin(self, orders: List[dict]) -> float:
        """Approximation: a hedged pair needs its width x quantity (Reg-T style for spreads)."""
        legs = []
        for o in orders:
            m = _KEY.match(o["tradingsymbol"])
            rest = m.group("rest") if m else ""
            k = float(rest[:-3]) if rest and rest != "FUT" else 0.0
            legs.append((o["transaction_type"], k, o["quantity"]))
        sells = [x for x in legs if x[0] == "SELL"]
        buys = [x for x in legs if x[0] == "BUY"]
        total = 0.0
        for _, k, q in sells:
            hedge = next((b for b in buys if b[2] >= q), None)
            total += abs(k - hedge[1]) * q if hedge else 0.2 * k * q
        return round(total, 2)

    def positions(self) -> List[dict]:
        return []


class HistoricalExchange:
    """Exchange-side data for the backtest: earnings and split dates (visible `notice_days` before
    they happen), sectors from the universe file, and holidays as weekdays without bars."""

    def __init__(self, store_path: Path, clock: Clock, symbols: Sequence[str], sectors: Dict[str, str],
                 notice_days: int = 14):
        self.conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True, check_same_thread=False)
        self.clock, self.symbols, self._sectors, self.notice = clock, set(symbols), sectors, notice_days
        self.fail = False

    def events(self, start: date, end: date) -> List[dict]:
        today = self.clock.today()
        out = []
        has_seen = self.conn.execute("SELECT name FROM sqlite_master WHERE name='events_seen'").fetchone()
        if has_seen:                       # recorded live: exactly what was known, and since when
            for sym, d, typ, first in self.conn.execute(
                    "SELECT symbol, event_date, event_type, first_seen FROM events_seen WHERE event_date BETWEEN ? AND ?",
                    (str(start), str(end))):
                if sym in self.symbols and date.fromisoformat(first[:10]) <= today:
                    out.append({"symbol": sym, "event_date": date.fromisoformat(d), "event_type": typ,
                                "description": "recorded", "source": "nse"})
        for sym, d, timing in self.conn.execute("SELECT symbol, date, timing FROM earnings WHERE date BETWEEN ? AND ?",
                                                (str(start), str(end))):
            ed = date.fromisoformat(d)
            if sym in self.symbols and ed - timedelta(days=self.notice) <= today:
                out.append({"symbol": sym, "event_date": ed, "event_type": "results",
                            "description": f"earnings ({timing or 'time n/a'})", "source": "nse"})
        for sym, d in self.conn.execute("SELECT symbol, ex_date FROM splits WHERE ex_date BETWEEN ? AND ?",
                                        (str(start), str(end))):
            ed = date.fromisoformat(d)
            if sym in self.symbols and ed - timedelta(days=self.notice) <= today:
                out.append({"symbol": sym, "event_date": ed, "event_type": "split", "description": "stock split",
                            "source": "nse"})
        return out

    def sectors(self) -> Dict[str, str]:
        return dict(self._sectors)

    def holidays(self) -> List[Tuple[date, str]]:
        dates = sorted(date.fromisoformat(r[0]) for r in self.conn.execute("SELECT DISTINCT date FROM bars"))
        if not dates:
            return []
        have, out, d = set(dates), [], dates[0]
        while d <= dates[-1]:
            if d.weekday() < 5 and d not in have:
                out.append((d, "market holiday (no bars)"))
            d += timedelta(days=1)
        return out

    def option_turnover(self, d: date) -> Dict[str, float]:
        return {}


def read_universe(path: Path) -> Dict[str, str]:
    with path.open(newline="") as f:
        return {r["symbol"].strip().upper(): r["sector"].strip() for r in csv.DictReader(f)}
