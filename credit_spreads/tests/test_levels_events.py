from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app import market
from app.analytics.levels import (compute_levels, fractal_pivots, nearest_resistance, nearest_support,
                                  weekly_bars)
from app.clock import SimClock
from app.data import csvs, daily, events, refdata
from app.data.calendar import TradingCalendar
from app.data.levels_store import load_levels, refresh_levels
from app.data.nse import (classify_event, parse_board_meetings, parse_bhavcopy_option_turnover,
                          parse_corporate_actions, parse_holidays, parse_nse_date, parse_sectors_csv)
from tests.conftest import SIM_NOW


def _bars(highs, lows=None, start=date(2026, 1, 5)):
    lows = lows if lows is not None else [h - 2 for h in highs]
    idx = pd.bdate_range(start, periods=len(highs)).date
    return pd.DataFrame({"open": highs, "high": highs, "low": lows, "close": highs}, index=idx)


# ----------------------------------------------------------------------------- fractals

def test_fractal_high_and_low_with_two_bars_each_side():
    highs = [10, 11, 15, 11, 10, 12, 13]
    lows = [9, 8, 12, 5, 9, 10, 11]
    piv = fractal_pivots(_bars(highs, lows), 2, "daily")
    assert [(p.kind, p.price) for p in piv] == [("high", 15), ("low", 5)]


def test_flat_double_top_counts_once():
    highs = [10, 11, 15, 15, 11, 10, 9]
    piv = [p for p in fractal_pivots(_bars(highs), 2, "daily") if p.kind == "high"]
    assert len(piv) == 1 and piv[0].price == 15


def test_weekly_bars_resample():
    df = _bars(list(range(10, 20)))          # two business weeks
    w = weekly_bars(df)
    assert len(w) == 2 and w["high"].iloc[0] == 14 and w["high"].iloc[1] == 19


def test_clustering_and_scoring():
    # Three swing highs near 100 (two recent), one near 120; ATR 4 -> threshold 2.
    highs = ([90] * 6 + [100] + [90] * 6 + [101] + [90] * 6 + [120] + [90] * 6 + [99.5] + [90] * 6)
    df = _bars(highs, [h - 3 for h in highs])
    levels = compute_levels(df, 4.0, fractal_n=5, lookback=250, cluster_atr=0.5, recency_halflife=60,
                            weekly_weight=2.0)
    near_100 = [lv for lv in levels if 98 < lv.price < 102 and lv.touches >= 3]
    assert near_100, levels
    assert near_100[0].score > max(lv.score for lv in levels if 119 < lv.price < 121)
    assert nearest_resistance(levels, 105).price == pytest.approx(120, abs=1.5)
    assert nearest_support(levels, 105).price == pytest.approx(near_100[0].price)


def test_recency_raises_score():
    old = [90] * 6 + [100] + [90] * 40
    new = [90] * 40 + [100] + [90] * 6
    s_old = max(lv.score for lv in compute_levels(_bars(old), 4.0) if lv.price == 100)
    s_new = max(lv.score for lv in compute_levels(_bars(new), 4.0) if lv.price == 100)
    assert s_new > s_old


def test_no_atr_no_levels():
    assert compute_levels(_bars([1, 2, 3]), 0) == []


# ----------------------------------------------------------------------------- NSE parsers

def test_parse_nse_dates():
    assert parse_nse_date("15-Nov-2026") == date(2026, 11, 15)
    assert parse_nse_date("15-11-2026") == date(2026, 11, 15)
    assert parse_nse_date("-") is None and parse_nse_date(None) is None


def test_classify_event():
    assert classify_event("Financial Results") == "results"
    assert classify_event("Dividend - Rs 5 Per Share") == "dividend"
    assert classify_event("Annual General Meeting") == "agm"
    assert classify_event("To consider fund raising") == "fund_raising"
    assert classify_event("Other business") == "board_meeting"


def test_parse_board_meetings_and_actions():
    bm = parse_board_meetings([
        {"bm_symbol": "KALYANKJIL", "bm_date": "12-Nov-2026", "bm_purpose": "Financial Results",
         "bm_desc": "Quarterly results"},
        {"bm_symbol": "", "bm_date": "12-Nov-2026"},           # skipped
    ])
    assert bm == [{"symbol": "KALYANKJIL", "event_date": date(2026, 11, 12), "event_type": "results",
                   "description": "Quarterly results", "source": "nse"}]
    ca = parse_corporate_actions([{"symbol": "ABC", "subject": "Dividend - Rs 2", "exDate": "20-Nov-2026",
                                   "recDate": "20-Nov-2026"}])
    assert {e["event_type"] for e in ca} == {"dividend:ex_date", "dividend:record_date"}


def test_parse_holidays_sectors_bhavcopy():
    h = parse_holidays({"FO": [{"tradingDate": "26-Jan-2026", "description": "Republic Day"}]})
    assert h == [(date(2026, 1, 26), "Republic Day")]
    s = parse_sectors_csv("Company Name,Industry,Symbol,Series,ISIN Code\nAcme Ltd,Capital Goods,ACME,EQ,X\n")
    assert s == {"ACME": "Capital Goods"}
    csv_text = ("TradDt,FinInstrmTp,TckrSymb,SttlmPric,TtlTradgVol\n"
                "2026-11-05,STO,ACME,10,1000\n2026-11-05,STO,ACME,5,200\n"
                "2026-11-05,IDO,NIFTY,100,99999\n2026-11-05,STF,ACME,500,10\n")
    assert parse_bhavcopy_option_turnover(csv_text) == {"ACME": 11000.0}


# ----------------------------------------------------------------------------- CSV overrides

def test_manual_csvs(tmp_path):
    p = tmp_path / "levels.csv"
    p.write_text("symbol,price,kind,note\nacme,580,support,weekly low\nACME,640,,\n")
    lv = csvs.manual_levels(p)
    assert [(m.symbol, m.price, m.kind) for m in lv] == [("ACME", 580.0, "support"), ("ACME", 640.0, "")]
    p.write_text("symbol,price,kind\nACME,abc,support\n")
    with pytest.raises(csvs.CSVError, match="number"):
        csvs.manual_levels(p)
    e = tmp_path / "ev.csv"
    e.write_text("symbol,event_date,event_type,description\nACME,2026-11-20,agm,\n")
    assert csvs.override_events(e)[0]["event_date"] == date(2026, 11, 20)
    e.write_text("symbol,event_date,event_type\nACME,20/11/2026,agm\n")
    with pytest.raises(csvs.CSVError, match="YYYY-MM-DD"):
        csvs.override_events(e)
    assert csvs.manual_levels(tmp_path / "missing.csv") == []


# ----------------------------------------------------------------------------- calendar

def test_trading_calendar():
    cal = TradingCalendar(frozenset({date(2026, 11, 9)}))
    assert not cal.is_trading_day(date(2026, 11, 7))         # Saturday
    assert not cal.is_trading_day(date(2026, 11, 9))         # holiday
    assert cal.next_trading_day(date(2026, 11, 6)) == date(2026, 11, 10)
    assert cal.trading_days_between(date(2026, 11, 6), date(2026, 11, 13)) == 4
    assert cal.last_n(date(2026, 11, 10), 2) == [date(2026, 11, 6), date(2026, 11, 10)]


# ----------------------------------------------------------------------------- jobs on the mock

def test_refdata_with_mock(mock_ctx):
    market.ensure_instruments(mock_ctx)
    assert refdata.refresh_sectors(mock_ctx) == 8
    assert refdata.sector_of(mock_ctx.engine, "STK01") == "Banks"
    assert refdata.sector_of(mock_ctx.engine, "NOPE") == "UNKNOWN"
    assert refdata.refresh_holidays(mock_ctx) > 0
    cal = TradingCalendar.load(mock_ctx.engine)
    assert not cal.is_trading_day(date(2026, 1, 26))
    assert refdata.refresh_turnover(mock_ctx, SIM_NOW.date(), 20) == 20
    assert refdata.refresh_turnover(mock_ctx, SIM_NOW.date(), 20) == 0      # cached
    top = refdata.universe(mock_ctx.engine, [f"STK{i:02d}" for i in range(1, 9)], SIM_NOW.date(), 5, 20, cal)
    assert len(top) == 5


def test_manual_sector_overrides(mock_ctx):
    path = mock_ctx.cfg.resolve(mock_ctx.cfg.paths.sectors_manual)
    path.write_text("symbol,sector\nSTK01,Fintech\n")
    refdata.refresh_sectors(mock_ctx)
    assert refdata.sector_of(mock_ctx.engine, "STK01") == "Fintech"


def test_daily_candles_and_indicators(mock_ctx):
    market.ensure_instruments(mock_ctx)
    yday = SIM_NOW.date() - timedelta(days=1)
    assert daily.update_underlying_daily(mock_ctx, ["STK01", "STK02"], through=yday) == 2
    df = daily.load_daily(mock_ctx.engine, "STK01")
    assert df.index[-1] < SIM_NOW.date()                    # partial today excluded
    assert df["atr14"].notna().sum() > 200 and df["hv20"].iloc[-1] > 0
    assert 0 <= df["hv_pctile_252"].iloc[-1] <= 100
    # incremental: nothing new to fetch
    assert daily.update_underlying_daily(mock_ctx, ["STK01"], through=yday) == 0


def test_level_refresh_and_manual_override(mock_ctx):
    market.ensure_instruments(mock_ctx)
    d = SIM_NOW.date() - timedelta(days=1)
    daily.update_underlying_daily(mock_ctx, ["STK01", "STK02"], through=d)
    counts = refresh_levels(mock_ctx, ["STK01", "STK02"], d)
    assert counts["STK01"] > 3
    lv = load_levels(mock_ctx.engine, "STK01", SIM_NOW.date())
    assert {x.source for x in lv} == {"auto"} and {x.kind for x in lv} == {"support", "resistance"}
    mock_ctx.cfg.resolve(mock_ctx.cfg.paths.levels_manual).write_text("symbol,price,kind\nSTK02,123.4,support\n")
    refresh_levels(mock_ctx, ["STK02"], d)
    lv2 = load_levels(mock_ctx.engine, "STK02", d)
    assert [(x.price, x.source, x.score) for x in lv2] == [(123.4, "manual", 10.0)]


# ----------------------------------------------------------------------------- events / F2

def _expiry():
    return date(2026, 12, 29)


def test_f2_fails_closed_without_any_fetch(mock_ctx):
    chk = events.check_f2(mock_ctx, "STK01", _expiry())
    assert not chk.passed and "stale" in chk.reason


def test_f2_fails_closed_when_fetch_errors(mock_ctx):
    mock_ctx.nse.fail = True
    events.fetch_events(mock_ctx)
    assert events.last_good_fetch(mock_ctx.engine) is None
    assert "stale" in events.check_f2(mock_ctx, "STK01", _expiry()).reason


def test_f2_staleness_window(mock_ctx):
    events.fetch_events(mock_ctx)
    assert events.is_fresh(mock_ctx.engine, SIM_NOW, 2)
    assert not events.is_fresh(mock_ctx.engine, SIM_NOW + timedelta(days=3), 2)


def test_f2_event_rules_on_mock(mock_ctx):
    events.fetch_events(mock_ctx)
    m = mock_ctx.broker.market
    results = {s.name: [d for d in s.results] for s in m.specs if not s.is_index}
    outcomes = {}
    for sym in results:
        outcomes[sym] = events.check_f2(mock_ctx, sym, _expiry())
    # A stock whose announced results fall before expiry fails with that event.
    for sym, chk in outcomes.items():
        upcoming = [d for d in results[sym] if SIM_NOW.date() <= d <= _expiry() + timedelta(days=1)
                    and m.days[max(0, m.day_index[d] - 7)] <= SIM_NOW.date()]
        if upcoming:
            assert not chk.passed and "event before expiry" in chk.reason
    assert any(c.passed for c in outcomes.values()) or any("likely" in c.reason for c in outcomes.values())


def test_f2_missing_history_fails_closed(mock_ctx):
    events.fetch_events(mock_ctx)
    chk = events.check_f2(mock_ctx, "UNLISTED", _expiry())
    assert not chk.passed and "missing" in chk.reason


def test_override_csv_event_blocks_trade(mock_ctx):
    events.fetch_events(mock_ctx)
    sym = "STK01"
    path = mock_ctx.cfg.resolve(mock_ctx.cfg.paths.events_override)
    path.write_text(f"symbol,event_date,event_type,description\n{sym},2026-11-20,agm,manual add\n")
    events.load_overrides(mock_ctx)
    chk = events.check_f2(mock_ctx, sym, _expiry())
    assert not chk.passed and "2026-11-20 agm" in chk.events
    assert "2026-11-20 agm" in events.new_events_for_position(mock_ctx, sym, _expiry())
    path.write_text("symbol,event_date,event_type\n")
    events.load_overrides(mock_ctx)
    assert "2026-11-20 agm" not in events.new_events_for_position(mock_ctx, sym, _expiry())


def test_estimated_results_rule(mock_ctx):
    """Unannounced results estimated to land inside the trade window fail F2."""
    events.fetch_events(mock_ctx)
    with mock_ctx.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM events WHERE symbol='STK01'")
        last = SIM_NOW.date() - timedelta(days=80)          # next results ~ 11 days out
        conn.exec_driver_sql("INSERT INTO events (symbol, event_date, event_type, source, fetched_at) "
                             "VALUES ('STK01', ?, 'results', 'nse', ?)", (last.isoformat(), str(SIM_NOW)))
    chk = events.check_f2(mock_ctx, "STK01", _expiry())
    assert not chk.passed and "likely" in chk.reason
    with mock_ctx.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE events SET event_date=? WHERE symbol='STK01'",
                             ((SIM_NOW.date() - timedelta(days=20)).isoformat(),))
    assert events.check_f2(mock_ctx, "STK01", _expiry()).passed
