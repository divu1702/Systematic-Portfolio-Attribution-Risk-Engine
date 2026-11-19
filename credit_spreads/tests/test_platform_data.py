"""Shared data service: one broker session for every system, the expiry rule, live session candles,
minute candles, margins, sectors freshness, events-file age, and NSE bhavcopy parsing with fallbacks."""

import io
import os
import time
import zipfile
from datetime import date, datetime, timedelta

import pytest

from app.data.models import Sector
from platform_core import nse_files
from platform_core.data_service import Leg
from tests.conftest import SIM_NOW

FO_UDIFF = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks
2026-11-17,2026-11-17,FO,NSE,STO,1,,ANGELONE,,2026-11-24,2026-11-24,380,CE,ANGELONE26NOV380CE,6.1,9.0,3.2,3.6,3.5,6.0,350.2,3.6,412,30,980,1,1,F1,2500,
2026-11-17,2026-11-17,FO,NSE,STO,2,,ANGELONE,,2026-11-24,2026-11-24,340,PE,ANGELONE26NOV340PE,2.0,4.1,1.6,3.9,3.9,2.1,350.2,3.9,150,10,500,1,1,F1,2500,
2026-11-17,2026-11-17,FO,NSE,STF,3,,ANGELONE,,2026-11-24,2026-11-24,,,ANGELONE26NOVFUT,351,380,349,351,351,352,350.2,351,9000,10,5000,1,1,F1,2500,
"""
CM_UDIFF = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks
2026-11-17,2026-11-17,CM,NSE,STK,1,INE,ANGELONE,EQ,,,,,ANGEL ONE LTD,356.0,379.15,348.2,350.05,350.0,357.0,,,,,41000000,1,1,F1,1,
2026-11-17,2026-11-17,CM,NSE,STK,2,INE,ANGELONE,BE,,,,,ANGEL ONE LTD,1,1,1,1,1,1,,,,,1,1,1,F1,1,
"""
FO_LEGACY = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP
OPTSTK,KALYANKJIL,24-Nov-2026,640,PE,10,12,9,11,11,40,5,20,2,11-NOV-2026
"""


def test_expiry_rule_and_instruments(platform):
    ds = platform.ds
    assert "STK01" in ds.stocks()
    exps = ds.expiries("STK01")
    assert exps[0] == date(2026, 11, 24)
    chosen, other = ds.expiry_for("STK01", date(2026, 11, 6))        # 18 days left: current month
    assert (chosen, other) == (date(2026, 11, 24), exps[1])
    chosen, other = ds.expiry_for("STK01", date(2026, 11, 10))       # 14 days left: next month
    assert chosen == exps[1] and other == date(2026, 11, 24)
    ch = ds.chain("STK01", chosen)
    assert ch.lot_size > 0 and ch.strike_step > 0


def test_session_row_from_live_quote(platform):
    ds = platform.ds
    q = ds.spot_quotes(["STK01"])["STK01"]
    assert q.day_high >= q.last >= q.day_low > 0 and q.volume > 0
    bars = ds.daily_bars("STK01") if len(ds.daily_bars("STK01")) else None
    import pandas as pd

    base = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [10]},
                        index=[date(2026, 11, 5)])
    out = ds.with_session(base, date(2026, 11, 6), q)
    assert list(out.index) == [date(2026, 11, 5), date(2026, 11, 6)]
    assert out.loc[date(2026, 11, 6), "close"] == q.last and out.loc[date(2026, 11, 6), "volume"] == q.volume
    again = ds.with_session(out, date(2026, 11, 6), q)                 # replaces, never duplicates
    assert len(again) == 2


def test_minute_candles_index_and_margin(platform):
    ds = platform.ds
    rows = ds.minute_candles("STK01", date(2026, 11, 4), date(2026, 11, 5))
    assert rows and all(r["date"].date() in (date(2026, 11, 4), date(2026, 11, 5)) for r in rows)
    assert ds.index_token("NIFTY 50") is None                         # the mock market has its own index
    idx = ds.index_bars("SYNTHETIC INDEX", days=60)
    assert len(idx) > 30 and {"open", "high", "low", "close"} <= set(idx.columns)
    ch = ds.chain("STK01", date(2026, 11, 24))
    calls = sorted(k for k, cp in ch.legs if cp == "CE")
    short, long_ = ch.leg(calls[len(calls) // 2], "CE"), ch.leg(calls[len(calls) // 2 + 2], "CE")
    m = ds.margin([Leg(short.tradingsymbol, short.strike, "CE", "SELL", ch.lot_size),
                   Leg(long_.tradingsymbol, long_.strike, "CE", "BUY", ch.lot_size)])
    assert m == pytest.approx((long_.strike - short.strike) * ch.lot_size * 1.05)


def test_sector_freshness(platform, tmp_path):
    ds, ctx = platform.ds, platform.ctx
    from sqlalchemy.dialects.sqlite import insert

    with ctx.engine.begin() as conn:
        conn.execute(insert(Sector).values(symbol="AAA", sector="Banks", source="nse", updated_at=SIM_NOW))
        conn.execute(insert(Sector).values(symbol="BBB", sector="Old", source="nse",
                                           updated_at=SIM_NOW - timedelta(days=200)))
    assert ds.sector_of("AAA") == "Banks"
    assert ds.sector_of("BBB") == "Old"                                 # stale, no file: last known
    f = platform.cfg.resolve(platform.cfg.paths.sectors_file)
    f.write_text("symbol,sector\nBBB,Capital Goods\nCCC,Metals\n")
    assert ds.sector_of("BBB") == "Capital Goods" and ds.sector_of("CCC") == "Metals"
    old = time.time() - 91 * 86400
    os.utime(f, (old, old))                                             # older than 90 days: not used
    assert ds.sector_of("CCC") == "UNKNOWN"


def test_events_file_age(platform):
    ds = platform.ds
    folder = ds.events_import_dir()
    folder.mkdir(parents=True, exist_ok=True)
    assert ds.newest_events_file_age_days() is None
    p = folder / "board_meetings.csv"
    p.write_text("symbol,date\nX,2026-11-20\n")
    two_days = time.time() - 2 * 86400
    os.utime(p, (two_days, two_days))
    assert ds.newest_events_file_age_days() == pytest.approx(2.0, abs=0.01)


def test_bhavcopy_parsers():
    oi, settle = nse_files.parse_fo(FO_UDIFF)
    key = ("ANGELONE", date(2026, 11, 24), 380.0, "CE")
    assert oi[key] == 412 * 2500                                       # contracts x lot = quantity
    assert settle[key] == 3.6 and len(oi) == 2                         # futures are not options
    cm = nse_files.parse_cm(CM_UDIFF)
    assert cm["ANGELONE"]["close"] == 350.05 and cm["ANGELONE"]["high"] == 379.15
    assert cm["ANGELONE"]["volume"] == 41000000                        # the EQ series only
    oi2, _ = nse_files.parse_fo(FO_LEGACY, lots={"KALYANKJIL": 1475})
    assert oi2[("KALYANKJIL", date(2026, 11, 24), 640.0, "PE")] == 20 * 1475
    oi3, _ = nse_files.parse_fo(FO_LEGACY)                              # no lot size: skipped, not guessed
    assert oi3 == {}


def test_official_eod_falls_back_to_the_import_folder(tmp_path):
    def refuse(url):
        raise RuntimeError("HTTP 403")

    folder = tmp_path / "bhavcopy"
    folder.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_0_0_0_20261117_F_0000.csv", FO_UDIFF)
    (folder / "BhavCopy_NSE_FO_0_0_0_20261117_F_0000.csv.zip").write_bytes(buf.getvalue())
    (folder / "BhavCopy_NSE_CM_0_0_0_20261117_F_0000.csv").write_text(CM_UDIFF)
    eod = nse_files.official_eod(date(2026, 11, 17), folder, fetch=refuse)
    assert eod.source == "import" and eod.closes["ANGELONE"]["close"] == 350.05 and eod.oi
    assert nse_files.official_eod(date(2026, 11, 18), folder, fetch=refuse) is None

    def serve(url):
        return CM_UDIFF.encode() if "/cm/" in url else FO_UDIFF.encode()

    assert nse_files.official_eod(date(2026, 11, 17), folder, fetch=serve).source == "nse"


def test_official_eod_from_kite_when_nse_has_nothing(platform, monkeypatch):
    ds = platform.ds
    monkeypatch.setattr(nse_files, "download", lambda url, timeout=20.0: (_ for _ in ()).throw(RuntimeError("403")))
    eod = ds.official_eod(date(2026, 11, 5), symbols=["STK01"])
    assert eod.source == "kite" and eod.closes["STK01"]["close"] > 0


def test_fixture_recorder_round_trip(platform, tmp_path):
    from level_system import fixtures

    saved = fixtures.record(platform.ds, tmp_path, {"STK01": date(2026, 11, 5)})
    assert saved["STK01"] > 200
    df = fixtures.load(tmp_path, "STK01")
    assert df.index[-1] <= date(2026, 11, 6) and {"open", "high", "low", "close", "volume"} <= set(df.columns)
    assert fixtures.load(tmp_path, "NOPE") is None
