"""The option-selling study on Indian data: the NSE loader turns the archiver's candles and the
recorder's quotes into the study's inputs, and the study runs end to end on them."""

import importlib.util
import math
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.analytics import black76
from app.backtest.store import connect

ROOT = Path(__file__).resolve().parents[1]
SIGMA, R = 0.30, 0.065
EXPIRIES = [date(2025, 12, 30), date(2026, 1, 27), date(2026, 2, 24), date(2026, 3, 31), date(2026, 4, 28),
            date(2026, 5, 26), date(2026, 6, 30), date(2026, 7, 28)]
START, END = date(2025, 12, 1), date(2026, 5, 15)
RECORDED_FROM = date(2026, 4, 24)          # the recorder ran for the last three weeks


@pytest.fixture(scope="module")
def study():
    spec = importlib.util.spec_from_file_location("option_selling_study", ROOT / "research" / "option_selling_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_nse_store(path: Path, seed: int = 3) -> Path:
    """Two stocks with 30% volatility; options priced with Black-76 at 30%, three monthly series
    listed at a time, candles every day, recorded quotes (4% wide) for the last three weeks."""
    rng = np.random.default_rng(seed)
    days = [START + timedelta(n) for n in range((END - START).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    conn = connect(path)
    for sym, s0, lot in (("AAA", 1000.0, 250), ("BBB", 250.0, 1000)):
        z = rng.standard_normal(len(days))
        closes = s0 * np.exp(np.cumsum(SIGMA / math.sqrt(252) * z - 0.5 * SIGMA ** 2 / 252))
        strikes = [round(s0 * (0.70 + 0.025 * i), 2) for i in range(25)]
        bars, opts, futs, chains, fq = [], [], [], [], []
        for d, s in zip(days, closes):
            bars.append((sym, str(d), s, s * 1.01, s * 0.99, s, 10**6))
            for e in [e for e in EXPIRIES if e >= d][:3]:
                T = (e - d).days / 365.0
                F = s * math.exp(R * T)
                futs.append((str(d), sym, str(e), F, F, F, F, 1000, 0))
                if d >= RECORDED_FROM:
                    fq.append((str(d), sym, str(e), F - 0.05, F + 0.05, F))
                for k in strikes:
                    for cp in ("CE", "PE"):
                        px = black76.price(F, k, T, SIGMA, R, cp) if T > 0 else max(
                            (s - k) if cp == "CE" else (k - s), 0.0)
                        if round(px, 2) < 0.05:
                            continue
                        opts.append((str(d), sym, str(e), k, cp, px, px, px, round(px, 2), 100, 0, lot))
                        if d >= RECORDED_FROM:
                            chains.append((str(d), sym, str(e), k, cp, round(px * 0.98, 2), round(px * 1.02, 2),
                                           None, None))
        conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", bars)
        conn.executemany("INSERT INTO option_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", opts)
        conn.executemany("INSERT INTO future_candles VALUES (?,?,?,?,?,?,?,?,?)", futs)
        conn.executemany("INSERT INTO chains VALUES (?,?,?,?,?,?,?,?,?)", chains)
        conn.executemany("INSERT INTO futures VALUES (?,?,?,?,?,?)", fq)
    conn.executemany("INSERT INTO events_seen VALUES (?,?,?,?)", [
        ("AAA", "2026-02-10", "results", "2026-01-20"), ("BBB", "2026-03-10", "bonus", "2026-02-01"),
        ("BBB", "2026-01-15", "dividend", "2026-01-01")])
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def nse_db(tmp_path_factory):
    return build_nse_store(tmp_path_factory.mktemp("nse") / "market_nse.db")


def test_nse_loader_recovers_the_volatility_and_spreads(study, nse_db):
    bars, chains, earn, splits, note = study.load_nse(nse_db, assume_spread=0.10, r=R)
    mid = (chains.bid + chains.ask) / 2
    priced = chains[(mid >= 1.0) & (chains.delta.abs() <= 0.6)]       # deep in-the-money IV is ill-posed
    assert len(priced) > 10_000
    assert (priced.iv - SIGMA).abs().quantile(0.99) < 0.01          # Black-76 inverted from candle closes
    assert priced.delta.between(-1, 1).all()
    assert ((priced.cp == "CE") == (priced.delta > 0)).all()
    recorded = chains.date >= pd_ts(RECORDED_FROM)
    rel = (chains.ask - chains.bid) / mid
    assert rel[recorded & (mid >= 1)].median() == pytest.approx(0.04, abs=0.002)    # real quotes as they are
    candle = ~recorded & (mid >= 2)
    assert rel[candle].median() == pytest.approx(0.04, abs=0.003)   # spread learnt from the recorded quotes
    assert (rel[candle] < 0.09).mean() > 0.95                        # not the 10% assumption
    assert set(earn.symbol) == {"AAA"} and str(earn.date.iloc[0].date()) == "2026-02-10"
    assert ("BBB", "2026-03-10") in {(s, str(d.date())) for s, d in zip(splits.symbol, splits.ex_date)}
    assert "recorded quotes" in note and "candle prices" in note


def pd_ts(d):
    import pandas as pd

    return pd.Timestamp(d)


def test_study_panel_on_nse_data(study, nse_db):
    bars, chains, earn, splits, _ = study.load_nse(nse_db, r=R)
    feat = study.underlying_features(bars, splits)
    atm = study.atm_panel(chains, feat, earn, splits)
    assert len(atm) > 100
    assert atm.expiration.max() <= pd_ts(END)                        # only windows that have finished
    assert atm.atm_iv.median() == pytest.approx(SIGMA, abs=0.01)
    assert 0.6 < atm.iv_over_rv.median() < 1.6
    bbb = atm[atm.symbol == "BBB"]
    assert not ((bbb.date < pd_ts(date(2026, 3, 10))) & (bbb.expiration >= pd_ts(date(2026, 3, 10)))).any()
    aaa = atm[atm.symbol == "AAA"]
    across = (aaa.date <= pd_ts(date(2026, 2, 10))) & (aaa.expiration >= pd_ts(date(2026, 2, 10)))
    assert across.any() and aaa[across].earn_in_window.all() and not aaa[~across].earn_in_window.any()
    opts = study.option_returns(chains, feat, atm)
    assert len(opts) > 0 and opts.s_T.notna().all()


def test_study_runs_end_to_end_on_nse_store(study, nse_db, capsys):
    study.main(["--market", "nse", "--db", str(nse_db)])
    out = capsys.readouterr().out
    for section in ("Option-selling study on Indian data", "Completed expiries in the sample: ",
                    "## A.", "## B.", "## C.", "## D.", "## E.", "Needs more than a year"):
        assert section in out


def test_study_on_empty_nse_store(study, tmp_path, capsys):
    db = tmp_path / "empty.db"
    connect(db).close()
    study.main(["--market", "nse", "--db", str(db)])
    assert "No option data" in capsys.readouterr().out


def test_study_reads_what_the_archiver_and_recorder_save(study, mock_ctx, capsys):
    """Kite-shaped data from the mock: archived candles plus one recorded scan load cleanly; no
    expiry has passed yet, so the study says so instead of reporting."""
    from app.data.history_archiver import run_archive
    from app.jobs import eod_job, morning_refresh, scan_job

    ctx = mock_ctx
    ctx.cache["archive_on_mock"] = True
    ctx.cfg = ctx.cfg.model_copy(update={"archive": ctx.cfg.archive.model_copy(
        update={"band": 0.06, "max_expiries": 2, "equity_days": 200})})
    run_archive(ctx, budget=10**6)
    ctx.is_mock = False                                # the recorder is off for mock contexts
    try:
        morning_refresh(ctx)
        scan_job(ctx)
        ctx.clock.set(datetime.combine(ctx.clock.today(), time(15, 35)))
        eod_job(ctx)
    finally:
        ctx.is_mock = True
    db = ctx.cfg.resolve(ctx.cfg.paths.market_store)
    conn = sqlite3.connect(db)
    n_candles = conn.execute("SELECT COUNT(*) FROM option_candles").fetchone()[0]
    n_quotes = conn.execute("SELECT COUNT(*) FROM chains").fetchone()[0]
    n_keys = conn.execute("SELECT COUNT(*) FROM (SELECT date, symbol, expiration, strike, cp FROM option_candles "
                          "WHERE volume > 0 UNION SELECT date, symbol, expiration, strike, cp FROM chains "
                          "WHERE bid > 0 AND ask > bid)").fetchone()[0]
    conn.close()
    assert n_candles > 0 and n_quotes > 0
    conn = sqlite3.connect(db)
    ip = study.intraday_prices(conn)
    conn.close()
    stale = int(ip.stale.sum())
    assert len(ip) > 0 and 0 < stale < 0.2 * len(ip)                 # thin strikes: no trade near 15:00 some days
    bars, chains, earn, splits, note = study.load_nse(db)
    assert len(chains) >= 0.98 * (n_keys - stale)                    # one row per option-day, nearly all priced
    assert f"{len(ip) - stale:,} of them at the trade nearest 15:00" in note
    assert not chains.duplicated(["date", "symbol", "expiration", "strike", "cp"]).any()
    assert chains.iv.between(0.05, 2.0).mean() > 0.95
    today = chains.date == pd_ts(ctx.clock.today())
    assert today.any() and (~today).any()                            # recorded day and archived history
    study.main(["--market", "nse", "--db", str(db)])
    assert "No completed holding windows yet" in capsys.readouterr().out
    from app.fmt import set_currency
    set_currency("INR")


def test_intraday_prices_use_synchronous_futures_and_drop_stale_prints(study, tmp_path):
    """A rising futures price through the day: an option that last traded at 14:45 is priced
    against the futures at 14:45 (its IV comes out right); one that traded only at 13:00 is
    dropped instead of being compared with the 15:29 futures close."""
    from app.data import barcodec

    d, e = date(2026, 3, 2), date(2026, 3, 31)
    conn = connect(tmp_path / "intraday.db")
    t0 = datetime.combine(d, time(9, 15))
    fut = [{"date": t0 + timedelta(minutes=m), "open": 1000 + m * 0.08, "high": 1000 + m * 0.08,
            "low": 1000 + m * 0.08, "close": 1000 + m * 0.08, "volume": 500, "oi": 0} for m in range(375)]
    fmin = {c["date"].hour * 60 + c["date"].minute: c["close"] for c in fut}
    conn.execute("INSERT INTO future_bars VALUES (?,?,?,?,?,?)",
                 ("AAA", str(e), str(d), "minute", len(fut), barcodec.encode(barcodec.candle_columns(fut))))
    fc = barcodec.daily_from(fut)
    conn.execute("INSERT INTO future_candles VALUES (?,?,?,?,?,?,?,?,?)",
                 (str(d), "AAA", str(e), fc["open"], fc["high"], fc["low"], fc["close"], fc["volume"], 0))
    conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", ("AAA", str(d), 1000, 1031, 999, 1029.8, 10**6))

    def trade(k, cp, hhmm):
        m = hhmm[0] * 60 + hhmm[1]
        T = ((e - d).days * 1440 + (930 - (m + 1))) / (365 * 1440)
        px = round(black76.price(fmin[m], k, T, SIGMA, R, cp), 2)
        return {"date": datetime.combine(d, time(*hhmm)), "open": px, "high": px, "low": px, "close": px,
                "volume": 250, "oi": 0}

    for k, cp, times in ((1000.0, "CE", [(14, 50), (15, 29)]), (1050.0, "CE", [(14, 45)]), (950.0, "PE", [(13, 0)])):
        cs = [trade(k, cp, t) for t in times]
        conn.execute("INSERT INTO option_bars VALUES (?,?,?,?,?,?,?,?,?)",
                     ("AAA", str(e), k, cp, str(d), "minute", len(cs), 250, barcodec.encode(barcodec.candle_columns(cs))))
        dc = barcodec.daily_from(cs)
        conn.execute("INSERT INTO option_candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (str(d), "AAA", str(e), k, cp, dc["open"], dc["high"], dc["low"], dc["close"], dc["volume"], 0, 250))
    conn.commit()
    conn.close()
    bars, chains, earn, splits, note = study.load_nse(tmp_path / "intraday.db", r=R)
    got = {(row.strike, row.cp): row.iv for row in chains.itertuples()}
    assert set(got) == {(1000.0, "CE"), (1050.0, "CE")}                 # the 13:00-only put is dropped
    assert got[(1050.0, "CE")] == pytest.approx(SIGMA, abs=0.003)         # 14:45 trade vs 14:45 futures
    assert got[(1000.0, "CE")] == pytest.approx(SIGMA, abs=0.003)         # 14:50 is nearer 15:00 than 15:29
    assert "1 option-days dropped" in note
    row = chains[chains.strike == 1050].iloc[0]
    daily_iv = study.implied_vol_vec(np.array([(row.bid + row.ask) / 2]), np.array([fc["close"]]), np.array([1050.0]),
                                     np.array([29 / 365]), R, np.array([True]))[0]
    assert abs(daily_iv - SIGMA) > 0.01      # daily candles only: the 14:45 trade meets the 15:29 futures close
