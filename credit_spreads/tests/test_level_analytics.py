"""Level-system analytics against hand-computed values: ATR, average volume, major and minor swings,
trend, VWAP, the high-volume node, zone strikes, zone OI share, local PCR, the zone OI test and the
Black-76 loss at the kill level."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.analytics import black76
from level_system import analytics as A
from level_system.analytics import BEAR_CALL, BULL_PUT


def frame(highs, lows=None, closes=None, volumes=None, start=date(2026, 1, 1)):
    n = len(highs)
    lows = lows if lows is not None else [h - 1 for h in highs]
    closes = closes if closes is not None else [(h + lo) / 2 for h, lo in zip(highs, lows)]
    volumes = volumes if volumes is not None else [1000] * n
    idx = [start + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=idx)


def test_atr_uses_completed_sessions_only():
    highs, lows, closes = [101.0] * 14 + [105.0, 120.0], [99.0] * 14 + [96.0, 80.0], [100.0] * 16
    bars = frame(highs, lows, closes)
    # True range 2 for 14 sessions (Wilder seed = 2), then 9: (2 x 13 + 9) / 14 = 2.5.
    assert A.atr_before(bars, bars.index[15]) == pytest.approx(2.5)
    assert A.atr_before(bars, bars.index[14]) == pytest.approx(2.0)      # the big day itself is not used
    assert A.atr_before(bars, bars.index[13]) is None                    # fewer than 14 sessions


def test_average_volume_and_multiple():
    bars = frame([10.0] * 25, volumes=list(range(1, 26)))
    d = bars.index[20]
    assert A.avg_volume(bars, d) == pytest.approx(10.5)                  # sessions 1..20, Day 0 excluded
    assert A.volume_multiple(bars, d) == pytest.approx(21 / 10.5)
    assert A.avg_volume(bars, bars.index[19]) is None                    # only 19 sessions before


def test_swings_confirmation_lookback_and_breaks():
    highs = [1, 2, 3, 2, 1, 5, 1, 2, 3, 4, 3, 2, 1]
    bars = frame([float(h) for h in highs], closes=[h - 0.5 for h in highs])
    hs = A.swings(bars, 2, "high")
    assert [s.pos for s in hs] == [2, 5, 9] and [s.price for s in hs] == [3, 5, 4]
    assert [s.pos for s in A.swings(bars, 2, "high", end=bars.index[11])] == [2, 5]   # 9 not yet confirmed
    assert [s.pos for s in A.swings(bars, 2, "high", unbroken=True)] == [5, 9]        # a close of 4.5 broke 3
    assert [s.pos for s in A.swings(bars, 2, "high", lookback=5)] == [9]
    lows = frame([10.0] * 13, lows=[10.0 - h for h in highs], closes=[10.0 - h + 0.5 for h in highs])
    assert [(s.pos, s.price) for s in A.swings(lows, 2, "low")] == [(2, 7.0), (5, 5.0), (9, 6.0)]
    assert [s.pos for s in A.swings(lows, 2, "low", unbroken=True)] == [5, 9]
    flat = frame([1.0, 3.0, 3.0, 1.0, 0.5])
    assert [s.pos for s in A.swings(flat, 1, "high")] == [1]             # a flat double top counts once
    with pytest.raises(ValueError):
        A.swings(bars, 2, "middle")


def test_trend_from_minor_swings():
    up = frame([1.0, 3, 2, 4, 3, 5, 4], lows=[0.0, 2, 1, 3, 2, 4, 3])
    assert A.trend(up, side_bars=1) == "up"
    down = frame([5.0, 3, 4, 2, 3, 1, 2], lows=[4.0, 2, 3, 1, 2, 0, 1])
    assert A.trend(down, side_bars=1) == "down"
    mixed = frame([1.0, 3, 2, 4, 3, 5, 4], lows=[3.0, 2, 3, 1, 2, 0, 1])
    assert A.trend(mixed, side_bars=1) == "neutral"
    assert A.trend(frame([1.0, 2.0]), side_bars=1) == "neutral"
    assert A.trend_agrees("up", BULL_PUT) is True and A.trend_agrees("up", BEAR_CALL) is False
    assert A.trend_agrees("down", BEAR_CALL) is True and A.trend_agrees("neutral", BULL_PUT) is None


def test_vwap_and_high_volume_node():
    candles = [{"high": 10, "low": 8, "close": 9, "volume": 100}, {"high": 12, "low": 10, "close": 11, "volume": 300},
               {"high": 50, "low": 40, "close": 45, "volume": 0}]
    assert A.vwap(candles) == pytest.approx((9 * 100 + 11 * 300) / 400)
    assert A.vwap([{"high": 1, "low": 1, "close": 1, "volume": 0}]) is None
    node = [{"high": 10.2, "low": 10.2, "close": 10.2, "volume": 100}, {"high": 10.7, "low": 10.7, "close": 10.7, "volume": 50},
            {"high": 11.4, "low": 11.4, "close": 11.4, "volume": 120}]
    assert A.hvn(node, 1.0) == pytest.approx(10.5)                       # bucket 10-11 holds 150 shares
    assert A.hvn(node, 0.5) == pytest.approx(11.25)                      # buckets 10.0-10.5 (100), 11.0-11.5 (120)
    tie = [{"high": 1.2, "low": 1.2, "close": 1.2, "volume": 5}, {"high": 2.2, "low": 2.2, "close": 2.2, "volume": 5}]
    assert A.hvn(tie, 1.0) == pytest.approx(1.5)                         # ties go to the lower bucket
    assert A.hvn([], 1.0) is None and A.dist_atr(None, 1, 1) is None
    assert A.dist_atr(105.0, 100.0, 2.5) == pytest.approx(2.0)


def test_zone_strikes():
    tens = [float(k) for k in range(300, 430, 10)]
    # ANGELONE, 17 Nov 2026: level 360.45, Day 0 high 379.15 -> protected 360-380, other side 350, 340, 330.
    assert A.setup1_zone(BEAR_CALL, 360.45, 379.15, tens) == ([360.0, 370.0, 380.0], [350.0, 340.0, 330.0])
    fives = [float(k) for k in range(300, 425, 5)]
    assert A.setup1_zone(BEAR_CALL, 360.45, 379.15, fives)[0] == [360.0, 365.0, 370.0, 375.0, 380.0]
    sixes = [float(k) for k in range(500, 700, 10)]
    assert A.setup1_zone(BULL_PUT, 600.5, 585.2, sixes) == ([610.0, 600.0, 590.0, 580.0], [620.0, 630.0, 640.0])
    # Kalyan Jewellers: level 648.95, bull put -> protected 640, 630, 620; other side 650, 660, 670.
    kal = [float(k) for k in range(600, 710, 10)]
    assert A.level_zone(BULL_PUT, 648.95, kal) == ([640.0, 630.0, 620.0], [650.0, 660.0, 670.0])
    assert A.level_zone(BEAR_CALL, 650.0, kal) == ([660.0, 670.0, 680.0], [650.0, 640.0, 630.0])
    assert A.setup1_zone(BEAR_CALL, 360.45, 999.0, tens) == ([], [])     # no listed strike above the high


def test_zone_oi_share_pcr_and_test():
    oi = {(640.0, "PE"): 100_000, (630.0, "PE"): 80_000, (620.0, "PE"): 60_000,
          (650.0, "CE"): 50_000, (660.0, "CE"): 40_000, (670.0, "CE"): 30_000,
          (640.0, "CE"): 10_000, (650.0, "PE"): 20_000, (700.0, "CE"): 500_000}
    prot, other = [640.0, 630.0, 620.0], [650.0, 660.0, 670.0]
    z0 = A.zone_oi(oi, BULL_PUT, prot, other)
    assert (z0.protected, z0.other) == (240_000, 120_000)
    assert z0.calls == 130_000 and z0.puts == 260_000
    assert z0.share == pytest.approx(390 / 890) and z0.local_pcr == pytest.approx(2.0)

    def now(protected, other_side):
        m = dict(oi)
        m[(640.0, "PE")] += protected - 240_000
        m[(650.0, "CE")] += other_side - 120_000
        return A.zone_oi(m, BULL_PUT, prot, other)

    ok = A.oi_test(z0, now(220_000, 130_000))
    assert ok.passed and ok.protected_change == pytest.approx(-1 / 12) and ok.other_change == pytest.approx(1 / 12)
    assert A.oi_test(z0, now(216_000, 132_000)).passed                    # exactly -10% and +10% pass
    low = A.oi_test(z0, now(210_000, 120_000))
    assert not low.passed and "protected-side OI -12.5%" in low.reason
    high = A.oi_test(z0, now(240_000, 140_000))
    assert not high.passed and "other-side OI +16.7%" in high.reason
    gone = A.zone_oi({k: v for k, v in oi.items() if k != (630.0, "PE")}, BULL_PUT, prot, other)
    assert not A.oi_test(z0, gone).passed and "630PE" in A.oi_test(z0, gone).reason
    empty0 = A.zone_oi({(640.0, "PE"): 0, (630.0, "PE"): 0, (620.0, "PE"): 0, (650.0, "CE"): 0, (660.0, "CE"): 0,
                        (670.0, "CE"): 0}, BULL_PUT, prot, other)
    assert not A.oi_test(empty0, now(240_000, 120_000)).passed            # new OI on the other side is a rise
    assert empty0.share is None and empty0.local_pcr is None


def test_years_to_expiry():
    now = datetime(2026, 11, 17, 15, 30)
    assert A.years_to_expiry(now, date(2026, 11, 24)) == pytest.approx(7 / 365)
    assert A.years_to_expiry(now, date(2026, 11, 24), less_days=1) == pytest.approx(6 / 365)
    assert A.years_to_expiry(now, date(2026, 11, 17)) == 0


def q(bid, ask):
    return SimpleNamespace(bid=bid, ask=ask, mid=(bid + ask) / 2)


def test_loss_at_kill_level_by_hand():
    from platform_core.config import CostsCfg, SettlementCfg
    from platform_core.costs import CostModel

    cm = CostModel(CostsCfg(), SettlementCfg())
    now, expiry = datetime(2026, 11, 17, 15, 20), date(2026, 12, 29)
    spot, fut, level, r, lot = 350.0, 351.5, 360.45, 0.065, 2500
    sq, lq = q(6.0, 6.4), q(2.4, 2.7)
    credit = 6.0 - 2.7

    def charges(s, lg):
        return cm.close_spread(s, lg, lot).total

    k = A.loss_at_kill(level=level, spot=spot, future=fut, now=now, expiry=expiry, cp="CE", short_k=380.0,
                       long_k=400.0, short_q=sq, long_q=lq, credit=credit, lot_size=lot, r=r, cushion_points=5,
                       exit_charges_per_lot=charges)
    T = (datetime(2026, 12, 29, 15, 30) - now).total_seconds() / (365 * 86400)
    s_iv = black76.implied_vol(6.2, fut, 380.0, T, r, "CE")
    l_iv = black76.implied_vol(2.55, fut, 400.0, T, r, "CE")
    F = level * fut / spot
    s_px = black76.price(F, 380.0, T - 1 / 365, s_iv + 0.05, r, "CE") + 0.2
    l_px = black76.price(F, 400.0, T - 1 / 365, l_iv + 0.05, r, "CE") - 0.15
    assert k.short_iv == pytest.approx(s_iv) and k.long_iv == pytest.approx(l_iv)
    assert k.forward == pytest.approx(F) and k.T == pytest.approx(T - 1 / 365)
    assert k.cost_to_close == pytest.approx(s_px - l_px)
    assert k.per_lot == pytest.approx((s_px - l_px - credit) * lot + charges(s_px, l_px))
    assert k.per_lot > 0
    no_cushion = A.loss_at_kill(level=level, spot=spot, future=fut, now=now, expiry=expiry, cp="CE", short_k=380.0,
                                long_k=400.0, short_q=sq, long_q=lq, credit=credit, lot_size=lot, r=r,
                                cushion_points=0, exit_charges_per_lot=charges)
    assert no_cushion.per_lot < k.per_lot                                 # the IV cushion makes the stop dearer
    assert A.loss_at_kill(level=level, spot=spot, future=fut, now=now, expiry=now.date(), cp="CE", short_k=380.0,
                          long_k=400.0, short_q=sq, long_q=lq, credit=credit, lot_size=lot, r=r, cushion_points=5,
                          exit_charges_per_lot=charges) is None
