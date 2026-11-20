"""The market-data repository: Parquet parts and compaction, the nightly candle collector (every strike,
passes before expiry, stock and index minutes, backfill), the live minute-quote recorder, NSE's daily
files and the bhavcopy backfill, the OI unit check, migration from the earlier archiver, and the
research study reading it."""

import importlib.util
import io
import json
import sqlite3
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from datalake import candles, migrate, nse, quotes
from datalake.api import Lake as Q
from datalake.store import Lake
from tests.conftest import SIM_NOW

ROOT = Path(__file__).resolve().parents[1]
NOV = date(2026, 11, 24)


def lake_cfg(platform, **kw):
    base = {"strikes": "band", "band_pct": 0.04, "expiries": 1, "indices": ["SYNTHETIC INDEX"],
            "equity_minute_since": date(2026, 6, 1)}
    cfg = platform.cfg.model_copy(update={"lake": platform.cfg.lake.model_copy(update={**base, **kw})})
    platform.cfg = cfg
    platform.ds.cfg = cfg
    return platform


def opt_rows(ts0, n, close0, tradingsymbol="XYZ26NOV100CE", strike=100.0, cp="CE", und="XYZ", exp=NOV):
    return pd.DataFrame({"underlying": und, "expiry": exp, "tradingsymbol": tradingsymbol, "strike": strike, "cp": cp,
                         "ts": [ts0 + timedelta(minutes=i) for i in range(n)],
                         "open": close0, "high": close0, "low": close0, "close": [close0 + i for i in range(n)],
                         "volume": 25, "oi": 1000})


# ----------------------------------------------------------------------------- storage and queries
def test_parts_compaction_and_queries(tmp_path):
    lk = Lake(tmp_path / "lake")
    t0 = datetime(2026, 11, 17, 9, 15)
    lk.write("options_1m", opt_rows(t0, 5, 10.0))
    newer = opt_rows(t0 + timedelta(minutes=4), 3, 50.0)                 # repeats 09:19 with a new close
    lk.write("options_1m", newer)
    q = Q(tmp_path / "lake")
    df = q.options("XYZ", NOV)
    assert len(df) == 7 and df.ts.is_monotonic_increasing
    assert df.set_index("ts").loc[t0 + timedelta(minutes=4), "close"] == 50.0      # the newest copy wins
    assert q.sql("SELECT count(*) AS n FROM options_1m").n[0] == 8                # the raw view keeps both
    folder = lk.partition_dir("options_1m", underlying="XYZ", expiry=NOV)
    assert len(list(folder.glob("*.parquet"))) == 2
    lk.compact(folder, "options_1m")
    assert [p.name for p in folder.glob("*.parquet")] == ["data.parquet"]
    df2 = Q(tmp_path / "lake").options("XYZ", NOV)
    pd.testing.assert_frame_equal(df.reset_index(drop=True), df2.reset_index(drop=True), check_like=True)
    assert "underlying" not in pd.read_parquet(folder / "data.parquet").columns   # partition values live in paths
    chain = q.chain_at("XYZ", NOV, "2026-11-17 09:17")
    assert len(chain) == 1 and chain.close[0] == 12.0 and chain.volume_day[0] == 75
    assert q.options("NOPE").empty


# ----------------------------------------------------------------------------- nightly candles
def test_candle_collector_passes_resume_and_compaction(platform):
    p = lake_cfg(platform)
    rep = candles.run(p, budget=10**6)
    assert rep.options > 0 and rep.futures > 0 and rep.symbols >= 9 and rep.rows > 0 and not rep.pending
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    rows = lake.catalog.execute("SELECT kind, passes, final, last_fetched FROM contracts")
    assert rows and all(r[1] == 2 and r[2] == 0 and r[3] == "2026-11-05" for r in rows)   # 18 days left: middle pass
    q = Q(lake.root)
    sym = rows and lake.catalog.execute("SELECT underlying FROM contracts WHERE kind='option' LIMIT 1")[0][0]
    opts = q.options(sym, NOV)
    assert len(opts) and opts.ts.min().date() >= NOV - timedelta(days=100) and opts.ts.max().date() == date(2026, 11, 5)
    assert not opts.duplicated(["tradingsymbol", "ts"]).any() and (opts.oi > 0).all()
    assert len(q.futures(sym)) and len(q.equity(sym)) and len(q.equity("SYNTHETIC INDEX"))
    assert len(q.instruments(p.ctx.clock.today())) > 0
    again = candles.run(p, budget=10**6)                                  # nothing new is due
    assert again.options == 0 and again.futures == 0
    # Expiry week: the final pass fetches only what is new.
    p.ctx.clock.set(datetime(2026, 11, 20, 16, 30))
    fin = candles.run(p, budget=10**6)
    assert fin.options >= len([r for r in rows if r[0] == "option"])     # plus strikes the move brought into the band
    assert {r[0] for r in lake.catalog.execute("SELECT final FROM contracts")} == {1}
    opts2 = Q(lake.root).options(sym, NOV)
    assert opts2.ts.max().date() == date(2026, 11, 20) and not opts2.duplicated(["tradingsymbol", "ts"]).any()
    p.ctx.clock.set(datetime(2026, 11, 26, 16, 30))
    assert candles.compact_finished(p) > 0
    folder = lake.partition_dir("options_1m", underlying=sym, expiry=NOV)
    assert [f.name for f in folder.glob("*.parquet")] == ["data.parquet"]
    assert len(Q(lake.root).options(sym, NOV)) == len(opts2)


def test_budget_stops_cleanly_and_the_next_run_continues(platform):
    p = lake_cfg(platform)
    first = candles.run(p, budget=20)
    assert first.requests <= 20 and first.pending > 0
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    done = lake.catalog.execute("SELECT count(*) FROM contracts")[0][0]
    assert done == first.options + first.futures
    rest = candles.run(p, budget=10**6)
    assert rest.options + rest.futures + done >= first.pending + done and not rest.pending


def test_stock_minute_backfill_goes_back_to_the_floor(platform):
    p = lake_cfg(platform, equity_minute_since=date(2026, 5, 1))
    candles.run(p, budget=10**6, backfill_budget=10**6)
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    series = lake.catalog.execute("SELECT symbol, first_ts, backfill_done FROM series")
    assert series and all(r[2] == 1 for r in series)
    first = min(datetime.fromisoformat(r[1]) for r in series)
    assert first.date() <= date(2026, 5, 4)                               # reached the floor (first session)
    eq = Q(lake.root).equity(series[0][0])
    assert not eq.duplicated(["ts"]).any() and eq.ts.min().date() <= date(2026, 5, 4)


# ----------------------------------------------------------------------------- live quotes
def tick(tok, last, bid, ask, vol, oi=0, depth=True):
    t = {"instrument_token": tok, "last_price": last, "volume_traded": vol, "oi": oi, "average_traded_price": last,
         "total_buy_quantity": 900, "total_sell_quantity": 800, "last_traded_quantity": 25,
         "exchange_timestamp": datetime(2026, 11, 17, 9, 15)}
    if depth:
        t["depth"] = {"buy": [{"price": bid - 0.05 * i, "quantity": 100 * (i + 1), "orders": i + 1} for i in range(5)],
                      "sell": [{"price": ask + 0.05 * i, "quantity": 50 * (i + 1), "orders": i + 1} for i in range(5)]}
    return t


def test_minute_book_rows():
    meta = {1: quotes.Inst(1, "XYZ26NOV100CE", "CE", "XYZ", "2026-11-24", 100.0), 2: quotes.Inst(2, "XYZ", "EQ", "XYZ")}
    book = quotes.MinuteBook(meta, depth=True)
    t = datetime(2026, 11, 17, 9, 15, 5)
    book.on_ticks([tick(1, 10.0, 9.9, 10.1, 100), tick(2, 500.0, 499.9, 500.1, 1000)], t)
    book.on_ticks([tick(1, 10.2, 10.1, 10.3, 150)], t + timedelta(seconds=30))
    book.on_ticks([tick(2, 501.0, 500.9, 501.1, 2000)], t + timedelta(seconds=70))      # 09:16
    book.tick(t + timedelta(seconds=130))                                                  # 09:17: closes 09:16
    rows = book.take()
    assert [(r["ts"].minute, r["token"]) for r in rows] == [(15, 1), (15, 2), (16, 2)]
    r = rows[0]
    assert (r["last"], r["bid"], r["ask"], r["volume"], r["ticks"]) == (10.2, 10.1, 10.3, 150, 2)
    assert r["bid_qty"] == 100 and r["ask_orders"] == 1 and r["a5_price"] == pytest.approx(10.5)
    assert r["tradingsymbol"] == "XYZ26NOV100CE" and r["strike"] == 100.0
    plain = quotes.MinuteBook(meta, depth=False)
    plain.on_ticks([tick(1, 10.0, 9.9, 10.1, 100)], t)
    plain.close_minute()
    assert "b1_price" not in plain.take()[0]


def test_instrument_selection_within_the_limit(platform):
    p = lake_cfg(platform, quotes=platform.cfg.lake.quotes.model_copy(update={"max_instruments": 120}))
    insts = quotes.select(p)
    assert len(insts) == 120 and len({i.token for i in insts}) == 120
    kinds = [i.kind for i in insts]
    assert kinds[0] == "INDEX" and kinds.count("EQ") == 8 and "FUT" in kinds
    opts = [i for i in insts if i.kind in ("CE", "PE")]
    spot = p.ds.spot_quotes(["STK01"])["STK01"].last
    near = [abs(i.strike - spot) for i in opts if i.underlying == "STK01"]
    assert near and max(near) < 0.25 * spot                               # the nearest strikes, not the wings
    groups = quotes.split(insts, 50)
    assert [len(g) for g in groups] == [50, 50, 20]


class FakeTicker:
    MODE_FULL = "full"

    def __init__(self, *a):
        self.subscribed, self.closed = [], False

    def connect(self, threaded=False):
        self.on_connect(self, {})

    def subscribe(self, tokens):
        self.subscribed += list(tokens)

    def set_mode(self, mode, tokens):
        self.mode = mode

    def close(self):
        self.closed = True


def test_quote_worker_writes_minutes_and_the_recorder_compacts(platform):
    p = lake_cfg(platform)
    root = p.cfg.resolve(p.cfg.lake.root)
    now = [datetime(2026, 11, 17, 9, 14, 30)]
    ticker = FakeTicker()

    def sleep(_):
        now[0] += timedelta(seconds=1)
        if now[0].second % 10 == 0 and now[0] < datetime(2026, 11, 17, 9, 18):
            ticker.on_ticks(ticker, [tick(11, 10.0 + now[0].minute, 9.9, 10.1, now[0].minute * 100, oi=500)])

    insts = [{"token": 11, "tradingsymbol": "XYZ26NOV100CE", "kind": "CE", "underlying": "XYZ", "expiry": "2026-11-24",
              "strike": 100.0}]
    stats = quotes.worker(0, insts, "k", "t", str(root), False, "09:18", 1, ticker_factory=lambda *a: ticker,
                          clock=lambda: now[0], sleep=sleep)
    assert ticker.subscribed == [11] and ticker.mode == "full" and ticker.closed
    assert stats["minutes"] == 4 and stats["rows"] == 4                     # 09:14 to 09:17
    rec = quotes.Recorder(p)
    msg = rec.finish(date(2026, 11, 17))
    assert "4 minute rows" in msg
    folder = Lake(root).partition_dir("quotes_1m", date=date(2026, 11, 17))
    assert [f.name for f in folder.glob("*.parquet")] == ["data.parquet"]
    df = Q(root).quotes("2026-11-17", underlying="XYZ")
    assert list(df.ts.dt.minute) == [14, 15, 16, 17] and df.bid.iloc[0] == 9.9 and (df.oi == 500).all()


# ----------------------------------------------------------------------------- NSE files
FO = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks
{d},{d},FO,NSE,STO,1,,AAA,,2026-11-24,2026-11-24,380,CE,AAA26NOV380CE,6.1,9.0,3.2,3.6,3.5,6.0,350.2,3.6,412,30,980,1,1,F1,2500,
{d},{d},FO,NSE,STF,3,,AAA,,2026-11-24,2026-11-24,,,AAA26NOVFUT,351,380,349,351,351,352,350.2,351,9000,10,5000,1,1,F1,2500,
"""
CM = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks
{d},{d},CM,NSE,STK,1,INE,AAA,EQ,,,,,AAA LTD,356.0,379.15,348.2,350.05,350.0,357.0,,,,,41000000,1,1,F1,1,
"""
LEGACY_FO = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP
OPTSTK,AAA,27-Jun-2024,640,PE,10,12,9,11,11,40,5,20,2,03-JUN-2024
"""


def zipped(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def fake_nse(missing=(), refused=()):
    calls = []

    def fetch(url):
        calls.append(url)
        if any(m in url for m in refused):
            raise nse.Refused("HTTP 403")
        if any(m in url for m in missing):
            raise nse.Missing(url)
        name = url.rsplit("/", 1)[1]
        if "BhavCopy_NSE_FO" in name:
            ymd = name.split("_")[6]
            return zipped(name[:-4], FO.format(d=f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"))
        if "BhavCopy_NSE_CM" in name:
            ymd = name.split("_")[6]
            return zipped(name[:-4], CM.format(d=f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"))
        if name.startswith("fo") and name.endswith("bhav.csv.zip"):
            return zipped(name[:-4], LEGACY_FO)
        return b"col1,col2\n1,2\n"
    return fetch, calls


def test_nse_urls_parsing_and_daily_files(platform):
    p = lake_cfg(platform)
    assert nse.url_for("fo_bhav", date(2024, 6, 3)).endswith("/DERIVATIVES/2024/JUN/fo03JUN2024bhav.csv.zip")
    assert nse.url_for("fo_bhav", date(2026, 11, 17)).endswith("BhavCopy_NSE_FO_0_0_0_20261117_F_0000.csv.zip")
    assert nse.url_for("cm_delivery", date(2026, 11, 17)).endswith("sec_bhavdata_full_17112026.csv")
    fo = nse.parse("fo_bhav", FO.format(d="2026-11-17"), date(2026, 11, 17))
    assert list(fo.symbol) == ["AAA", "AAA"] and fo.expiry[0] == NOV and fo.oi_raw[0] == 412 and fo.lot[0] == 2500
    assert fo.format[0] == "udiff" and fo["src_FinInstrmNm"][0] == "AAA26NOV380CE"
    old = nse.parse("fo_bhav", LEGACY_FO, date(2024, 6, 3))
    assert old.format[0] == "legacy" and old.expiry[0] == date(2024, 6, 27) and old.oi_raw[0] == 20
    fetch, calls = fake_nse(missing=("FOVOLT",))
    rep = nse.run_daily(p, date(2026, 11, 6), fetch=fetch, pause=0)
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    st = dict(((k, d), s) for k, d, s in lake.catalog.execute("SELECT kind, date, status FROM nse_files"))
    assert st[("fo_bhav", "2026-11-06")] == "ok" and st[("fo_volatility", "2026-11-06")] == "missing"
    assert (lake.root / "nse_raw" / "fo_bhav" / "2026" / "BhavCopy_NSE_FO_0_0_0_20261106_F_0000.csv.zip").exists()
    q = Q(lake.root)
    assert len(q.bhav_fo("AAA")) == 12 and q.bhav_cm("AAA").close.iloc[0] == 350.05  # 2 contracts x 6 sessions
    n = len(calls)
    nse.run_daily(p, date(2026, 11, 6), fetch=fetch, pause=0)
    assert all("FOVOLT" in u for u in calls[n:])                          # only what is still missing


def test_bhavcopy_backfill_is_polite_and_stops_when_refused(platform):
    p = lake_cfg(platform, nse=platform.cfg.lake.nse.model_copy(update={"backfill_years": 1}))
    fetch, calls = fake_nse(missing=("20261102",))
    rep = nse.backfill(p, files=10, fetch=fetch, pause=0, today=date(2026, 11, 6))
    assert len(calls) == 10 and "fo_bhav 2026-11-02" in rep.missing
    assert calls[0].endswith("BhavCopy_NSE_FO_0_0_0_20261105_F_0000.csv.zip")          # newest first
    rep2 = nse.backfill(p, files=10, fetch=fetch, pause=0, today=date(2026, 11, 6))
    assert all("20261105" not in u for u in calls[10:])                                  # done ones are skipped
    blocked, bcalls = fake_nse(refused=("nsearchives",))
    rep3 = nse.backfill(p, files=50, fetch=blocked, pause=0, today=date(2026, 11, 6))
    assert len(bcalls) == 3 and "stopped after 3 refusals in a row" in rep3.refused


def test_oi_unit_is_settled_against_kite(platform):
    from platform_core.db import get_state

    p = lake_cfg(platform)
    ds, today = p.ds, p.ctx.clock.today()
    ch = ds.chain("STK01", NOV)
    q = ds.quotes([leg.key for leg in ch.legs.values()])
    rows = [{"symbol": "STK01", "instrument": "STO", "expiry": NOV, "strike": k, "cp": cp, "oi_raw": q[leg.key].oi / ch.lot_size,
             "row": n, "trade_date": today, "date": today}
            for n, ((k, cp), leg) in enumerate(ch.legs.items()) if leg.key in q and q[leg.key].oi]
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    lake.write("bhav_fo", pd.DataFrame(rows))
    assert nse.detect_oi_unit(p, lake, today) == "lots"
    assert get_state(p.pengine, "nse_oi_unit") == "lots" and lake.catalog.get_meta("nse_oi_unit") == "lots"


# ----------------------------------------------------------------------------- migration and the study
def test_migration_from_the_earlier_archiver(platform, tmp_path):
    from app.backtest.store import connect
    from app.data import barcodec

    p = lake_cfg(platform)
    store = tmp_path / "market_nse.db"
    conn = connect(store)
    t0 = datetime(2026, 11, 3, 9, 15)
    cs = [{"date": t0 + timedelta(minutes=m), "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0 + m / 10,
           "volume": 50, "oi": 1000 + m} for m in range(0, 30, 3)]
    blob = barcodec.encode(barcodec.candle_columns(cs))
    conn.execute("INSERT INTO option_bars VALUES (?,?,?,?,?,?,?,?,?)",
                 ("AAA", "2026-11-24", 227.5, "PE", "2026-11-03", "minute", len(cs), 250, blob))
    conn.execute("INSERT INTO future_bars VALUES (?,?,?,?,?,?)", ("AAA", "2026-11-24", "2026-11-03", "minute", len(cs), blob))
    conn.execute("INSERT INTO archive_progress VALUES (?,?,?,?,?,?,?)",
                 ("AAA26NOV227.5PE", "option", "AAA", "2026-11-24", "2026-11-03", 2, 0))
    conn.commit()
    conn.close()
    msg = migrate.migrate(p, store)
    assert "10 option and 10 futures candles" in msg
    q = Q(p.cfg.resolve(p.cfg.lake.root))
    o = q.options("AAA", NOV)
    assert len(o) == 10 and o.tradingsymbol[0] == "AAA26NOV227.5PE" and o.ts[0] == pd.Timestamp(t0)
    assert o.close.iloc[-1] == pytest.approx(12.7) and o.oi.iloc[-1] == 1027
    assert q.futures("AAA").tradingsymbol[0] == "AAA26NOVFUT"
    lake = Lake(p.cfg.resolve(p.cfg.lake.root))
    assert lake.catalog.execute("SELECT passes, last_fetched FROM contracts")[0] == (1, "2026-11-03")
    assert migrate.migrate(p, store) == "already migrated"


def test_research_study_reads_the_repository(tmp_path):
    from app.analytics import black76
    from app.backtest.store import connect

    spec = importlib.util.spec_from_file_location("oss", ROOT / "research" / "option_selling_study.py")
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    sigma, r = 0.30, 0.065
    d, e = date(2026, 3, 2), date(2026, 3, 31)
    lk = Lake(tmp_path / "lake")
    t0 = datetime.combine(d, time(9, 15))
    fut = pd.DataFrame({"underlying": "AAA", "expiry": e, "tradingsymbol": "AAA26MARFUT",
                        "ts": [t0 + timedelta(minutes=m) for m in range(375)],
                        "close": [1000 + m * 0.08 for m in range(375)], "volume": 500, "oi": 0})
    fut["open"] = fut["high"] = fut["low"] = fut["close"]
    lk.write("futures_1m", fut)
    fmin = {(t.hour * 60 + t.minute): c for t, c in zip(fut.ts, fut.close)}
    rows = []
    for k, cp, times in ((1000.0, "CE", [(14, 50), (15, 29)]), (1050.0, "CE", [(14, 45)]), (950.0, "PE", [(13, 0)])):
        for hh, mm in times:
            m = hh * 60 + mm
            T = ((e - d).days * 1440 + (930 - (m + 1))) / (365 * 1440)
            px = round(black76.price(fmin[m], k, T, sigma, r, cp), 2)
            rows.append({"underlying": "AAA", "expiry": e, "tradingsymbol": f"AAA26MAR{int(k)}{cp}", "strike": k, "cp": cp,
                         "ts": datetime.combine(d, time(hh, mm)), "open": px, "high": px, "low": px, "close": px,
                         "volume": 250, "oi": 0})
    lk.write("options_1m", pd.DataFrame(rows))
    oc, fc, ip = study.lake_frames(tmp_path / "lake")
    assert len(oc) == 3 and len(fc) == 1 and ip.stale.sum() == 1
    db = tmp_path / "store.db"
    conn = connect(db)
    conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?)", ("AAA", str(d), 1000, 1031, 999, 1029.8, 10**6))
    conn.commit()
    conn.close()
    bars, chains, earn, splits, note = study.load_nse(db, r=r, lake=tmp_path / "lake")
    got = {(row.strike, row.cp): row.iv for row in chains.itertuples()}
    assert set(got) == {(1000.0, "CE"), (1050.0, "CE")}
    assert got[(1050.0, "CE")] == pytest.approx(sigma, abs=0.003) and got[(1000.0, "CE")] == pytest.approx(sigma, abs=0.003)
