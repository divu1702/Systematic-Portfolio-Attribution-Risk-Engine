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
from tests.conftest import SIM_NOW, shared_market


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
    class C:
        pass
    c = C()
    c.cfg = cfg
    lot = 100
    assert liquidity_ok(c, _quote(10, 10.4, 60_000, 6_000), lot) == (True, "")
    assert not liquidity_ok(c, _quote(10, 11, 60_000, 6_000), lot)[0]         # 9.5% wide
    assert "OI" in liquidity_ok(c, _quote(10, 10.4, 40_000, 6_000), lot)[1]   # 400 lots
    assert "volume" in liquidity_ok(c, _quote(10, 10.4, 60_000, 4_000), lot)[1]
    assert not liquidity_ok(c, _quote(0, 10.4, 60_000, 6_000), lot)[0]


# ----------------------------------------------------------------------------- scan on the mock

def _ctx_at(cfg, tmp_path, when=SIM_NOW, name="scan.db"):
    return build_context(cfg, mock=True, clock=SimClock(when), db_path=tmp_path / name, env={},
                         market=shared_market())


def test_scan_without_turnover_rejects_everything_at_f1(cfg, tmp_path):
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


@pytest.fixture
def relaxed_cfg(raw_config, write_config):
    """Thresholds loosened so that the synthetic market yields accepted spreads; each filter is
    still evaluated and recorded."""
    r = raw_config
    r["iv"].update(min_iv_hv_ratio=0.01, proxy_hv_pctile=1)
    r["events"].update(estimate_results=False, buffer_days=0)
    r["direction"] = {"max_dist_atr": 50}
    r["levels"].update(min_buffer_atr=0.0)
    r["strikes"] = {"delta_min": 0.05, "delta_max": 0.45, "min_dist_atr": 0.0}
    r["spread"]["min_credit_width"] = 0.05
    r["ev"]["min_ev_ratio"] = 0.0
    r["liquidity"] = {"max_spread_pct": 0.2, "min_oi_lots": 0, "min_vol_lots": 0}
    return load_config(write_config(r))


def test_relaxed_scan_produces_ranked_spreads(relaxed_cfg, tmp_path):
    ctx = _ctx_at(relaxed_cfg, tmp_path)
    morning_refresh(ctx)
    scan_job(ctx)                                     # first scan seeds snapshots
    # Fake a low-IV snapshot inside the F5 look-back plus a "cause" event for every stock.
    with ctx.engine.begin() as conn:
        for sym in [s.name for s in ctx.broker.market.specs if not s.is_index]:
            d = SIM_NOW.date() - timedelta(days=4)
            conn.exec_driver_sql("INSERT OR REPLACE INTO iv_snapshots (ts, date, symbol, expiry, fut_price, atm_iv, "
                                 "ivp_252, method) VALUES (?, ?, ?, ?, 1, 0.1, 0, 'proxy')",
                                 (str(SIM_NOW), d.isoformat(), sym, "2026-11-24"))
            conn.exec_driver_sql("INSERT INTO events (symbol, event_date, event_type, source, fetched_at) "
                                 "VALUES (?, ?, 'news', 'manual', ?)", (sym, (d + timedelta(days=1)).isoformat(),
                                                                        str(SIM_NOW)))
    ctx.clock.set(SIM_NOW + timedelta(minutes=1))
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
