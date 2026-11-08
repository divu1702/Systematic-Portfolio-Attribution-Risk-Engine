"""First Indian study: what stock prices alone say about selling stock options.

Needs only daily bars, which the Kite archiver saves on its first night (up to 2,000 days for every
F&O stock, plus the NIFTY 50 for beta), so it runs before any option history exists. Every `--step`
trading days, every stock is entered as if selling a 0.25-delta put and a 0.25-delta call that
expire `--horizon` trading days later (15 = enter ~4 weeks before expiry, exit a week before), with
strikes placed from a volatility forecast known at entry. By signal, it measures:

  A. Direction: how often the stock ends beyond, or touches, each strike, by trend, momentum,
     short-term reversal, recent shock and market regime. A bull put spread wants the put columns
     low, a bear call spread the call columns. "Tilt" is how much more often the call side was hit
     than the put side, against all stocks on the same dates: positive = the stocks went up.
  B. Volatility forecasts: which forecast of the coming weeks' volatility is most accurate (the
     denominator of the planned IV / forecast richness ranking), and volatility after a shock.
  C. Beta: big moves and how put breaches cluster on bad market days, by beta to the index.
  D. Support/resistance: do our levels hold better than chance on Indian stocks?

Hypotheses and decision rules: research/research_plan.md. Statistics: per-date averages across
stocks, Newey-West t-statistics across dates (holding windows overlap), and each effect for both
halves of the period: only effects with the same sign in both halves count.

Caveats: survivorship (only stocks in F&O today); no past results dates (an outside-results-season
view is given instead); suspected splits or bonuses (one-day moves beyond -40% / +67%) cut a stock's
history into separate pieces, which would also hide a real crash that large.

Usage: python research/india_stock_study.py [--db data/market_nse.db] > research/results_india_stocks.md
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import option_selling_study as oss  # noqa: E402  (table format, Newey-West t, level test)

Z25 = 0.6745                      # |d1| of a 0.25-delta option
EWMA_LAMBDA = 0.94
SHOCK_ATR, SHOCK_LOOKBACK = 1.5, 10   # F5's definition of a large move
JUMP_LO, JUMP_HI = 0.6, 1.67      # one-day ratios treated as a split or bonus
MIN_HISTORY = 260                 # bars before a series' first entry (beta uses 250)
SEASONS = ((1, 10, 2, 15), (4, 10, 5, 31), (7, 10, 8, 14), (10, 10, 11, 14))   # results seasons (m, d, m, d)


# ----------------------------------------------------------------------------- data

def load(db: Path, index: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tables = {t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    bars = pd.read_sql_query("SELECT symbol, date, open, high, low, close FROM bars WHERE close > 0", conn)
    idx = (pd.read_sql_query("SELECT date, close FROM index_bars WHERE symbol=? AND close > 0", conn, params=(index,))
           if "index_bars" in tables else pd.DataFrame(columns=["date", "close"]))
    conn.close()
    bars["date"], idx["date"] = pd.to_datetime(bars["date"]), pd.to_datetime(idx["date"])
    for c in ("open", "high", "low"):
        bars[c] = bars[c].where(bars[c] > 0, bars["close"])
    return bars, idx


def segments(bars: pd.DataFrame):
    """Cut each stock's history at suspected splits or bonuses into separate series."""
    parts, jumps = [], []
    for sym, g in bars.sort_values("date").groupby("symbol"):
        g = g.reset_index(drop=True)
        ratio = g.close / g.close.shift(1)
        cut = ((ratio < JUMP_LO) | (ratio > JUMP_HI)).fillna(False)
        jumps += [(sym, g.date[i].date(), float(ratio[i])) for i in np.flatnonzero(cut.values)]
        for k, part in g.groupby(cut.cumsum()):
            parts.append(part.assign(stock=sym, series=sym if k == 0 else f"{sym}#{k}"))
    cols = list(bars.columns) + ["stock", "series"]
    return (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)), jumps


def yang_zhang(g: pd.DataFrame, n: int) -> pd.Series:
    """Yang-Zhang daily volatility over n days (overnight, open-to-close and Rogers-Satchell parts)."""
    o, h, lo, c = (np.log(g[k]) for k in ("open", "high", "low", "close"))
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    rs = (h - c) * (h - o) + (lo - c) * (lo - o)
    var = (o - c.shift(1)).rolling(n).var() + k * (c - o).rolling(n).var() + (1 - k) * rs.rolling(n).mean()
    return np.sqrt(var.clip(lower=0))


def _fwd(x: pd.Series, H: int, how: str) -> pd.Series:
    """Statistic of x over the next H rows (t+1 .. t+H)."""
    rev = getattr(x[::-1].rolling(H, min_periods=H), how)()[::-1]
    return rev.shift(-1)


def series_features(g: pd.DataFrame, idx_ret: pd.Series, H: int) -> pd.DataFrame:
    g = g.sort_values("date").reset_index(drop=True)
    c = g.close
    r = np.log(c).diff()
    ann = math.sqrt(252)
    out = pd.DataFrame({"date": g.date, "stock": g.stock, "series": g.series, "close": c, "n": np.arange(len(g))})
    for w in (50, 200):
        sma = c.rolling(w).mean()
        out[f"above{w}"] = np.where(sma.notna(), (c > sma).astype(float), np.nan)
    out["mom3"], out["mom6"] = c.shift(5) / c.shift(68) - 1, c.shift(5) / c.shift(131) - 1
    out["rev5"], out["rev21"] = c / c.shift(5) - 1, c / c.shift(21) - 1
    out["hv5"], out["hv20"], out["hv60"] = (r.rolling(w).std() * ann for w in (5, 20, 60))
    out["ewma"] = np.sqrt((r ** 2).ewm(alpha=1 - EWMA_LAMBDA, adjust=False, min_periods=20).mean()) * ann
    out["yz20"] = yang_zhang(g, 20) * ann
    pc = c.shift(1)
    tr = pd.concat([g.high - g.low, (g.high - pc).abs(), (g.low - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    move = c - pc
    shock = move.abs() >= SHOCK_ATR * atr.shift(1)
    recent = shock.astype(float).rolling(SHOCK_LOOKBACK, min_periods=1).max().astype(bool)
    last_sign = np.sign(move.where(shock)).ffill(limit=SHOCK_LOOKBACK - 1)
    out["shock"] = np.where(recent, np.where(last_sign > 0, "up", "down"), "none")
    if len(idx_ret):
        ri = pd.Series(idx_ret.reindex(g.date.values).values)
        rr = pd.Series(r.values)
        out["beta"] = (rr.rolling(250, min_periods=200).cov(ri) / ri.rolling(250, min_periods=200).var()).values
    else:
        out["beta"] = np.nan
    out["fwd_ret"] = np.log(c.shift(-H) / c)
    out["fwd_min"], out["fwd_max"] = _fwd(c, H, "min"), _fwd(c, H, "max")
    out["rv_fwd"] = np.sqrt(252 * _fwd(r ** 2, H, "mean"))
    out["end_date"] = g.date.shift(-H)
    return out


def in_season(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    for y in range(start.year, end.year + 1):
        for m1, d1, m2, d2 in SEASONS:
            if start <= pd.Timestamp(y, m2, d2) and end >= pd.Timestamp(y, m1, d1):
                return True
    return False


def entries(bars: pd.DataFrame, idx: pd.DataFrame, H: int, step: int):
    seg, jumps = segments(bars)
    idx = idx.sort_values("date")
    idx_close = idx.set_index("date").close
    idx_ret = np.log(idx_close).diff()
    feats = [series_features(g, idx_ret, H) for _, g in seg.groupby("series")]
    f = pd.concat(feats, ignore_index=True) if feats else pd.DataFrame()
    if f.empty:
        return f, jumps, seg
    days = sorted(f.date.unique())
    picks = set(days[::step])
    e = f[f.date.isin(picks) & (f.n >= MIN_HISTORY) & f.fwd_ret.notna() & f.ewma.gt(0)].copy()
    sT = e.ewma * math.sqrt(H / 252)
    kp, kc = -Z25 * sT + 0.5 * sT ** 2, Z25 * sT + 0.5 * sT ** 2
    e["put_end"], e["call_end"] = (e.fwd_ret < kp).astype(float), (e.fwd_ret > kc).astype(float)
    e["put_touch"] = (np.log(e.fwd_min / e.close) < kp).astype(float)
    e["call_touch"] = (np.log(e.fwd_max / e.close) > kc).astype(float)
    e["z"] = e.fwd_ret / sT
    e["big"] = (e.z.abs() > 2).astype(float)
    e["season"] = [in_season(a, b) for a, b in zip(e.date, e.end_date)]
    if len(idx_close):
        sma = idx_close.rolling(50).mean()
        mkt = pd.DataFrame({"above": np.where(sma.notna(), (idx_close > sma).astype(float), np.nan),
                            "fwd": np.log(idx_close.shift(-H) / idx_close)}, index=idx_close.index)
        e = e.join(mkt, on="date")
    else:
        e["above"], e["fwd"] = np.nan, np.nan
    for col in ("mom3", "mom6", "rev5", "rev21", "beta"):
        e[col + "_t"] = e.groupby("date")[col].transform(_terciles)
    return e.reset_index(drop=True), jumps, seg


def _terciles(x: pd.Series) -> pd.Series:
    ok = x.notna()
    out = pd.Series(np.nan, index=x.index)
    if ok.sum() >= 6:
        out[ok] = pd.qcut(x[ok].rank(method="first"), 3, labels=False)
    return out


# ----------------------------------------------------------------------------- A: direction

def _lags(H: int, step: int) -> int:
    return max(4, math.ceil(H / step) + 1)


def direction_rows(e: pd.DataFrame, H: int, step: int) -> pd.DataFrame:
    lags = _lags(H, step)
    dates = sorted(e.date.unique())
    mid = dates[len(dates) // 2] if dates else None
    base = e.groupby("date")[["put_end", "call_end"]].mean()
    base_tilt = base.call_end - base.put_end
    rows = []

    def add(signal, bucket, mask, market_wide=False):
        """market_wide: every stock shares the signal on a date, so compare with all dates instead."""
        sub = e[mask]
        if len(sub) < 30:
            rows.append({"signal": signal, "bucket": bucket, "entries": len(sub)})
            return
        per = sub.groupby("date")[["put_end", "call_end", "put_touch", "call_touch", "z"]].mean()
        tilt = (per.call_end - per.put_end) - (base_tilt.mean() if market_wide else base_tilt.reindex(per.index))
        first, second = tilt[tilt.index < mid], tilt[tilt.index >= mid]
        rows.append({"signal": signal, "bucket": bucket, "entries": len(sub),
                     "put ends beyond %": 100 * sub.put_end.mean(), "call ends beyond %": 100 * sub.call_end.mean(),
                     "put touched %": 100 * sub.put_touch.mean(), "call touched %": 100 * sub.call_touch.mean(),
                     "drift (sigma)": sub.z.mean(), "tilt vs all (pts)": 100 * tilt.mean(),
                     "t": oss.nw_tstat(tilt.values, lags) if signal != "all" else np.nan,
                     "tilt 1st half": 100 * first.mean() if len(first) else np.nan,
                     "tilt 2nd half": 100 * second.mean() if len(second) else np.nan})

    add("all", "", e.index == e.index)
    add("price vs 50-day average", "above", e.above50 == 1)
    add("price vs 50-day average", "below", e.above50 == 0)
    add("price vs 200-day average", "above", e.above200 == 1)
    add("price vs 200-day average", "below", e.above200 == 0)
    names = {0: "bottom third", 1: "middle third", 2: "top third"}
    for col, label in (("mom3", "momentum, 3 months (skip last week)"), ("mom6", "momentum, 6 months (skip last week)"),
                       ("rev5", "last week's return"), ("rev21", "last month's return")):
        for k in (0, 1, 2):
            add(label, names[k], e[col + "_t"] == k)
    for s in ("up", "down", "none"):
        add("large move in the last 10 days", s, e.shock == s)
    up = (e.above50 == 1) & (e.mom6_t == 2)
    down = (e.above50 == 0) & (e.mom6_t == 0)
    add("trend and 6-month momentum agree", "both up", up)
    add("trend and 6-month momentum agree", "both down", down)
    if e.above.notna().any():
        add("index vs its 50-day average", "above", e.above == 1, market_wide=True)
        add("index vs its 50-day average", "below", e.above == 0, market_wide=True)
    if e.beta_t.notna().any():
        for k, name in ((0, "low third"), (1, "middle third"), (2, "high third")):
            add("beta to the index", name, e.beta_t == k)
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- B: volatility forecasts

def forecast_tables(e: pd.DataFrame) -> str:
    dates = sorted(e.date.unique())
    mid = dates[len(dates) // 2]
    ok = e[(e.rv_fwd > 0) & (e.hv5 > 0) & (e.hv20 > 0) & (e.hv60 > 0)].copy()
    first, second = ok[ok.date < mid], ok[ok.date >= mid]
    X = lambda d: np.column_stack([np.ones(len(d)), np.log(d.hv5), np.log(d.hv20), np.log(d.hv60)])  # noqa: E731
    coef = np.linalg.lstsq(X(first), np.log(first.rv_fwd), rcond=None)[0] if len(first) > 50 else None
    ok["har"] = np.exp(X(ok) @ coef) if coef is not None else np.nan
    second = ok[ok.date >= mid]
    methods = (("HV20 (what F3 uses)", "hv20"), ("HV60", "hv60"), ("EWMA (0.94)", "ewma"),
               ("Yang-Zhang 20 (uses open/high/low)", "yz20"), ("HAR (fitted on the 1st half)", "har"))
    rows = []
    for label, col in methods:
        d = second[second[col] > 0]
        if d.empty:
            continue
        err = np.log(d[col] / d.rv_fwd)
        rank = d.groupby("date").apply(lambda x: x[col].rank().corr(x.rv_fwd.rank()) if len(x) >= 6 else np.nan,
                                       include_groups=False)
        rows.append({"forecast": label, "median abs error (log)": float(err.abs().median()),
                     "bias (log, + = too high)": float(err.mean()), "rank corr across stocks": float(rank.mean())})
    out = ["Second half of the period (out of sample for HAR):\n", oss.table(pd.DataFrame(rows))]
    if second.beta_t.notna().any():
        rows = []
        for k, name in ((0, "low"), (1, "middle"), (2, "high")):
            d = second[second.beta_t == k]
            if d.empty:
                continue
            rows.append({"beta third": name, "stock-entries": len(d),
                         "EWMA median abs error": float(np.log(d.ewma / d.rv_fwd).abs().median()),
                         "HAR median abs error": float(np.log(d.har / d.rv_fwd).abs().median())
                         if d.har.notna().any() else np.nan})
        out += ["\nBy beta (is volatility harder to forecast for high-beta stocks?):\n", oss.table(pd.DataFrame(rows))]
    rows = []
    for s in ("up", "down", "none"):
        d = ok[ok.shock == s]
        if len(d):
            rows.append({"large move in the last 10 days": s, "stock-entries": len(d),
                         "later vol / HV20": float((d.rv_fwd / d.hv20).median()),
                         "later vol / EWMA": float((d.rv_fwd / d.ewma).median())})
    out += ["\nAfter a large move, does volatility stay high? (medians; 1.00 = forecast right)\n",
            oss.table(pd.DataFrame(rows))]
    return "\n".join(out)


# ----------------------------------------------------------------------------- C: beta and clustering

def beta_tables(e: pd.DataFrame) -> str:
    if not e.beta_t.notna().any():
        return "No index bars in the store yet (archive.index), so no beta."
    rows = []
    for k, name in ((0, "low"), (1, "middle"), (2, "high")):
        d = e[e.beta_t == k]
        rows.append({"beta third": name, "stock-entries": len(d), "median beta": float(d.beta.median()),
                     "median later vol": float(d.rv_fwd.median()), "moves beyond 2 sigma %": 100 * d.big.mean(),
                     "put ends beyond %": 100 * d.put_end.mean(), "call ends beyond %": 100 * d.call_end.mean()})
    out = [oss.table(pd.DataFrame(rows))]
    per_date = e.groupby("date").fwd.first().dropna()
    if len(per_date) >= 20:
        worst = per_date[per_date <= per_date.quantile(0.10)].index
        rows = []
        for k, name in ((0, "low"), (1, "middle"), (2, "high")):
            d = e[e.beta_t == k]
            rows.append({"beta third": name, "put ends beyond %, all dates": 100 * d.put_end.mean(),
                         "put ends beyond %, worst 10% of index windows": 100 * d[d.date.isin(worst)].put_end.mean()})
        out += ["\nOn the index's worst 10% of windows, how many put strikes are hit at once?\n",
                oss.table(pd.DataFrame(rows))]
    return "\n".join(out)


# ----------------------------------------------------------------------------- main

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "market_nse.db")
    ap.add_argument("--index", default="NIFTY 50")
    ap.add_argument("--horizon", type=int, default=15, help="holding window in trading days")
    ap.add_argument("--step", type=int, default=5, help="trading days between entries")
    ap.add_argument("--levels-step", type=int, default=5, help="evaluate levels every Nth day; 0 = skip")
    args = ap.parse_args(argv)
    bars, idx = load(args.db, args.index)
    title = "# What Indian stock prices say about selling stock options\n"
    if bars.empty:
        print(title + f"\nNo stock bars in {args.db} yet: run `python -m app archive` first.")
        return
    e, jumps, seg = entries(bars, idx, args.horizon, args.step)
    print(title)
    print(f"Source: {args.db.name}; {bars.symbol.nunique()} stocks, {bars.date.min():%Y-%m-%d} to "
          f"{bars.date.max():%Y-%m-%d}; index {args.index}: {len(idx):,} days. Entries every {args.step} trading days, "
          f"held {args.horizon} trading days; strikes at 0.25 delta from an EWMA volatility forecast. "
          f"{len(e):,} stock-entries on {e.date.nunique() if len(e) else 0} dates.\n")
    if jumps:
        shown = ", ".join(f"{s} {d} (x{r:.2f})" for s, d, r in jumps[:15])
        print(f"Suspected splits or bonuses ({len(jumps)}), history cut there: {shown}"
              f"{' ...' if len(jumps) > 15 else ''}.\n")
    if e.empty or e.date.nunique() < 10:
        print(f"Too little history yet: each stock needs {MIN_HISTORY} trading days before its first entry.")
        return
    print("## A. Direction: how often each strike is hit\n")
    print("If the forecast were right and stocks had no drift, roughly 27% of puts and 23% of calls would end beyond "
          "a 0.25-delta strike (the exact values depend on volatility). Tilt: how much more "
          "often the call side than the put side was hit, against all stocks on the same dates "
          "(+ = these stocks rose more; for the index rows, against all dates); t (NW) across dates.\n")
    print(oss.table(direction_rows(e, args.horizon, args.step)), "\n")
    off = e[~e.season]
    print(f"### Outside results season ({len(off):,} stock-entries; results dates are not known for the past)\n")
    if off.date.nunique() >= 10:
        print(oss.table(direction_rows(off, args.horizon, args.step)), "\n")
    print("## B. Volatility forecasts: which predicts the coming weeks best?\n")
    print(forecast_tables(e), "\n")
    print("## C. Beta: big moves and clustering\n")
    print(beta_tables(e), "\n")
    print("## D. Do support/resistance levels hold on Indian stocks?\n")
    if args.levels_step <= 0:
        print("Skipped (--levels-step 0).\n")
    else:
        feat = oss.underlying_features(seg.drop(columns=["symbol"]).rename(columns={"series": "symbol"}),
                                       pd.DataFrame(columns=["symbol", "ex_date", "to_factor", "for_factor"]))
        if feat.groupby("symbol").size().max() > 260 + oss.LEVEL_HORIZON:
            print(oss.levels_test(feat, step=args.levels_step), "\n")
        else:
            print("Needs more than a year of daily bars per stock.\n")


if __name__ == "__main__":
    main()
