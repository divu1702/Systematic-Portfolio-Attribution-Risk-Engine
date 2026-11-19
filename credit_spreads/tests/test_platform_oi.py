"""The earlier OI-wall system on the platform: its books trade as accounts oi_tight (main rules) and
oi_loose (v2 rules) through the shared paper broker, with its own rules and records unchanged."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.models import Equity, Trade
from app.execution.entries import enter_from_scan
from app.execution.paper import PaperExecutor, legs_of
from app.execution.router import get_executor
from app.monitor.exits import REGULAR, run_monitor
from app.monitor.marks import quote_keys
from app.risk.limits import open_trades
from app.strategy.scanner import run_scan
from platform_core import oi_bridge
from platform_core.broker import PaperBroker
from platform_core.costs import CostModel
from platform_core.data_service import DataService
from platform_core.db import init_platform_db
from platform_core.models import POrder, PTrade
from tests.conftest import SIM_NOW, relaxed_ctx
from tests.test_strategies import TREND, with_sleeves

GO_LIVE = date(2026, 11, 2)
V2 = {**TREND, "name": "v2", "description": "stand-in for the v2 sleeve (yields entries on the synthetic market)"}


def platform_for(ctx, pcfg):
    pengine = init_platform_db(pcfg.db_path)
    broker = PaperBroker(pengine, pcfg, DataService(ctx, pcfg, pengine))
    broker.ensure_accounts()
    books = oi_bridge.attach(ctx, broker, pcfg)
    return SimpleNamespace(ctx=ctx, pcfg=pcfg, pengine=pengine, broker=broker, books=books)


@pytest.fixture
def oi(relaxed_cfg, platform_cfg, tmp_path):
    ctx = relaxed_ctx(with_sleeves(relaxed_cfg, V2), tmp_path)
    return platform_for(ctx, platform_cfg.model_copy(update={"go_live_date": GO_LIVE}))


def enter(ctx):
    return enter_from_scan(ctx, get_executor(ctx), run_scan(ctx))


def test_both_systems_charge_the_same(cfg, platform_cfg):
    from app.strategy.costs import entry_costs, exit_costs

    cm = CostModel(platform_cfg.costs, platform_cfg.settlement)
    assert cfg.costs.exchange_txn_pct == platform_cfg.costs.exchange_txn_pct == 0.0003553
    for sp, lp, qty in [(14.75, 8.40, 2500), (3.2, 0.05, 725), (120.0, 61.5, 150)]:
        assert entry_costs(cfg.costs, sp, lp, qty) == pytest.approx(cm.open_spread(sp, lp, qty).total)
        assert exit_costs(cfg.costs, sp, lp, qty) == pytest.approx(cm.close_spread(sp, lp, qty).total)


def test_attach_checks_mode_and_books(platform_cfg):
    fake = SimpleNamespace(cfg=SimpleNamespace(mode="live", strategy_names=["main", "v2"]), parent=None, cache={})
    with pytest.raises(RuntimeError, match="paper trading only"):
        oi_bridge.attach(fake, None, platform_cfg)
    fake.cfg.mode, fake.cfg.strategy_names = "paper", ["main"]
    with pytest.raises(RuntimeError, match="v2"):
        oi_bridge.attach(fake, None, platform_cfg)
    fake.cfg.strategy_names = ["main", "v2", "extra"]
    assert oi_bridge.attach(fake, None, platform_cfg) == {"main": "oi_tight", "v2": "oi_loose"}
    assert fake.cache[oi_bridge.FACTORY_KEY](SimpleNamespace(strategy="extra")) is None   # unmapped: earlier executor


def test_books_fill_through_the_shared_broker(oi):
    ctx, b = oi.ctx, oi.broker
    assert oi.books == {"main": "oi_tight", "v2": "oi_loose"}
    assert get_executor(ctx).account == "oi_tight" and get_executor(ctx.for_strategy("v2")).account == "oi_loose"
    msgs = enter(ctx)
    assert any(m.startswith("ENTRY") for m in msgs) and any(m.startswith("[v2] ENTRY") for m in msgs)
    old = {t.trade_id: t for t in open_trades(ctx.engine)}
    new = {t.id: t for t in b.open_trades()}
    assert old and set(old) == set(new)                                   # same trade ids in both records
    for tid, t in old.items():
        p = new[tid]
        assert p.account == {"main": "oi_tight", "v2": "oi_loose"}[t.strategy] and p.source == "oi_system"
        assert p.symbol == t.symbol and p.direction == t.side and p.book == t.strategy
        assert p.credit == pytest.approx(t.entry_credit) and p.entry_charges == pytest.approx(t.costs, abs=0.01)
        assert p.margin == pytest.approx(t.margin)
        assert p.one_r == pytest.approx(t.risk, abs=0.01)                 # 1R: planned loss at its own stop
    for acc in ("oi_tight", "oi_loose"):
        st, mine = b.state(acc), [p for p in new.values() if p.account == acc]
        assert mine and st.used_margin == pytest.approx(sum(p.margin for p in mine), abs=0.05)
        assert st.cash_balance == pytest.approx(
            500_000 + sum(p.credit * p.lots * p.lot_size - p.entry_charges for p in mine), abs=0.1)
    for acc in ("level_strict", "level_research", "discretionary"):
        assert b.state(acc).cash_balance == 500_000 and b.state(acc).used_margin == 0


def test_exits_close_both_records_with_the_same_pnl(oi):
    ctx, b = oi.ctx, oi.broker
    enter(ctx)
    ctx.clock.set(SIM_NOW + timedelta(minutes=20))
    t = open_trades(ctx.engine, "main")[0]
    res = get_executor(ctx).close_spread(t, ctx.broker.quotes(quote_keys([t])), "profit_target")
    assert res.ok
    with Session(ctx.engine) as s:
        old = s.scalars(select(Trade).where(Trade.trade_id == t.trade_id)).one()
    new = b.get_trade(t.trade_id)
    assert old.status == new.status == "closed" and old.exit_reason == new.exit_reason == "profit_target"
    assert old.pnl_net == pytest.approx(new.pnl_inr, abs=0.02) and old.exit_cost == pytest.approx(new.exit_cost)
    assert old.exit_costs == pytest.approx(new.exit_charges, abs=0.01)
    assert new.pnl_r == pytest.approx(new.pnl_inr / t.risk, abs=1e-3)
    assert b.state("oi_tight").used_margin == pytest.approx(
        sum(p.margin for p in b.open_trades("oi_tight")), abs=0.05)


def test_replayed_exit_uses_candle_prices(oi):
    ctx, b = oi.ctx, oi.broker
    enter(ctx)
    t = open_trades(ctx.engine, "v2")[0]
    res = get_executor(ctx.for_strategy("v2")).close_spread(t, {}, "time_stop", ts=SIM_NOW + timedelta(hours=1),
                                                            replayed=True, prices=(2.0, 0.5))
    assert res.ok
    new = b.get_trade(t.trade_id)
    assert new.exit_cost == pytest.approx(2.05 - 0.45)                     # one tick of slippage on each leg
    with Session(ctx.engine) as s:
        old = s.scalars(select(Trade).where(Trade.trade_id == t.trade_id)).one()
    assert old.replayed and old.pnl_net == pytest.approx(new.pnl_inr, abs=0.02)


def test_monitor_and_end_of_day_run_unchanged(oi):
    from app.jobs import eod_job

    ctx = oi.ctx
    enter(ctx)
    ctx.clock.set(SIM_NOW + timedelta(minutes=20))
    run_monitor(ctx, get_executor(ctx), REGULAR)
    ctx.clock.set(SIM_NOW.replace(hour=15, minute=35))
    eod_job(ctx)
    with Session(ctx.engine) as s:
        assert {e.strategy for e in s.scalars(select(Equity))} == {"main", "v2"}
    old_open = {t.trade_id for t in open_trades(ctx.engine)}
    assert old_open == {t.id for t in oi.broker.open_trades()}             # records still agree


def test_expiry_settlement_is_mirrored_into_the_earlier_records(oi):
    ctx, b = oi.ctx, oi.broker
    enter(ctx)
    t = open_trades(ctx.engine, "main")[0]
    short, _ = legs_of(t)
    spot = short["strike"] + (3 if t.side == "bear_call" else -3)          # the short leg finishes 3 in the money
    b.settle_expired(t.expiry, {t.symbol: spot})
    msgs = oi_bridge.sync_closed(ctx, b)
    assert any(t.symbol in m and "expiry_settlement" in m for m in msgs)
    with Session(ctx.engine) as s:
        old = s.scalars(select(Trade).where(Trade.trade_id == t.trade_id)).one()
    new = b.get_trade(t.trade_id)
    assert old.status == "closed" and old.exit_reason == "expiry_settlement"
    assert old.exit_cost == pytest.approx(3.0) and old.pnl_net == pytest.approx(new.pnl_inr, abs=0.02)
    assert oi_bridge.sync_closed(ctx, b) == []                              # mirrored once


def test_trades_from_before_the_platform_close_in_the_earlier_records(relaxed_cfg, platform_cfg, tmp_path):
    ctx = relaxed_ctx(with_sleeves(relaxed_cfg, V2), tmp_path)
    enter_from_scan(ctx, PaperExecutor(ctx), run_scan(ctx))                # opened before the platform existed
    t = open_trades(ctx.engine, "main")[0]
    p = platform_for(ctx, platform_cfg.model_copy(update={"go_live_date": GO_LIVE}))
    res = get_executor(ctx).close_spread(t, ctx.broker.quotes(quote_keys([t])), "test")
    assert res.ok and p.broker.get_trade(t.trade_id) is None
    with Session(ctx.engine) as s:
        assert s.scalars(select(Trade).where(Trade.trade_id == t.trade_id)).one().status == "closed"
    assert p.broker.state("oi_tight").cash_balance == 500_000


def test_no_entries_before_go_live(relaxed_cfg, platform_cfg, tmp_path):
    ctx = relaxed_ctx(with_sleeves(relaxed_cfg, V2), tmp_path)
    p = platform_for(ctx, platform_cfg)                                    # go_live_date not set
    msgs = enter(ctx)
    assert msgs and not any("ENTRY" in m for m in msgs)
    assert any("has not started" in m for m in msgs)
    assert not open_trades(ctx.engine)
    with Session(p.pengine) as s:
        assert s.scalar(select(func.count()).select_from(PTrade)) == 0
        assert {o.status for o in s.scalars(select(POrder))} == {"rejected"}
