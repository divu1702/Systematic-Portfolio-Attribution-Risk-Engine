"""Shared paper broker: fills at natural prices plus a tick, the Zerodha cost stack (checked by hand),
margin blocking with the physical-delivery step-up, rejections, expiry settlement with STT on exercise
and delivery brokerage, the per-account ledger and the account summaries."""

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from platform_core import accounts
from platform_core.broker import PaperBroker, SpreadOrder
from platform_core.costs import CostModel
from platform_core.data_service import DataService
from platform_core.models import Account, AccountDay, LedgerEntry, POrder, PTrade

GO_LIVE = date(2026, 11, 2)
EXPIRY = date(2026, 11, 24)          # a Tuesday; no holidays in the mock calendar that week


@pytest.fixture
def live(platform):
    cfg = platform.cfg.model_copy(update={"go_live_date": GO_LIVE})
    ds = DataService(platform.ctx, cfg, platform.pengine)
    b = PaperBroker(platform.pengine, cfg, ds)
    b.ensure_accounts()
    return SimpleNamespace(ctx=platform.ctx, cfg=cfg, ds=ds, broker=b, pengine=platform.pengine)


def bear_call(ds, account="level_strict", lots=1, offset=(1, 3), **kw) -> SpreadOrder:
    ch = ds.chain("STK01", EXPIRY)
    spot = ds.spot_quotes(["STK01"])["STK01"].last
    ks = sorted(k for k, cp in ch.legs if cp == "CE" and k > spot)
    s, lg = ch.leg(ks[offset[0]], "CE"), ch.leg(ks[offset[1]], "CE")
    return SpreadOrder(account, "STK01", "bear_call", EXPIRY, "CE", s.tradingsymbol, s.strike, lg.tradingsymbol,
                       lg.strike, lots, ch.lot_size, ch.tick_size, **kw)


def legs(t: PTrade):
    lg = json.loads(t.legs_json)
    return next(x for x in lg if x["side"] == "SELL"), next(x for x in lg if x["side"] == "BUY")


def ledger(pengine, account):
    with Session(pengine) as s:
        return s.scalars(select(LedgerEntry).where(LedgerEntry.account == account).order_by(LedgerEntry.id)).all()


def test_costs_by_hand(platform_cfg):
    cm = CostModel(platform_cfg.costs, platform_cfg.settlement)
    sell = cm.order("SELL", 10.0, 2500)                          # turnover 25,000
    assert sell.brokerage == 20 and sell.stt == pytest.approx(37.5) and sell.exchange == pytest.approx(8.8825)
    assert sell.sebi == pytest.approx(0.025) and sell.stamp == 0 and sell.gst == pytest.approx(0.18 * 28.9075)
    assert sell.total == pytest.approx(71.61085)
    buy = cm.order("BUY", 4.0, 2500)                             # turnover 10,000
    assert buy.stt == 0 and buy.stamp == pytest.approx(0.3) and buy.exchange == pytest.approx(3.553)
    assert buy.total == pytest.approx(28.10434)
    assert cm.open_spread(10.0, 4.0, 2500).total == pytest.approx(71.61085 + 28.10434)
    assert cm.close_spread(4.0, 10.0, 2500).total == pytest.approx(cm.order("BUY", 4.0, 2500).total
                                                                   + cm.order("SELL", 10.0, 2500).total)
    assert cm.order("BUY", 0.0, 2500).total == 0                  # nothing traded, nothing charged
    with pytest.raises(ValueError):
        cm.order("HOLD", 1.0, 1)


def test_settlement_by_hand(platform_cfg):
    cm = CostModel(platform_cfg.costs, platform_cfg.settlement)
    calls = [("SELL", 380.0, "CE", 2500), ("BUY", 400.0, "CE", 2500)]
    net, ch = cm.settlement(calls, 370.0)                         # both out of the money: worthless, no charges
    assert net == 0 and ch.total == 0
    net, ch = cm.settlement(calls, 390.0)                         # the short leg is in the money
    assert net == pytest.approx(-25_000) and ch.stt == 0
    assert ch.brokerage == pytest.approx(0.0025 * 380 * 2500) and ch.gst == pytest.approx(0.18 * 2375)
    net, ch = cm.settlement(calls, 410.0)                         # both in the money: the full width is lost
    assert net == pytest.approx(-50_000)
    assert ch.stt == pytest.approx(0.0015 * 10 * 2500)            # STT on exercise: the bought leg's intrinsic value
    assert ch.brokerage == pytest.approx(0.0025 * (380 + 400) * 2500)
    puts = [("SELL", 340.0, "PE", 1000), ("BUY", 320.0, "PE", 1000)]
    net, ch = cm.settlement(puts, 330.0)
    assert net == pytest.approx(-10_000) and ch.brokerage == pytest.approx(0.0025 * 340 * 1000)


def test_orders_rejected_before_go_live(platform):
    b = PaperBroker(platform.pengine, platform.cfg, platform.ds)   # the shipped go_live_date is null
    b.ensure_accounts()
    b.ensure_accounts()                                            # idempotent
    assert b.state("discretionary").started is False
    r = b.open_spread(bear_call(platform.ds, "discretionary"))
    assert not r.ok and "has not started" in r.reason
    with Session(platform.pengine) as s:
        o = s.scalars(select(POrder)).one()
        assert o.status == "rejected" and "go_live_date" in o.rejection_reason
        assert s.scalar(select(func.count()).select_from(PTrade)) == 0
        assert sorted(a.id for a in s.scalars(select(Account))) == sorted(platform.cfg.accounts)
        assert s.scalar(select(func.count()).select_from(LedgerEntry)) == 0


def test_accounts_open_once_on_go_live(live):
    live.broker.ensure_accounts()
    for acc in live.cfg.accounts:
        rows = ledger(live.pengine, acc)
        assert [r.entry_type for r in rows] == ["opening"] and rows[0].cash_balance == 500_000
        assert rows[0].ts.date() == GO_LIVE
        assert live.broker.state(acc).available == 500_000


def test_open_mark_close_and_running_ledger(live):
    b = live.broker
    order = bear_call(live.ds)
    r = b.open_spread(order)
    assert r.ok and r.credit > 0 and r.margin > 0 and not r.margin_fallback
    assert r.short_fill.price == pytest.approx(r.short_fill.natural - 0.05)    # sold at the bid less a tick
    assert r.long_fill.price == pytest.approx(r.long_fill.natural + 0.05)      # bought at the ask plus a tick
    t = b.open_trades("level_strict")[0]
    qty = t.lots * t.lot_size
    width = abs(order.long_strike - order.short_strike)
    assert t.max_loss == pytest.approx((width - r.credit) * qty, abs=0.01)
    assert t.one_r == pytest.approx(t.max_loss)                                # no stop given: 1R = maximum loss
    assert t.rom == pytest.approx((r.credit * qty - b.costs.round_trip(r.short_fill.price, r.long_fill.price, qty))
                                  / r.margin)
    st = b.state("level_strict")
    assert st.cash_balance == pytest.approx(500_000 + r.credit * qty - r.charges, abs=0.02)
    assert st.used_margin == pytest.approx(r.margin) and st.available == pytest.approx(st.cash_balance - r.margin)
    assert b.state("oi_tight").cash_balance == 500_000                         # accounts are separate
    m = b.mark()[t.id]
    assert m["cost_to_close"] > 0
    v = b.account_value("level_strict")
    assert v["value"] == pytest.approx(st.cash_balance - m["cost_to_close"] * qty, abs=0.02)

    x = b.close_spread(t.id, "profit_target")
    assert x.ok and x.short_fill.price == pytest.approx(x.short_fill.natural + 0.05)
    with Session(live.pengine) as s:
        t2 = s.get(PTrade, t.id)
    assert t2.status == "closed" and t2.exit_reason == "profit_target" and t2.holding_days == 0
    assert t2.pnl_inr == pytest.approx((t2.credit - t2.exit_cost) * qty - t2.entry_charges - t2.exit_charges, abs=0.02)
    assert t2.pnl_r == pytest.approx(t2.pnl_inr / t2.one_r, abs=1e-3)
    rows = ledger(live.pengine, "level_strict")
    assert [x.entry_type for x in rows] == ["opening", "premium", "charges", "margin_block",
                                            "premium", "charges", "margin_release", "realised_pnl"]
    cash = 0.0
    for row in rows:                                                          # running balances
        if row.entry_type in ("opening", "premium", "charges", "settlement"):
            cash += row.amount
        assert row.cash_balance == pytest.approx(cash, abs=0.05)
    end = b.state("level_strict")
    assert end.used_margin == 0 and end.cash_balance == pytest.approx(500_000 + t2.pnl_inr, abs=0.05)
    assert b.close_spread(t.id, "again").ok is False


def test_rejected_when_available_margin_is_short(live, monkeypatch):
    monkeypatch.setattr(live.ds, "margin", lambda legs, prices=None: 600_000.0)
    r = live.broker.open_spread(bear_call(live.ds))
    assert not r.ok and "insufficient margin" in r.reason
    with Session(live.pengine) as s:
        assert s.scalars(select(POrder)).one().status == "rejected"
        assert s.scalar(select(func.count()).select_from(PTrade)) == 0
    assert [x.entry_type for x in ledger(live.pengine, "level_strict")] == ["opening"]


def test_rejected_without_depth(live):
    r = live.broker.open_spread(bear_call(live.ds, lots=100_000))
    assert not r.ok and "no fill" in r.reason and "depth" in r.reason


def test_margin_fallback_blocks_the_maximum_loss(live):
    live.ctx.broker.fail_margin = True
    r = live.broker.open_spread(bear_call(live.ds))
    assert r.ok and r.margin_fallback
    t = live.broker.open_trades("level_strict")[0]
    assert t.margin == pytest.approx(t.max_loss)
    assert ledger(live.pengine, "level_strict")[-1].note.startswith("margin (fallback")


def test_delivery_margin_step_up(live):
    b = live.broker
    b.open_spread(bear_call(live.ds))
    t = b.open_trades("level_strict")[0]
    short, long_ = legs(t)
    qty = short["qty"]
    spot = short["strike"] + 5                       # short leg in the money, long leg not
    full = 0.20 * qty * spot
    assert b.delivery_addon(t, date(2026, 11, 17), spot) == 0                    # time-stop session: no step-up yet
    assert b.delivery_addon(t, date(2026, 11, 18), spot) == pytest.approx(0.10 * full)   # the Wednesday before
    assert b.delivery_addon(t, date(2026, 11, 19), spot) == pytest.approx(0.25 * full)
    assert b.delivery_addon(t, date(2026, 11, 20), spot) == pytest.approx(0.45 * full)
    assert b.delivery_addon(t, date(2026, 11, 23), spot) == pytest.approx(0.70 * full)
    assert b.delivery_addon(t, EXPIRY, spot) == pytest.approx(full)
    assert b.delivery_addon(t, date(2026, 11, 20), short["strike"] - 10) == 0    # out of the money: nothing to deliver
    assert b.delivery_addon(t, date(2026, 11, 20), long_["strike"] + 10) == 0    # both in the money: obligations net off
    assert b.delivery_addon(t, date(2026, 11, 20), None) == 0


def test_daily_margin_recompute(live, monkeypatch):
    b = live.broker
    b.open_spread(bear_call(live.ds))
    t = b.open_trades("level_strict")[0]
    short, _ = legs(t)
    base = t.margin + 5_000
    monkeypatch.setattr(live.ds, "margin", lambda legs, prices=None: base)
    assert b.recompute_margins(date(2026, 11, 9)) == []
    assert b.open_trades("level_strict")[0].margin == pytest.approx(base)
    assert b.state("level_strict").used_margin == pytest.approx(base)
    assert ledger(live.pengine, "level_strict")[-1].entry_type == "margin_adjust"

    itm = short["strike"] + 5
    monkeypatch.setattr(live.ds, "spot_quotes", lambda syms: {"STK01": SimpleNamespace(last=itm, mid=None)})
    b.recompute_margins(date(2026, 11, 20))                                    # two sessions before expiry
    want = base + 0.45 * 0.20 * short["qty"] * itm
    assert b.open_trades("level_strict")[0].margin == pytest.approx(want, abs=0.02)
    assert b.state("level_strict").used_margin == pytest.approx(want, abs=0.02)

    monkeypatch.setattr(live.ds, "margin", lambda legs, prices=None: 900_000.0)
    msgs = b.recompute_margins(date(2026, 11, 10))
    assert msgs and "margin shortfall" in msgs[0]


def test_expiry_settlement_charges_stt_and_delivery_brokerage(live):
    b = live.broker
    b.open_spread(bear_call(live.ds))
    t = b.open_trades("level_strict")[0]
    short, _ = legs(t)
    qty, spot = short["qty"], short["strike"] + 7
    assert b.settle_expired(date(2026, 11, 23), {"STK01": spot}) == []         # not expired yet
    assert "settlement waits" in b.settle_expired(EXPIRY, {})[0]               # no official close: stays open
    assert b.open_trades("level_strict")
    msgs = b.settle_expired(EXPIRY, {"STK01": spot})
    assert len(msgs) == 1 and "settled at expiry" in msgs[0]
    with Session(live.pengine) as s:
        t2 = s.get(PTrade, t.id)
    charges = 0.0025 * short["strike"] * qty * 1.18                            # delivery brokerage plus GST
    assert t2.status == "closed" and t2.exit_reason == "expiry_settlement"
    assert t2.exit_charges == pytest.approx(charges, abs=0.01)
    assert t2.pnl_inr == pytest.approx(t.credit * qty - 7 * qty - t.entry_charges - charges, abs=0.05)
    kinds = [x.entry_type for x in ledger(live.pengine, "level_strict")]
    assert kinds[-4:] == ["settlement", "charges", "margin_release", "realised_pnl"]
    st = b.state("level_strict")
    assert st.used_margin == 0 and st.cash_balance == pytest.approx(500_000 + t2.pnl_inr, abs=0.05)


def test_account_summaries_positions_and_funds(live, monkeypatch):
    b = live.broker
    b.open_spread(bear_call(live.ds))
    b.open_spread(bear_call(live.ds, account="oi_tight", offset=(2, 4)))
    b.record_day(date(2026, 11, 6))
    b.record_day(date(2026, 11, 6))                                           # one row per account and day
    with Session(live.pengine) as s:
        assert s.scalar(select(func.count()).select_from(AccountDay)) == 5
    ov = accounts.overview(b, date(2026, 11, 9))
    assert [o["account"] for o in ov] == list(live.cfg.accounts)
    strict = next(o for o in ov if o["account"] == "level_strict")
    assert strict["open_positions"] == 1 and strict["win_rate"] is None and strict["closed_trades"] == 0
    assert strict["total_pnl"] == pytest.approx(strict["value"] - 500_000)
    assert strict["equity_curve"] == [{"date": "2026-11-06", "value": pytest.approx(strict["value"], abs=0.02)}]
    assert strict["today_pnl"] == pytest.approx(strict["value"] - strict["equity_curve"][-1]["value"], abs=0.02)
    idle = next(o for o in ov if o["account"] == "discretionary")
    assert idle["value"] == 500_000 and idle["open_positions"] == 0 and idle["total_pnl"] == 0

    pos = accounts.positions(b, "level_strict")
    assert len(pos) == 1 and pos[0]["cost_to_close"] > 0 and pos[0]["margin"] > 0 and len(pos[0]["legs"]) == 2
    f = accounts.funds(live.pengine, "level_strict")
    assert f[0]["type"] == "margin_block" and f[-1]["type"] == "opening"
    assert f[0]["available"] == pytest.approx(f[0]["cash_balance"] - f[0]["used_margin"])

    b.mark()                                                                  # saved marks stand in after the close
    with monkeypatch.context() as mp:
        mp.setattr(live.ds, "quotes", lambda keys: {})
        after = accounts.positions(b, "level_strict")
        assert after[0]["cost_to_close"] == pos[0]["cost_to_close"] and after[0]["stale_mark"]
        v = b.account_value("level_strict")
        assert v["unrealised"] == pytest.approx(after[0]["pnl"], abs=0.02)

    b.close_spread(pos[0]["id"], "test")
    s2 = accounts.summary(b, "level_strict")
    assert s2["closed_trades"] == 1 and s2["win_rate"] in (0.0, 1.0) and s2["open_positions"] == 0
    with Session(live.pengine) as s:
        closed = s.get(PTrade, pos[0]["id"])
    assert s2["pnl_r"] == pytest.approx(closed.pnl_r, abs=0.01)
    assert s2["total_pnl"] == pytest.approx(closed.pnl_inr, abs=0.05)
