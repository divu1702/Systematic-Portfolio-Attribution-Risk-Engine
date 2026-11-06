import math

import numpy as np
import pandas as pd
import pytest

from app.analytics import black76 as b76
from app.analytics import vol
from app.config import CostsCfg
from app.strategy.costs import (CostModelIncomplete, entry_costs, exit_costs, order_cost,
                                round_trip_costs)
from app.strategy.spreads import BEAR_CALL, BULL_PUT, spread_metrics


# ----------------------------------------------------------------------------- Black-76

def test_black76_atm_call_closed_form():
    # F=K, r=0: C = F * (2N(sigma*sqrt(T)/2) - 1)
    expected = 100 * (2 * b76.norm_cdf(0.1) - 1)
    assert b76.price(100, 100, 1.0, 0.2, 0.0, "CE") == pytest.approx(expected, rel=1e-12)
    assert expected == pytest.approx(7.9656, abs=1e-4)


def test_black76_hull_futures_put_example():
    # Hull, Options Futures and Other Derivatives: European put on futures,
    # F=20, K=20, r=9%, T=4 months, sigma=25% -> about 1.12
    assert b76.price(20, 20, 4 / 12, 0.25, 0.09, "PE") == pytest.approx(1.1166, abs=1e-4)


@pytest.mark.parametrize("F,K,T,s,r", [(584, 600, 25 / 365, 0.45, 0.065), (1500, 1400, 0.1, 0.3, 0.07),
                                       (100, 130, 0.05, 0.8, 0.0)])
def test_put_call_parity(F, K, T, s, r):
    c = b76.price(F, K, T, s, r, "CE")
    p = b76.price(F, K, T, s, r, "PE")
    assert c - p == pytest.approx(math.exp(-r * T) * (F - K), abs=1e-9)


@pytest.mark.parametrize("cp", ["CE", "PE"])
@pytest.mark.parametrize("K", [500, 584, 650])
def test_implied_vol_roundtrip(cp, K):
    F, T, r = 584.0, 25 / 365, 0.065
    for sigma in (0.12, 0.35, 0.9):
        px = b76.price(F, K, T, sigma, r, cp)
        if px < 1e-6:
            continue
        assert b76.implied_vol(px, F, K, T, r, cp) == pytest.approx(sigma, abs=1e-6)


def test_implied_vol_rejects_impossible_prices():
    F, K, T, r = 100, 90, 0.1, 0.05
    intrinsic = math.exp(-r * T) * 10
    assert b76.implied_vol(intrinsic - 0.01, F, K, T, r, "CE") is None
    assert b76.implied_vol(F, F, K, T, r, "CE") is None
    assert b76.implied_vol(1.0, F, K, 0.0, r, "CE") is None
    assert b76.implied_vol(0.0, F, K, T, r, "PE") is None


@pytest.mark.parametrize("cp", ["CE", "PE"])
def test_greeks_match_finite_differences(cp):
    F, K, T, s, r = 584, 600, 25 / 365, 0.45, 0.065
    g = b76.greeks(F, K, T, s, r, cp)
    h = 0.01
    p = lambda f=F, sig=s, t=T: b76.price(f, K, t, sig, r, cp)  # noqa: E731
    assert g.delta == pytest.approx((p(f=F + h) - p(f=F - h)) / (2 * h), rel=1e-5)
    assert g.gamma == pytest.approx((p(f=F + h) - 2 * p() + p(f=F - h)) / h ** 2, rel=1e-3)
    assert g.vega == pytest.approx((p(sig=s + 1e-5) - p(sig=s - 1e-5)) / 2e-5, rel=1e-5)
    # theta is dV/dt with t = calendar time passing, i.e. -dV/dT
    assert g.theta == pytest.approx(-(p(t=T + 1e-6) - p(t=T - 1e-6)) / 2e-6, rel=1e-4)
    assert (0 < g.delta < 1) if cp == "CE" else (-1 < g.delta < 0)


def test_prob_above_is_n_d2_and_complements():
    F, X, T, s = 584, 611, 25 / 365, 0.45
    p = b76.prob_above(F, X, T, s)
    _, d2 = b76.d1_d2(F, X, T, s)
    assert p == pytest.approx(b76.norm_cdf(d2))
    assert 0 < p < 0.5
    assert b76.prob_above(F, F * 10, T, s) < 1e-6
    assert b76.prob_above(F, 0, T, s) == 1.0


# ----------------------------------------------------------------------------- realised vol

def test_hv_matches_numpy():
    rng = np.random.default_rng(1)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 60))))
    h = vol.hv(close, 20, 252)
    rets = np.diff(np.log(close.to_numpy()))[-20:]
    assert h.iloc[-1] == pytest.approx(np.std(rets, ddof=1) * math.sqrt(252))
    assert h.iloc[:20].isna().all() and not math.isnan(h.iloc[20])


def test_hv_of_constant_growth_is_zero():
    close = pd.Series(100 * 1.01 ** np.arange(30))
    assert vol.hv(close, 20).iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_atr_wilder_hand_computed():
    high = pd.Series([10, 11, 12, 11, 13], dtype=float)
    low = pd.Series([9, 10, 10, 10, 11], dtype=float)
    close = pd.Series([9.5, 10.5, 11.5, 10.5, 12.5])
    # TR: 1, 1.5, 2, 1.5, 2.5 ; window 3 -> seed mean(1,1.5,2)=1.5, then (1.5*2+1.5)/3=1.5,
    # then (1.5*2+2.5)/3 = 1.8333
    a = vol.atr(high, low, close, 3)
    assert a.iloc[:2].isna().all()
    assert a.iloc[2] == pytest.approx(1.5)
    assert a.iloc[3] == pytest.approx(1.5)
    assert a.iloc[4] == pytest.approx(5.5 / 3)


def test_percentiles():
    assert vol.percentile_rank([1, 2, 3, 4], 3.5) == 75.0
    assert vol.percentile_rank([], 1) is None
    assert vol.percentile_value(list(range(101)), 80) == pytest.approx(80)
    s = pd.Series([1.0, 2, 3, 4, 0.5])
    r = vol.rolling_percentile_rank(s, 3)
    assert math.isnan(r.iloc[0])
    assert r.iloc[3] == 100.0      # 4 vs [2, 3]
    assert r.iloc[4] == 0.0        # 0.5 vs [3, 4]


# ----------------------------------------------------------------------------- surface

def _chain(F, T, r, smile):
    out = []
    for K in range(500, 680, 10):
        for cp in ("CE", "PE"):
            s = smile(K)
            px = b76.price(F, K, T, s, r, cp)
            if px < 0.05:
                continue
            out.append(vol.option_iv(px * 0.99, px * 1.01, F, K, T, r, cp))
    return out


def test_atm_iv_interpolates_two_nearest_strikes():
    F, T, r = 584.0, 25 / 365, 0.065
    ivs = _chain(F, T, r, lambda K: 0.30 + 0.001 * (K - 580))
    # nearest strikes 580 (0.30) and 590 (0.31) -> 0.304 at 584
    assert vol.atm_iv(ivs, F) == pytest.approx(0.304, abs=2e-4)


def test_zero_bid_quote_is_skipped():
    o = vol.option_iv(0, 5, 584, 600, 0.07, 0.065, "CE")
    assert o.iv is None


def test_25_delta_iv():
    F, T, r = 584.0, 25 / 365, 0.065
    ivs = _chain(F, T, r, lambda K: 0.40)
    assert vol.delta_iv(ivs, 0.25, "PE") == pytest.approx(0.40, abs=1e-4)
    assert vol.delta_iv(ivs, 0.25, "CE") == pytest.approx(0.40, abs=1e-4)


# ----------------------------------------------------------------------------- payoff maths

def test_kalyan_worked_example():
    """Spec: KALYANKJIL at 584, sell 600C at 21, buy 640C at 10, lot 1,350."""
    m = spread_metrics(BEAR_CALL, 600, 640, 21, 10, 1350)
    assert m.credit == 11
    assert m.credit_lot == 14_850
    assert m.width == 40
    assert m.max_loss == 39_150
    assert m.breakeven == 611
    assert m.return_on_max_loss == pytest.approx(0.379, abs=1e-3)   # "38%"
    assert m.breakeven_win_rate == pytest.approx(0.725, abs=1e-4)


def test_bull_put_metrics_and_validation():
    m = spread_metrics(BULL_PUT, 500, 480, 8, 3, 1000)
    assert m.credit == 5 and m.max_loss == 15_000 and m.breakeven == 495
    with pytest.raises(ValueError):
        spread_metrics(BULL_PUT, 500, 520, 8, 3, 1000)
    with pytest.raises(ValueError):
        spread_metrics(BEAR_CALL, 600, 580, 8, 3, 1000)


# ----------------------------------------------------------------------------- costs

def test_order_cost_sell_leg_hand_computed():
    c = CostsCfg()
    oc = order_cost(c, "SELL", 21, 1350)          # turnover 28,350
    assert oc.brokerage == 20
    assert oc.stt == pytest.approx(42.525)
    assert oc.exchange == pytest.approx(28_350 * 0.0003503)
    assert oc.sebi == pytest.approx(0.02835)
    assert oc.stamp == 0
    assert oc.gst == pytest.approx(0.18 * (20 + 28_350 * 0.0003503 + 0.02835))
    assert oc.total == pytest.approx(77.877, abs=1e-3)


def test_buy_leg_has_stamp_not_stt():
    oc = order_cost(CostsCfg(), "BUY", 10, 1350)
    assert oc.stt == 0 and oc.stamp == pytest.approx(13_500 * 0.00003)


def test_round_trip_costs_kalyan():
    c = CostsCfg()
    rt = round_trip_costs(c, 21, 10, 1350)
    assert rt == pytest.approx(entry_costs(c, 21, 10, 1350) + exit_costs(c, 21, 10, 1350))
    # four orders of brokerage plus GST on it at minimum
    assert rt > 4 * 20 * 1.18
    # Credit after round-trip costs, per share
    m = spread_metrics(BEAR_CALL, 600, 640, 21, 10, 1350, costs_per_share=rt / 1350)
    assert 10.7 < m.credit < 11


def test_cost_model_refuses_when_rate_missing():
    with pytest.raises(CostModelIncomplete, match="exchange_txn_pct"):
        order_cost(CostsCfg(exchange_txn_pct=None), "SELL", 10, 100)
