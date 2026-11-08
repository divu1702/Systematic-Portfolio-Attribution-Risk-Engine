"""Empirical study: where does selling single-stock options pay, and do our filters find it?

Two data sources, same analyses:
- `--market us` (default): the DoltHub US store (data/market_us.db, `python -m app backtest load`):
  real end-of-day bid/ask/IV/delta, daily bars, earnings dates and splits.
- `--market nse`: the Indian store built by this system (data/market_nse.db): Kite daily candles
  saved by the history archiver, quotes saved by the live recorder, and results dates as first seen.
  IV and delta are computed from each option's close with the same expiry's futures close as the
  forward (Black-76). Bid/ask comes from recorded quotes where available, otherwise an assumed
  spread (`--assume-spread`, as a fraction of the price), reported in the output.

Not a strategy backtest. It measures, held to expiry with no exit rules:
  A. implied vs subsequently realised volatility (the volatility risk premium), by condition
  B. returns from selling single options and 1-strike vertical spreads, by delta and side
  C. how our entry filters (F3 IV/HV, F4 IV percentile, F5 recent shock) change A and B
  D. earnings inside vs outside the holding window
  E. whether support/resistance levels from our level engine hold better than chance

Usage: python research/option_selling_study.py [--market us|nse] [--db PATH] > research/results_<market>.md
Statistics: per-expiry averages, t-statistics with Newey-West (4 lags) across expiries, because
overlapping holding periods make individual trades far from independent.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.analytics.levels import compute_levels  # noqa: E402

DB = ROOT / "data" / "market_us.db"
SHOCK_ATR, SHOCK_LOOKBACK = 1.5, 10            # F5 defaults
MIN_DTE, MAX_DTE, TARGET_DTE = 15, 35, 28        # strategy's DTE window
LEVEL_HORIZON = 15                               # trading days, roughly a holding period


# ----------------------------------------------------------------------------- helpers

def nw_tstat(x: np.ndarray, lags: int = 4) -> float:
    """Newey-West t-statistic of the mean of a (time-ordered) series."""
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 8:
        return float("nan")
    e = x - x.mean()
    s = (e @ e) / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1 - k / (lags + 1)
        s += 2 * w * (e[k:] @ e[:-k]) / n
    se = math.sqrt(max(s, 1e-18) / n)
    return x.mean() / se


def table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(str(c) for c in cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(_fmt(v) for v in r.values) + " |")
    return "\n".join(out)


def _fmt(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return "-"
        return f"{v:.2f}" if abs(v) >= 0.01 or v == 0 else f"{v:.4f}"
    return str(v)


def by_expiry_stats(df: pd.DataFrame, col: str) -> dict:
    """Average of per-expiry means (each expiry weighted equally, so it matches the t-statistic),
    Newey-West t across those means, number of trades and expiries."""
    per = df.groupby("expiration")[col].mean().sort_index()
    return {"mean": float(per.mean()), "t": nw_tstat(per.values), "trades": len(df), "expiries": len(per)}


# ----------------------------------------------------------------------------- data

def _b76(F, K, T, sigma, r, is_call):
    """Black-76 price and delta, vectorised; puts by put-call parity."""
    from scipy.special import ndtr

    sv = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sv * sv) / sv
    df = np.exp(-r * T)
    call = df * (F * ndtr(d1) - K * ndtr(d1 - sv))
    price = np.where(is_call, call, call - df * (F - K))
    delta = df * np.where(is_call, ndtr(d1), ndtr(d1) - 1.0)
    return price, delta


def implied_vol_vec(px, F, K, T, r, is_call, lo=0.01, hi=5.0, iters=50):
    """Vectorised Black-76 implied vol by bisection; NaN where no vol in [lo, hi] fits the price."""
    px, F, K, T = (np.asarray(x, float) for x in (px, F, K, T))
    is_call = np.asarray(is_call, bool)
    with np.errstate(invalid="ignore", divide="ignore"):
        df = np.exp(-r * T)
        intrinsic = df * np.where(is_call, np.maximum(F - K, 0), np.maximum(K - F, 0))
        ok = (px > intrinsic) & (px < df * np.where(is_call, F, K)) & (T > 0) & (F > 0) & (K > 0)
        Ts, Fs = np.where(ok, T, 1.0), np.where(ok, F, 1.0)
        Ks = np.where(ok, K, 1.0)
        a, b = np.full(px.shape, lo), np.full(px.shape, hi)
        for _ in range(iters):
            m = 0.5 * (a + b)
            high = _b76(Fs, Ks, Ts, m, r, is_call)[0] > px
            a, b = np.where(high, a, m), np.where(high, m, b)
        ok &= _b76(Fs, Ks, Ts, np.full(px.shape, hi), r, is_call)[0] >= px
    iv = 0.5 * (a + b)
    iv[~ok] = np.nan
    return iv


def _with_iv(df: pd.DataFrame, price, F, r: float, T=None) -> pd.DataFrame:
    if df.empty:
        return df.assign(iv=pd.Series(dtype=float), delta=pd.Series(dtype=float))
    T = (df.expiration - df.date).dt.days.values / 365.0 if T is None else np.asarray(T, float)
    K, F, price = df.strike.values.astype(float), np.asarray(F, float), np.asarray(price, float)
    is_call = (df.cp == "CE").values
    iv = implied_vol_vec(price, F, K, T, r, is_call)
    with np.errstate(invalid="ignore", divide="ignore"):
        _, delta = _b76(F, K, np.where(T > 0, T, 1e-6), np.nan_to_num(iv, nan=0.3), r, is_call)
    return df.assign(iv=iv, delta=np.where(np.isnan(iv), np.nan, delta))


DBINS = [0.0, 0.10, 0.20, 0.30, 0.40, 0.60, 1.01]
STEP = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}


def intraday_prices(conn, at: int = 900, window: int = 30) -> pd.DataFrame:
    """Per option-day with archived intraday candles: the trade closest to `at` (minutes after
    midnight, 900 = 15:00, when the strategy scans) within +-`window` minutes, and the same
    expiry's futures price at that minute. Synchronous prices keep a stale option print from
    distorting IV. Columns: date, symbol, expiration, strike, cp, px, fwd, T (years), stale (False
    when a trade was found; True rows are option-days with candles but none near `at`)."""
    from app.data import barcodec

    fut = {}
    for sym, exp, d, interval, blob in conn.execute("SELECT symbol, expiration, date, interval, data FROM future_bars"):
        fut[(sym, exp, d)] = (barcodec.decode(blob), STEP.get(interval, 1))
    rows = []
    for sym, exp, k, cp, d, interval, blob in conn.execute(
            "SELECT symbol, expiration, strike, cp, date, interval, data FROM option_bars"):
        step = STEP.get(interval, 1)
        c = barcodec.decode(blob)
        i = barcodec.at_minute(c, at - step, window)              # candle whose close is nearest `at`
        f = fut.get((sym, exp, d))
        j = barcodec.at_minute(f[0], int(c["minute"][i]), 15) if i >= 0 and f else -1
        if j < 0:
            rows.append((d, sym, exp, k, cp, np.nan, np.nan, np.nan, True))
            continue
        close_min = int(c["minute"][i]) + step
        days = (date.fromisoformat(exp) - date.fromisoformat(d)).days
        T = (days * 1440 + (15 * 60 + 30 - close_min)) / (365.0 * 1440)
        rows.append((d, sym, exp, k, cp, float(c["close"][i]), float(f[0]["close"][j]), T, False))
    df = pd.DataFrame(rows, columns=["date", "symbol", "expiration", "strike", "cp", "px", "fwd", "T", "stale"])
    df["date"], df["expiration"] = pd.to_datetime(df["date"]), pd.to_datetime(df["expiration"])
    return df
CAPITAL_EVENTS = ("bonus", "split", "rights", "demerger")


def load_nse(db: Path, assume_spread: float = 0.04, r: float = 0.065, tick: float = 0.05, min_obs: int = 20,
             at: int = 900, window: int = 30):
    """The Indian store in the same shapes as load(): bars, chains, earnings, splits, plus a note.

    Option rows come from two writers. Recorded 15:00 quotes (real bid/ask) are used as they are.
    Archived candles fill the other days. With intraday candles, an option-day is priced at the trade
    closest to 15:00 (`at`, within +-`window` minutes) against the futures at the same minute, and
    dropped if it did not trade near 15:00. Without them (archive.interval = day), at the day's close
    (the last trade, so thin strikes can be stale; only days with volume). Their bid/ask is the price +- half the
    median relative spread recorded for that stock and delta bucket (at least `min_obs` quotes),
    else for that delta bucket across stocks, else `assume_spread`. The forward is the same expiry's
    futures: candle close for candle rows, recorded quote for quote rows, else spot * e^(rT).
    """
    from app.data.events import RESULTS_TYPES

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    def q(sql, dates):
        df = pd.read_sql_query(sql, conn)
        for c in dates:                       # also types the columns of an empty result
            df[c] = pd.to_datetime(df[c])
        return df
    keys = ["date", "symbol", "expiration", "strike", "cp"]
    bars = q("SELECT symbol, date, open, high, low, close FROM bars", ["date"])
    oc = q("SELECT date, symbol, expiration, strike, cp, close AS px FROM option_candles WHERE volume > 0 "
           "AND close > 0", ["date", "expiration"])
    rec = q("SELECT date, symbol, expiration, strike, cp, bid, ask FROM chains WHERE bid > 0 AND ask > bid",
            ["date", "expiration"])
    fc = q("SELECT date, symbol, expiration, close AS fwd FROM future_candles WHERE expiration != 'continuous' "
           "AND close > 0", ["date", "expiration"])
    fq = q("SELECT date, symbol, expiration, CASE WHEN bid > 0 AND ask > 0 THEN (bid + ask) / 2 ELSE last END "
           "AS fwd FROM futures", ["date", "expiration"])
    ev = q("SELECT symbol, event_date AS date, event_type FROM events_seen", ["date"])
    ip = intraday_prices(conn, at, window)
    conn.close()
    spot = bars[["symbol", "date", "close"]].rename(columns={"close": "spot"})

    def forward(df, fut):
        df = df.merge(fut, on=["date", "symbol", "expiration"], how="left").merge(spot, on=["symbol", "date"],
                                                                                  how="left")
        T = (df.expiration - df.date).dt.days.values / 365.0
        return df, np.where(df.fwd.notna(), df.fwd, df.spot * np.exp(r * T))

    rec, F = forward(rec, fq)
    rec = _with_iv(rec, ((rec.bid + rec.ask) / 2).values, F, r)
    rec["rel"] = (rec.ask - rec.bid) / ((rec.ask + rec.bid) / 2)
    rec["dbin"] = pd.cut(rec.delta.abs(), DBINS, right=False, labels=False)
    oc = oc.merge(rec[keys], on=keys, how="left", indicator=True)
    oc = oc[oc._merge == "left_only"].drop(columns="_merge")
    n_stale = 0
    if len(ip):
        oc = oc.merge(ip.rename(columns={"px": "px_i", "fwd": "fwd_i"}), on=keys, how="left")
        stale = oc.stale.fillna(False).astype(bool)
        n_stale = int(stale.sum())
        oc = oc[~stale].reset_index(drop=True)
    else:
        oc = oc.assign(px_i=np.nan, fwd_i=np.nan, T=np.nan)
    oc, F = forward(oc, fc)
    synced = oc.px_i.notna().values
    oc["px"] = np.where(synced, oc.px_i, oc.px)
    F = np.where(synced, oc.fwd_i, F)
    T_day = (oc.expiration - oc.date).dt.days.values / 365.0
    oc = _with_iv(oc, oc.px.values, F, r, np.where(synced, oc["T"], T_day))
    oc["dbin"] = pd.cut(oc.delta.abs(), DBINS, right=False, labels=False)
    by_sym = rec.groupby(["symbol", "dbin"]).rel.agg(["median", "size"])
    by_sym = by_sym[by_sym["size"] >= min_obs]["median"].rename("rel_sym").reset_index()
    by_bin = rec.groupby("dbin").rel.agg(["median", "size"])
    by_bin = by_bin[by_bin["size"] >= min_obs]["median"].rename("rel_bin").reset_index()
    oc = oc.merge(by_sym, on=["symbol", "dbin"], how="left").merge(by_bin, on="dbin", how="left")
    rel = oc.rel_sym.fillna(oc.rel_bin).fillna(assume_spread).values
    half = np.maximum(rel * oc.px.values / 2, tick / 2)
    oc["bid"], oc["ask"] = np.maximum(oc.px.values - half, 0.0), oc.px.values + half
    cols = keys + ["bid", "ask", "iv", "delta"]
    chains = pd.concat([rec[cols], oc[cols]], ignore_index=True) if len(oc) else rec[cols].copy()
    chains = chains.dropna(subset=["iv"]).reset_index(drop=True)
    earn = ev[ev.event_type.isin(RESULTS_TYPES)].assign(timing=None)[["symbol", "date", "timing"]].drop_duplicates()
    # Capital changes adjust NSE contracts and break the price series; the factor is not stored, so
    # these dates only exclude the windows across them (factor 1 = no price adjustment).
    cap = ev.loc[ev.event_type.isin(CAPITAL_EVENTS), ["symbol", "date"]]
    b = bars.sort_values(["symbol", "date"])
    jump = b.close / b.groupby("symbol").close.shift(1)
    cap = pd.concat([cap, b.loc[(jump < 0.6) | (jump > 1.67), ["symbol", "date"]]])
    splits = cap.rename(columns={"date": "ex_date"}).drop_duplicates().assign(to_factor=1.0, for_factor=1.0)
    n_sym, n_bin = int(oc.rel_sym.notna().sum()), int((oc.rel_sym.isna() & oc.rel_bin.notna()).sum())
    note = (f"Indian data: {len(rec):,} recorded quotes (real bid/ask) and {len(oc):,} candle prices, "
            f"{int(synced.sum()):,} of them at the trade nearest {at // 60:02d}:{at % 60:02d} with the futures at the "
            f"same minute; {n_stale:,} option-days dropped for no trade within {window} minutes of it; candle "
            f"spreads from recorded quotes of the same stock for {n_sym:,}, of all stocks for {n_bin:,}, assumed "
            f"{assume_spread:.0%} of the price for {len(oc) - n_sym - n_bin:,}. Rows without a valid IV are "
            f"dropped, leaving {len(chains):,}. Results dates as recorded: {len(earn):,}. Windows across "
            f"{len(splits):,} capital changes (bonus, split, rights, demerger, or a one-day jump over 40%) "
            f"are excluded.")
    return bars, chains, earn, splits, note


def load(db: Path):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    bars = pd.read_sql_query("SELECT symbol, date, open, high, low, close FROM bars", conn, parse_dates=["date"])
    chains = pd.read_sql_query("SELECT date, symbol, expiration, strike, cp, bid, ask, iv, delta FROM chains",
                               conn, parse_dates=["date", "expiration"])
    earn = pd.read_sql_query("SELECT symbol, date, timing FROM earnings", conn, parse_dates=["date"])
    splits = pd.read_sql_query("SELECT symbol, ex_date, to_factor, for_factor FROM splits", conn, parse_dates=["ex_date"])
    conn.close()
    return bars, chains, earn, splits


def underlying_features(bars: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Split-adjusted returns, HV20, Wilder ATR14, F5 shock flag; raw close kept for payoffs."""
    out = []
    for sym, g in bars.sort_values("date").groupby("symbol"):
        g = g.set_index("date").copy()
        f = pd.Series(1.0, index=g.index)
        for _, s in splits[splits.symbol == sym].iterrows():
            if s.to_factor and s.for_factor and s.to_factor != s.for_factor:
                f[g.index < s.ex_date] *= s.to_factor / s.for_factor
        for c in ("open", "high", "low", "close"):
            g["adj_" + c] = g[c] / f
        r = np.log(g.adj_close).diff()
        g["ret"] = r
        g["hv20"] = r.rolling(20).std() * math.sqrt(252)
        pc = g.adj_close.shift(1)
        tr = pd.concat([g.adj_high - g.adj_low, (g.adj_high - pc).abs(), (g.adj_low - pc).abs()], axis=1).max(axis=1)
        atr = np.full(len(tr), np.nan)
        if len(tr) > 14:
            a = tr.iloc[1:15].mean()
            atr[14] = a
            for i in range(15, len(tr)):
                a = (a * 13 + tr.iloc[i]) / 14
                atr[i] = a
        g["atr"] = atr
        move = (g.adj_close - pc).abs()
        g["shock_day"] = move >= SHOCK_ATR * pd.Series(atr, index=g.index).shift(1)
        g["shock10"] = g.shock_day.rolling(SHOCK_LOOKBACK, min_periods=1).max().astype(bool)
        g["symbol"] = sym
        out.append(g.reset_index())
    return pd.concat(out, ignore_index=True)


def close_on_or_before(feat: pd.DataFrame):
    idx = {sym: g.set_index("date")["close"] for sym, g in feat.groupby("symbol")}

    def get(sym, d):
        s = idx[sym]
        s = s[s.index <= d]
        return float(s.iloc[-1]) if len(s) else np.nan
    return get, idx


# ----------------------------------------------------------------------------- A: VRP

def atm_panel(chains: pd.DataFrame, feat: pd.DataFrame, earn: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """One row per symbol-day: the expiry nearest 28 DTE inside 15-35, its ATM IV, forward RV to expiry."""
    ch = chains.copy()
    ch["dte"] = (ch.expiration - ch.date).dt.days
    ch = ch[(ch.dte >= MIN_DTE) & (ch.dte <= MAX_DTE) & (ch.iv > 0)]
    spot = feat[["symbol", "date", "close"]].rename(columns={"close": "spot"})
    ch = ch.merge(spot, on=["symbol", "date"], how="inner")
    ch["dist"] = (ch.dte - TARGET_DTE).abs()
    pick = ch.loc[ch.groupby(["symbol", "date"]).dist.idxmin(), ["symbol", "date", "expiration"]]
    ch = ch.merge(pick, on=["symbol", "date", "expiration"])
    ch["kdist"] = (ch.strike - ch.spot).abs()
    nearest = ch.loc[ch.groupby(["symbol", "date"]).kdist.idxmin(), ["symbol", "date", "strike"]]
    atm = ch.merge(nearest, on=["symbol", "date", "strike"]).groupby(["symbol", "date", "expiration"]).agg(
        atm_iv=("iv", "mean"), spot=("spot", "first")).reset_index()
    rets = {sym: g.set_index("date")["ret"] for sym, g in feat.groupby("symbol")}
    split_dates = {s: list(g.ex_date) for s, g in splits.groupby("symbol")}
    earn_dates = {s: np.array(sorted(g.date.values)) for s, g in earn.groupby("symbol")}
    rv, n_ret, earn_in, split_in, since_earn = [], [], [], [], []
    for row in atm.itertuples():
        r = rets[row.symbol]
        w = r[(r.index > row.date) & (r.index <= row.expiration)].dropna()
        n_ret.append(len(w))
        rv.append(math.sqrt(252 * float((w ** 2).mean())) if len(w) >= 5 else np.nan)
        ed = earn_dates.get(row.symbol, np.array([], dtype="datetime64[ns]"))
        earn_in.append(bool(((ed >= np.datetime64(row.date)) & (ed <= np.datetime64(row.expiration))).any()))
        past = ed[ed < np.datetime64(row.date)]
        since_earn.append((row.date - pd.Timestamp(past[-1])).days if len(past) else np.nan)
        split_in.append(any(row.date < s <= row.expiration for s in split_dates.get(row.symbol, [])))
    atm["rv_fwd"], atm["n_ret"], atm["earn_in_window"] = rv, n_ret, earn_in
    atm["split_in_window"], atm["days_since_earn"] = split_in, since_earn
    atm = atm.merge(feat[["symbol", "date", "hv20", "atr", "shock10", "adj_close"]], on=["symbol", "date"], how="left")
    atm["iv_hv"] = atm.atm_iv / atm.hv20
    atm = atm.sort_values(["symbol", "date"])
    atm["ivp"] = atm.groupby("symbol").atm_iv.transform(
        lambda s: s.rolling(120, min_periods=60).apply(lambda x: (x[:-1] < x[-1]).mean() * 100, raw=True))
    atm["iv_over_rv"] = atm.atm_iv / atm.rv_fwd
    atm["vrp_var"] = atm.atm_iv ** 2 - atm.rv_fwd ** 2
    last_bar = feat.groupby("symbol").date.max()
    done = atm.expiration.values <= atm.symbol.map(last_bar).values      # held to expiry: window complete
    return atm[done & ~atm.split_in_window & atm.rv_fwd.notna()]


def bucket_iv_hv(x):
    return "< 1.00" if x < 1.0 else ("1.00-1.25" if x < 1.25 else ">= 1.25")


def bucket_ivp(x):
    if np.isnan(x):
        return "n/a"
    return "< 30" if x < 30 else ("30-70" if x < 70 else ">= 70")


def vrp_tables(atm: pd.DataFrame) -> str:
    out = []
    def summarise(df, label):
        return {"condition": label, "symbol-days": len(df), "median IV/RV": df.iv_over_rv.median(),
                "IV > RV share": (df.iv_over_rv > 1).mean(), "mean IV-RV (vol pts)": (df.atm_iv - df.rv_fwd).mean() * 100}
    rows = [summarise(atm, "all")]
    rows += [summarise(atm[atm.earn_in_window == e], f"earnings in window: {'yes' if e else 'no'}") for e in (True, False)]
    out.append(table(pd.DataFrame(rows)))
    clean = atm[~atm.earn_in_window]
    rows = []
    for lab, sub in clean.groupby(clean.iv_hv.apply(bucket_iv_hv)):
        rows.append(summarise(sub, f"IV/HV20 {lab}"))
    for lab, sub in clean.groupby(clean.ivp.apply(bucket_ivp)):
        rows.append(summarise(sub, f"IV percentile {lab}"))
    for lab, sub in clean.groupby(clean.shock10):
        rows.append(summarise(sub, f"shock in last 10 days: {'yes' if lab else 'no'}"))
    f345 = clean[(clean.iv_hv >= 1.25) & (clean.ivp >= 70) & clean.shock10]
    rows.append(summarise(f345, "F3 and F4 and shock (our setup)"))
    out.append(table(pd.DataFrame(rows)))
    return "\n\n".join(out)


# ----------------------------------------------------------------------------- B/C/D: selling options to expiry

def option_returns(chains: pd.DataFrame, feat: pd.DataFrame, atm: pd.DataFrame) -> pd.DataFrame:
    ch = chains.copy()
    ch["dte"] = (ch.expiration - ch.date).dt.days
    ch = ch[(ch.dte >= MIN_DTE) & (ch.dte <= MAX_DTE) & (ch.bid > 0) & (ch.ask > ch.bid)]
    ch["mid"] = (ch.bid + ch.ask) / 2
    ch = ch[(ch.ask - ch.bid) / ch.mid <= 0.5]
    ch["adelta"] = ch.delta.abs()
    ch = ch[(ch.adelta >= 0.05) & (ch.adelta < 0.55)]
    cond = atm[["symbol", "date", "expiration", "spot", "iv_hv", "ivp", "shock10", "earn_in_window", "atm_iv",
                "rv_fwd"]]
    ch = ch.merge(cond, on=["symbol", "date", "expiration"], how="inner")
    get_close, _ = close_on_or_before(feat)
    exp_close = {}
    for sym, e in ch[["symbol", "expiration"]].drop_duplicates().itertuples(index=False):
        exp_close[(sym, e)] = get_close(sym, e)
    ch["s_T"] = [exp_close[(s, e)] for s, e in zip(ch.symbol, ch.expiration)]
    ch = ch[ch.s_T.notna()]
    ch["payoff"] = np.where(ch.cp == "CE", np.maximum(ch.s_T - ch.strike, 0), np.maximum(ch.strike - ch.s_T, 0))
    ch["pnl_mid_pct_spot"] = (ch.mid - ch.payoff) / ch.spot * 100
    ch["pnl_bid_pct_spot"] = (ch.bid - ch.payoff) / ch.spot * 100
    ch["ret_on_premium"] = (ch.mid - ch.payoff) / ch.mid
    ch["win"] = ch.payoff < ch.mid
    ch["dbin"] = pd.cut(ch.adelta, [0.05, 0.15, 0.25, 0.35, 0.45, 0.55], right=False,
                        labels=["0.05-0.15", "0.15-0.25", "0.25-0.35", "0.35-0.45", "0.45-0.55"])
    return ch


def verticals(opts: pd.DataFrame, wing_pct: float = 0.0, short_band=(0.20, 0.30)) -> pd.DataFrame:
    """Short leg at |delta| in short_band; long leg the next listed strike further out of the money
    (wing_pct = 0) or the listed strike closest to wing_pct % of spot further out."""
    rows = []
    for (sym, d, e, cp), g in opts.groupby(["symbol", "date", "expiration", "cp"]):
        g = g.sort_values("strike")
        ks = g.strike.values
        for i, r in enumerate(g.itertuples()):
            if not (short_band[0] <= r.adelta <= short_band[1]):
                continue
            if wing_pct <= 0:
                j = i - 1 if cp == "PE" else i + 1
            else:
                target = r.strike - wing_pct / 100 * r.spot if cp == "PE" else r.strike + wing_pct / 100 * r.spot
                cands = [k for k in range(len(g)) if (k < i if cp == "PE" else k > i)]
                if not cands:
                    continue
                j = min(cands, key=lambda k: abs(ks[k] - target))
                if abs(ks[j] - target) > 0.35 * wing_pct / 100 * r.spot:
                    continue
            if j < 0 or j >= len(g):
                continue
            lg = g.iloc[j]
            width = abs(r.strike - lg.strike)
            if width <= 0:
                continue
            credit_mid = r.mid - lg.mid
            credit_nat = r.bid - lg.ask
            pay = (min(max(r.strike - r.s_T, 0), width) if cp == "PE" else min(max(r.s_T - r.strike, 0), width))
            rows.append({"symbol": sym, "date": d, "expiration": e, "cp": cp, "width_pct_spot": width / r.spot * 100,
                         "credit_mid_w": credit_mid / width, "credit_nat_w": credit_nat / width,
                         "pnl_mid_w": (credit_mid - pay) / width, "pnl_nat_w": (credit_nat - pay) / width,
                         "pnl_mid_ml": (credit_mid - pay) / max(width - credit_mid, 1e-9),
                         "win": pay < credit_mid, "iv_hv": r.iv_hv, "ivp": r.ivp, "shock10": r.shock10,
                         "earn_in_window": r.earn_in_window, "adelta": r.adelta})
    return pd.DataFrame(rows)


def option_tables(opts: pd.DataFrame) -> str:
    out = []
    clean = opts[~opts.earn_in_window]
    rows = []
    for (cp, b), g in clean.groupby(["cp", "dbin"], observed=True):
        s = by_expiry_stats(g, "pnl_mid_pct_spot")
        sb = by_expiry_stats(g, "pnl_bid_pct_spot")
        rows.append({"side": "put" if cp == "PE" else "call", "|delta|": b, "trades": s["trades"],
                     "win rate": g.win.mean(), "mean P&L at mid (% of spot)": s["mean"], "t (NW)": s["t"],
                     "mean P&L selling at bid (% of spot)": sb["mean"], "t bid (NW)": sb["t"],
                     "mean return on premium (mid)": g.ret_on_premium.mean()})
    out.append(table(pd.DataFrame(rows)))
    return "\n\n".join(out)


def condition_rows(df: pd.DataFrame, col: str, extra_win: str = "win") -> pd.DataFrame:
    rows = []
    def add(label, sub):
        if len(sub) < 30:
            rows.append({"condition": label, "trades": len(sub), "win rate": sub[extra_win].mean() if len(sub) else np.nan,
                         "mean": sub[col].mean() if len(sub) else np.nan, "t (NW)": np.nan})
            return
        s = by_expiry_stats(sub, col)
        rows.append({"condition": label, "trades": s["trades"], "win rate": sub[extra_win].mean(), "mean": s["mean"],
                     "t (NW)": s["t"]})
    clean = df[~df.earn_in_window]
    add("all (earnings in window)", df[df.earn_in_window])
    add("all (no earnings in window)", clean)
    for lab, sub in clean.groupby(clean.iv_hv.apply(bucket_iv_hv)):
        add(f"IV/HV20 {lab}", sub)
    for lab, sub in clean.groupby(clean.ivp.apply(bucket_ivp)):
        add(f"IV percentile {lab}", sub)
    for lab, sub in clean.groupby(clean.shock10):
        add(f"shock in last 10 days: {'yes' if lab else 'no'}", sub)
    add("F3 and F4 and shock (our setup)", clean[(clean.iv_hv >= 1.25) & (clean.ivp >= 70) & clean.shock10])
    add("F3 and F4, no recent shock", clean[(clean.iv_hv >= 1.25) & (clean.ivp >= 70) & ~clean.shock10])
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- E: levels

def levels_test(feat: pd.DataFrame, step: int = 2) -> str:
    """For days where spot sits within 1.5 ATR above a support (or below a resistance), how often does
    the close reach the level (thesis stop) or 0.25 ATR beyond it (where F6 puts the short strike)
    within LEVEL_HORIZON days, versus the same-size move on any day of the same stock?"""
    res = {"support": [], "resistance": []}
    for sym, g in feat.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        closes = g.adj_close.values
        atr = g.atr.values
        n = len(g)
        fut_min = np.full(n, np.nan)
        fut_max = np.full(n, np.nan)
        for i in range(n - LEVEL_HORIZON):
            w = closes[i + 1:i + 1 + LEVEL_HORIZON]
            fut_min[i], fut_max[i] = w.min(), w.max()
        down = (closes - fut_min) / atr        # largest drop within the horizon, in ATRs
        up = (fut_max - closes) / atr
        daily = pd.DataFrame({"high": g.adj_high, "low": g.adj_low, "close": g.adj_close, "open": g.adj_open})
        daily.index = g.date.dt.date
        for i in range(260, n - LEVEL_HORIZON, step):
            if np.isnan(atr[i]) or atr[i] <= 0:
                continue
            hist = daily.iloc[:i]                     # bars up to the previous day
            lv = compute_levels(hist, float(atr[i - 1]))
            spot = closes[i]
            sups = [l for l in lv if 0 <= spot - l.price <= 1.5 * atr[i]]
            ress = [l for l in lv if 0 <= l.price - spot <= 1.5 * atr[i]]
            if sups:
                s = max(sups, key=lambda l: l.price)
                d_level = (spot - s.price) / atr[i]
                d_strike = d_level + 0.25
                res["support"].append((sym, d_level, d_strike, down[i], s.score))
            if ress:
                r = min(ress, key=lambda l: l.price)
                d_level = (r.price - spot) / atr[i]
                res["resistance"].append((sym, d_level, d_level + 0.25, up[i], r.score))
        res.setdefault("_ctrl_down", {})[sym] = down[~np.isnan(down)]
        res.setdefault("_ctrl_up", {})[sym] = up[~np.isnan(up)]
    rows = []
    for kind, ctrl_key in (("support", "_ctrl_down"), ("resistance", "_ctrl_up")):
        ev = pd.DataFrame(res[kind], columns=["symbol", "d_level", "d_strike", "move", "score"])
        if ev.empty:
            continue
        for target, col in (("close beyond the level (thesis stop)", "d_level"),
                            ("close 0.25 ATR beyond it (short strike)", "d_strike")):
            actual = (ev.move >= ev[col]).mean()
            expected = np.mean([(res[ctrl_key][s] >= d).mean() for s, d in zip(ev.symbol, ev[col])])
            rows.append({"level": kind, "event within 15 days": target, "cases": len(ev), "actual rate": actual,
                         "same-size move on any day": expected, "difference": actual - expected})
        hi = ev[ev.score >= ev.score.median()]
        actual = (hi.move >= hi.d_strike).mean()
        expected = np.mean([(res[ctrl_key][s] >= d).mean() for s, d in zip(hi.symbol, hi.d_strike)])
        rows.append({"level": kind, "event within 15 days": "short strike reached, top-half scored levels",
                     "cases": len(hi), "actual rate": actual, "same-size move on any day": expected,
                     "difference": actual - expected})
    return table(pd.DataFrame(rows))


# ----------------------------------------------------------------------------- main

def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--market", choices=["us", "nse"], default="us")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--assume-spread", type=float, default=0.04,
                    help="NSE only: bid-ask as a fraction of price where no quote was recorded")
    ap.add_argument("--levels-step", type=int, default=2, help="evaluate levels every Nth day (speed)")
    ap.add_argument("--risk-free", type=float, default=0.065, help="NSE only: rate for Black-76 (as config.yaml)")
    ap.add_argument("--at", default="15:00", help="NSE only: price options at the trade nearest this time (HH:MM)")
    ap.add_argument("--window", type=int, default=30, help="NSE only: ... within this many minutes, else drop")
    args = ap.parse_args(argv)
    if args.market == "nse":
        db = args.db or ROOT / "data" / "market_nse.db"
        hh, mm = (int(x) for x in args.at.split(":"))
        bars, chains, earn, splits, notes = load_nse(db, args.assume_spread, args.risk_free, at=hh * 60 + mm,
                                                     window=args.window)
        title = "Option-selling study on Indian data (NSE, recorded by this system)"
    else:
        db = args.db or DB
        bars, chains, earn, splits = load(db)
        notes = "US data from DoltHub: real end-of-day bid/ask."
        title = "Option-selling study on real US data"
    if chains.empty:
        print(f"# {title}\n\nNo option data in {db} yet.")
        return
    feat = underlying_features(bars, splits)
    atm = atm_panel(chains, feat, earn, splits)
    print(f"# {title}\n\nSource: {db.name}; {chains.symbol.nunique()} stocks, "
          f"{chains.date.min():%Y-%m-%d} to {chains.date.max():%Y-%m-%d}; {len(chains):,} option quotes. "
          f"Held to expiry, no exit rules. Expiries {MIN_DTE}-{MAX_DTE} days out at entry. {notes}\n")
    n_exp = atm.expiration.nunique()
    print(f"Completed expiries in the sample: {n_exp}. t-statistics need at least 8 (shown as n/a below that); "
          f"treat anything under ~24 as anecdotal.\n")
    if atm.empty:
        print("No completed holding windows yet: an option enters the study once its expiry has passed and "
              "the stock's close on that day is stored.")
        return
    print("## A. Implied vs subsequently realised volatility (ATM, ~28 days)\n")
    print(vrp_tables(atm), "\n")
    opts = option_returns(chains, feat, atm)
    if opts.empty:
        print("No tradeable option quotes (|delta| 0.05-0.55, spread under half the price) in completed windows.")
        return
    print("## B. Selling one option, held to expiry (no earnings in the window)\n")
    print("P&L per share as % of the stock price; t (NW) uses per-expiry means.\n")
    print(option_tables(opts), "\n")
    v = verticals(opts)
    print("## C. 1-strike vertical spreads, short leg at |delta| 0.20-0.30, held to expiry\n")
    if v.empty:
        print("No spreads in completed windows yet.\n")
    else:
        section_c(opts, v)
    print("## D. Single 0.15-0.35 delta options by condition (P&L at mid, % of spot)\n")
    core = opts[(opts.adelta >= 0.15) & (opts.adelta < 0.35)]
    for cp in ("PE", "CE"):
        print(f"### Short {'puts' if cp == 'PE' else 'calls'}\n")
        print(table(condition_rows(core[core.cp == cp], "pnl_mid_pct_spot")), "\n")
    print("## E. Do support/resistance levels hold?\n")
    if feat.groupby("symbol").size().max() > 260 + LEVEL_HORIZON:
        print(levels_test(feat, step=args.levels_step), "\n")
    else:
        print("Needs more than a year of daily bars per stock.\n")


def section_c(opts: pd.DataFrame, v: pd.DataFrame) -> None:
    print(f"Median credit/width at mid {v.credit_mid_w.median():.2f}, at natural prices {v.credit_nat_w.median():.2f}; "
          f"share with natural credit/width >= 0.25: {(v.credit_nat_w >= 0.25).mean():.1%}.\n")
    for cp in ("PE", "CE"):
        sub = v[v.cp == cp]
        print(f"### {'Bull put' if cp == 'PE' else 'Bear call'} spreads: P&L at mid as a share of width\n")
        print(table(condition_rows(sub, "pnl_mid_w")), "\n")
        print(f"Same spreads at natural prices (sell bid, buy ask): mean P&L {sub.pnl_nat_w.mean():.3f} of width, "
              f"t (NW) {by_expiry_stats(sub[~sub.earn_in_window], 'pnl_nat_w')['t']:.2f} (no-earnings windows).\n")
    print("### Wing width: does a wider spread survive the bid-ask? (no earnings in the window)\n")
    rows = []
    for label, vv in (("next listed strike", v), ("wing ~3% of spot", verticals(opts, 3.0)),
                      ("wing ~6% of spot", verticals(opts, 6.0)),
                      ("0.15-0.20 delta short, wing ~6%", verticals(opts, 6.0, (0.15, 0.20)))):
        for cp in ("PE", "CE"):
            if vv.empty:
                break
            sub = vv[(vv.cp == cp) & ~vv.earn_in_window]
            if len(sub) < 30:
                continue
            m, n = by_expiry_stats(sub, "pnl_mid_w"), by_expiry_stats(sub, "pnl_nat_w")
            rows.append({"spread": label, "side": "bull put" if cp == "PE" else "bear call", "trades": len(sub),
                         "median width % spot": sub.width_pct_spot.median(),
                         "median credit/width natural": sub.credit_nat_w.median(), "win rate": sub.win.mean(),
                         "P&L/width at mid": m["mean"], "t mid": m["t"], "P&L/width at natural": n["mean"],
                         "t natural": n["t"]})
    print(table(pd.DataFrame(rows)) if rows else "Not enough spreads per side yet.", "\n")


if __name__ == "__main__":
    main()
