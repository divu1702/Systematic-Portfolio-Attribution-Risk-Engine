"""Trading rules of the level system on hand-built chains: liquidity, the short strike as far beyond the
level as the 10% return-on-margin floor allows, the long strike 1.5 x ATR beyond it, the research
fallback, the screener's reference spread, and sizing with the gap caps and portfolio limits."""

from types import SimpleNamespace

import pytest

from level_system import sizing, strikes as K
from level_system.analytics import BEAR_CALL, BULL_PUT
from platform_core.config import load_platform_config
from platform_core.costs import CostModel

CFG = load_platform_config()
COSTS = CostModel(CFG.costs, CFG.settlement)


class Q:
    def __init__(self, bid, ask, oi=200_000):
        self.bid, self.ask, self.oi, self.last = bid, ask, oi, (bid + ask) / 2

    @property
    def mid(self):
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask >= self.bid else None

    @property
    def spread_pct(self):
        m = self.mid
        return (self.ask - self.bid) / m if m else None


class Chain:
    def __init__(self, strikes, lot=100, tick=0.05):
        self.lot_size, self.tick_size, self.underlying = lot, tick, "XYZ"
        self.legs = {(float(k), cp): SimpleNamespace(tradingsymbol=f"XYZ{k}{cp}", strike=float(k), cp=cp,
                                                     key=f"NFO:XYZ{k}{cp}") for k in strikes for cp in ("CE", "PE")}
        self.future = None

    @property
    def strikes(self):
        return sorted({k for k, _ in self.legs})

    def leg(self, k, cp):
        return self.legs.get((round(k, 2), cp))


class DS:
    """Basket margin = width x quantity (a plain stand-in for Zerodha's figure)."""

    def __init__(self):
        self.calls = 0

    def margin(self, legs, prices=None):
        self.calls += 1
        s, b = (next(lg for lg in legs if lg.side == side) for side in ("SELL", "BUY"))
        return abs(b.strike - s.strike) * s.qty


def call_quotes(chain, prices, oi=200_000, spread=0.02):
    """prices: strike -> mid premium of the call."""
    out = {}
    for k, px in prices.items():
        half = max(px * spread / 2, 0.05)
        out[chain.leg(k, "CE").key] = Q(round(px - half, 2), round(px + half, 2), oi)
    return out


def test_liquidity_rules():
    assert K.liquidity(Q(9.9, 10.1, oi=100_000), 300.0, CFG).ok                   # 3.0 cr, spread 2%
    thin = K.liquidity(Q(9.9, 10.1, oi=99_000), 300.0, CFG)
    assert not thin.ok and "OI x spot 2.97 cr" in thin.reason
    wide = K.liquidity(Q(9.0, 10.0, oi=500_000), 300.0, CFG)
    assert not wide.ok and "bid-ask 10.5%" in wide.reason
    assert not K.liquidity(None, 300.0, CFG).ok and not K.liquidity(Q(0, 1.0), 300.0, CFG).ok


def test_short_strike_is_the_furthest_that_clears_ten_percent():
    chain = Chain(range(300, 460, 10))
    # Bear call, level 342, ATR 20 -> long strike 30 beyond the short (1.5 x 20).
    prices = {350: 12.0, 360: 8.0, 370: 5.4, 380: 3.6, 390: 2.3, 400: 1.5, 410: 0.9, 420: 0.55, 430: 0.3, 440: 0.2, 450: 0.1}
    q = call_quotes(chain, prices)
    ds = DS()
    c = K.choose(ds, COSTS, chain, q, level=342.0, direction=BEAR_CALL, atr=20.0, spot=330.0, cfg=CFG)
    assert c.nearest.short_k == 350 and c.nearest.long_k == 380
    assert c.best is not None and c.best.rom >= 0.10
    for t in c.tried:                                   # everything further out than `best` is below 10%
        if t.get("rom") is not None and t["short"] > c.best.short_k:
            assert t["rom"] < 0.10
    credit = (q[chain.leg(c.best.short_k, "CE").key].bid - q[chain.leg(c.best.long_k, "CE").key].ask)
    want = (credit * 100 - COSTS.round_trip(q[chain.leg(c.best.short_k, "CE").key].bid,
                                            q[chain.leg(c.best.long_k, "CE").key].ask, 100)) / ((c.best.long_k - c.best.short_k) * 100)
    assert c.best.rom == pytest.approx(want)


def test_research_fallback_when_nothing_clears_the_hurdle():
    chain = Chain(range(300, 460, 10))
    prices = {k: 4.0 - (k - 350) / 50 for k in range(350, 460, 10)}           # liquid, but the credit is thin
    c = K.choose(DS(), COSTS, chain, call_quotes(chain, prices), level=342.0, direction=BEAR_CALL, atr=20.0,
                 spot=330.0, cfg=CFG)
    assert c.best is None and c.nearest.short_k == 350 and "10% return on margin" in c.reason


def test_long_strike_moves_one_further_when_illiquid():
    chain = Chain(range(300, 460, 10))
    prices = {350: 12.0, 360: 8.0, 370: 5.4, 380: 3.6, 390: 2.3, 400: 1.5, 410: 0.9}
    q = call_quotes(chain, prices)
    q[chain.leg(380, "CE").key] = Q(3.0, 4.2)                                     # 33% wide: fails
    k, notes = K.long_for(chain, chain.strikes, 350.0, BEAR_CALL, 20.0, CFG, q, 330.0)
    assert k == 390 and "long 380" in notes[0]
    q[chain.leg(390, "CE").key] = Q(2.0, 2.8)
    assert K.long_for(chain, chain.strikes, 350.0, BEAR_CALL, 20.0, CFG, q, 330.0)[0] is None


def test_put_side_and_reference_spread():
    chain = Chain(range(200, 420, 10))
    assert K.beyond_strikes(chain.strikes, 305.0, BULL_PUT)[:3] == [300.0, 290.0, 280.0]
    assert K.long_candidates(chain.strikes, 300.0, BULL_PUT, 20.0, 1.5)[:2] == [270.0, 260.0]
    # Reference spread: spot 300, ATR 20 -> calls: short nearest 320, long first >= 350.
    prices = {320: 6.0, 350: 1.8}
    p, info = K.reference(DS(), COSTS, chain, call_quotes(chain, prices), spot=300.0, atr=20.0, direction=BEAR_CALL, cfg=CFG)
    assert (p.short_k, p.long_k) == (320.0, 350.0) and info["rom"] == pytest.approx(p.rom, abs=1e-4)
    assert p.margin == 30 * 100 and p.max_loss_lot == pytest.approx((30 - p.credit) * 100)


def test_sizing_on_the_stop_and_the_caps():
    s = sizing.size(loss_per_lot=7_000, max_loss_lot=20_000, capital=500_000, cfg=CFG, book="strict")
    assert s.lots == 3 and s.one_r == 21_000                                       # floor(25,000 / 7,000)
    capped = sizing.size(loss_per_lot=5_000, max_loss_lot=30_000, capital=500_000, cfg=CFG, book="strict")
    assert capped.lots == 2 and any("gap cap" in n for n in capped.notes)          # 5 lots -> 75,000 / 30,000
    assert sizing.size(loss_per_lot=30_000, max_loss_lot=40_000, capital=500_000, cfg=CFG, book="strict").lots == 0
    assert sizing.size(loss_per_lot=None, max_loss_lot=1, capital=500_000, cfg=CFG, book="strict").lots == 0
    assert sizing.size(loss_per_lot=-5, max_loss_lot=1, capital=500_000, cfg=CFG, book="strict").lots == 0


def test_portfolio_limits_apply_to_the_strict_book_only():
    T = lambda sector, ml: SimpleNamespace(sector=sector, max_loss=ml)            # noqa: E731
    three = [T("Banks", 10_000), T("IT", 10_000), T("Metals", 10_000)]
    full = sizing.size(loss_per_lot=5_000, max_loss_lot=10_000, capital=500_000, cfg=CFG, book="strict", open_trades=three,
                       sector="Auto")
    assert full.lots == 0 and "3 positions open" in full.reason
    same = sizing.size(loss_per_lot=5_000, max_loss_lot=10_000, capital=500_000, cfg=CFG, book="strict",
                       open_trades=[T("Banks", 10_000)], sector="Banks")
    assert same.lots == 0 and "sector Banks" in same.reason
    unknown = sizing.size(loss_per_lot=5_000, max_loss_lot=10_000, capital=500_000, cfg=CFG, book="strict",
                          open_trades=[T(None, 10_000)], sector=None)
    assert unknown.lots == 0 and "unknown sectors" in unknown.reason
    room = sizing.size(loss_per_lot=5_000, max_loss_lot=30_000, capital=500_000, cfg=CFG, book="strict",
                       open_trades=[T("IT", 70_000), T("Banks", 30_000)], sector="Auto")
    assert room.lots == 0 or room.lots * 30_000 <= 125_000 - 100_000                # 25% combined cap
    research = sizing.size(loss_per_lot=5_000, max_loss_lot=10_000, capital=500_000, cfg=CFG, book="research",
                           open_trades=three + [T("Auto", 100_000)], sector="Auto")
    assert research.lots == 5                                                       # exempt from portfolio limits
