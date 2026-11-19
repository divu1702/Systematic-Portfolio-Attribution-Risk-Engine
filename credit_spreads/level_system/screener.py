"""The daily screener (spec: Screener), run at 15:20 on the snapshot.

1. Event check: remove a stock with any scheduled corporate event between today and the expiry the
   15-day rule picks; stale event data (older than event_staleness_days) removes every stock.
2. Liquidity check: remove a stock whose strikes nearest 1 x ATR from spot (call side above, put side
   below) fail the liquidity rules.
3. ROI ranking: a reference spread on each side (short strike nearest 1 x ATR from spot, long strike the
   first at least 1.5 x ATR beyond it), natural prices, the 15-day-rule expiry, basket margin. Rank by
   the higher of the two returns on margin; both are shown.
4. Setup scan: done by the engine on the stocks that remain.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from platform_core.models import ScreenerRow

from . import strikes as K
from .analytics import BEAR_CALL, BULL_PUT, atr_before

log = logging.getLogger(__name__)


@dataclass
class StockRow:
    symbol: str
    spot: Optional[float] = None
    atr: Optional[float] = None
    expiry: Optional[date] = None
    sector: Optional[str] = None
    event_ok: bool = False
    liquidity_ok: bool = False
    events: List[str] = field(default_factory=list)
    reason: str = ""
    chain: object = None
    bars: object = None
    rom_call: Optional[float] = None
    rom_put: Optional[float] = None
    ref: Dict[str, dict] = field(default_factory=dict)
    liq: Dict[str, dict] = field(default_factory=dict)
    rank: Optional[int] = None
    setups: List[str] = field(default_factory=list)

    @property
    def remaining(self) -> bool:
        return self.event_ok and self.liquidity_ok

    @property
    def best_rom(self) -> Optional[float]:
        vals = [v for v in (self.rom_call, self.rom_put) if v is not None]
        return max(vals) if vals else None

    def detail(self) -> dict:
        return {"spot": self.spot, "atr": self.atr, "expiry": self.expiry.isoformat() if self.expiry else None,
                "sector": self.sector, "events": self.events, "reason": self.reason, "reference": self.ref,
                "liquidity": self.liq, "setups": self.setups}


def filter_stocks(eng, snap, stocks: List[str], spots: Dict[str, object], today: date) -> List[StockRow]:
    """Steps 1 and 2. Fetches the two near-1-ATR strikes and the reference long strikes in one batch."""
    cfg, ds = eng.cfg, eng.ds
    fresh = ds.events_fresh()
    rows: List[StockRow] = []
    wanted: List[str] = []
    for sym in stocks:
        r = StockRow(sym)
        rows.append(r)
        q = spots.get(sym)
        if q is None or not q.last:
            r.reason = "no live quote"
            continue
        r.spot = float(q.last)
        r.expiry, _ = ds.expiry_for(sym, today)
        if r.expiry is None:
            r.reason = "no listed expiry"
            continue
        if not fresh:
            r.reason = f"event data older than {cfg.screener.event_staleness_days} days"
        else:
            evs = ds.events_between(sym, today, r.expiry)
            r.events = [f"{e.event_date} {e.event_type}" for e in evs]
            r.event_ok = not evs
            if evs:
                r.reason = "event before expiry: " + ", ".join(r.events)
        bars = eng.bars_with_session(sym, q, today)
        r.bars = bars
        r.atr = atr_before(bars, today, cfg.atr_period) if len(bars) else None
        if r.atr is None:
            r.reason = r.reason or "not enough daily history for the ATR"
            r.event_ok = False
            continue
        try:
            r.chain = ds.chain(sym, r.expiry)
        except LookupError:
            r.reason = r.reason or "no option chain"
            r.event_ok = False
            continue
        for direction in (BEAR_CALL, BULL_PUT):
            short_k, long_k = _ref_strikes(r, direction, cfg)
            cp = "CE" if direction == BEAR_CALL else "PE"
            for k in (short_k, long_k):
                leg = r.chain.leg(k, cp) if k is not None else None
                if leg:
                    wanted.append(leg.key)
        if r.chain.future:
            wanted.append(r.chain.future.key)
    quotes = snap.get(wanted)
    for r in rows:
        if r.chain is None or r.atr is None:
            continue
        ok = True
        for direction in (BEAR_CALL, BULL_PUT):
            short_k, _ = _ref_strikes(r, direction, cfg)
            cp = "CE" if direction == BEAR_CALL else "PE"
            leg = r.chain.leg(short_k, cp) if short_k is not None else None
            liq = K.liquidity(quotes.get(leg.key) if leg else None, r.spot, cfg)
            r.liq[direction] = {"strike": short_k, "ok": liq.ok, "oi_value_cr": round(liq.oi_value / 1e7, 2),
                                "spread_pct": liq.spread_pct, "reason": liq.reason}
            ok = ok and liq.ok
        r.liquidity_ok = ok
        if not ok and not r.reason:
            r.reason = "illiquid near 1 x ATR: " + "; ".join(f"{d}: {v['reason']}" for d, v in r.liq.items() if not v["ok"])
        snap.register_chain(r.chain)
    return rows


def _ref_strikes(r: StockRow, direction: str, cfg):
    strikes = r.chain.strikes
    if direction == BEAR_CALL:
        short_k = K.nearest_strike([k for k in strikes if k > r.spot], r.spot + r.atr, "above")
    else:
        short_k = K.nearest_strike([k for k in strikes if k < r.spot], r.spot - r.atr, "below")
    if short_k is None:
        return None, None
    longs = K.long_candidates(strikes, short_k, direction, r.atr, cfg.strikes.width_atr)
    return short_k, (longs[0] if longs else None)


def rank(eng, snap, rows: List[StockRow]) -> List[StockRow]:
    """Step 3 on the remaining stocks: reference spreads both sides, ranked by the higher RoM."""
    for r in rows:
        if not r.remaining:
            continue
        for direction in (BEAR_CALL, BULL_PUT):
            try:
                p, info = K.reference(eng.ds, eng.costs, r.chain, snap.q, spot=r.spot, atr=r.atr,
                                      direction=direction, cfg=eng.cfg)
            except Exception as exc:                       # one stock's margin call must not stop the list
                log.warning("reference spread for %s %s failed: %s", r.symbol, direction, exc)
                p, info = None, {"reason": str(exc)}
            r.ref[direction] = info
            if p is not None:
                if direction == BEAR_CALL:
                    r.rom_call = p.rom
                else:
                    r.rom_put = p.rom
    ranked = sorted([r for r in rows if r.remaining and r.best_rom is not None], key=lambda r: -r.best_rom)
    for i, r in enumerate(ranked, 1):
        r.rank = i
    return ranked


def save(pengine, d: date, rows: List[StockRow]) -> None:
    with Session(pengine) as s:
        for r in rows:
            stmt = insert(ScreenerRow).values(date=d, symbol=r.symbol, event_flag=r.event_ok, liquidity_flag=r.liquidity_ok,
                                              rom_call=r.rom_call, rom_put=r.rom_put, rank=r.rank,
                                              detail_json=json.dumps(r.detail(), default=str))
            s.execute(stmt.on_conflict_do_update(index_elements=["date", "symbol"], set_={
                c: getattr(stmt.excluded, c) for c in ("event_flag", "liquidity_flag", "rom_call", "rom_put", "rank",
                                                       "detail_json")}))
        s.commit()
