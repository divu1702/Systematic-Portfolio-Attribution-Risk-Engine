"""Running without anyone at the keyboard: once-a-day jobs at their due time or as soon as possible after
it (late start, sleep, late Kite login), deadlines, retries, the quote recorder kept running in its
window, keeping the laptop awake on trading days, catching up missed official re-checks, one process at
a time, and the start-with-the-laptop service files for Windows, macOS and Linux."""

import plistlib
import subprocess
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.clock import SimClock
from app.context import build_context
from app.scheduler import _LOCK
from platform_core import runtime, scheduler as S, service
from platform_core.daily import DailyJob, DailyRunner, build_jobs, is_daily
from platform_core.db import get_state, put_state
from platform_core.instance import SingleInstance
from platform_core.keepawake import KeepAwake
from tests.conftest import shared_market

DAY = date(2026, 11, 6)                     # a Friday, a trading day in the mock calendar


class Capture:
    def __init__(self):
        self.sent = []

    def send(self, text, kind="info"):
        self.sent.append((kind, text))


@pytest.fixture
def plat(cfg, platform_cfg, tmp_path):
    note = Capture()
    ctx = build_context(cfg, mock=True, clock=SimClock(datetime.combine(DAY, time(7, 0))), db_path=tmp_path / "m.db",
                        env={}, market=shared_market(), notifier=note)
    p = runtime.build(platform_cfg.model_copy(update={"go_live_date": date(2026, 11, 2)}), ctx=ctx)
    p.sent = note.sent
    return p


def at(p, hh, mm, d=DAY):
    p.ctx.clock.set(datetime.combine(d, time(hh, mm, 5)))


def jobs(calls, fail=()):
    def fn(name):
        def run():
            if name in fail:
                raise RuntimeError(f"{name} broke")
            calls.append(name)
        return run
    return [DailyJob("early", time(8, 30), time(15, 0), False, fn("early")),
            DailyJob("needs_kite", time(9, 30), time(15, 15), True, fn("needs_kite")),
            DailyJob("close", time(15, 20), time(15, 28), True, fn("close")),
            DailyJob("evening", time(18, 30), time(23, 59), False, fn("evening"))]


def test_due_time_one_job_per_tick_and_done_once(plat):
    calls = []
    r = DailyRunner(plat, jobs(calls), _LOCK)
    at(plat, 8, 29)
    assert r.tick() is None and calls == []                       # not due yet
    at(plat, 8, 30)
    assert r.tick() == "early" and calls == ["early"]
    at(plat, 8, 31)
    assert r.tick() is None                                       # done today
    restarted = DailyRunner(plat, jobs(calls), _LOCK)             # a restart the same day does not repeat it
    assert restarted.tick() is None and calls == ["early"]


def test_late_start_catches_up_in_order_and_misses_after_deadlines(plat):
    calls = []
    r = DailyRunner(plat, jobs(calls), _LOCK)
    at(plat, 10, 0)                                               # switched on at 10:00: both morning jobs run late
    assert [r.tick(), r.tick(), r.tick()] == ["early", "needs_kite", None]
    assert get_state(plat.pengine, "daily:early")["late_min"] == 90
    calls.clear()
    at(plat, 15, 21, d=date(2026, 11, 9))                         # next session, switched on at 15:21
    assert [r.tick(), r.tick()] == ["close", None] and calls == ["close"]
    for name in ("early", "needs_kite"):                          # their deadlines (15:00, 15:15) had passed
        assert get_state(plat.pengine, f"daily:{name}")["status"] == "missed"
        assert any(f"{name} missed today" in t for _, t in plat.sent)
    at(plat, 18, 31, d=date(2026, 11, 9))
    assert r.tick() == "evening" and calls == ["close", "evening"]


def test_waits_for_the_kite_login_then_runs(plat, monkeypatch):
    calls = []
    r = DailyRunner(plat, jobs(calls), _LOCK)
    monkeypatch.setattr(plat.ctx.broker, "session_valid", lambda: False)
    at(plat, 9, 31)
    r.tick()                                                      # runs "early" (no session needed)
    assert r.tick() is None and "needs_kite" not in calls
    at(plat, 9, 51)
    r.tick()
    assert any(k == "login" and "Waiting for today's Kite login" in t for k, t in plat.sent)
    n = len(plat.sent)
    r.tick()
    assert len(plat.sent) == n                                    # reminded once a day
    monkeypatch.setattr(plat.ctx.broker, "session_valid", lambda: True)
    assert r.tick() == "needs_kite"


def test_failures_retry_every_five_minutes_until_the_deadline(plat):
    calls = []
    r = DailyRunner(plat, jobs(calls, fail=("early",)), _LOCK)
    at(plat, 8, 30)
    assert r.tick() == "early"
    st = get_state(plat.pengine, "daily:early")
    assert st["status"] == "due" and st["attempts"] == 1 and "early broke" in st["error"]
    assert sum("Job early failed" in t for _, t in plat.sent) == 1
    at(plat, 8, 32)
    assert r.tick() is None                                       # waits out the retry interval
    at(plat, 8, 35)
    assert r.tick() == "early" and get_state(plat.pengine, "daily:early")["attempts"] == 2
    assert sum("Job early failed" in t for _, t in plat.sent) == 1   # told once


def test_nothing_runs_on_holidays_and_keep_awake_follows_the_window(plat):
    held = []
    backend = SimpleNamespace(name="fake", hold=lambda: held.append("on") or True, release=lambda: held.append("off"))
    calls = []
    r = DailyRunner(plat, jobs(calls), _LOCK, awake=KeepAwake(backend))
    at(plat, 8, 20)
    r.tick()
    assert held == []                                             # before 08:35
    at(plat, 8, 40)
    r.tick()
    assert held == ["on"]
    at(plat, 19, 0)
    r.tick()
    assert held == ["on", "off"]
    at(plat, 10, 0, d=date(2026, 11, 7))                          # Saturday
    assert r.tick() is None and held == ["on", "off"]


def test_quote_recorder_is_kept_running_in_its_window(plat, monkeypatch):
    started, running = [], [False]
    r = DailyRunner(plat, [], _LOCK, quotes_window=(time(9, 14), time(15, 31)),
                    start_quotes=lambda: started.append(plat.ctx.clock.now()) or "started",
                    quotes_running=lambda: running[0])
    at(plat, 9, 0)
    r.tick()
    assert started == []
    at(plat, 11, 0)                                               # switched on mid-morning
    r.tick()
    assert len(started) == 1
    at(plat, 11, 2)
    r.tick()
    assert len(started) == 1                                      # retries no more than every 5 minutes
    running[0] = True
    at(plat, 11, 10)
    r.tick()
    assert len(started) == 1
    running[0] = False                                            # a connection process died
    at(plat, 12, 0)
    r.tick()
    assert len(started) == 2
    at(plat, 15, 40)
    r.tick()
    assert len(started) == 2                                      # after the window


def test_scheduler_wiring(plat):
    specs = S.job_specs(plat)
    daily = {j.name for j in build_jobs(specs)}
    assert {"oi_instruments", "oi_scan", "oi_eod", "level_close_run", "official_recheck", "official_catchup",
            "lake_candles", "lake_nse", "lake_quotes_stop", "login_link", "gap_check"} <= daily
    assert "lake_quotes_start" not in daily and "oi_monitor" not in daily and "oi_weekly_report" not in daily
    sched = S.build_scheduler(plat, keep_awake=False)
    ids = {j.id for j in sched.get_jobs()}
    assert "daily_runner" in ids and not (ids & daily)
    assert {"oi_monitor", "exit_checks", "oi_weekly_report"} <= ids
    assert plat.daily is not None and "job(s) due now" in plat.daily.summary()
    assert is_daily({"hour": 9, "minute": 30}) and not is_daily({"hour": "9-15", "minute": "*/5"})


def test_official_recheck_catches_up_missed_days(plat, monkeypatch):
    seen = []
    monkeypatch.setattr(plat.engine, "official_recheck",
                        lambda d=None: seen.append(d) or SimpleNamespace(warnings=[], mismatches=[], as_dict=lambda: {},
                                                                        ts=datetime(2026, 11, 6, 18, 30), phase="official"))
    monkeypatch.setattr("level_system.report.save_run", lambda root, rep: None)
    at(plat, 8, 50)
    assert S.official_catchup(plat) == "nothing to catch up"      # first run: starts the record
    put_state(plat.pengine, "official_last", "2026-11-03", plat.now())
    assert S.official_catchup(plat) == "caught up 2 day(s)"
    assert seen == [date(2026, 11, 4), date(2026, 11, 5)]
    assert get_state(plat.pengine, "official_last") == "2026-11-05"


def test_only_one_platform_process(tmp_path):
    a, b = SingleInstance(tmp_path / "p.lock"), SingleInstance(tmp_path / "p.lock")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


# ----------------------------------------------------------------------------- service files
def recorder():
    calls = []

    def run(cmd):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return calls, run


def test_linux_service(tmp_path):
    calls, run = recorder()
    svc = service.Service(tmp_path / "platform.yaml", project=tmp_path, python="/venv/bin/python", platform="linux",
                          run=run, home=tmp_path / "home")
    out = svc.install()
    unit = svc.unit_path.read_text()
    assert "Restart=always" in unit and f"WorkingDirectory={tmp_path}" in unit
    assert "ExecStart=/venv/bin/python -m platform_core --config" in unit and unit.rstrip().endswith("default.target")
    assert ["systemctl", "--user", "enable", "--now", "credit-spreads.service"] in calls
    assert any(c[:2] == ["loginctl", "enable-linger"] for c in calls) and all(l.startswith(("ok", "wrote")) for l in out)
    svc.stop()
    assert calls[-1] == ["systemctl", "--user", "stop", "credit-spreads.service"]


def test_macos_service(tmp_path):
    calls, run = recorder()
    svc = service.Service(tmp_path / "platform.yaml", project=tmp_path, python="/venv/bin/python", platform="darwin",
                          run=run, home=tmp_path / "home")
    svc.install()
    pl = plistlib.loads(svc.plist_path.read_bytes())
    assert pl["RunAtLoad"] and pl["KeepAlive"] and pl["ProgramArguments"][:3] == ["/venv/bin/python", "-m", "platform_core"]
    assert pl["ProgramArguments"][-1] == "run" and pl["WorkingDirectory"] == str(tmp_path)
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)


def test_windows_service(tmp_path, monkeypatch):
    calls, run = recorder()
    monkeypatch.setenv("USERDOMAIN", "LAPTOP")
    monkeypatch.setenv("USERNAME", "owner")
    svc = service.Service(tmp_path / "platform.yaml", project=tmp_path, python=r"D:\option selling\.venv\Scripts\python.exe",
                          platform="win32", run=run)
    svc.install()
    xml = svc.xml_path.read_text(encoding="utf-16")
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.find("t:Principals/t:Principal/t:UserId", ns).text == "LAPTOP\\owner"
    assert root.find("t:Triggers/t:LogonTrigger", ns) is None                 # time triggers only: no admin rights
    rep = root.find("t:Triggers/t:CalendarTrigger/t:Repetition", ns)
    assert rep.find("t:Interval", ns).text == "PT5M" and rep.find("t:Duration", ns).text == "P1D"
    assert root.find("t:Actions/t:Exec/t:Command", ns).text == '"D:\\option selling\\.venv\\Scripts\\python.exe"'
    st = root.find("t:Settings", ns)
    assert st.find("t:ExecutionTimeLimit", ns).text == "PT0S" and st.find("t:DisallowStartIfOnBatteries", ns).text == "false"
    assert st.find("t:RestartOnFailure/t:Count", ns).text == "999" and st.find("t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    ex = root.find("t:Actions/t:Exec", ns)
    assert ex.find("t:Arguments", ns).text.endswith(" run") and ex.find("t:WorkingDirectory", ns).text == str(tmp_path)
    assert calls[0][:3] == ["schtasks", "/Create", "/TN"] and calls[1] == ["schtasks", "/Run", "/TN", "CreditSpreadsPlatform"]
    svc.stop()
    assert ["schtasks", "/Change", "/TN", "CreditSpreadsPlatform", "/DISABLE"] in calls
    svc.start()
    assert calls[-2:] == [["schtasks", "/Change", "/TN", "CreditSpreadsPlatform", "/ENABLE"],
                          ["schtasks", "/Run", "/TN", "CreditSpreadsPlatform"]]


def test_concurrent_ticks_never_run_a_job_twice(plat):
    import threading as th

    calls, gate = [], th.Event()

    def slow():
        calls.append("slow")
        gate.wait(5)

    r = DailyRunner(plat, [DailyJob("slow", time(8, 30), time(15, 0), False, slow)], _LOCK)
    at(plat, 8, 31)
    t = th.Thread(target=r.tick)
    t.start()
    while not calls:
        pass
    assert r.tick() is None                                        # a second tick while the first is busy
    gate.set()
    t.join()
    assert calls == ["slow"] and r.tick() is None
