import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select

from app.broker.base import Level, Quote
from app.clock import SimClock
from app.config import load_config
from app.context import build_context
from app.data.levels_store import StoredLevel
from app.data.models import Candidate, IVSnapshot
from app.jobs import morning_refresh, scan_job
from app.strategy import ev
from app.strategy.scanner import STAGES, choose_direction, liquidity_ok, run_scan
from app.strategy.spreads import BEAR_CALL, BULL_PUT
from app.views import scan_report
from tests.conftest import SIM_NOW, relaxed_ctx, shared_market


# ----------------------------------------------------------------------------- EV maths

def test_win_prob_sides_are_complements():
    p_put = ev.win_prob(BULL_PUT, 100, 95, 0.08, 0.3)
    p_call = ev.win_prob(BEAR_CALL, 100, 95, 0.08, 0.3)
    assert p_put + p_call == pytest.approx(1.0)
    assert p_put > 0.5


def test_forecast_vol_blend():
    assert ev.forecast_vol(0.2, 0.4, 0.5) == pytest.approx(0.3)
    assert ev.forecast_vol(0.2, 0.4, 0.0) == pytest.approx(0.4)


def test_avg_loss_bounded_by_stop_and_max_loss():
    # credit 5, width 20: max loss 15, stop loss (2-1)*5 = 5
    a = ev.avg_loss_given_loss(BULL_PUT, 100, 95, 0.08, 0.35, 5, 20, 2.0)
    assert 0 < a <= 5
    # wide stop -> capped at max loss
    b = ev.avg_loss_given_loss(BEAR_CALL, 100, 105, 0.08, 0.35, 5, 20, 100.0)
    assert 5 < b <= 15


def test_evaluate_kalyan_shape():
    r = ev.evaluate(BEAR_CALL, 584, 611, 25 / 365, 0.45, 0.30, 11, 40, 1350, 0.5, 2.0)
    assert r.p_model > r.p_market            # lower forecast vol -> higher win chance for a seller
    assert r.ev == pytest.approx((r.p_model * 11 - (1 - r.p_model) * r.avg_loss) * 1350)
    assert r.ev_ratio == pytest.approx(r.ev / 39_150)


# ----------------------------------------------------------------------------- direction / liquidity

def _lv(price, score):
    return StoredLevel(price, "", "daily", 3, score, "auto")


def test_choose_direction():
    atr = 10
    side, lv = choose_direction([_lv(95, 2), _lv(150, 9)], 100, atr, 1.5)
    assert side == BULL_PUT and lv.price == 95
    side, lv = choose_direction([_lv(90, 1), _lv(108, 3)], 100, atr, 1.5)
    assert side == BEAR_CALL and lv.price == 108          # both in range, resistance scores higher
    side, lv = choose_direction([_lv(95, 5), _lv(108, 3)], 100, atr, 1.5)
    assert side == BULL_PUT
    assert choose_direction([_lv(50, 9)], 100, atr, 1.5) == (None, None)


def _quote(bid, ask, oi, volume):
    return Quote("NFO:X", None, bid, (Level(bid, 100),), (Level(ask, 100),), volume, oi)


def test_liquidity_rules(cfg):
    from types import SimpleNamespace

    c = SimpleNamespace(cfg=cfg)
    lot = 100
    assert liquidity_ok(c, _quote(10, 10.4, 0, 0), lot) == (True, "")        # OI/volume off by default
    assert not liquidity_ok(c, _quote(10, 11, 60_000, 6_000), lot)[0]         # 9.5% wide
    assert not liquidity_ok(c, _quote(0, 10.4, 60_000, 6_000), lot)[0]
    strict = SimpleNamespace(cfg=cfg.model_copy(update={"liquidity": cfg.liquidity.model_copy(
        update={"min_oi_lots": 500, "min_vol_lots": 50})}))
    assert "OI" in liquidity_ok(strict, _quote(10, 10.4, 40_000, 6_000), lot)[1]   # 400 lots
    assert "volume" in liquidity_ok(strict, _quote(10, 10.4, 60_000, 4_000), lot)[1]


def test_depth_must_cover_order_both_sides(cfg):
    from types import SimpleNamespace

    from app.strategy.scanner import depth_ok

    c = SimpleNamespace(cfg=cfg)
    deep = Quote("NFO:A", None, 10, tuple(Level(10 - i * 0.05, 300) for i in range(5)),
                 tuple(Level(10.1 + i * 0.05, 300) for i in range(5)), 0, 0)
    thin_ask = Quote("NFO:B", None, 5, deep.bids, (Level(5.1, 200),), 0, 0)
    assert depth_ok(c, deep, deep, 1500) == (True, "")
    ok, why = depth_ok(c, deep, thin_ask, 1000)
    assert not ok and "long leg depth" in why


# ----------------------------------------------------------------------------- scan on the mock

def _ctx_at(cfg, tmp_path, when=SIM_NOW, name="scan.db"):
    return build_context(cfg, mock=True, clock=SimClock(when), db_path=tmp_path / name, env={},
                         market=shared_market())


def test_default_universe_is_every_fno_stock(cfg, tmp_path):
    ctx = _ctx_at(cfg, tmp_path)
    morning_refresh(ctx)
    res = run_scan(ctx)
    assert res.funnel["F1 universe"] == 0 and len(res.outcomes) == 8


def test_scan_without_turnover_rejects_everything_at_f1(raw_config, write_config, tmp_path):
    raw_config["universe"]["top_n"] = 5                  # ranking mode needs bhavcopy turnover
    cfg = load_config(write_config(raw_config))
    ctx = _ctx_at(cfg, tmp_path)
    morning_refresh(ctx)
    res = run_scan(ctx)
    assert res.funnel["F1 universe"] == 8 and not res.ranked
    with ctx.engine.connect() as conn:
        rows = conn.execute(select(Candidate.reject_reason, Candidate.f1_pass)).all()
    assert len(rows) == 8 and all(r.f1_pass is False and "no option turnover" in r.reject_reason for r in rows)


def test_full_scan_persists_candidates_and_snapshots(cfg, tmp_path):
    ctx = _ctx_at(cfg, tmp_path)
    morning_refresh(ctx)
    res = scan_job(ctx)
    assert sum(res.funnel.values()) == 8
    assert list(res.funnel) == STAGES
    with ctx.engine.connect() as conn:
        snaps = conn.execute(select(func.count()).select_from(IVSnapshot)).scalar()
        n = conn.execute(select(func.count()).select_from(Candidate)).scalar()
        methods = {r[0] for r in conn.execute(select(IVSnapshot.method))}
        reasons = [r[0] for r in conn.execute(select(Candidate.reject_reason)) if r[0]]
    assert snaps >= 6 and methods == {"proxy"}       # cold start uses the HV proxy
    assert n >= 8 and all(":" in r for r in reasons)    # every rejection names its stage
    text = scan_report(ctx, res, show_rejections=1)
    assert "Rejection funnel" in text and "8 F&O stocks scanned" in text


def test_f5_fails_closed_on_cold_start(cfg, tmp_path):
    raw = load_config().model_dump()
    ctx = _ctx_at(cfg, tmp_path)
    morning_refresh(ctx)
    res = scan_job(ctx)
    f5 = [o for o in res.outcomes if o.stage == "F5 cause passed"]
    for o in f5:
        assert "history" in o.reason or "not below" in o.reason


def test_relaxed_scan_produces_ranked_spreads(relaxed_cfg, tmp_path):
    ctx = relaxed_ctx(relaxed_cfg, tmp_path)
    res = run_scan(ctx)
    assert res.ranked, {o.symbol: (o.stage, o.reason) for o in res.outcomes}
    ratios = [c.ev_ratio for c in res.ranked]
    assert ratios == sorted(ratios, reverse=True)
    assert len(res.selected) == min(relaxed_cfg.scan.top_n, len(res.ranked))
    best = res.ranked[0]
    assert best.credit_nat < best.credit_gross                     # costs deducted
    assert best.max_loss == pytest.approx((best.width - best.credit_nat) * best.lot_size)
    assert best.credit_nat >= 0.05 * best.width
    assert all(best.flags[f"f{i}"] for i in range(1, 10))
    assert best.side == BULL_PUT and best.long_k < best.short_k or best.side == BEAR_CALL and best.long_k > best.short_k
    with ctx.engine.connect() as conn:
        acc = conn.execute(select(Candidate).where(Candidate.scan_id == res.scan_id,
                                                   Candidate.accepted.is_(True))).mappings().all()
    assert len(acc) == len(res.ranked)
    inputs = json.loads(acc[0]["inputs_json"])
    for key in ("spot", "atr14", "hv20", "atm_iv", "level", "short_quote", "round_trip_costs", "ivp_method"):
        assert key in inputs


# ----------------------------------------------------------------------------- path-simulation EV

def _paths_cfg():
    return load_config()


def _put_spread(iv=0.40):
    from app.analytics import black76 as b
    F, T = 500.0, 25 / 365
    credit = b.price(F, 470, T, iv, 0.065, "PE") - b.price(F, 460, T, iv, 0.065, "PE")
    return F, T, credit


def test_paths_without_rules_are_fair_at_market_vol():
    """No stop, no target, held to expiry, vol = IV: expected P&L ~ 0 (risk-neutral pricing)."""
    import numpy as np
    from app.strategy.ev import simulate_exits

    F, T, credit = _put_spread()
    z = np.random.default_rng(0).standard_normal((40_000, 25))
    pnl, why = simulate_exits(BULL_PUT, F, 470, 460, 0.40, 0.40, 0.40, T, 0.0, 25, credit, credit, 0, 0, None,
                              1e9, -1.0, False, 0.0, z, 1)
    assert (why == 3).all()
    assert abs(pnl.mean()) < 0.06 * credit


def test_path_ev_vs_expiry_formula():
    from app.strategy.ev import evaluate, evaluate_paths

    cfg = _paths_cfg()
    F, T, credit = _put_spread()
    old = evaluate(BULL_PUT, F, 470 - credit, T, 0.40, 0.30, credit, 10, 1, 0.5, 2.0)
    new = evaluate_paths(BULL_PUT, F, 470, 460, 0.40, 0.40, 0.30, T, 7 / 365, 13, credit, credit, 10, 1,
                         0.0, 0.0, None, cfg, seed=1)
    assert sum(new.exit_mix.values()) == pytest.approx(1.0)
    assert new.exit_mix["hard_stop"] > 0.15                  # stops fire mid-trade
    assert new.p_model < old.p_model                         # expiry formula overstates the win rate
    assert new.ev_ratio < old.ev_ratio
    again = evaluate_paths(BULL_PUT, F, 470, 460, 0.40, 0.40, 0.30, T, 7 / 365, 13, credit, credit, 10, 1,
                           0.0, 0.0, None, cfg, seed=1)
    assert again == new                                      # same inputs, same EV


def test_path_ev_rewards_rich_iv_and_punishes_cheap_iv():
    from app.strategy.ev import evaluate_paths

    cfg = _paths_cfg()
    F, T, credit = _put_spread(iv=0.60)
    rich = evaluate_paths(BULL_PUT, F, 470, 460, 0.60, 0.60, 0.20, T, 7 / 365, 13, credit, credit, 10, 1,
                          0.0, 0.0, None, cfg, seed=2)
    cheap = evaluate_paths(BULL_PUT, F, 470, 460, 0.60, 0.60, 0.90, T, 7 / 365, 13, credit, credit, 10, 1,
                           0.0, 0.0, None, cfg, seed=2)
    assert rich.ev > 0 > cheap.ev
    assert rich.p_model > cheap.p_model


def test_thesis_stop_and_half_spread_cost():
    from app.strategy.ev import evaluate_paths

    cfg = _paths_cfg()
    F, T, credit = _put_spread()
    base = dict(side=BULL_PUT, F=F, short_k=470, long_k=460, iv_short=0.4, iv_long=0.4, hv20=0.3, T0=T,
                T_exit=7 / 365, n_days=13, credit_gross=credit, credit_nat=credit, width=10, lot=1, cfg=cfg, seed=3)
    no_level = evaluate_paths(half_short=0, half_long=0, level=None, **base)
    with_level = evaluate_paths(half_short=0, half_long=0, level=495.0, **base)
    assert no_level.exit_mix["thesis_stop"] == 0 and with_level.exit_mix["thesis_stop"] > 0.2
    wide = evaluate_paths(half_short=0.3, half_long=0.3, level=None, **base)
    assert wide.ev < no_level.ev                              # paying the spread to close costs money


def test_default_scan_uses_path_ev(cfg):
    assert cfg.ev.method == "paths" and cfg.ev.n_paths == 2000
