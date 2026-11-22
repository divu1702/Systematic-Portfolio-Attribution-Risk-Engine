"""The platform's daily schedule (spec: Schedule). IST, trading days only (NSE's holiday calendar), one job
at a time (the earlier system's job lock), a failing job alerts instead of stopping the scheduler, and
market jobs wait for a valid Kite session.

Shared and level system:
  08:45  Kite login link on Telegram (one daily login serves both systems)
  08:45  events reminder when the newest events file is more than data.events_file_max_age_days old
  09:25  daily margin recompute for every open position (basket margin plus the delivery step-up)
  09:30  gap check: an overnight gap beyond a level exits at once
  every exit_check_every_min minutes, 10:00-15:00: exit checks (profit targets; overdue exits)
  15:20  screener, confirmations, entries and exits; daily report and Telegram summary
  18:30  bhavcopy and the official re-check (close_mismatch), expiry settlement for every account,
         account values, and after the last session of a month the monthly comparison
Data repository (datalake/): 09:14 live minute quotes start (until 15:31), 16:30 option, futures, stock
and index 1-minute candles, 17:45 history backfill (Kite and NSE), 18:40 NSE's daily files and a summary.
Earlier OI-wall system, unchanged: instruments 08:30, events 08:45, gap check 09:20, monitor every
5 minutes, scan 15:00 (entries only once go_live_date is reached), end of day 15:35, archive 16:10,
quote snapshots and the weekly report. Its own 09:00 login reminder is replaced by the 08:45 link.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from app.clock import IST, parse_hhmm

log = logging.getLogger(__name__)


def _hm(x: str) -> Dict[str, int]:
    t = parse_hhmm(x)
    return {"hour": t.hour, "minute": t.minute}


def login_link(p) -> bool:
    ctx = p.ctx
    p.broker.ensure_accounts()                         # opens every account on go_live_date
    if ctx.broker.session_valid():
        return False
    ctx.notifier.send("Good morning. One Kite login for the whole platform (both systems): "
                      f"{ctx.broker.login_url()}  Then send the request_token with /token, or run "
                      "`python -m platform_core login` on the laptop.", "login")
    return True


def events_reminder(p) -> bool:
    """Telegram reminder when the newest events file is older than data.events_file_max_age_days."""
    age = p.ds.newest_events_file_age_days()
    limit = p.cfg.data.events_file_max_age_days
    if age is not None and age <= limit:
        return False
    auto = p.ds.events_fresh()
    what = "No events file has been uploaded yet" if age is None else f"The newest events file is {age:.1f} days old"
    p.notifier.send(
        f"{what}. Upload today's NSE board-meetings export from the phone (control centre, Upload). Event data older "
        f"than {p.cfg.screener.event_staleness_days} days removes every stock from the screener."
        + (" NSE's automated fetch is working today, so the file is a fallback." if auto else
           " NSE's automated fetch is not working, so the file is what keeps the screener running."), "warning")
    return True


def margins(p) -> List[str]:
    return p.broker.recompute_margins(p.today())


def gap_check(p):
    return p.engine.gap_check()


def exit_check(p):
    now = p.now().time()
    if now > parse_hhmm("15:00") or now < parse_hhmm("10:00"):
        return None
    return p.engine.exit_check()


def close_run(p):
    from level_system import report

    rep = p.engine.run_close()
    root = p.cfg.resolve(p.cfg.paths.reports)
    report.save_run(root, rep)
    path = report.write_daily(p, rep)
    p.notifier.send(report.telegram_summary(rep) + f"\nReport: {path.name}", "info")
    for w in rep.warnings:
        p.notifier.send(f"Level system: {w}", "warning")
    return rep


def _official_day(p, d, record: bool):
    from level_system import comparison, report

    from . import oi_bridge
    from .db import get_state, put_state

    rep = p.engine.official_recheck(d)
    rep.warnings += oi_bridge.sync_closed(p.ctx, p.broker)
    if record:
        try:
            p.broker.record_day(d)                   # values after settlement (saved marks if Kite is closed)
        except Exception as exc:
            rep.warnings.append(f"account values not recorded: {exc}")
    report.save_run(p.cfg.resolve(p.cfg.paths.reports), rep)
    for w in rep.warnings:
        p.notifier.send(f"{'18:30' if record else f'Catch-up for {d}'}: {w}", "warning")
    cal = p.ds.calendar()
    if cal.next_trading_day(d).month != d.month:
        path = comparison.write(p, d)
        p.notifier.send(f"Monthly comparison for {d:%B %Y} ready: {path.name}", "info")
    last = get_state(p.pengine, "official_last")
    if not last or last < d.isoformat():
        put_state(p.pengine, "official_last", d.isoformat(), p.now())
    return rep


def official(p):
    return _official_day(p, p.today(), record=True)


def official_catchup(p) -> str:
    """08:50: the official re-check and expiry settlement for trading days the laptop missed at 18:30
    (up to 7). Settlement then uses NSE's official closes of those days."""
    from datetime import date as _date

    from .db import get_state, put_state

    cal, today = p.ds.calendar(), p.today()
    last = get_state(p.pengine, "official_last")
    if not last:
        put_state(p.pengine, "official_last", cal.prev_trading_day(today).isoformat(), p.now())
        return "nothing to catch up"
    days, d = [], cal.next_trading_day(_date.fromisoformat(last))
    while d < today and len(days) < 7:
        days.append(d)
        d = cal.next_trading_day(d)
    for d in days:
        _official_day(p, d, record=False)
    if days:
        p.notifier.send(f"Caught up the official re-check for {', '.join(str(x) for x in days)} "
                        "(the laptop was off at 18:30).", "info")
    return f"caught up {len(days)} day(s)"


# ---------------------------------------------------------------------- the data repository (datalake/)
def lake_quotes_start(p) -> str:
    from datalake.quotes import Recorder

    if getattr(p.ctx, "is_mock", False):
        return "live quotes are not recorded on the synthetic market"
    rec = getattr(p, "recorder", None) or Recorder(p)
    p.recorder = rec
    msg = rec.start()
    if "not started" in msg or "off" in msg:
        p.notifier.send(f"Data repository: {msg}", "warning")
    return msg


def lake_quotes_stop(p) -> str:
    from datalake.quotes import Recorder

    rec = getattr(p, "recorder", None)
    if rec is None:
        if getattr(p.ctx, "is_mock", False):
            return "quote recording was not running"
        return Recorder(p).finish(p.today())          # a restart during the day: still compact the day's files
    msg = rec.stop()
    p.recorder = None
    return msg


def quotes_running(p) -> bool:
    rec = getattr(p, "recorder", None)
    return bool(rec and any(proc.is_alive() for proc in rec.procs.values()))


def lake_candles(p):
    from datalake import candles

    rep = candles.run(p)
    if rep.urgent or rep.missed:
        p.notifier.send(f"Data repository: {rep.text()}", "warning")
    return rep


def lake_backfill(p):
    from datalake import candles, nse

    msgs = []
    if p.cfg.lake.backfill_requests_per_run:
        msgs.append(candles.run_backfill(p).text())
    if p.cfg.lake.nse.backfill_years:
        msgs.append(nse.backfill(p).text())
    return "; ".join(msgs)


def lake_nse(p):
    """NSE's daily files, the OI unit check, compaction of finished partitions, and a short summary."""
    from datalake import candles, nse, status

    rep = nse.run_daily(p)
    n = candles.compact_finished(p)
    text = status.report(p.cfg.resolve(p.cfg.lake.root), p.today(), short=True)
    p.notifier.send(f"{rep.text()}; {n} partitions compacted.\n{text}", "info")
    return rep


def job_specs(p) -> List[Tuple[str, Dict, Callable, bool]]:
    """(name, cron fields, function, needs a Kite session): the earlier system's jobs (its scan gated by
    go_live_date) and the platform's. Pure data, so tests can inspect it."""
    from app import scheduler as old
    from app.jobs import scan_job

    ctx, s, cfg = p.ctx, p.cfg.schedule, p.cfg
    out = []
    for name, fields, fn, needs in old.job_specs(ctx):
        if name == "login_reminder":
            continue
        if name == "scan":
            fn = (lambda: scan_job(ctx, enter=cfg.is_live(ctx.clock.today())))
        out.append((f"oi_{name}", fields, fn, needs))
    every = s.exit_check_every_min
    minutes = ",".join(str(m) for m in range(0, 60, every)) if every < 60 else "0"
    hours = "10-15" if every < 60 else f"10-15/{every // 60}"
    out += [
        ("login_link", _hm(s.login_link), lambda: login_link(p), False),
        ("events_reminder", _hm(s.events_reminder), lambda: events_reminder(p), False),
        ("margins", {"hour": 9, "minute": 25}, lambda: margins(p), True),
        ("gap_check", _hm(s.gap_check), lambda: gap_check(p), True),
        ("exit_checks", {"hour": hours, "minute": minutes}, lambda: exit_check(p), True),
        ("level_close_run", _hm(cfg.screener.run_at), lambda: close_run(p), True),
        ("official_recheck", _hm(s.official_recheck), lambda: official(p), False),
        ("official_catchup", {"hour": 8, "minute": 50}, lambda: official_catchup(p), False),
    ]
    lk = cfg.lake
    if lk.enabled:
        q = lk.quotes
        stop = (datetime.combine(date.today(), parse_hhmm(q.end)) + timedelta(minutes=1)).time()
        out += [
            ("lake_quotes_start", _hm(q.start), lambda: lake_quotes_start(p), True),
            ("lake_quotes_stop", {"hour": stop.hour, "minute": stop.minute}, lambda: lake_quotes_stop(p), False),
            ("lake_candles", {"hour": 16, "minute": 30}, lambda: lake_candles(p), True),
            ("lake_backfill", {"hour": 17, "minute": 45}, lambda: lake_backfill(p), True),
            ("lake_nse", {"hour": 18, "minute": 40}, lambda: lake_nse(p), False),
        ]
    return out


class RepeatingGuard:
    """The earlier system's guard for jobs that repeat through the day; without a Kite session they are
    skipped quietly (the daily runner sends one login reminder)."""

    def __init__(self, ctx, name, fn, needs_session, trading_day_only=True):
        from app.scheduler import Guard

        self.ctx, self.needs_session = ctx, needs_session
        self.guard = Guard(ctx, name, fn, False, trading_day_only)

    def __call__(self):
        if self.needs_session and not self.ctx.broker.session_valid():
            return None
        return self.guard()


def build_scheduler(p, blocking: bool = False, keep_awake: Optional[bool] = None):
    """Repeating jobs on cron; once-a-day jobs through the daily runner (due time, catch-up, deadline),
    ticking every minute at hh:mm:05."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from app.scheduler import _LOCK

    from .daily import DailyRunner, build_jobs
    from .keepawake import KeepAwake

    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone=IST)
    specs = job_specs(p)
    daily = build_jobs(specs)
    names = {j.name for j in daily} | {"lake_quotes_start"}
    for name, fields, fn, needs_session in specs:
        if name in names:
            continue
        fields = {"day_of_week": "mon-fri", **fields}
        sched.add_job(RepeatingGuard(p.ctx, name, fn, needs_session, trading_day_only=name != "oi_weekly_report"),
                      CronTrigger(timezone=IST, **fields), id=name, name=name, max_instances=1, coalesce=True,
                      misfire_grace_time=300)
    q = p.cfg.lake.quotes
    window = (parse_hhmm(q.start), parse_hhmm(q.end)) if p.cfg.lake.enabled and q.enabled else None
    if keep_awake is None:
        keep_awake = not getattr(p.ctx, "is_mock", False)
    runner = DailyRunner(p, daily, _LOCK, awake=KeepAwake() if keep_awake else None, quotes_window=window,
                         start_quotes=lambda: lake_quotes_start(p), quotes_running=lambda: quotes_running(p))
    p.daily = runner
    sched.add_job(runner.tick, CronTrigger(timezone=IST, second=5), id="daily_runner", name="daily_runner",
                  max_instances=1, coalesce=True, misfire_grace_time=55)
    return sched
