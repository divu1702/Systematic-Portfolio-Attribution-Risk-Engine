"""Deterministic synthetic NSE stock-options market for tests, demos and the simulator.

It is NOT real data. Tickers are made up (STK01..), prices are generated from a seed. It exists
so the whole pipeline can run offline and so that tests are reproducible. What it models:

- 5-minute spot paths per trading day (75 bars, 09:15-15:30), overnight gaps, vol clustering.
- Price shocks followed by an IV spike that decays (the setup the strategy looks for), and
  quarterly results dates with a pre-results IV run-up and a post-results move.
- Monthly expiries on the last Tuesday, strike grids fixed at contract launch, Kite-format
  tradingsymbols, lot sizes, tick size 0.05.
- Option quotes priced with Black-76 off a skewed smile, bid/ask spreads that widen away from
  the money, 5-level depth, volume and OI in shares. Some names are deliberately illiquid.
- One index (SYNIDX) with options, which the system must exclude (index options are out of scope).

Data is never served beyond the clock's current time (no look-ahead).
"""

from __future__ import annotations

import calendar
import math
import re
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..analytics import black76
from ..clock import Clock

BARS = 75                      # 5-minute bars per session, 09:15-15:30
INTRADAY_MINUTES = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15,
                    "30minute": 30, "60minute": 60}
BAR = timedelta(minutes=5)
OPEN_T = time(9, 15)
CLOSE_T = time(15, 30)
TICK = 0.05
SECTORS = ["Banks", "IT", "Auto", "Pharma", "Metals", "Energy", "FMCG", "Capital Goods"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
FIXED_HOLIDAYS = [(1, 26), (8, 15), (10, 2), (12, 25)]   # national holidays, if on a weekday
INDEX = "SYNIDX"
_KEY_RE = re.compile(
    r"^(?P<name>[A-Z0-9]+?)(?P<yy>\d{2})(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<rest>.+)$")


def _crc(*parts) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode())


def round_tick(x: float, up: bool) -> float:
    n = math.ceil(x / TICK - 1e-9) if up else math.floor(x / TICK + 1e-9)
    return round(n * TICK, 2)


def strike_step(price: float) -> float:
    for limit, step in ((100, 1), (250, 2.5), (500, 5), (1000, 10), (2500, 20), (5000, 50)):
        if price < limit:
            return step
    return 100


def last_tuesday(y: int, m: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def fmt_strike(k: float) -> str:
    return str(int(k)) if float(k).is_integer() else f"{k:g}"


@dataclass
class SymbolSpec:
    name: str
    sector: str
    s0: float
    base_vol: float
    iv_premium: float
    lot: int
    spread_base: float
    liq_lots: float               # ATM daily volume in lots
    is_index: bool = False
    results: List[date] = field(default_factory=list)
    shocks: Dict[int, Tuple[float, int]] = field(default_factory=dict)  # day idx -> (jump, bar)


@dataclass(frozen=True)
class InstrumentInfo:
    token: int
    exchange: str
    tradingsymbol: str
    name: str
    kind: str                     # EQ, FUT, CE, PE, INDEX
    expiry: Optional[date]
    strike: Optional[float]
    lot: int


class SyntheticMarket:
    def __init__(self, seed: int = 7, n_symbols: int = 20, anchor: Optional[date] = None,
                 history_days: int = 620, future_days: int = 260, r: float = 0.065):
        self.seed, self.r = seed, r
        anchor = anchor or date.today()
        self.days: List[date] = self._calendar(anchor, history_days, future_days)
        self.day_index: Dict[date, int] = {d: i for i, d in enumerate(self.days)}
        rng = np.random.default_rng(seed)
        self.specs: List[SymbolSpec] = [self._make_spec(i, rng) for i in range(n_symbols)]
        self.specs.append(SymbolSpec(INDEX, "Index", 24000.0, 0.14, 1.1, 75, 0.004, 50000, is_index=True))
        self.paths: Dict[str, np.ndarray] = {}
        self.ivs: Dict[str, np.ndarray] = {}
        for k, spec in enumerate(self.specs):
            self._generate(spec, np.random.default_rng(seed * 1000 + k))
        self.expiries: List[date] = sorted({self._expiry_on_trading_day(last_tuesday(d.year, d.month))
                                            for d in self.days})
        self._instruments: Dict[int, InstrumentInfo] = {}
        self._by_symbol: Dict[str, InstrumentInfo] = {}
        self._strike_grid_cache: Dict[Tuple[str, date], List[float]] = {}

    # ------------------------------------------------------------------ construction
    def _calendar(self, anchor: date, back: int, fwd: int) -> List[date]:
        def ok(d: date) -> bool:
            return d.weekday() < 5 and (d.month, d.day) not in FIXED_HOLIDAYS
        days, d = [], anchor
        while len(days) < back:
            if ok(d):
                days.append(d)
            d -= timedelta(days=1)
        days.reverse()
        d, n = anchor + timedelta(days=1), 0
        while n < fwd:
            if ok(d):
                days.append(d)
                n += 1
            d += timedelta(days=1)
        return days

    def holidays(self) -> List[date]:
        out, d, end = [], self.days[0], self.days[-1]
        while d <= end:
            if d.weekday() < 5 and d not in self.day_index:
                out.append(d)
            d += timedelta(days=1)
        return out

    def _expiry_on_trading_day(self, d: date) -> date:
        while d not in self.day_index and d > self.days[0]:
            d -= timedelta(days=1)
        return d

    def _make_spec(self, i: int, rng: np.random.Generator) -> SymbolSpec:
        s0 = float(np.exp(rng.uniform(np.log(150), np.log(3000))))
        illiquid = rng.random() < 0.25
        lot = max(25, int(round(700_000 / s0 / 25.0)) * 25)
        spec = SymbolSpec(
            name=f"STK{i + 1:02d}",
            sector=SECTORS[i % len(SECTORS)],
            s0=s0,
            base_vol=float(rng.uniform(0.22, 0.42)),
            iv_premium=float(rng.uniform(1.03, 1.12)),
            lot=lot,
            spread_base=float(rng.uniform(0.05, 0.09) if illiquid else rng.uniform(0.008, 0.02)),
            liq_lots=float(rng.uniform(80, 300) if illiquid else rng.uniform(1500, 6000)),
        )
        # Quarterly results, announced to the exchange 7 trading days ahead.
        first = self.days[0] + timedelta(days=int(rng.integers(0, 91)))
        d = first
        while d <= self.days[-1]:
            spec.results.append(self._expiry_on_trading_day(d) if d in self.day_index else self._next_day(d))
            d += timedelta(days=int(91 + rng.integers(-6, 7)))
        # Non-results shocks, roughly every 45 sessions.
        n = len(self.days)
        for idx in range(30, n):
            if rng.random() < 1 / 45:
                jump = float(rng.choice([-1, 1]) * rng.uniform(0.045, 0.085))
                bar = 0 if rng.random() < 0.5 else int(rng.integers(5, BARS - 5))
                spec.shocks[idx] = (jump, bar)
        return spec

    def _next_day(self, d: date) -> date:
        while d not in self.day_index:
            d += timedelta(days=1)
            if d > self.days[-1]:
                return self.days[-1]
        return d

    def _generate(self, spec: SymbolSpec, rng: np.random.Generator) -> None:
        n = len(self.days)
        result_idx = {self.day_index[d] for d in spec.results if d in self.day_index}
        for ri in result_idx:
            if ri not in spec.shocks:
                spec.shocks[ri] = (float(rng.choice([-1, 1]) * rng.uniform(0.03, 0.07)), 0)
        path = np.empty((n, BARS + 1))
        iv = np.empty(n)
        regime, iv_spike = 1.0, 0.0
        prev_close = spec.s0
        per_year = 252 * BARS
        for d in range(n):
            if d in spec.shocks:
                regime, iv_spike = 1.8, float(rng.uniform(0.55, 0.85))
            sigma_day = spec.base_vol * regime
            bar_sig = sigma_day / math.sqrt(per_year)
            gap = rng.normal(0, sigma_day / math.sqrt(252) * 0.35)
            jump, jbar = spec.shocks.get(d, (0.0, -1))
            opening = prev_close * math.exp(gap + (jump if jbar == 0 else 0.0))
            steps = rng.normal(-0.5 * bar_sig ** 2, bar_sig, BARS)
            if jbar > 0:
                steps[jbar - 1] += jump
            path[d, 0] = opening
            path[d, 1:] = opening * np.exp(np.cumsum(steps))
            prev_close = path[d, -1]
            # IV: premium over realised, spike after shocks, run-up into results, crush after.
            days_to_result = min((ri - d for ri in result_idx if ri >= d), default=99)
            pre = 0.18 * max(0, 10 - days_to_result) / 10 if days_to_result > 0 else 0.0
            iv[d] = spec.base_vol * spec.iv_premium * (1 + iv_spike + pre) * float(rng.uniform(0.97, 1.03))
            regime = 1.0 + (regime - 1.0) * 0.9
            iv_spike *= 0.87
        self.paths[spec.name] = path
        self.ivs[spec.name] = iv

    # ------------------------------------------------------------------ time helpers
    def locate(self, ts: datetime) -> Tuple[int, int]:
        """(day index, bar index) of the latest price at or before ts."""
        d = ts.date()
        if d < self.days[0]:
            raise ValueError(f"{ts} is before the synthetic history starts ({self.days[0]})")
        if d in self.day_index:
            di = self.day_index[d]
            open_dt = datetime.combine(d, OPEN_T)
            if ts < open_dt:
                return (di - 1, BARS) if di > 0 else (0, 0)
            j = min(BARS, int((ts - open_dt) / BAR))
            return di, j
        # Weekend/holiday: last close.
        while d not in self.day_index:
            d -= timedelta(days=1)
        return self.day_index[d], BARS

    def bar_time(self, di: int, j: int) -> datetime:
        return datetime.combine(self.days[di], OPEN_T) + j * BAR

    def spec(self, name: str) -> SymbolSpec:
        for s in self.specs:
            if s.name == name:
                return s
        raise KeyError(name)

    def spot(self, name: str, ts: datetime) -> float:
        di, j = self.locate(ts)
        return float(self.paths[name][di, j])

    def atm_vol(self, name: str, ts: datetime) -> float:
        di, j = self.locate(ts)
        spec = self.spec(name)
        jump, jbar = spec.shocks.get(di, (0.0, -1))
        if jbar > 0 and j < jbar and di > 0:
            return float(self.ivs[name][di - 1])
        return float(self.ivs[name][di])

    def smile(self, name: str, ts: datetime, F: float, K: float, T: float) -> float:
        atm = self.atm_vol(name, ts)
        m = math.log(K / F) / max(atm * math.sqrt(max(T, 1e-4)), 1e-6)
        return max(0.05, atm * (1 - 0.06 * m + 0.012 * m * m))

    # ------------------------------------------------------------------ instruments
    def listed_expiries(self, d: date, n: int = 3) -> List[date]:
        return [e for e in self.expiries if e >= d][:n]

    def strike_grid(self, name: str, expiry: date, d: Optional[date] = None) -> List[float]:
        """Strikes listed for an expiry as of day d: the step is fixed at contract launch (about
        three months before expiry) and strikes are added as the stock moves, never removed."""
        ei = self.day_index[expiry]
        launch = max(0, ei - 63)
        di = self.day_index.get(d, ei) if d else ei
        di = max(launch, min(di, ei))
        key = (name, expiry, di)
        if key not in self._strike_grid_cache:
            closes = self.paths[name][launch:di + 1, -1]
            ref = float(self.paths[name][launch, 0])
            step = strike_step(ref)
            lo = math.ceil(min(ref, float(closes.min())) * 0.6 / step)
            hi = math.floor(max(ref, float(closes.max())) * 1.4 / step)
            self._strike_grid_cache[key] = [float(round(i * step, 2)) for i in range(max(lo, 1), hi + 1)]
        return self._strike_grid_cache[key]

    def _register(self, info: InstrumentInfo) -> InstrumentInfo:
        self._instruments[info.token] = info
        self._by_symbol[f"{info.exchange}:{info.tradingsymbol}"] = info
        return info

    def instruments(self, exchange: str, d: date) -> List[dict]:
        rows = []
        for spec in self.specs:
            if exchange == "NSE":
                kind = "INDEX" if spec.is_index else "EQ"
                ts_name = "SYNTHETIC INDEX" if spec.is_index else spec.name
                info = self._register(InstrumentInfo(_crc("NSE", spec.name) % 10**7, "NSE", ts_name,
                                                     spec.name, kind, None, None, 1))
                rows.append(self._kite_row(info, "INDICES" if spec.is_index else "NSE", spec))
                continue
            for e in self.listed_expiries(d):
                yy, mon = f"{e.year % 100:02d}", MONTHS[e.month - 1]
                fut = self._register(InstrumentInfo(_crc("FUT", spec.name, e) % 10**8, "NFO",
                                                    f"{spec.name}{yy}{mon}FUT", spec.name, "FUT", e, None, spec.lot))
                rows.append(self._kite_row(fut, "NFO-FUT", spec))
                for k in self.strike_grid(spec.name, e, d):
                    for cp in ("CE", "PE"):
                        info = self._register(InstrumentInfo(
                            _crc("OPT", spec.name, e, fmt_strike(k), cp) % 10**8, "NFO",
                            f"{spec.name}{yy}{mon}{fmt_strike(k)}{cp}", spec.name, cp, e, k, spec.lot))
                        rows.append(self._kite_row(info, "NFO-OPT", spec))
        return rows

    @staticmethod
    def _kite_row(info: InstrumentInfo, segment: str, spec: SymbolSpec) -> dict:
        return {
            "instrument_token": info.token, "exchange_token": str(info.token % 100000),
            "tradingsymbol": info.tradingsymbol,
            "name": spec.name if info.kind != "INDEX" else INDEX,
            "last_price": 0.0, "expiry": info.expiry, "strike": float(info.strike or 0.0),
            "tick_size": TICK, "lot_size": info.lot,
            "instrument_type": "EQ" if info.kind == "INDEX" else info.kind,
            "segment": segment, "exchange": info.exchange,
        }

    def info(self, key_or_token) -> InstrumentInfo:
        if isinstance(key_or_token, int):
            if key_or_token not in self._instruments:
                raise KeyError(f"unknown instrument token {key_or_token}")
            return self._instruments[key_or_token]
        if key_or_token not in self._by_symbol:
            self._register(self._parse_key(key_or_token))
        return self._by_symbol[key_or_token]

    def _parse_key(self, key: str) -> InstrumentInfo:
        """Rebuild an instrument from its "EXCHANGE:TRADINGSYMBOL" key (works for expired ones)."""
        exch, _, sym = key.partition(":")
        names = {s.name for s in self.specs}
        if exch == "NSE":
            if sym in names:
                return InstrumentInfo(_crc("NSE", sym) % 10**7, "NSE", sym, sym, "EQ", None, None, 1)
            if sym == "SYNTHETIC INDEX":
                return InstrumentInfo(_crc("NSE", INDEX) % 10**7, "NSE", sym, INDEX, "INDEX", None, None, 1)
        m = _KEY_RE.match(sym) if exch == "NFO" else None
        if not m or m.group("name") not in names:
            raise KeyError(f"unknown instrument {key!r}")
        name, yy, mon, rest = m.group("name"), int(m.group("yy")), m.group("mon"), m.group("rest")
        month = MONTHS.index(mon) + 1
        expiry = next((e for e in self.expiries if e.year % 100 == yy and e.month == month), None)
        if expiry is None:
            raise KeyError(f"no synthetic expiry for {key!r}")
        lot = self.spec(name).lot
        if rest == "FUT":
            return InstrumentInfo(_crc("FUT", name, expiry) % 10**8, "NFO", sym, name, "FUT", expiry, None, lot)
        cp, k = rest[-2:], float(rest[:-2])
        return InstrumentInfo(_crc("OPT", name, expiry, fmt_strike(k), cp) % 10**8, "NFO", sym, name, cp, expiry, k, lot)

    # ------------------------------------------------------------------ pricing
    def price_at(self, info: InstrumentInfo, di: int, j: int) -> float:
        spec = self.spec(info.name)
        S = float(self.paths[info.name][di, j])
        if info.kind in ("EQ", "INDEX"):
            return S
        ts = self.bar_time(di, j)
        T = black76.year_fraction((datetime.combine(info.expiry, CLOSE_T) - ts).total_seconds())
        F = S * math.exp(self.r * T)
        if info.kind == "FUT":
            return F
        sigma = self.smile(spec.name, ts, F, info.strike, T)
        return black76.price(F, info.strike, T, sigma, self.r, info.kind)

    def quote(self, key: str, ts: datetime) -> dict:
        info = self.info(key)
        spec = self.spec(info.name)
        di, j = self.locate(ts)
        px = self.price_at(info, di, j)
        rng = np.random.default_rng(_crc(self.seed, key, di, j))
        frac = max(j, 1) / BARS
        if info.kind in ("EQ", "INDEX", "FUT"):
            half = max(TICK, px * 0.0002)
            vol_lots, oi_lots, depth_lots = spec.liq_lots * 4, spec.liq_lots * 20, spec.liq_lots / 10
        else:
            S = float(self.paths[info.name][di, j])
            T = max(black76.year_fraction((datetime.combine(info.expiry, CLOSE_T) - ts).total_seconds()), 1e-4)
            atm = self.atm_vol(info.name, ts)
            z = abs(math.log(info.strike / S)) / (atm * math.sqrt(T))
            half = max(TICK, px * spec.spread_base * (1 + 0.6 * z) / 2)
            liq = math.exp(-0.5 * (z / 1.4) ** 2)
            vol_lots, oi_lots = spec.liq_lots * liq, spec.liq_lots * 3 * liq
            depth_lots = max(1.0, spec.liq_lots * liq / 400)
        bid = round_tick(px - half, up=False)
        ask = round_tick(px + half, up=True)
        if bid < TICK or px < 0.5 * TICK:
            bid = 0.0
        if ask <= bid:
            ask = round(bid + TICK, 2)
        lot = info.lot if info.kind not in ("EQ", "INDEX") else 1
        buy, sell = [], []
        for lvl in range(5):
            q_b = int(max(1, round(depth_lots * rng.uniform(0.5, 2.0)))) * lot
            q_s = int(max(1, round(depth_lots * rng.uniform(0.5, 2.0)))) * lot
            if bid > 0 and bid - lvl * TICK > 0:
                buy.append({"price": round(bid - lvl * TICK, 2), "quantity": q_b, "orders": int(rng.integers(1, 9))})
            sell.append({"price": round(ask + lvl * TICK, 2), "quantity": q_s, "orders": int(rng.integers(1, 9))})
        day_open = self.price_at(info, di, 0)
        return {
            "instrument_token": info.token, "timestamp": ts, "last_trade_time": ts,
            "last_price": round(round_tick(px, up=False), 2),
            "volume": int(vol_lots * frac) * lot, "oi": int(oi_lots) * lot,
            "buy_quantity": int(sum(lv["quantity"] for lv in buy) * rng.uniform(1.5, 6)),
            "sell_quantity": int(sum(lv["quantity"] for lv in sell) * rng.uniform(1.5, 6)),
            "last_quantity": lot * int(rng.integers(1, 6)),
            "ohlc": {"open": round(day_open, 2), "high": round(max(day_open, px), 2),
                     "low": round(min(day_open, px), 2), "close": round(self.price_at(info, max(di - 1, 0), BARS), 2)},
            "depth": {"buy": buy, "sell": sell},
        }

    def candles(self, token: int, start: datetime, end: datetime, interval: str, now: datetime) -> List[dict]:
        info = self.info(token)
        end = min(end, now)
        out: List[dict] = []
        if start.date() < self.days[0]:
            start = datetime.combine(self.days[0], time(0))
        if interval == "day":
            for d in self.days:
                if d < start.date() or d > end.date():
                    continue
                di = self.day_index[d]
                last_bar = BARS if datetime.combine(d, CLOSE_T) <= now else self.locate(now)[1]
                if last_bar == 0 and d == now.date():
                    continue
                if info.expiry and d > info.expiry:
                    break
                if info.kind in ("EQ", "INDEX"):
                    prices = list(self.paths[info.name][di, :last_bar + 1])
                else:
                    prices = [self.price_at(info, di, j) for j in range(last_bar + 1)]
                # Bars are 5-minute closes; widen slightly for the range traded between them.
                intrabar = 1.002
                out.append({"date": datetime.combine(d, time(0)), "open": round(prices[0], 2),
                            "high": round(max(prices) * intrabar, 2), "low": round(min(prices) / intrabar, 2),
                            "close": round(prices[-1], 2),
                            "volume": int(self.spec(info.name).liq_lots * 4) * info.lot, "oi": 0})
            return out
        step = INTRADAY_MINUTES.get(interval)
        if step is None:
            raise ValueError(f"synthetic market has no {interval!r} candles")
        lot = info.lot if info.kind not in ("EQ", "INDEX") else 1
        spec = self.spec(info.name)
        session = BARS * 5
        for d in self.days:
            if d < start.date() or d > end.date():
                continue
            if info.expiry and d > info.expiry:
                break
            di = self.day_index[d]
            closes = [self.price_at(info, di, j) for j in range(BARS + 1)]        # 5-minute marks
            path = np.interp(np.arange(session + 1), np.arange(0, session + 1, 5), closes)
            act = self.activity(info, di)
            traded = (np.random.default_rng(_crc(self.seed, "trades", info.token, di)).random(session) < act
                      if interval == "minute" else np.ones(session, bool))        # Kite: no candle without a trade
            vol = int(max(1, round(spec.liq_lots * act * step / 75))) * lot
            oi = (int(spec.liq_lots * 3 * act) if info.kind in ("CE", "PE")
                  else int(spec.liq_lots * 20) if info.kind == "FUT" else 0) * lot
            for m0 in range(0, session, step):
                t0 = datetime.combine(d, OPEN_T) + timedelta(minutes=m0)
                if t0 < start or t0 + timedelta(minutes=step) > end or not traded[m0]:
                    continue
                seg = path[m0:m0 + step + 1]
                out.append({"date": t0, "open": round(float(seg[0]), 2), "high": round(float(seg.max()), 2),
                            "low": round(float(seg.min()), 2), "close": round(float(seg[-1]), 2),
                            "volume": vol, "oi": oi})
        return out

    def activity(self, info: InstrumentInfo, di: int) -> float:
        """Share of minutes an instrument trades: stocks and futures always, options less the further
        they are from the money (like the quote's liquidity)."""
        if info.kind not in ("CE", "PE"):
            return 1.0
        S = float(self.paths[info.name][di, 0])
        ts = self.bar_time(di, 0)
        T = max(black76.year_fraction((datetime.combine(info.expiry, CLOSE_T) - ts).total_seconds()), 1e-4)
        z = abs(math.log(info.strike / S)) / (self.atm_vol(info.name, ts) * math.sqrt(T))
        return min(1.0, 0.02 + 0.98 * math.exp(-(z / 0.6) ** 2))

    # ------------------------------------------------------------------ exchange-side data
    def announced_events(self, as_of: date, ahead_days: int = 60, behind_days: int = 120) -> List[dict]:
        """Results board meetings visible on `as_of` (announced 7 trading days ahead)."""
        out = []
        for spec in self.specs:
            if spec.is_index:
                continue
            for rd in spec.results:
                if rd not in self.day_index:
                    continue
                ann_idx = max(0, self.day_index[rd] - 7)
                if self.days[ann_idx] > as_of:
                    continue
                if as_of - timedelta(days=behind_days) <= rd <= as_of + timedelta(days=ahead_days):
                    out.append({"symbol": spec.name, "date": rd, "purpose": "Financial Results",
                                "announced": self.days[ann_idx]})
        return out

    def sectors(self) -> Dict[str, str]:
        return {s.name: s.sector for s in self.specs if not s.is_index}

    def option_turnover(self, d: date) -> Dict[str, float]:
        """Daily stock-option premium turnover in rupees (bhavcopy stand-in)."""
        if d not in self.day_index:
            return {}
        di = self.day_index[d]
        out = {}
        for s in self.specs:
            if s.is_index:
                continue
            S = float(self.paths[s.name][di, -1])
            noise = np.random.default_rng(_crc(self.seed, "turn", s.name, di)).uniform(0.7, 1.3)
            out[s.name] = s.liq_lots * s.lot * S * 0.02 * 40 * noise
        return out

