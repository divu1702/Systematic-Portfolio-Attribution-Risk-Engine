"""Strikes, width, liquidity and return on margin (spec: Strikes, width and liquidity; Screener steps 2-3).

1. Short strike: the furthest strike beyond the level that still gives return on margin >= min_rom with
   the long strike from step 2. If none reaches it, the strict book skips the trade and the research
   book takes the nearest strike beyond the level.
2. Long strike: the first strike at least width_atr x ATR beyond the short strike that passes the
   liquidity check; if it fails, one strike further out.
3. Return on margin: net credit at natural prices (sell at the bid, buy at the ask) x quantity, less
   round-trip charges, divided by Zerodha's basket margin for the two legs together.
Liquidity (both legs): strike OI (quantity) x spot >= min_oi_value_inr, and (ask - bid) / mid <= max_spread_pct.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from platform_core.data_service import Leg

from .analytics import BEAR_CALL, own_type


@dataclass(frozen=True)
class Liquidity:
    ok: bool
    oi_value: float
    spread_pct: Optional[float]
    reason: str = ""


def liquidity(q, spot: float, cfg) -> Liquidity:
    if q is None:
        return Liquidity(False, 0.0, None, "no quote")
    oi_value = float(q.oi or 0) * spot
    sp = q.spread_pct
    fails = []
    if oi_value < cfg.liquidity.min_oi_value_inr:
        fails.append(f"OI x spot {oi_value / 1e7:.2f} cr < {cfg.liquidity.min_oi_value_inr / 1e7:.2f} cr")
    if sp is None:
        fails.append("no two-sided quote")
    elif sp > cfg.liquidity.max_spread_pct + 1e-12:
        fails.append(f"bid-ask {sp:.1%} > {cfg.liquidity.max_spread_pct:.0%}")
    return Liquidity(not fails, oi_value, sp, "; ".join(fails))


def beyond_strikes(strikes: Sequence[float], level: float, direction: str) -> List[float]:
    """Strikes beyond the level on the side the spread sells, nearest first."""
    ks = sorted(set(strikes))
    return [k for k in ks if k > level] if direction == BEAR_CALL else [k for k in ks if k < level][::-1]


def nearest_strike(strikes: Sequence[float], target: float, side: str) -> Optional[float]:
    """The listed strike nearest `target`, restricted to above (`side`="above") or below the reference."""
    ks = sorted(set(strikes))
    if not ks:
        return None
    return min(ks, key=lambda k: (abs(k - target), k if side == "above" else -k))


def width_target(short_k: float, direction: str, atr: float, width_atr: float) -> float:
    return short_k + width_atr * atr if direction == BEAR_CALL else short_k - width_atr * atr


def long_candidates(strikes: Sequence[float], short_k: float, direction: str, atr: float, width_atr: float) -> List[float]:
    """Strikes at least width_atr x ATR beyond the short strike, nearest first."""
    t = width_target(short_k, direction, atr, width_atr)
    ks = sorted(set(strikes))
    return [k for k in ks if k >= t - 1e-9] if direction == BEAR_CALL else [k for k in ks if k <= t + 1e-9][::-1]


@dataclass
class Priced:
    direction: str
    cp: str
    short_k: float
    long_k: float
    short_symbol: str
    long_symbol: str
    short_bid: float
    long_ask: float
    credit: float                 # per share, natural prices
    lot_size: int
    margin: float                 # one lot, basket margin
    round_trip: float             # one lot
    rom: Optional[float]
    margin_fallback: bool = False
    short_liq: Optional[Liquidity] = None
    long_liq: Optional[Liquidity] = None

    @property
    def width(self) -> float:
        return abs(self.long_k - self.short_k)

    @property
    def max_loss_lot(self) -> float:
        return (self.width - self.credit) * self.lot_size

    def as_dict(self) -> dict:
        return {"short": self.short_k, "long": self.long_k, "credit": round(self.credit, 2),
                "margin": round(self.margin, 2), "rom": None if self.rom is None else round(self.rom, 4),
                "max_loss_lot": round(self.max_loss_lot, 2), "lot": self.lot_size,
                "margin_fallback": self.margin_fallback}


def price_spread(ds, costs, chain, direction: str, short_k: float, long_k: float, quotes: Dict[str, object],
                 margin_fallback_ok: bool = False) -> Optional[Priced]:
    """Natural-price credit, round-trip charges and basket margin for one lot."""
    cp = own_type(direction)
    sl, ll = chain.leg(short_k, cp), chain.leg(long_k, cp)
    if sl is None or ll is None:
        return None
    sq, lq = quotes.get(sl.key), quotes.get(ll.key)
    if sq is None or lq is None or not sq.bid or not lq.ask:
        return None
    lot = chain.lot_size
    credit = sq.bid - lq.ask
    rt = costs.round_trip(sq.bid, lq.ask, lot)
    fallback = False
    if credit * lot - rt <= 0:
        margin, rom = 0.0, None
    else:
        try:
            margin = ds.margin([Leg(sl.tradingsymbol, short_k, cp, "SELL", lot), Leg(ll.tradingsymbol, long_k, cp, "BUY", lot)],
                               {sl.tradingsymbol: sq.bid, ll.tradingsymbol: lq.ask})
        except Exception:
            if not margin_fallback_ok:
                raise
            margin, fallback = abs(long_k - short_k) * lot - credit * lot, True
        rom = (credit * lot - rt) / margin if margin > 0 else None
    return Priced(direction, cp, short_k, long_k, sl.tradingsymbol, ll.tradingsymbol, sq.bid, lq.ask, credit, lot,
                  margin, rt, rom, fallback)


@dataclass
class StrikeChoice:
    best: Optional[Priced]                 # furthest short strike with RoM >= min_rom
    nearest: Optional[Priced]              # the nearest strike beyond the level (the research fallback)
    tried: List[dict] = field(default_factory=list)
    reason: str = ""


def long_for(chain, strikes, short_k, direction, atr, cfg, quotes, spot) -> Tuple[Optional[float], List[str]]:
    """Step 2: the first strike >= width_atr x ATR beyond the short that passes liquidity, else one further."""
    notes = []
    cp = own_type(direction)
    for k in long_candidates(strikes, short_k, direction, atr, cfg.strikes.width_atr)[:2]:
        leg = chain.leg(k, cp)
        liq = liquidity(quotes.get(leg.key) if leg else None, spot, cfg)
        if liq.ok:
            return k, notes
        notes.append(f"long {k:g}: {liq.reason}")
    return None, notes


def choose(ds, costs, chain, quotes, *, level: float, direction: str, atr: float, spot: float, cfg,
           max_candidates: int = 15) -> StrikeChoice:
    """Spec steps 1-2 for a confirmed setup."""
    cp = own_type(direction)
    strikes = chain.strikes
    tried, best, nearest = [], None, None
    for k in beyond_strikes(strikes, level, direction)[:max_candidates]:
        leg = chain.leg(k, cp)
        sliq = liquidity(quotes.get(leg.key) if leg else None, spot, cfg)
        if not sliq.ok:
            tried.append({"short": k, "skip": f"short {sliq.reason}"})
            continue
        lk, notes = long_for(chain, strikes, k, direction, atr, cfg, quotes, spot)
        if lk is None:
            tried.append({"short": k, "skip": "; ".join(notes) or "no long strike far enough out"})
            continue
        p = price_spread(ds, costs, chain, direction, k, lk, quotes, margin_fallback_ok=True)
        if p is None:
            tried.append({"short": k, "long": lk, "skip": "no two-sided quotes"})
            continue
        p.short_liq = sliq
        p.long_liq = liquidity(quotes.get(chain.leg(lk, cp).key), spot, cfg)
        tried.append({**p.as_dict()})
        if nearest is None:
            nearest = p
        if p.rom is not None and p.rom >= cfg.strikes.min_rom - 1e-12:
            best = p                                  # keep going: a further strike may still clear the hurdle
        if p.credit <= 0:
            break                                     # further out only pays less
    reason = "" if best else (f"no strike beyond the level reaches {cfg.strikes.min_rom:.0%} return on margin"
                              if nearest else "no liquid strike beyond the level")
    return StrikeChoice(best, nearest, tried, reason)


def reference(ds, costs, chain, quotes, *, spot: float, atr: float, direction: str, cfg) -> Tuple[Optional[Priced], dict]:
    """Screener step 3: short strike nearest 1 x ATR from spot (above for the bear call, below for the
    bull put), long strike the first at least width_atr x ATR beyond it, natural prices, basket margin."""
    strikes = chain.strikes
    if direction == BEAR_CALL:
        side = [k for k in strikes if k > spot]
        target = spot + atr
    else:
        side = [k for k in strikes if k < spot]
        target = spot - atr
    short_k = nearest_strike(side, target, "above" if direction == BEAR_CALL else "below")
    if short_k is None:
        return None, {"reason": "no strike on that side"}
    longs = long_candidates(strikes, short_k, direction, atr, cfg.strikes.width_atr)
    if not longs:
        return None, {"short": short_k, "reason": "no strike far enough out for the long leg"}
    p = price_spread(ds, costs, chain, direction, short_k, longs[0], quotes, margin_fallback_ok=True)
    if p is None:
        return None, {"short": short_k, "long": longs[0], "reason": "no two-sided quotes"}
    return p, p.as_dict()
