"""Strategies side by side: paper sleeves defined as overrides of the main rules, evaluated on the
same signals, each in its own paper book with its own limits, exits, equity and drawdown pause."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config, ConfigError, load_config
from app.data import state
from app.data.models import Candidate, Equity, Trade
from app.execution.entries import enter_from_scan
from app.execution.paper import PaperExecutor
from app.monitor.exits import REGULAR, THESIS, decide_exit, run_monitor
from app.monitor.marks import MarkResult
from app.risk.limits import kill_switches, open_trades
from app.strategy import rules
from app.strategy.scanner import run_scan
from app.strategy.signals import StockSignals
from app.strategy.spreads import BEAR_CALL, BULL_PUT
from app.strategy.structures import wing_strikes
from tests.conftest import SIM_NOW, relaxed_ctx

TREND = {"name": "trend", "description": "test sleeve",
         "overrides": {"direction": {"method": "trend", "trend_days": 20},
                       "spread": {"wing": "pct", "wing_pct": [0.03, 0.05]},
                       "exit": {"tp_frac": 0.25, "thesis_stop": False}}}


def with_sleeves(cfg: Config, *sleeves) -> Config:
    return Config(**{**cfg.model_dump(exclude={"base_dir"}), "strategies": list(sleeves)}, base_dir=cfg.base_dir)


@pytest.fixture
def books_ctx(relaxed_cfg, tmp_path):
    return relaxed_ctx(with_sleeves(relaxed_cfg, TREND), tmp_path)


# ----------------------------------------------------------------------------- config and context

def test_sleeve_inherits_overrides_and_is_always_paper(cfg):
    c = with_sleeves(cfg.model_copy(update={"mode": "paper"}), TREND)
    t = c.for_strategy("trend")
    assert c.strategy_names == ["main", "trend"]
    assert (t.direction.method, t.direction.trend_days, t.spread.wing, t.exit.tp_frac) == ("trend", 20, "pct", 0.25)
    assert t.exit.stop_mult == c.exit.stop_mult and t.risk == c.risk          # everything else inherited
    assert t.mode == "paper" and t.strategies == []
    assert c.strategy_hash() == cfg.strategy_hash()                           # sleeves don't change the main hash
    assert t.strategy_hash() != c.strategy_hash()
    with pytest.raises(KeyError):
        c.for_strategy("nope")


@pytest.mark.parametrize("bad", [
    {"name": "main"}, {"name": "Bad-Name"}, {"name": "x", "overrides": {"broker": {"provider": "mock"}}},
    {"name": "y", "overrides": {"iv": {"ivp_history_days": 30}}}, {"name": "z", "overrides": {"exit": {"tp_frac": 3}}},
    {"name": "w", "overrides": {"spread": {"wing": "sideways"}}}])
def test_invalid_sleeves_are_rejected_at_load(raw_config, write_config, bad):
    raw_config["strategies"] = [bad]
    with pytest.raises(ConfigError):
        load_config(write_config(raw_config))


def test_duplicate_sleeve_names_rejected(raw_config, write_config):
    raw_config["strategies"] = [{"name": "a"}, {"name": "a"}]
    with pytest.raises(ConfigError, match="unique"):
        load_config(write_config(raw_config))


def test_context_books_share_everything_but_rules(mock_ctx):
    mock_ctx.cfg = with_sleeves(mock_ctx.cfg, TREND)
    main, trend = mock_ctx.books()
    assert (main.strategy, trend.strategy) == ("main", "trend") and main is mock_ctx
    assert trend.engine is main.engine and trend.cache is main.cache and trend.broker is main.broker
    assert trend.cfg.direction.method == "trend" and main.cfg.direction.method == "levels"
    assert trend.for_strategy("main") is mock_ctx and trend.parent is mock_ctx
    mock_ctx.cfg = mock_ctx.cfg.model_copy(update={"capital": 123456.0})       # main config replaced
    assert mock_ctx.for_strategy("trend").cfg.capital == 123456.0
    ghost = mock_ctx.for_strategy("retired", missing_ok=True)                  # a sleeve removed from config
    assert ghost.strategy == "retired" and ghost.cfg is mock_ctx.cfg


# ----------------------------------------------------------------------------- rules and structures

def _signals(ctx, closes, atm_iv=0.30, spot=None):
    idx = pd.date_range(end=SIM_NOW.date() - timedelta(days=1), periods=len(closes), freq="B").date
    daily = pd.DataFrame({"close": closes, "atr14": [2.0] * len(closes), "hv20": [0.2] * len(closes)}, index=idx)
    spot = closes[-1] if spot is None else spot
    snap = SimpleNamespace(atm_iv=atm_iv, spot=spot, fut_price=spot)
    exps = [SIM_NOW.date() + timedelta(days=18), SIM_NOW.date() + timedelta(days=53)]
    return StockSignals(ctx, "STK01", SIM_NOW.date(), exps, daily, 2.0, 0.2, exps[0], snap, 90.0, "true", None)


@pytest.fixture
def open_rules(mock_ctx, monkeypatch):
    monkeypatch.setattr(rules.ev_data, "check_f2", lambda ctx, s, e: SimpleNamespace(passed=True, reason="", events=[]))
    mock_ctx.cfg = with_sleeves(mock_ctx.cfg, {"name": "t", "overrides": {
        "iv": {"min_iv_hv_ratio": 0, "timing": "shock_passed", "calm_days": 5},
        "direction": {"method": "trend", "trend_days": 20}}})
    return mock_ctx.for_strategy("t")


def test_trend_direction_sells_the_side_the_trend_favours(open_rules):
    from app.data.calendar import TradingCalendar

    cal = TradingCalendar.load(open_rules.engine)
    up = _signals(open_rules, list(np.linspace(90, 110, 60)), atm_iv=0.4)
    d = rules.decide_entry(open_rules, up, cal)
    assert d.ok and d.side == BULL_PUT and d.anchor_kind == "trend"
    assert d.anchor == pytest.approx(np.mean(np.linspace(90, 110, 60)[-20:]))
    assert d.flags["f3"] is None                                        # F3 off at 0
    down = _signals(open_rules, list(np.linspace(110, 90, 60)), atm_iv=0.4)
    assert rules.decide_entry(open_rules, down, cal).side == BEAR_CALL


def test_shock_passed_timing(open_rules):
    from app.data.calendar import TradingCalendar

    cal = TradingCalendar.load(open_rules.engine)
    calm = list(100 + 0.05 * np.sin(np.arange(60)))
    assert rules.decide_entry(open_rules, _signals(open_rules, calm, atm_iv=0.4), cal).ok
    shocked = calm[:-3] + [calm[-4] + 6.0, calm[-4] + 6.1, calm[-4] + 6.0]    # a 3x ATR jump 3 sessions ago
    d = rules.decide_entry(open_rules, _signals(open_rules, shocked, atm_iv=0.4), cal)
    assert d.stage == "F5 shock passed" and "large move" in d.reason
    d = rules.decide_entry(open_rules, _signals(open_rules, calm, atm_iv=0.0001), cal)
    assert d.stage == "F5 shock passed" and "not below IV" in d.reason   # realised vol must sit below IV


def test_wings_by_percent_of_spot(cfg):
    c = cfg.model_copy(update={"spread": cfg.spread.model_copy(update={"wing": "pct", "wing_pct": [0.03, 0.05, 0.50]})})
    chain = SimpleNamespace(strikes=[float(k) for k in range(800, 1201, 10)], strike_step=10.0)
    got = wing_strikes(c, chain, 950.0, BULL_PUT, 1000.0)
    assert [k for k, _ in got] == [920.0, 900.0]                         # 3% and 5% of 1000 below 950; 50% unlisted
    assert got[0][1] == {"wing_pct": 0.03, "width_steps": 3.0}
    calls = wing_strikes(c, chain, 1050.0, BEAR_CALL, 1000.0)
    assert [k for k, _ in calls] == [1080.0, 1100.0]
    steps = wing_strikes(cfg, chain, 950.0, BULL_PUT, 1000.0)
    assert [k for k, _ in steps] == [950.0 - w * 10 for w in cfg.spread.widths_steps]


# ----------------------------------------------------------------------------- scan, entries, exits

def test_two_books_scan_and_enter_separately(books_ctx):
    res = run_scan(books_ctx)
    assert list(res.books) == ["main", "trend"]
    assert res.outcomes is res.books["main"].outcomes and res.ranked == res.books["main"].ranked
    main, trend = res.books["main"], res.books["trend"]
    assert len(main.outcomes) == len(trend.outcomes) == 8                # same stocks, same signals
    assert trend.ranked, {o.symbol: (o.stage, o.reason) for o in trend.outcomes}
    for c in trend.ranked:
        i = c.inputs
        assert c.strategy == "trend" and i["direction_method"] == "trend" and "wing_pct" in i
        assert c.side == (BULL_PUT if i["spot"] > i["trend_avg"] else BEAR_CALL)
    assert all(c.inputs["direction_method"] == "levels" and "level_score" in c.inputs for c in main.ranked)
    with Session(books_ctx.engine) as s:
        tags = {r for (r,) in s.execute(select(Candidate.strategy).distinct())}
        hashes = dict(s.execute(select(Candidate.strategy, Candidate.config_hash).distinct()).all())
    assert tags == {"main", "trend"} and hashes["main"] != hashes["trend"]
    msgs = enter_from_scan(books_ctx, PaperExecutor(books_ctx), res)
    assert any(m.startswith("ENTRY") for m in msgs) and any(m.startswith("[trend] ENTRY") for m in msgs)
    by_book = {name: open_trades(books_ctx.engine, name) for name in ("main", "trend")}
    assert by_book["main"] and by_book["trend"]
    assert all(t.strategy == "trend" and "-trend-" in t.trade_id for t in by_book["trend"])
    assert len(open_trades(books_ctx.engine)) == len(by_book["main"]) + len(by_book["trend"])
    # One spread per stock per book: the same stock may be open in both books.
    for name, ts in by_book.items():
        assert len({t.symbol for t in ts}) == len(ts)


def test_books_have_their_own_pauses_and_share_the_kill_switch(books_ctx):
    trend = books_ctx.for_strategy("trend")
    state.put(books_ctx.engine, state.dd_key("trend"), "dd 11%")
    assert any("drawdown pause" in b for b in kill_switches(trend))
    assert not any("drawdown pause" in b for b in kill_switches(books_ctx))
    msgs = enter_from_scan(books_ctx, PaperExecutor(books_ctx), run_scan(books_ctx))
    assert open_trades(books_ctx.engine, "main") and not open_trades(books_ctx.engine, "trend")
    assert any(m.startswith("[trend] Entries blocked") and "--strategy trend" in m for m in msgs)
    state.put(books_ctx.engine, state.KILLED, "test")
    assert kill_switches(books_ctx) and kill_switches(trend)


def test_exits_follow_each_books_rules(books_ctx):
    from app.data.calendar import TradingCalendar

    enter_from_scan(books_ctx, PaperExecutor(books_ctx), run_scan(books_ctx))
    cal = TradingCalendar.load(books_ctx.engine)
    tm, tt = open_trades(books_ctx.engine, "main")[0], open_trades(books_ctx.engine, "trend")[0]

    def mark(t, ctc, spot):
        return MarkResult(t, SIM_NOW, spot, ctc, None, True, cost_to_close_mid=ctc)

    beyond = lambda t: t.level_ref - 1 if t.side == BULL_PUT else t.level_ref + 1  # noqa: E731
    mctx, tctx = books_ctx, books_ctx.for_strategy("trend")
    # Thesis stop: on for the main rules, off in the sleeve.
    assert decide_exit(mctx, tm, mark(tm, tm.entry_credit, beyond(tm)), THESIS, cal, check_events=False)[0] == "thesis_stop"
    assert decide_exit(tctx, tt, mark(tt, tt.entry_credit, beyond(tt)), THESIS, cal, check_events=False) is None
    # Profit target: the main rules keep 50% of the credit, the sleeve waits for 75%.
    assert decide_exit(mctx, tm, mark(tm, 0.45 * tm.entry_credit, None), REGULAR, cal, check_events=False)[0] == "profit_target"
    assert decide_exit(tctx, tt, mark(tt, 0.45 * tt.entry_credit, None), REGULAR, cal, check_events=False) is None
    assert decide_exit(tctx, tt, mark(tt, 0.2 * tt.entry_credit, None), REGULAR, cal, check_events=False)[0] == "profit_target"


def test_monitor_eod_and_reports_per_book(books_ctx, tmp_path):
    from app.jobs import eod_job
    from app.reporting.weekly import write_report
    from app.views import books_table, status_report

    enter_from_scan(books_ctx, PaperExecutor(books_ctx), run_scan(books_ctx))
    books_ctx.clock.set(SIM_NOW + timedelta(minutes=20))
    run_monitor(books_ctx, PaperExecutor(books_ctx), REGULAR)          # marks every book's trades
    with Session(books_ctx.engine) as s:
        marked = {t.strategy for t in s.scalars(select(Trade)) if t.mfe is not None}
    assert marked == {"main", "trend"}
    books_ctx.clock.set(SIM_NOW.replace(hour=15, minute=35))
    eod_job(books_ctx)
    with Session(books_ctx.engine) as s:
        eq = {e.strategy: e.equity for e in s.scalars(select(Equity))}
    assert set(eq) == {"main", "trend"}
    assert all(abs(v - books_ctx.cfg.capital) < 0.05 * books_ctx.cfg.capital for v in eq.values())
    st = status_report(books_ctx)
    assert "Paper sleeve [trend]" in st
    table = books_table(books_ctx)
    assert "main" in table and "trend" in table
    p_main = write_report(books_ctx, out_dir=tmp_path)
    p_trend = write_report(books_ctx.for_strategy("trend"), out_dir=tmp_path)
    assert p_main != p_trend and "_trend" in p_trend.name
    html = p_trend.read_text()
    assert "Paper sleeve <strong>trend</strong>" in html and "Strategies side by side" in html


def test_open_trades_of_a_removed_sleeve_are_still_managed(books_ctx):
    enter_from_scan(books_ctx, PaperExecutor(books_ctx), run_scan(books_ctx))
    n_trend = len(open_trades(books_ctx.engine, "trend"))
    assert n_trend
    books_ctx.cfg = with_sleeves(books_ctx.cfg)                         # sleeve removed from config
    books_ctx.clock.set(SIM_NOW + timedelta(minutes=20))
    msgs = run_monitor(books_ctx, PaperExecutor(books_ctx), REGULAR)
    assert not any("could not mark" in m for m in msgs)
    with Session(books_ctx.engine) as s:
        assert all(t.mfe is not None for t in s.scalars(select(Trade).where(Trade.strategy == "trend")))


def test_cli_report_all_and_resume_one_book(books_ctx, tmp_path, monkeypatch, capsys):
    from app import cli

    state.put(books_ctx.engine, state.dd_key("trend"), "dd")
    state.put(books_ctx.engine, state.DD_PAUSED, "dd main")
    monkeypatch.setattr(cli, "_ctx", lambda args: books_ctx)
    assert cli.main(["resume", "--drawdown", "--strategy", "trend"]) == 0
    assert not state.flag(books_ctx.engine, state.dd_key("trend")) and state.flag(books_ctx.engine, state.DD_PAUSED)
    assert cli.main(["resume", "--drawdown", "--strategy", "nope"]) == 2
    books_ctx.cfg = books_ctx.cfg.model_copy(update={"reporting": books_ctx.cfg.reporting.model_copy(
        update={"out_dir": str(tmp_path / "reports")})})
    assert cli.main(["report", "--strategy", "all"]) == 0
    out = capsys.readouterr().out
    assert out.count("report written to") == 2
