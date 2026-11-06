from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.broker.base import Level, Quote
from app.data import state
from app.data.calendar import TradingCalendar
from app.data.models import Equity, Mark, Order, Trade
from app.execution.entries import enter_from_scan
from app.execution.paper import PaperExecutor, simulate_fill
from app.execution.router import get_executor
from app.monitor.exits import GAP, REGULAR, THESIS, decide_exit, run_monitor
from app.monitor.marks import MarkResult, quote_keys
from app.monitor.replay import catch_up
from app.risk.equity import eod_equity
from app.risk.limits import kill_switches, open_trades, size_and_check
from app.risk.sizing import lots_for
from app.safety import LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE, LiveTradingDisabled
from app.scheduler import Guard, job_specs
from app.sim import run_trading
from app.strategy.scanner import run_scan
from app.strategy.spreads import BULL_PUT
from tests.conftest import SIM_NOW, relaxed_ctx


def Q(bids, asks, ts=None):
    return Quote("NFO:X", ts, 0, tuple(Level(p, q) for p, q in bids), tuple(Level(p, q) for p, q in asks), 0, 0)


# ----------------------------------------------------------------------------- fills

def test_fill_top_of_book_plus_one_tick():
    f = simulate_fill(Q([(9.9, 1000)], [(10.1, 1000)]), "BUY", 500, 0.05, 1)
    assert f.ok and f.natural == 10.1 and f.price == pytest.approx(10.15)
    f = simulate_fill(Q([(9.9, 1000)], [(10.1, 1000)]), "SELL", 500, 0.05, 1)
    assert f.ok and f.price == pytest.approx(9.85)


def test_fill_walks_book_vwap():
    q = Q([(9.9, 100), (9.8, 100), (9.7, 300)], [(10.1, 100)])
    f = simulate_fill(q, "SELL", 300, 0.05, 0)
    assert f.levels == 3 and f.vwap == pytest.approx((990 + 980 + 970) / 300)


def test_fill_rejected_when_five_levels_short():
    q = Q([(9.9, 100)] * 5, [(10.1, 100)])
    f = simulate_fill(q, "SELL", 600, 0.05, 1)
    assert not f.ok and "depth" in f.reason


# ----------------------------------------------------------------------------- sizing / limits

def test_lots_floor_never_rounds_up():
    assert lots_for(10_000, 3_400) == 2
    assert lots_for(10_000, 10_001) == 0
    assert lots_for(10_000, 0) == 0


class _Cand:
    def __init__(self, symbol="AAA", side=BULL_PUT, max_loss=4_000):
        self.symbol, self.side, self.max_loss = symbol, side, max_loss


class _T:
    def __init__(self, symbol, side, max_loss, sector="IT", margin=10_000):
        self.symbol, self.side, self.max_loss, self.sector, self.margin = symbol, side, max_loss, sector, margin


def test_size_and_check_limits(mock_ctx):
    eq = 500_000                     # per trade 10k, total 40k, margin cap 250k
    lots, v = size_and_check(mock_ctx, _Cand(max_loss=4_000), "Banks", 5_000, [], eq)
    assert lots == 2 and v == []
    open_ = [_T("B", BULL_PUT, 36_000)]
    lots, v = size_and_check(mock_ctx, _Cand(max_loss=4_000), "Banks", 5_000, open_, eq)
    assert lots == 1 and v == []                    # trimmed to fit the 8% total
    lots, v = size_and_check(mock_ctx, _Cand(max_loss=4_000), "IT", 5_000, [_T("B", "bear_call", 1)], eq)
    assert any("sector IT" in x for x in v)
    three = [_T(c, BULL_PUT, 1, sector=c) for c in "BCD"]
    assert any("bull_put spreads open" in x for x in size_and_check(mock_ctx, _Cand(), "Z", 1, three, eq)[1])
    five = [_T(c, "bear_call", 1, sector=c) for c in "BCDEF"]
    assert any("spreads open (max 5)" in x for x in size_and_check(mock_ctx, _Cand(), "Z", 1, five, eq)[1])
    assert any("margin" in x for x in size_and_check(mock_ctx, _Cand(), "Z", 200_000, [], eq)[1])
    assert size_and_check(mock_ctx, _Cand(max_loss=12_000), "Z", 1, [], eq)[0] == 0
    assert any("already has" in x for x in size_and_check(mock_ctx, _Cand("B"), "Q", 1, [_T("B", "bear_call", 1, "W")], eq)[1])


def test_stop_basis_sizing(mock_ctx):
    from app.risk.sizing import plan_lots, risk_at_stop_per_share, risk_per_lot

    cfg = mock_ctx.cfg                                   # stop 2x, overshoot 1.25, budget 2%, gap cap 5%
    assert risk_at_stop_per_share(cfg, 3.0, 10.0) == pytest.approx(3.75)
    assert risk_at_stop_per_share(cfg, 6.0, 10.0) == pytest.approx(4.0)      # capped at max loss
    assert risk_per_lot(cfg, 3.0, 10.0, 1000) == pytest.approx(3750)
    # 10,000 budget / 3,750 -> 2 lots; gap cap 25,000 / 7,000 max loss -> 3 lots; take 2
    assert plan_lots(cfg, 500_000, 3750, 7000) == (2, "")
    # gap cap binds: max loss 13,000/lot -> only 1 lot fits 25,000
    assert plan_lots(cfg, 500_000, 3000, 13_000) == (1, "")
    lots, why = plan_lots(cfg, 500_000, 3000, 30_000)
    assert lots == 0 and "gap cap" in why
    c = _Cand(max_loss=7000)
    c.risk_per_lot = 3750
    assert size_and_check(mock_ctx, c, "Z", 1, [], 500_000) == (2, [])


def test_kill_switches(mock_ctx):
    assert kill_switches(mock_ctx, 500_000) == []
    state.put(mock_ctx.engine, state.KILLED, "telegram /kill")
    assert "kill switch" in kill_switches(mock_ctx, 500_000)[0]
    state.clear(mock_ctx.engine, state.KILLED)
    state.put(mock_ctx.engine, state.DD_PAUSED, "dd 11%")
    assert "drawdown pause" in kill_switches(mock_ctx, 500_000)[0]
    state.clear(mock_ctx.engine, state.DD_PAUSED)
    mock_ctx.clock.set(SIM_NOW.replace(hour=9, minute=16) + timedelta(days=3))
    assert any("opening window" in x for x in kill_switches(mock_ctx, 500_000))


# ----------------------------------------------------------------------------- end to end on the mock

@pytest.fixture
def traded(relaxed_cfg, tmp_path):
    ctx = relaxed_ctx(relaxed_cfg, tmp_path)
    res = run_scan(ctx)
    msgs = enter_from_scan(ctx, PaperExecutor(ctx), res)
    return ctx, res, msgs


def test_entries_record_trade_orders_and_alerts(traded):
    ctx, res, msgs = traded
    trades = open_trades(ctx.engine)
    assert trades, msgs
    assert any(m.startswith("ENTRY") for m in msgs)
    t = trades[0]
    assert t.mode == "paper" and t.lots >= 1 and t.entry_credit > 0 and t.costs > 0
    r, cap = ctx.cfg.risk, ctx.cfg.capital
    assert t.risk <= r.per_trade * cap + 1e-6                        # sized by loss at the stop
    assert t.risk < t.max_loss                                       # stop risk is below full max loss
    assert t.max_loss <= r.gap_cap_per_trade * cap + 1e-6            # gap cap on full max loss
    assert sum(x.risk for x in trades) <= r.max_total * cap + 1e-6
    assert sum(x.max_loss for x in trades) <= r.gap_cap_total * cap + 1e-6
    assert len({x.sector for x in trades}) == len(trades)              # max 1 per sector
    with Session(ctx.engine) as s:
        orders = list(s.scalars(select(Order).where(Order.trade_id == t.trade_id).order_by(Order.ts, Order.leg)))
    assert {(o.leg, o.side) for o in orders} == {("long", "BUY"), ("short", "SELL")}
    for o in orders:                                    # paper fills are worse than natural
        assert (o.fill_px > o.limit_px) if o.side == "BUY" else (o.fill_px < o.limit_px)
    assert [k for k, _ in ctx.notifier.sent].count("entry") == len(trades)


def test_margin_fallback_is_flagged(relaxed_cfg, tmp_path):
    ctx = relaxed_ctx(relaxed_cfg, tmp_path)
    ctx.broker.fail_margin = True
    enter_from_scan(ctx, PaperExecutor(ctx), run_scan(ctx))
    trades = open_trades(ctx.engine)
    assert trades and all(t.margin_fallback for t in trades)
    assert all(t.margin == pytest.approx(t.max_loss * 1.5) for t in trades)


def test_entries_blocked_by_kill_switch(relaxed_cfg, tmp_path):
    ctx = relaxed_ctx(relaxed_cfg, tmp_path)
    state.put(ctx.engine, state.KILLED, "test")
    msgs = enter_from_scan(ctx, PaperExecutor(ctx), run_scan(ctx))
    assert msgs and "blocked" in msgs[0] and not open_trades(ctx.engine)


def _mark(t, ctc, spot, ts=None):
    return MarkResult(t, ts or SIM_NOW, spot, ctc, None, True)


def test_exit_rule_priority(traded):
    ctx, _, _ = traded
    t = open_trades(ctx.engine)[0]
    cal = TradingCalendar.load(ctx.engine)
    c = t.entry_credit
    safe_spot = (t.short_k * 1.5) if t.side == BULL_PUT else (t.short_k * 0.6)
    assert decide_exit(ctx, t, _mark(t, c, safe_spot), REGULAR, cal) is None
    assert decide_exit(ctx, t, _mark(t, 2 * c, safe_spot), REGULAR, cal) == ("hard_stop", False)
    assert decide_exit(ctx, t, _mark(t, 0.5 * c, safe_spot), REGULAR, cal) == ("profit_target", False)
    beyond_level = t.level_ref - 1 if t.side == BULL_PUT else t.level_ref + 1
    assert decide_exit(ctx, t, _mark(t, c, beyond_level), THESIS, cal) == ("thesis_stop", False)
    assert decide_exit(ctx, t, _mark(t, c, beyond_level), REGULAR, cal) is None   # thesis only at 15:15
    beyond_long = t.long_k - 1 if t.side == BULL_PUT else t.long_k + 1
    assert decide_exit(ctx, t, _mark(t, 3 * c, beyond_long), GAP, cal) == ("gap_through_long", True)
    near_expiry = datetime.combine(t.expiry - timedelta(days=3), SIM_NOW.time())
    assert decide_exit(ctx, t, _mark(t, c, safe_spot, near_expiry), REGULAR, cal) == ("time_stop", False)
    # An event outranks the stop.
    with ctx.engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO events (symbol, event_date, event_type, source, fetched_at) "
                             "VALUES (?, ?, 'agm', 'manual', ?)", (t.symbol, str(t.expiry - timedelta(days=2)),
                                                                   str(SIM_NOW)))
    assert decide_exit(ctx, t, _mark(t, 2 * c, safe_spot), REGULAR, cal)[0].startswith("event")


def test_close_short_first_and_never_leave_naked_short(traded):
    ctx, _, _ = traded
    t = open_trades(ctx.engine)[0]
    ex = PaperExecutor(ctx)
    quotes = ctx.broker.quotes(quote_keys([t]))
    from app.execution.paper import legs_of
    short, long_ = legs_of(t)
    thin = dict(quotes)
    thin[f"NFO:{short['tradingsymbol']}"] = Q([(1, 1)], [(1.1, 1)])     # cannot buy back the short
    res = ex.close_spread(t, thin, "hard_stop")
    assert not res.ok and "long kept" in res.reason
    assert open_trades(ctx.engine)                                     # still open, nothing sold
    res = ex.close_spread(t, quotes, "hard_stop")
    assert res.ok
    with Session(ctx.engine) as s:
        tr = s.get(Trade, t.trade_id)
        exits = list(s.scalars(select(Order).where(Order.trade_id == t.trade_id, Order.purpose == "exit",
                                                   Order.status == "filled")))
    assert tr.status == "closed" and tr.exit_reason == "hard_stop"
    assert tr.pnl_net == pytest.approx(tr.pnl_gross - tr.costs - tr.exit_costs)
    assert {(o.leg, o.side) for o in exits} == {("short", "BUY"), ("long", "SELL")}


def test_monitor_marks_and_mae_mfe(traded):
    ctx, _, _ = traded
    ctx.clock.set(SIM_NOW + timedelta(minutes=10))
    run_monitor(ctx, PaperExecutor(ctx))
    with ctx.engine.connect() as conn:
        n = conn.execute(select(func.count()).select_from(Mark)).scalar()
    assert n >= len(open_trades(ctx.engine))
    for t in open_trades(ctx.engine):
        assert t.mae <= 0 <= t.mfe or t.mae <= t.mfe


def test_replay_rebuilds_missed_marks(traded):
    ctx, _, _ = traded
    before = len(open_trades(ctx.engine))
    nxt = TradingCalendar.load(ctx.engine).next_trading_day(SIM_NOW.date())
    ctx.clock.set(datetime.combine(nxt, SIM_NOW.time().replace(hour=13)))   # slept since yesterday 15:01
    catch_up(ctx, PaperExecutor(ctx))
    with ctx.engine.connect() as conn:
        replayed = conn.execute(select(func.count()).select_from(Mark).where(Mark.source == "replay")).scalar()
        closed = conn.execute(select(func.count()).select_from(Trade).where(Trade.replayed.is_(True))).scalar()
    assert replayed > 0
    assert closed + len(open_trades(ctx.engine)) == before


def test_eod_equity_and_drawdown_pause(traded):
    ctx, _, _ = traded
    ctx.clock.set(SIM_NOW.replace(hour=15, minute=35))
    p = eod_equity(ctx)
    assert p.equity == pytest.approx(ctx.cfg.capital + p.realised + p.unrealised)
    assert p.margin_used > 0
    with ctx.engine.begin() as conn:            # pretend an earlier peak far above
        conn.exec_driver_sql("INSERT INTO equity (date, equity, realised, unrealised, margin_used, peak, drawdown) "
                             "VALUES ('2026-10-01', 600000, 0, 0, 0, 600000, 0)")
    p = eod_equity(ctx)
    assert p.drawdown >= 0.10 and state.flag(ctx.engine, state.DD_PAUSED)
    assert any(k == "risk" for k, _ in ctx.notifier.sent)


# ----------------------------------------------------------------------------- router / scheduler

def test_router_paper_and_live_guard(mock_ctx, raw_config, write_config):
    from app.config import load_config

    assert isinstance(get_executor(mock_ctx), PaperExecutor)
    raw_config["mode"] = "live"
    mock_ctx.cfg = load_config(write_config(raw_config))
    with pytest.raises(LiveTradingDisabled):
        get_executor(mock_ctx)
    mock_ctx.env = {LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE}
    with pytest.raises(NotImplementedError, match="milestone 8"):
        get_executor(mock_ctx)


def test_guard_skips_holidays_and_missing_session(mock_ctx):
    calls = []
    with mock_ctx.engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO holidays (date, description, source) VALUES (?, 'x', 'manual')",
                             (SIM_NOW.date().isoformat(),))
    Guard(mock_ctx, "t", lambda: calls.append(1))()
    assert calls == []
    with mock_ctx.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM holidays")
    mock_ctx.broker.session_valid = lambda: False
    Guard(mock_ctx, "t", lambda: calls.append(1))()
    assert calls == [] and mock_ctx.notifier.sent[-1][0] == "login"
    mock_ctx.broker.session_valid = lambda: True
    Guard(mock_ctx, "boom", lambda: 1 / 0)()
    assert mock_ctx.notifier.sent[-1][0] == "error"


def test_job_specs_cover_the_spec_schedule(mock_ctx):
    names = {n: f for n, f, _, _ in job_specs(mock_ctx)}
    assert names["gap_check"] == {"hour": 9, "minute": 20}
    assert names["scan"] == {"hour": 15, "minute": 0}
    assert names["eod"] == {"hour": 15, "minute": 35}
    assert names["login_reminder"] == {"hour": 9, "minute": 0}
    assert names["monitor"]["minute"] == "*/5"


def test_sim_trading_days_end_to_end(relaxed_cfg, tmp_path):
    ctx = relaxed_ctx(relaxed_cfg, tmp_path)
    m = ctx.broker.market
    start = m.day_index[SIM_NOW.date()] + 1
    days = m.days[start:start + 6]
    results = run_trading(ctx, days, outage_prob=0.5, seed=3)
    with ctx.engine.connect() as conn:
        n_marks = conn.execute(select(func.count()).select_from(Mark)).scalar()
        n_eq = conn.execute(select(func.count()).select_from(Equity)).scalar()
        n_trades = conn.execute(select(func.count()).select_from(Trade)).scalar()
    assert n_eq == 6 and n_trades > 0 and n_marks > 0
    assert any(r["outage"] for r in results)
