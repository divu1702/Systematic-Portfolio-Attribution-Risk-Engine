"""Milestone 9: the combined schedule (both systems on one Kite session, entries gated by go_live_date),
the 08:45 login link and events reminder, the 15:20 report and Telegram summary, the 18:30 job, the
monthly comparison of discretionary and strict-book trades, and the command line."""

import io
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.clock import SimClock
from app.context import build_context
from app.data.calendar import TradingCalendar
from app.jobs import catch_up_daily, morning_refresh
from level_system import comparison, report
from platform_core import cli, runtime, scheduler as S
from platform_core.broker import SpreadOrder
from tests.conftest import shared_market

RUN = datetime(2026, 11, 6, 15, 20)


class Capture:
    def __init__(self):
        self.sent = []

    def send(self, text, kind="info"):
        self.sent.append((kind, text))


@pytest.fixture
def plat(cfg, platform_cfg, tmp_path):
    from platform_core.config import LiquidityCfg

    note = Capture()
    ctx = build_context(cfg, mock=True, clock=SimClock(RUN), db_path=tmp_path / "m.db", env={}, market=shared_market(),
                        notifier=note)
    morning_refresh(ctx)
    catch_up_daily(ctx, TradingCalendar.load(ctx.engine).prev_trading_day(ctx.clock.today()))
    pcfg = platform_cfg.model_copy(update={"go_live_date": date(2026, 11, 2),
                                           "liquidity": LiquidityCfg(min_oi_value_inr=1, max_spread_pct=0.30)})
    p = runtime.build(pcfg, ctx=ctx)
    p.sent = note.sent
    return p


def test_job_specs_cover_both_systems(plat):
    specs = {name: (fields, needs) for name, fields, _, needs in S.job_specs(plat)}
    for name in ("oi_instruments", "oi_gap_check", "oi_monitor", "oi_scan", "oi_eod", "oi_archive"):
        assert name in specs
    assert "oi_login_reminder" not in specs                              # replaced by the 08:45 link
    assert specs["login_link"][0] == {"hour": 8, "minute": 45} and specs["login_link"][1] is False
    assert specs["events_reminder"][0] == {"hour": 8, "minute": 45}
    assert specs["gap_check"][0] == {"hour": 9, "minute": 30} and specs["gap_check"][1] is True
    assert specs["exit_checks"][0] == {"hour": "10-15", "minute": "0,30"}
    assert specs["level_close_run"][0] == {"hour": 15, "minute": 20}
    assert specs["official_recheck"][0] == {"hour": 18, "minute": 30}
    sched = S.build_scheduler(plat)
    assert {j.id for j in sched.get_jobs()} == set(specs)


def test_the_earlier_systems_entries_wait_for_go_live(plat, platform_cfg, monkeypatch):
    seen = []
    import app.jobs

    monkeypatch.setattr(app.jobs, "scan_job", lambda ctx, enter=False: seen.append(enter))
    dict((n, f) for n, _, f, _ in S.job_specs(plat))["oi_scan"]()
    plat.cfg = platform_cfg                                               # go_live_date not set
    dict((n, f) for n, _, f, _ in S.job_specs(plat))["oi_scan"]()
    assert seen == [True, False]


def test_login_link_and_events_reminder(plat, monkeypatch):
    assert S.login_link(plat) is False                                    # the mock session is valid
    monkeypatch.setattr(plat.ctx.broker, "session_valid", lambda: False)
    assert S.login_link(plat) is True and "Kite login" in plat.sent[-1][1] and plat.sent[-1][0] == "login"
    assert S.events_reminder(plat) is True and "No events file" in plat.sent[-1][1]
    folder = plat.ds.events_import_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "board_meetings.csv").write_text("SYMBOL,BOARD MEETING DATE\nSTK01,20-Nov-2026\n")
    assert S.events_reminder(plat) is False                               # fresh file: no reminder
    import os
    import time

    old = time.time() - 1.5 * 86400
    os.utime(folder / "board_meetings.csv", (old, old))
    assert S.events_reminder(plat) is True and "1.5 days old" in plat.sent[-1][1]


def test_close_run_writes_the_report_and_telegram_summary(plat):
    rep = S.close_run(plat)
    root = plat.cfg.resolve(plat.cfg.paths.reports)
    page = (root / "daily" / f"screener_{RUN.date()}.html").read_text()
    for section in ("Confirmed, ready to trade", "Armed, in the confirmation window", "Top 20 by ROI"):
        assert section in page
    assert list((root / "runs").glob("*_close.json"))
    text = next(t for k, t in plat.sent if t.startswith("Screener"))
    assert "Top" in text and "by ROI" in text and len(text) <= 4000
    assert rep.screener and rep.screener[0]["symbol"] in page


def test_official_job_and_the_monthly_comparison(plat):
    def trade(account, symbol="STK05", offset=(1, 3), source="system", close=True):
        ds = plat.ds
        ch = ds.chain(symbol, date(2026, 11, 24))
        spot = ds.spot_quotes([symbol])[symbol].last
        ks = sorted(k for k, cp in ch.legs if cp == "CE" and k > spot)
        s, lg = ch.leg(ks[offset[0]], "CE"), ch.leg(ks[offset[1]], "CE")
        r = plat.broker.open_spread(SpreadOrder(account, symbol, "bear_call", date(2026, 11, 24), "CE", s.tradingsymbol,
                                                s.strike, lg.tradingsymbol, lg.strike, 1, ch.lot_size, ch.tick_size,
                                                source=source, setup_tag="none" if account == "discretionary" else "1",
                                                level=s.strike, one_r=5_000))
        assert r.ok, r.reason
        if close:
            assert plat.broker.close_spread(r.trade_id, "test").ok
        return r.trade_id

    trade("discretionary", source="discretionary")                        # matched with the strict trade below
    trade("level_strict")
    trade("discretionary", symbol="STK01", source="discretionary")        # only the owner
    trade("level_strict", symbol="STK06")                                 # only the system
    trade("level_research", symbol="STK07")
    data = comparison.build(plat, RUN.date())
    g = data["groups"]
    assert len(data["pairs"]) == 1 and data["pairs"][0][0].symbol == "STK05"
    assert g["only the owner"].trades == 1 and g["only the system (strict)"].trades == 1
    assert g["all owner trades"].trades == 2 and g["research book (separate)"].trades == 1
    assert g["all strict trades"].closed == 2 and g["all strict trades"].worst
    assert "none" in data["by_tag"]
    path = comparison.write(plat, RUN.date())
    html = path.read_text()
    assert "Matched pairs" in html and "STK05" in html and path.name == "comparison_2026-11.html"
    fr = comparison.filter_review(plat)
    assert fr["strict_closed"] == 2 and fr["filters"]["volume"]["passed"] == 3 and not fr["filters"]["volume"]["ready"]
    assert "Quality filters" in html and "no rule changes before 30" in html
    plat.ctx.clock.set(datetime(2026, 11, 30, 18, 30))                    # the month's last session
    rep = S.official(plat)
    assert rep.phase == "official"
    assert any("Monthly comparison" in t for _, t in plat.sent)


def test_cli_set_pin_doctor_and_status(tmp_path, platform_cfg, capsys):
    env = tmp_path / ".env"
    env.write_text("KITE_API_KEY=x\nKITE_API_SECRET=y\n")
    assert cli.main(["set-pin", "--env", str(env), "--pin", "2468"]) == 0
    text = env.read_text()
    assert "CONTROL_PIN_HASH=pbkdf2_sha256$" in text and "2468" not in text
    secret = next(line for line in text.splitlines() if line.startswith("CONTROL_SECRET=")).split("=", 1)[1]
    assert len(secret) == 64
    assert cli.main(["set-pin", "--env", str(env), "--pin", "1357"]) == 0
    assert f"CONTROL_SECRET={secret}" in env.read_text()                 # the cookie secret is kept
    assert cli.main(["set-pin", "--env", str(env), "--pin", "12"]) == 1
    capsys.readouterr()
    cfg_path = platform_cfg.base_dir / "platform.yaml"
    cli.main(["--config", str(cfg_path), "doctor", "--env", str(env)])
    out = capsys.readouterr().out
    assert "CONTROL_PIN_HASH               set" in out and "2468" not in out and "x\n" not in out
    assert "no control-centre PIN" not in out
