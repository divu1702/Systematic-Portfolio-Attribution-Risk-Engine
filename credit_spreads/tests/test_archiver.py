"""Kite history archiver: intraday candles in two passes per contract, compressed storage,
request budget counted per Kite request, resumable, and honest about what was missed."""

import math
import sqlite3
from datetime import date, datetime, time, timedelta

import numpy as np
import pytest

from app.data import barcodec
from app.data.history_archiver import archive_status, chunks, last_session, run_archive

NOV, DEC = date(2026, 11, 24), date(2026, 12, 29)


def _archive_cfg(ctx, **kw):
    base = {"band": 0.04, "max_expiries": 2, "equity_days": 120}
    ctx.cfg = ctx.cfg.model_copy(update={"archive": ctx.cfg.archive.model_copy(update={**base, **kw})})
    return ctx


@pytest.fixture
def archive_ctx(mock_ctx):
    mock_ctx.cache["archive_on_mock"] = True
    return _archive_cfg(mock_ctx)


def _db(ctx):
    return sqlite3.connect(ctx.cfg.resolve(ctx.cfg.paths.market_store))


def test_codec_round_trip_and_size():
    t0 = datetime(2026, 11, 6, 9, 15)
    rng = np.random.default_rng(3)
    px, rows = 41.35, []
    for k in range(0, 375, 2):
        px = max(0.05, round((px + rng.normal(0, 0.2)) / 0.05) * 0.05)
        rows.append({"date": t0 + timedelta(minutes=k), "open": round(px, 2), "high": round(px + 0.1, 2),
                     "low": round(max(0.05, px - 0.1), 2), "close": round(px, 2),
                     "volume": int(rng.integers(1, 30)) * 650, "oi": 1_300_000 + 650 * k})
    cols = barcodec.candle_columns(list(reversed(rows)))              # any order in, sorted out
    blob = barcodec.encode(cols)
    back = barcodec.decode(blob)
    assert list(back["minute"]) == [9 * 60 + 15 + k for k in range(0, 375, 2)]
    for k in ("open", "high", "low", "close"):
        assert np.allclose(back[k], [r[k] for r in rows])
    assert list(back["volume"]) == [r["volume"] for r in rows] and list(back["oi"]) == [r["oi"] for r in rows]
    assert len(blob) / len(rows) < 15                                 # vs ~100 bytes as SQLite rows
    q = barcodec.encode({"minute": [570, 585], "bid": [10.05, 10.1], "ask": [10.4, 10.35], "last": [10.2, 10.25],
                         "bid_qty": [650, 1300], "ask_qty": [650, 650], "bid_depth": [3250, 3900],
                         "ask_depth": [2600, 2600], "volume": [6500, 9100], "oi": [10**6, 10**6 + 650]}, b"Q")
    assert np.allclose(barcodec.decode(q)["ask"], [10.4, 10.35])
    assert barcodec.at_minute(back, 15 * 60, 30) >= 0 and barcodec.at_minute({"minute": np.array([600])}, 900, 30) == -1


def test_chunks_follow_kite_span_limits():
    s, e = datetime(2026, 8, 16), datetime(2026, 11, 5, 23, 59)
    assert len(chunks(s, e, "minute")) == 2                           # 81 days, 60 per request
    assert len(chunks(s, e, "5minute")) == 1 and len(chunks(s, e, "day")) == 1
    parts = chunks(s, e, "minute")
    assert parts[0][0] == s and parts[-1][1] == e and all(b - a <= timedelta(days=60) for a, b in parts)


def test_first_pass_minute_candles_budget_and_resume(archive_ctx):
    ctx = archive_ctx
    first = run_archive(ctx, budget=10)
    assert first.requests <= 10 and first.pending > 0                 # budget respected, work left over
    for _ in range(40):
        rep = run_archive(ctx, budget=400)
        if rep.requests == 0:
            break
    assert rep.requests == 0 and rep.pending == 0                     # everything done, nothing re-fetched
    db = _db(ctx)
    prog = db.execute("SELECT tradingsymbol, kind, expiration, last_date, requests, final FROM archive_progress").fetchall()
    assert prog and {p[2] for p in prog} == {str(NOV)}                 # Dec (53 days left) waits for its first pass
    assert all(p[3] == "2026-11-05" and p[5] == 0 for p in prog)       # up to the last closed session
    assert {p[4] for p in prog if p[1] == "option"} == {2}             # 81 days of 1-minute data = 2 requests
    sym, exp, k, cp, d, interval, n, lot, blob = db.execute(
        "SELECT * FROM option_bars ORDER BY n DESC LIMIT 1").fetchone()
    cols = barcodec.decode(blob)
    assert interval == "minute" and len(cols["minute"]) == n and (np.diff(cols["minute"]) > 0).all()
    assert (cols["volume"] > 0).all() and (cols["close"] > 0).all()
    daily = db.execute("SELECT close, volume, high, low FROM option_candles WHERE date=? AND symbol=? AND "
                       "expiration=? AND strike=? AND cp=?", (d, sym, exp, k, cp)).fetchone()
    assert daily[0] == pytest.approx(cols["close"][-1]) and daily[1] == int(cols["volume"].sum())
    assert daily[2] == pytest.approx(cols["high"].max()) and daily[3] == pytest.approx(cols["low"].min())
    assert lot == ctx.broker.market.spec(sym).lot
    n_fut_bars = db.execute("SELECT COUNT(*) FROM future_bars").fetchone()[0]
    n_cont = db.execute("SELECT COUNT(*) FROM future_candles WHERE expiration='continuous'").fetchone()[0]
    n_bars = db.execute("SELECT COUNT(DISTINCT symbol), MAX(date) FROM bars").fetchone()
    db.close()
    assert n_fut_bars > 0 and n_cont > 0 and n_bars == (8, "2026-11-05")
    assert "intraday option candles" in archive_status(ctx)


def test_thin_strikes_store_fewer_minutes(archive_ctx):
    ctx = _archive_cfg(archive_ctx, band=0.25, max_expiries=1)
    run_archive(ctx, budget=10**6)
    db = _db(ctx)
    rows = db.execute("SELECT b.symbol, b.strike, SUM(b.n), COUNT(*) FROM option_bars b GROUP BY b.symbol, b.strike, b.cp").fetchall()
    spots = dict(db.execute("SELECT symbol, close FROM bars WHERE date='2026-11-05'").fetchall())
    db.close()
    near = [n / days for s, k, n, days in rows if abs(k / spots[s] - 1) < 0.02]
    far = [n / days for s, k, n, days in rows if abs(k / spots[s] - 1) > 0.15]
    assert near and far and np.median(near) > 3 * np.median(far)       # rows scale with trading, not with time


def test_final_pass_in_expiry_week(archive_ctx):
    ctx = archive_ctx
    run_archive(ctx, budget=10**6)                                     # first pass for the Nov series
    ctx.clock.set(datetime.combine(date(2026, 11, 19), time(16, 10)))  # 3 trading days before expiry
    rep = run_archive(ctx, budget=10**6)
    assert rep.options > 0 and rep.expiring == [str(NOV)] and not rep.urgent
    db = _db(ctx)
    nov = db.execute("SELECT last_date, requests, final FROM archive_progress WHERE kind='option' AND expiration=?",
                     (str(NOV),)).fetchall()
    dec = db.execute("SELECT COUNT(*) FROM archive_progress WHERE expiration=? AND final=0", (str(DEC),)).fetchone()[0]
    sym, k, cp = db.execute("SELECT symbol, strike, cp FROM option_bars WHERE expiration=? GROUP BY symbol, strike, cp "
                            "ORDER BY SUM(n) DESC LIMIT 1", (str(NOV),)).fetchone()
    days = [d for (d,) in db.execute("SELECT date FROM option_candles WHERE symbol=? AND strike=? AND cp=? AND "
                                     "expiration=? AND date>='2026-11-02' ORDER BY date", (sym, k, cp, str(NOV)))]
    db.close()
    assert all(r[0] == "2026-11-19" and r[2] == 1 for r in nov)        # complete up to the final pass
    assert {r[1] for r in nov} == {2, 3}       # 2 + 1 requests; strikes new to the band: full history at once
    assert dec > 0                                                      # Dec now within 45 days: first pass
    from app.data.calendar import TradingCalendar
    cal = TradingCalendar.load(ctx.engine)
    want, d = [], date(2026, 11, 2)
    while d <= date(2026, 11, 19):
        if cal.is_trading_day(d):
            want.append(str(d))
        d += timedelta(days=1)
    assert days == want                                                 # no gap between the two passes
    assert run_archive(ctx, budget=10**6).requests == 0                  # Nov is not fetched again
    db = _db(ctx)
    again = db.execute("SELECT MAX(requests) FROM archive_progress WHERE expiration=?", (str(NOV),)).fetchone()[0]
    db.close()
    assert again == 3


def test_missed_final_pass_is_reported(archive_ctx):
    ctx = archive_ctx
    run_archive(ctx, budget=10**6)                                     # first pass only
    ctx.clock.set(datetime.combine(date(2026, 11, 26), time(16, 10)))  # Nov series has expired unseen
    rep = run_archive(ctx, budget=10**6)
    assert rep.missed > 0 and "expired before their final pass" in rep.text()
    assert run_archive(ctx, budget=10**6).missed == 0                  # reported once
    assert "missed final pass" in archive_status(ctx)


def test_budget_exhaustion_flags_urgent_series(archive_ctx):
    ctx = archive_ctx
    ctx.clock.set(datetime.combine(date(2026, 11, 19), time(16, 10)))
    rep = run_archive(ctx, budget=30)
    assert rep.pending > 0 and rep.urgent > 0 and "about to expire" in rep.text()
    from app.scheduler import archive_job

    sent = []
    ctx.notifier.send = lambda msg, kind="info": sent.append((kind, msg))
    ctx.cfg = ctx.cfg.model_copy(update={"archive": ctx.cfg.archive.model_copy(update={"max_requests_per_run": 30})})
    archive_job(ctx)
    assert sent and sent[0][0] == "warning"


def test_daily_interval_keeps_the_old_behaviour(archive_ctx):
    ctx = _archive_cfg(archive_ctx, interval="day")
    run_archive(ctx, budget=10**6)
    db = _db(ctx)
    n_bars = db.execute("SELECT COUNT(*) FROM option_bars").fetchone()[0]
    n_daily = db.execute("SELECT COUNT(*) FROM option_candles").fetchone()[0]
    reqs = {r for (r,) in db.execute("SELECT requests FROM archive_progress WHERE kind='option'")}
    db.close()
    assert n_bars == 0 and n_daily > 0 and reqs == {1}


def test_last_session_waits_for_the_close(mock_ctx):
    from app.data.calendar import TradingCalendar

    cal = TradingCalendar.load(mock_ctx.engine)
    assert last_session(cal, datetime(2026, 11, 6, 15, 0)) == date(2026, 11, 5)
    assert last_session(cal, datetime(2026, 11, 6, 16, 10)) == date(2026, 11, 6)
    assert last_session(cal, datetime(2026, 11, 8, 10, 0)) == date(2026, 11, 6)       # Sunday


def test_archiver_off_for_mock_and_when_disabled(mock_ctx):
    assert run_archive(mock_ctx).requests == 0                         # mock without the test flag
    mock_ctx.cache["archive_on_mock"] = True
    mock_ctx.cfg = mock_ctx.cfg.model_copy(update={"archive": mock_ctx.cfg.archive.model_copy(update={"enabled": False})})
    assert run_archive(mock_ctx).requests == 0


# ----------------------------------------------------------------------------- intraday quote snapshots

SNAP_DAY = date(2026, 11, 9)                     # the trading day after SIM_NOW


def _snap_at(ctx, hh, mm):
    from app.data.market_recorder import snapshot_quotes

    ctx.clock.set(datetime.combine(SNAP_DAY, time(hh, mm)))     # the clock only moves forward
    return snapshot_quotes(ctx)


def test_quote_snapshots_are_staged_then_compacted(mock_ctx):
    from app.data.market_recorder import compact_snapshots

    ctx = mock_ctx
    ctx.is_mock = False                                  # the recorder is off for mock contexts
    try:
        assert _snap_at(ctx, 9, 17) == 0                 # before snapshots.start
        n1, n2 = _snap_at(ctx, 9, 32), _snap_at(ctx, 9, 47)
    finally:
        ctx.is_mock = True
    assert n1 > 0 and abs(n2 - n1) <= 0.05 * n1          # the band follows the futures price
    db = _db(ctx)
    n_keys = db.execute("SELECT COUNT(*) FROM (SELECT DISTINCT symbol, expiration, strike, cp FROM quote_snap_rows)").fetchone()[0]
    both = db.execute("SELECT symbol, expiration, strike, cp FROM quote_snap_rows WHERE cp='PE' GROUP BY symbol, "
                      "expiration, strike, cp HAVING COUNT(*) = 2 LIMIT 1").fetchone()
    staged = db.execute("SELECT COUNT(*), COUNT(DISTINCT ts) FROM quote_snap_rows").fetchone()
    kinds = {k for (k,) in db.execute("SELECT DISTINCT cp FROM quote_snap_rows")}
    exps = db.execute("SELECT symbol, COUNT(DISTINCT expiration) FROM quote_snap_rows WHERE cp IN ('CE','PE') "
                      "GROUP BY symbol").fetchall()
    worst = db.execute("SELECT MAX(ABS(o.strike / ((f.bid + f.ask) / 2) - 1)) FROM quote_snap_rows o JOIN quote_snap_rows f "
                       "ON f.ts=o.ts AND f.symbol=o.symbol AND f.expiration=o.expiration AND f.cp='FUT' "
                       "WHERE o.cp IN ('CE','PE')").fetchone()[0]
    db.close()
    assert staged == (n1 + n2, 2) and kinds == {"EQ", "FUT", "CE", "PE"}
    assert all(n <= 2 for _, n in exps) and worst <= 0.15 + 1e-9    # nearest 2 series, +-15% band
    assert compact_snapshots(ctx) == n1 + n2
    db = _db(ctx)
    left = db.execute("SELECT COUNT(*) FROM quote_snap_rows").fetchone()[0]
    blob = db.execute("SELECT data FROM quote_snaps WHERE symbol=? AND expiration=? AND strike=? AND cp=?", both).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM quote_snaps").fetchone()[0]
    db.close()
    q = barcodec.decode(blob)
    assert left == 0 and total == n_keys and list(q["minute"]) == [9 * 60 + 32, 9 * 60 + 47]
    assert ((q["ask"] >= q["bid"]) & (q["ask"] > 0)).all()
    ctx.is_mock = False
    try:
        _snap_at(ctx, 10, 2)
    finally:
        ctx.is_mock = True
    compact_snapshots(ctx)                               # a later snapshot merges into the same blobs
    db = _db(ctx)
    minutes = [list(barcodec.decode(b)["minute"]) for (b,) in db.execute("SELECT data FROM quote_snaps WHERE cp='EQ'")]
    db.close()
    assert len(minutes) == 8 and all(m == [572, 587, 602] for m in minutes)
    assert "quote snapshots" in archive_status(ctx)


def test_snapshots_off_and_failures_do_not_disturb_trading(mock_ctx):
    from app.scheduler import snapshot_job

    ctx = mock_ctx
    assert snapshot_job(ctx) == 0                        # mock context: recorder off
    ctx.is_mock = False
    sent = []
    ctx.notifier.send = lambda msg, kind="info": sent.append(kind)
    real = ctx.broker.quotes
    ctx.broker.quotes = lambda keys: (_ for _ in ()).throw(RuntimeError("Kite down"))
    try:
        ctx.clock.set(datetime.combine(SNAP_DAY, time(10, 2)))
        assert snapshot_job(ctx) == 0 and snapshot_job(ctx) == 0
    finally:
        ctx.broker.quotes = real
        ctx.is_mock = True
    assert sent == ["warning"]                           # one alert a day, not one per snapshot
    ctx.cfg = ctx.cfg.model_copy(update={"snapshots": ctx.cfg.snapshots.model_copy(update={"every_min": 0})})
    from app.scheduler import job_specs
    assert "snapshots" not in {n for n, *_ in job_specs(ctx)}


def test_a_failing_contract_is_skipped_and_retried(archive_ctx):
    ctx = archive_ctx
    real = ctx.broker.historical
    bad = {}

    def flaky(token, s, e, interval, continuous=False):
        info = ctx.broker.market.info(token)
        if info.kind == "PE" and not bad:
            bad[token] = info
        if token in bad:
            raise RuntimeError("Kite: something went wrong")
        return real(token, s, e, interval, continuous=continuous)

    ctx.broker.historical = flaky
    rep = run_archive(ctx, budget=10**6)
    assert rep.errors == 1 and rep.options > 0 and "1 fetches failed" in rep.text()
    ctx.broker.historical = real
    again = run_archive(ctx, budget=10**6)
    assert again.options == 1 and again.errors == 0       # retried on the next run
