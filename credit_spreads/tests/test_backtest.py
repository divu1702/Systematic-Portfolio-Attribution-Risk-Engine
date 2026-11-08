import math
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import func, select

from app.analytics import black76
from app.backtest import runner
from app.backtest.broker import HistoricalBroker, HistoricalExchange, future_symbol, option_symbol
from app.backtest.store import connect
from app.clock import SimClock
from app.config import load_config
from app.data.models import Equity, IVSnapshot


def _weekdays(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_store(path: Path, symbols=("AAA", "BBB"), n_days=90, chain_from=40, split=None, seed=1):
    """A small store in the DoltHub layout: random-walk bars, weekly Friday expiries 7-40 days out,
    strikes every 5 within +-25%, bid/ask from Black-76 at 30% IV with a 4% spread."""
    rng = np.random.default_rng(seed)
    days = _weekdays(date(2025, 1, 6), n_days)
    conn = connect(path)
    for si, sym in enumerate(symbols):
        S = 100.0 + 20 * si
        for i, d in enumerate(days):
            S *= math.exp(rng.normal(0, 0.015))
            o, c = S * (1 + rng.normal(0, 0.003)), S
            conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?)",
                         (sym, str(d), o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1_000_000))
            if i < chain_from:
                continue
            rows = 0
            e = d + timedelta(days=(4 - d.weekday()) % 7 or 7)
            while (e - d).days <= 40:
                if (e - d).days >= 7:
                    T = (e - d).days / 365
                    F = c * math.exp(0.045 * T)
                    for k in range(int(c * 0.75 / 5) * 5, int(c * 1.25 / 5) * 5 + 5, 5):
                        for cp in ("CE", "PE"):
                            px = black76.price(F, k, T, 0.30, 0.045, cp)
                            if px < 0.03:
                                continue
                            half = max(0.01, px * 0.02)
                            conn.execute("INSERT INTO chains VALUES (?,?,?,?,?,?,?,?,?)",
                                         (str(d), sym, str(e), float(k), cp, round(px - half, 2), round(px + half, 2),
                                          0.30, None))
                            rows += 1
                e += timedelta(days=7)
            conn.execute("INSERT INTO chain_days_loaded VALUES (?,?,?)", (sym, str(d), rows))
        conn.execute("INSERT INTO earnings VALUES (?,?,?)", (sym, str(days[-1] + timedelta(days=30)), "After market close"))
        conn.execute("INSERT INTO earnings VALUES (?,?,?)", (sym, str(days[chain_from] - timedelta(days=20)), "Before market open"))
    if split:
        conn.execute("INSERT INTO splits VALUES (?,?,?,?)", split)
    conn.commit()
    conn.close()
    return days


@pytest.fixture
def store(tmp_path):
    p = tmp_path / "market.db"
    days = build_store(p)
    return p, days


def _broker(store, when):
    p, days = store
    clock = SimClock(when)
    return HistoricalBroker(p, clock, ["AAA", "BBB"], r=0.045), clock


def test_instruments_and_real_quotes(store):
    p, days = store
    d = days[50]
    b, clock = _broker(store, datetime.combine(d, time(15, 25)))
    eq = b.instruments("NSE")
    nfo = b.instruments("NFO")
    assert {r["tradingsymbol"] for r in eq} == {"AAA", "BBB"}
    futs = [r for r in nfo if r["instrument_type"] == "FUT"]
    opts = [r for r in nfo if r["instrument_type"] in ("CE", "PE")]
    assert futs and opts and all(r["lot_size"] == 100 for r in nfo)
    o = opts[0]
    q = b.quotes([f"NFO:{o['tradingsymbol']}"])[f"NFO:{o['tradingsymbol']}"]
    assert q.ask > q.bid > 0 and len(q.asks) == 5
    # Parity forward equals spot x e^(rT) when there are no dividends.
    f = futs[0]
    fq = b.quotes([f"NFO:{f['tradingsymbol']}"])[f"NFO:{f['tradingsymbol']}"]
    spot = b.quotes(["NSE:AAA"])["NSE:AAA"].last if f["name"] == "AAA" else b.quotes(["NSE:BBB"])["NSE:BBB"].last
    T = (f["expiry"] - d).days / 365
    assert fq.last == pytest.approx(spot * math.exp(0.045 * T), rel=0.01)


def test_missing_leg_is_modelled_and_counted(store):
    p, days = store
    d = days[50]
    b, clock = _broker(store, datetime.combine(d, time(15, 25)))
    opt = next(r for r in b.instruments("NFO") if r["instrument_type"] == "PE" and r["name"] == "AAA")
    key = f"NFO:{opt['tradingsymbol']}"
    with sqlite3.connect(p) as conn:                 # remove that strike from the next day's sample
        conn.execute("DELETE FROM chains WHERE date=? AND symbol='AAA' AND expiration=? AND strike=? AND cp='PE'",
                     (str(days[51]), str(opt["expiry"]), opt["strike"]))
    b2, clock2 = _broker(store, datetime.combine(days[51], time(15, 25)))
    q = b2.quote_raw([key])[key]
    assert q["modelled"] is True and b2.modelled_quotes == 1
    assert q["depth"]["sell"][0]["price"] > q["depth"]["buy"][0]["price"] > 0


def test_no_look_ahead_and_expired_contracts(store):
    p, days = store
    d = days[45]
    b, clock = _broker(store, datetime.combine(d, time(15, 25)))
    tok = next(r["instrument_token"] for r in b.instruments("NSE") if r["tradingsymbol"] == "AAA")
    rows = b.historical(tok, datetime(2024, 1, 1), datetime(2030, 1, 1), "day")
    assert rows[-1]["date"].date() == d
    expired = option_symbol("AAA", d - timedelta(days=3), 100, "CE")
    assert b.quote_raw([f"NFO:{expired}"]) == {}


def test_split_adjusts_history_only_after_ex_date(tmp_path):
    p = tmp_path / "m.db"
    days = build_store(p, symbols=("AAA",), split=("AAA", "2025-03-03", 2.0, 1.0))
    ex = date(2025, 3, 3)
    before = HistoricalBroker(p, SimClock(datetime.combine(ex - timedelta(days=3), time(15))), ["AAA"])
    after = HistoricalBroker(p, SimClock(datetime.combine(ex, time(15))), ["AAA"])
    tok = next(r["instrument_token"] for r in before.instruments("NSE"))
    raw = {r["date"]: r["close"] for r in before.historical(tok, datetime(2025, 1, 1), datetime(2025, 2, 27), "day")}
    adj = {r["date"]: r["close"] for r in after.historical(tok, datetime(2025, 1, 1), datetime(2025, 2, 27), "day")}
    some = next(iter(raw))
    assert adj[some] == pytest.approx(raw[some] / 2)
    assert after.splits_on(ex) == ["AAA"]


def test_exchange_events_respect_notice(store):
    p, days = store
    early = days[50]
    ex = HistoricalExchange(p, SimClock(datetime.combine(early, time(9))), ["AAA", "BBB"], {"AAA": "X", "BBB": "Y"},
                            notice_days=14)
    future_earnings = days[-1] + timedelta(days=30)
    evs = ex.events(early - timedelta(days=120), future_earnings + timedelta(days=5))
    assert all(e["event_date"] != future_earnings for e in evs)        # not announced yet
    late = HistoricalExchange(p, SimClock(datetime.combine(future_earnings - timedelta(days=10), time(9))),
                              ["AAA", "BBB"], {}, notice_days=14)
    assert any(e["event_date"] == future_earnings for e in late.events(early, future_earnings + timedelta(days=5)))
    assert ex.sectors() == {"AAA": "X", "BBB": "Y"}


def test_trading_days_need_chains(store):
    p, days = store
    td = runner.trading_days(p, days[0], days[-1])
    assert td[0] == days[40] and td[-1] == days[-1]


def test_backtest_end_to_end_smoke(store, tmp_path):
    p, days = store
    cfg = runner.us_config(load_config(), capital=100_000)
    td = runner.trading_days(p, days[0], days[-1])
    ctx = runner.make_context(cfg, p, tmp_path / "bt.db", td[0], {"AAA": "Tech", "BBB": "Energy"})
    run = runner.run_backtest(ctx, td, warmup=10)
    with ctx.engine.connect() as conn:
        n_eq = conn.execute(select(func.count()).select_from(Equity).where(Equity.strategy == "main")).scalar()
        n_snap = conn.execute(select(func.count()).select_from(IVSnapshot)).scalar()
    assert n_eq == len(td)
    assert n_snap == 2 * len(td)
    from app.views import backtest_summary
    text = backtest_summary(run)
    assert "Go-live scorecard" in text and "Rejection funnel" in text
    assert "$" in text                                          # dollars, not rupees
    from app.fmt import set_currency
    set_currency("INR")


def test_us_config_costs_per_contract():
    cfg = runner.us_config(load_config())
    from app.strategy.costs import order_cost, round_trip_costs
    assert order_cost(cfg.costs, "SELL", 1.5, 300, lot=100).total == pytest.approx(3 * 0.70)
    assert round_trip_costs(cfg.costs, 1.5, 0.6, 100, lot=100) == pytest.approx(4 * 0.70)
    assert cfg.market.currency == "USD" and not cfg.monitor.replay_missed


def test_live_recorder_builds_a_replayable_nse_store(mock_ctx, tmp_path):
    """Record one day from the (mock) live system, then replay it with the historical broker."""
    from datetime import datetime, time

    from app.jobs import eod_job, morning_refresh, scan_job

    mock_ctx.is_mock = False                       # the recorder is off for mock contexts
    try:
        morning_refresh(mock_ctx)
        scan_job(mock_ctx)
        mock_ctx.clock.set(datetime.combine(mock_ctx.clock.today(), time(15, 35)))
        eod_job(mock_ctx)
    finally:
        mock_ctx.is_mock = True
    store = mock_ctx.cfg.resolve(mock_ctx.cfg.paths.market_store)
    conn = sqlite3.connect(store)
    n_chain = conn.execute("SELECT COUNT(*) FROM chains").fetchone()[0]
    n_extra = conn.execute("SELECT COUNT(*) FROM chain_extras WHERE lot > 0 AND bid_depth >= bid_qty").fetchone()[0]
    n_fut = conn.execute("SELECT COUNT(*) FROM futures").fetchone()[0]
    n_bars = conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
    n_ev = conn.execute("SELECT COUNT(*) FROM events_seen").fetchone()[0]
    with_iv = conn.execute("SELECT COUNT(*) FROM chains WHERE iv > 0").fetchone()[0]
    conn.close()
    assert n_chain > 100 and n_extra == n_chain and n_fut > 0 and n_bars >= 8 and n_ev > 0
    assert with_iv > 0.8 * n_chain

    uni = runner.store_universe(store)
    assert uni and all(sec != "UNKNOWN" for sec in uni.values())        # sectors were recorded
    cfg = runner.nse_config(mock_ctx.cfg)
    d = mock_ctx.clock.today()
    ctx = runner.make_context(cfg, store, tmp_path / "replay.db", d, uni)
    ctx.clock.set(datetime.combine(d, time(15, 25)))
    rows = ctx.broker.instruments("NFO")
    spec = mock_ctx.broker.market.spec(next(iter(uni)))
    lots = {r["lot_size"] for r in rows if r["name"] == spec.name}
    assert lots == {spec.lot}                                            # recorded lot size, not a default
    opt = next(r for r in rows if r["instrument_type"] == "PE" and r["name"] == spec.name)
    q = ctx.broker.quotes([f"NFO:{opt['tradingsymbol']}"])[f"NFO:{opt['tradingsymbol']}"]
    assert q.bids and q.asks and q.bids[0].quantity % spec.lot == 0       # recorded depth, in lots
    fut = next(r for r in rows if r["instrument_type"] == "FUT" and r["name"] == spec.name)
    fq = ctx.broker.quotes([f"NFO:{fut['tradingsymbol']}"])[f"NFO:{fut['tradingsymbol']}"]
    assert fq.last > 0
    assert "NSE quotes recorded" in ctx.banner
    from app.fmt import set_currency
    set_currency("INR")
