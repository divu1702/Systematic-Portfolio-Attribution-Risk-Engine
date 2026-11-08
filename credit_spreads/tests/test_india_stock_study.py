"""The first Indian study (stock prices only) on a synthetic market with known properties:
planted momentum, known betas, a 1:1 bonus, and an index."""

import importlib.util
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.backtest.store import connect

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def study():
    spec = importlib.util.spec_from_file_location("india_stock_study", ROOT / "research" / "india_stock_study.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_store(path: Path, n_stocks=15, n_days=720, seed=11):
    """Index with 15% volatility; stocks = beta x index + 30% stock-specific noise + a drift of
    +-80% a year that keeps its sign for 150 days (momentum by construction). STK03 has a 1:1
    bonus (price halves) on day 450."""
    rng = np.random.default_rng(seed)
    days, d = [], date(2023, 1, 2)
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    dt = 1 / 252
    rm = rng.normal(0.10 * dt, 0.15 * math.sqrt(dt), n_days)
    idx = 20000 * np.exp(np.cumsum(rm))
    conn = connect(path)
    conn.executemany("INSERT INTO index_bars VALUES (?,?,?,?,?,?)",
                     [("NIFTY 50", str(dd), c, c * 1.005, c * 0.995, c) for dd, c in zip(days, idx)])
    betas = np.linspace(0.5, 1.6, n_stocks)
    for i in range(n_stocks):
        drift = np.repeat(rng.choice([-0.8, 0.8], size=n_days // 150 + 1), 150)[:n_days]
        r = betas[i] * rm + rng.normal(0, 0.30 * math.sqrt(dt), n_days) + drift * dt
        c = 500 * np.exp(np.cumsum(r))
        sym = f"STK{i + 1:02d}"
        if sym == "STK03":
            c[450:] = c[450:] / 2
        o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, 0.003, n_days))
        hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n_days)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n_days)))
        conn.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?)",
                         [(sym, str(dd), a, b, cc, dd2, 10**6) for dd, a, b, cc, dd2 in zip(days, o, hi, lo, c)])
    conn.commit()
    conn.close()
    return path, dict(zip([f"STK{i + 1:02d}" for i in range(n_stocks)], betas))


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return build_store(tmp_path_factory.mktemp("india") / "market_nse.db")


def test_entries_breach_rates_betas_and_bonus(study, store):
    db, betas = store
    bars, idx = study.load(db, "NIFTY 50")
    e, jumps, seg = study.entries(bars, idx, 15, 5)
    assert [j[0] for j in jumps] == ["STK03"] and jumps[0][2] == pytest.approx(0.5, abs=0.05)
    assert {"STK03", "STK03#1"} <= set(seg.series)                    # history cut at the bonus
    assert e.n.min() >= study.MIN_HISTORY and len(e) > 1000
    assert not ((e.stock == "STK03") & (e.date < pd.Timestamp(seg[seg.series == "STK03#1"].date.min()))
                & (e.end_date >= pd.Timestamp(seg[seg.series == "STK03#1"].date.min()))).any()
    assert 0.12 < e.put_end.mean() < 0.40 and 0.12 < e.call_end.mean() < 0.40
    assert (e.put_touch >= e.put_end).all() and (e.call_touch >= e.call_end).all()
    est = e.groupby("stock").beta.median()
    for sym in ("STK01", "STK08", "STK15"):
        assert est[sym] == pytest.approx(betas[sym], abs=0.35)          # 250-day beta to the index
    assert e.groupby("beta_t").beta.median().is_monotonic_increasing


def test_direction_table_finds_planted_momentum(study, store):
    db, _ = store
    bars, idx = study.load(db, "NIFTY 50")
    e, _, _ = study.entries(bars, idx, 15, 5)
    rows = study.direction_rows(e, 15, 5).set_index(["signal", "bucket"])
    top = rows.loc[("momentum, 6 months (skip last week)", "top third")]
    bottom = rows.loc[("momentum, 6 months (skip last week)", "bottom third")]
    assert top["tilt vs all (pts)"] > 0 > bottom["tilt vs all (pts)"]   # winners kept rising
    assert top["put ends beyond %"] < bottom["put ends beyond %"]        # so their puts were hit less
    assert rows.loc[("all", ""), "entries"] == len(e)
    regime = rows.loc[("index vs its 50-day average", "above"), "tilt vs all (pts)"]
    assert regime != 0                                                   # compared with all dates, not itself


def test_forecasts_and_full_report(study, store, capsys):
    db, _ = store
    study.main(["--db", str(db), "--levels-step", "10"])
    out = capsys.readouterr().out
    for section in ("## A. Direction", "Outside results season", "## B. Volatility forecasts", "EWMA (0.94)",
                    "HAR (fitted on the 1st half)", "## C. Beta", "worst 10% of index windows",
                    "## D. Do support/resistance levels hold", "Suspected splits or bonuses (1)"):
        assert section in out, section
    bars, idx = study.load(db, "NIFTY 50")
    e, _, _ = study.entries(bars, idx, 15, 5)
    ok = e[e.rv_fwd > 0]
    assert abs(np.log(ok.ewma / ok.rv_fwd).median()) < 0.15             # unbiased on a constant-vol market


def test_empty_and_short_stores(study, tmp_path, capsys):
    db = tmp_path / "empty.db"
    connect(db).close()
    study.main(["--db", str(db)])
    assert "No stock bars" in capsys.readouterr().out
    short, _ = build_store(tmp_path / "short.db", n_stocks=8, n_days=200)
    study.main(["--db", str(short)])
    assert "Too little history yet" in capsys.readouterr().out
