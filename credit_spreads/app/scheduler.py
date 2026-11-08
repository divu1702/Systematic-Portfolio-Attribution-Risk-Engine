"""APScheduler wiring (spec milestone 6): IST, weekdays, NSE holidays honoured.

08:30 instrument master, holidays, sectors   08:45 events
09:00 login reminder (if no valid Kite session)   09:20 gap check
every 5 min 09:25-15:30 monitor (15:15 = thesis check)   15:00 scan + entries
15:35 end of day: candles, levels, turnover, events, equity   weekly report (milestone 7)

Jobs never overlap (one lock), a job that raises sends an alert instead of killing the
scheduler, and market jobs are skipped without a valid broker session.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time
from typing import Callable, Dict, List, Optional, Tuple

from .clock import IST, MARKET_CLOSE, parse_hhmm
from .data import events
from .data.calendar import TradingCalendar
from .jobs import eod_job, monitor_job, morning_refresh, scan_job

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


class Guard:
    """Wraps a job: trading-day check, session check, single-flight lock, error alerts."""

    def __init__(self, ctx, name: str, fn: Callable, needs_session: bool = True, trading_day_only: bool = True):
        self.ctx, self.name, self.fn = ctx, name, fn
        self.needs_session, self.trading_day_only = needs_session, trading_day_only

    def __call__(self):
        ctx = self.ctx
        today = ctx.clock.today()
        if self.trading_day_only and not TradingCalendar.load(ctx.engine).is_trading_day(today):
            log.info("%s skipped: %s is not a trading day", self.name, today)
            return None
        if self.needs_session and not ctx.broker.session_valid():
            key = f"nosession_{today}_{self.name}"
            if not ctx.cache.get(key):
                ctx.cache[key] = True
                ctx.notifier.send(f"{self.name} skipped: no valid Kite session today. Log in: "
                                  f"{ctx.broker.login_url()}", "login")
            return None
        if not _LOCK.acquire(timeout=240):
            log.warning("%s skipped: another job is still running", self.name)
            return None
        try:
            return self.fn()
        except Exception as exc:  # keep the scheduler alive, tell the owner
            log.exception("job %s failed", self.name)
            ctx.notifier.send(f"Job {self.name} failed: {exc}", "error")
            return None
        finally:
            _LOCK.release()


def login_reminder(ctx) -> None:
    if not ctx.broker.session_valid():
        ctx.notifier.send("Good morning. Kite login needed before trading: "
                          f"{ctx.broker.login_url()} (then send the request_token with /token, or run "
                          "`python -m app login`)", "login")


def monitor_slot(ctx) -> Optional[List[str]]:
    """The 5-minute monitor; the 09:20 slot is the gap check and 15:15 the thesis check."""
    s = ctx.cfg.schedule
    t = ctx.clock.now().time().replace(second=0, microsecond=0)
    if t <= parse_hhmm(s.gap_check) or t > MARKET_CLOSE:
        return None
    phase = "thesis" if t == parse_hhmm(s.thesis_exit) else "regular"
    return monitor_job(ctx, phase)


def job_specs(ctx) -> List[Tuple[str, Dict, Callable, bool]]:
    """(name, cron fields, function, needs_session). Pure data, so tests can inspect it."""
    s = ctx.cfg.schedule
    def hm(x):
        t = parse_hhmm(x)
        return {"hour": t.hour, "minute": t.minute}
    every = s.monitor_every_min
    return [
        ("instruments", hm(s.instruments_refresh), lambda: morning_refresh(ctx), False),
        ("events", hm(s.events_fetch), lambda: events.fetch_events(ctx), False),
        ("login_reminder", hm(s.login_reminder), lambda: login_reminder(ctx), False),
        ("gap_check", hm(s.gap_check), lambda: monitor_job(ctx, "gap"), True),
        ("monitor", {"hour": "9-15", "minute": f"*/{every}"}, lambda: monitor_slot(ctx), True),
        ("scan", hm(s.scan), lambda: scan_job(ctx, enter=True), True),
        ("eod", hm(s.eod), lambda: eod_job(ctx), True),
        ("weekly_report", {**hm(ctx.cfg.reporting.weekly_time), "day_of_week": ctx.cfg.reporting.weekly_day},
         lambda: weekly_report_job(ctx), False),
        ("archive", hm(s.archive), lambda: archive_job(ctx), True),
    ]


def archive_job(ctx):
    """Nightly Kite history archiving within the request budget (see app/data/history_archiver.py)."""
    from .data.history_archiver import run_archive

    rep = run_archive(ctx)
    if rep.expiring or (rep.requests and not rep.pending):
        ctx.notifier.send(rep.text(), "info")
    return rep


def weekly_report_job(ctx):
    from .reporting.weekly import write_report

    path = write_report(ctx)
    ctx.notifier.send(f"Weekly report ready: {path}", "info")
    return path


def build_scheduler(ctx, blocking: bool = True, extra: Optional[List] = None):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone=IST)
    for name, fields, fn, needs_session in job_specs(ctx) + (extra or []):
        fields = {"day_of_week": "mon-fri", **fields}
        sched.add_job(Guard(ctx, name, fn, needs_session, trading_day_only=name != "weekly_report"),
                      CronTrigger(timezone=IST, **fields),
                      id=name, name=name, max_instances=1, coalesce=True, misfire_grace_time=120)
    return sched
