"""The level system's daily runs on the synthetic market: the screener, setups advancing and entering
each book, the dry run before go-live, exits (kill level, gap, profit target, time stop, event exit),
missed sessions, chain snapshots and the 18:30 official re-check."""

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SimClock
from app.context import build_context
from app.data.calendar import TradingCalendar
from app.data.models import Event
from app.jobs import catch_up_daily, morning_refresh
from level_system import store
from level_system.analytics import BEAR_CALL, BULL_PUT, atr_before, level_zone, zone_oi
from level_system.engine import LevelEngine, oi_map
from level_system.setups import Arm
from level_system.state import Machine
from platform_core.broker import PaperBroker, SpreadOrder
from platform_core.data_service import DataService
from platform_core.db import init_platform_db
from platform_core.models import CloseCheck, PTrade, ScreenerRow, SetupRow
from platform_core.nse_files import OfficialEOD
from platform_core.parquet_store import read_snapshots
from tests.conftest import shared_market

RUN = datetime(2026, 11, 6, 15, 20)
GO_LIVE = date(2026, 11, 2)


def build(cfg, pcfg, tmp_path, when=RUN):
    ctx = build_context(cfg, mock=True, clock=SimClock(when), db_path=tmp_path / "m.db", env={}, market=shared_market())
    morning_refresh(ctx)
    catch_up_daily(ctx, TradingCalendar.load(ctx.engine).prev_trading_day(ctx.clock.today()))
    pengine = init_platform_db(pcfg.db_path)
    ds = DataService(ctx, pcfg, pengine)
    broker = PaperBroker(pengine, pcfg, ds)
    broker.ensure_accounts()
    return SimpleNamespace(ctx=ctx, cfg=pcfg, pengine=pengine, ds=ds, broker=broker, eng=LevelEngine(ds, broker, pcfg))


def relaxed(pcfg, live=True):
    """The synthetic market's far strikes are wide, so mechanics tests loosen the liquidity rules (as the
    earlier system's tests do); every rule is still evaluated."""
    from platform_core.config import LiquidityCfg

    upd = {"liquidity": LiquidityCfg(min_oi_value_inr=1, max_spread_pct=0.30)}
    if live:
        upd["go_live_date"] = GO_LIVE
    return pcfg.model_copy(update=upd)


@pytest.fixture
def lvl(cfg, platform_cfg, tmp_path):
    return build(cfg, relaxed(platform_cfg), tmp_path)


def inject(lvl, symbol="STK05", setup=1, direction=BEAR_CALL, level_atr=0.3, vol_ok=True, day0=date(2026, 11, 5)):
    """An armed setup whose Day 0 OI equals today's, so the zone OI test passes unchanged."""
    ds = lvl.ds
    spot = ds.spot_quotes([symbol])[symbol].last
    bars = ds.daily_bars(symbol, end=day0)
    atr = atr_before(bars, day0)
    level = spot + level_atr * atr if direction == BEAR_CALL else spot - level_atr * atr
    expiry, _ = ds.expiry_for(symbol, day0)
    ch = ds.chain(symbol, expiry)
    quotes = ds.quotes([leg.key for leg in ch.legs.values()])
    prot, other = level_zone(direction, level, ch.strikes)
    z0 = zone_oi(oi_map(ch, quotes), direction, prot, other)
    arm = Arm(setup, direction, level, day0, atr, 3.0 if vol_ok else 1.0, 2.0)
    m = Machine(setup, direction, level, day0, atr, vol_ok, z0)
    sid = store.create(lvl.pengine, symbol, arm, m, expiry=expiry, zone={"protected": prot, "other": other},
                       day0_oi={}, signals={}, now=datetime.combine(day0, datetime.min.time()))
    return sid, level, atr


def open_level_trade(lvl, symbol="STK05", level=None, account="level_strict", offset=(1, 3)):
    ds = lvl.ds
    ch = ds.chain(symbol, date(2026, 11, 24))
    spot = ds.spot_quotes([symbol])[symbol].last
    ks = sorted(k for k, cp in ch.legs if cp == "CE" and k > spot)
    s, lg = ch.leg(ks[offset[0]], "CE"), ch.leg(ks[offset[1]], "CE")
    r = lvl.broker.open_spread(SpreadOrder(account, symbol, BEAR_CALL, date(2026, 11, 24), "CE", s.tradingsymbol, s.strike,
                                           lg.tradingsymbol, lg.strike, 1, ch.lot_size, ch.tick_size, source="system",
                                           level=level if level is not None else s.strike, book="strict", setup_tag="1",
                                           one_r=10_000))
    assert r.ok
    return r.trade_id, spot


def test_run_close_screens_ranks_and_snapshots(lvl):
    rep = lvl.eng.run_close()
    with Session(lvl.pengine) as s:
        rows = {r.symbol: r for r in s.scalars(select(ScreenerRow).where(ScreenerRow.date == RUN.date()))}
    assert len(rows) == 8
    ranked = sorted((r for r in rows.values() if r.rank), key=lambda r: r.rank)
    assert [r.rank for r in ranked] == list(range(1, len(ranked) + 1)) and ranked
    assert all(r.event_flag and r.liquidity_flag for r in ranked)
    best = [max(v for v in (r.rom_call, r.rom_put) if v is not None) for r in ranked]
    assert best == sorted(best, reverse=True)
    out = [r for r in rows.values() if not (r.event_flag and r.liquidity_flag)]
    assert sum(rep.removed.values()) == len(out)
    assert all(json.loads(r.detail_json)["reason"] for r in out)
    assert rep.screener[0]["symbol"] == ranked[0].symbol and "reference" in rep.screener[0]
    snap = read_snapshots(lvl.cfg.resolve(lvl.cfg.paths.chain_snap), RUN.date())
    assert len(snap) and snap["iv"].notna().any() and {"bid", "ask", "oi", "volume"} <= set(snap.columns)


def test_events_older_than_two_days_remove_every_stock(cfg, platform_cfg, tmp_path):
    lv = build(cfg, platform_cfg, tmp_path)
    lv.ctx.clock.set(RUN + timedelta(days=3))                  # no fetch since: stale
    rep = lv.eng.run_close()
    assert rep.removed.get("event") == 8 and not rep.screener


def test_confirmed_setup_enters_the_strict_book(lvl):
    sid, level, atr = inject(lvl)
    rep = lvl.eng.run_close()
    adv = next(a for a in rep.advanced if a["setup_id"] == sid)
    assert adv["state"] == "confirmed" and adv["strict"] and not adv["research"]
    entry = next(e for e in rep.entries if e["setup_id"] == sid)
    assert entry["book"] == "strict" and entry["trade_id"] and entry["rom"] >= 0.10
    t = lvl.broker.get_trade(entry["trade_id"])
    legs = json.loads(t.legs_json)
    short = next(lg for lg in legs if lg["side"] == "SELL")
    assert t.account == "level_strict" and t.setup_tag == "1" and t.setup_id == sid and t.level == pytest.approx(level)
    assert short["strike"] > level                                        # the short strike sits beyond the level
    assert t.one_r == pytest.approx(t.lots * t.loss_at_kill, abs=1)       # 1R = lots x loss at the kill level
    assert t.max_loss <= 0.15 * 500_000 + 1                               # gap cap per position
    assert store.get(lvl.pengine, sid).strict_trade_id == t.id


def test_failed_volume_trades_in_the_research_book(lvl):
    sid, _, _ = inject(lvl, vol_ok=False)
    rep = lvl.eng.run_close()
    entry = next(e for e in rep.entries if e["setup_id"] == sid)
    assert entry["book"] == "research" and "volume" in entry["failed_filters"]
    t = lvl.broker.get_trade(entry["trade_id"])
    assert t.account == "level_research" and "volume" in json.loads(t.failed_filters)
    assert not lvl.broker.open_trades("level_strict")


def test_before_go_live_the_books_only_report(cfg, platform_cfg, tmp_path):
    lv = build(cfg, relaxed(platform_cfg, live=False), tmp_path)           # go_live_date not set
    sid, _, _ = inject(lv)
    rep = lv.eng.run_close()
    e = next(x for x in rep.entries if x["setup_id"] == sid)
    assert e["dry_run"] and "trade_id" not in e and not rep.live
    with Session(lv.pengine) as s:
        assert not s.scalars(select(PTrade)).all()
    assert "would enter" in store.get(lv.pengine, sid).note


def test_a_close_beyond_the_level_cancels_the_setup(lvl):
    sid, _, _ = inject(lvl, level_atr=-0.3)                               # bear call level below today's close
    rep = lvl.eng.run_close()
    assert next(a for a in rep.advanced if a["setup_id"] == sid)["state"] == "cancelled"
    assert not [e for e in rep.entries if e["setup_id"] == sid]


def test_missed_sessions_are_judged_without_late_entries(lvl):
    sid, _, _ = inject(lvl, day0=date(2026, 11, 3))
    rep = lvl.eng.run_close()
    row = store.get(lvl.pengine, sid)
    sessions = json.loads(row.sessions_json)
    assert [s["date"] for s in sessions] == ["2026-11-04", "2026-11-05"] and all(s["missed"] for s in sessions)
    assert row.state == "expired" and not [e for e in rep.entries if e["setup_id"] == sid]
    assert any("was missed" in w for w in rep.warnings)


def test_kill_level_only_at_the_close_snapshot_and_gaps(lvl):
    tid, spot = open_level_trade(lvl, level=None)
    with Session(lvl.pengine) as s:                                       # level just below the price: beyond
        s.get(PTrade, tid).level = spot - 1.0
        s.commit()
    assert not lvl.eng.exit_check().exits                                 # intraday checks do not kill
    rep = lvl.eng.run_close()
    x = next(e for e in rep.exits if e["trade_id"] == tid)
    assert x["reason"] == "kill_level" and x["ok"] and lvl.broker.get_trade(tid).exit_reason == "kill_level"
    tid2, spot = open_level_trade(lvl, offset=(2, 4))
    with Session(lvl.pengine) as s:
        s.get(PTrade, tid2).level = spot - 1.0
        s.commit()
    assert lvl.eng.gap_check().exits[0]["reason"] == "kill_gap"


def test_profit_target_time_stop_and_event_exit(lvl):
    tid, spot = open_level_trade(lvl)
    with Session(lvl.pengine) as s:                                       # pretend the credit was large
        s.get(PTrade, tid).credit = 500.0
        s.commit()
    assert lvl.eng.exit_check().exits[0]["reason"] == "profit_target"

    tid2, _ = open_level_trade(lvl, offset=(2, 4))
    with lvl.ctx.engine.begin() as conn:                                  # results announced for Monday
        conn.execute(Event.__table__.insert().values(symbol="STK05", event_date=date(2026, 11, 9), event_type="results",
                                                     source="test", fetched_at=RUN))
    assert not [e for e in lvl.eng.exit_check().exits if e["trade_id"] == tid2]   # waits for the 15:20 snapshot
    rep = lvl.eng.run_close()
    assert next(e for e in rep.exits if e["trade_id"] == tid2)["reason"] == "event_exit: results on 2026-11-09"


def test_time_stop_five_sessions_before_expiry(cfg, platform_cfg, tmp_path):
    lv = build(cfg, relaxed(platform_cfg), tmp_path)
    tid, _ = open_level_trade(lv, offset=(3, 5), level=99_999.0)          # a level price never reaches
    lv.ctx.clock.set(datetime(2026, 11, 16, 15, 20))                      # six sessions left: holds
    assert not [e for e in lv.eng.exit_check().exits if e["trade_id"] == tid]
    lv.ctx.clock.set(datetime(2026, 11, 17, 15, 20))                      # the Tuesday a week before expiry
    morning_refresh(lv.ctx)
    rep = lv.eng.run_close()
    assert next(e for e in rep.exits if e["trade_id"] == tid)["reason"] == "time_stop"


def test_official_recheck_logs_close_mismatch(lvl, monkeypatch):
    sid, level, _ = inject(lvl)
    lvl.eng.run_close()
    lvl.ctx.clock.set(RUN.replace(hour=18, minute=30))
    eod = OfficialEOD(RUN.date(), "import", closes={"STK05": {"open": 1, "high": level + 3, "low": 1, "close": level + 2,
                                                              "volume": 1}})
    monkeypatch.setattr(lvl.ds, "official_eod", lambda d, symbols=(), option_keys=(): eod)
    rep = lvl.eng.official_recheck(RUN.date())
    assert any(m["kind"] == "setup_close" and m["symbol"] == "STK05" for m in rep.mismatches)
    with Session(lvl.pengine) as s:
        c = s.scalars(select(CloseCheck).where(CloseCheck.kind == "setup_close")).one()
    assert c.mismatch and c.official_value == pytest.approx(level + 2) and "official beyond" in c.note
