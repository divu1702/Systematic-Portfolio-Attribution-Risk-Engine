"""Once-a-day jobs at their due time, or as soon as possible after it.

A laptop is not a server: it can be switched on at 10:30, sleep through 15:20, or wait for the
morning Kite login until 09:40. A plain schedule skips whatever it missed. Here every once-a-day job
has a due time and a deadline; a tick every minute (at hh:mm:05) runs the most urgent job that is
due, not yet done today, still before its deadline and, if it needs one, has a Kite session. A job
past its deadline is recorded as missed and the owner is told once. A job that fails is retried
every five minutes until its deadline. Progress lives in the platform database, so a restart never
repeats a job that has run. The same tick keeps the live-quote recorder running during its window
and keeps the laptop awake on trading days (`service` in platform.yaml).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional

from app.clock import parse_hhmm

from .db import get_state, put_state

log = logging.getLogger(__name__)

# Latest time (IST) a once-a-day job may still start the same day. After it, the job is missed.
DEADLINES: Dict[str, str] = {
    "oi_instruments": "15:30", "oi_events": "15:00", "oi_gap_check": "15:00", "oi_scan": "15:25",
    "oi_eod": "23:30", "oi_archive": "23:30",
    "login_link": "15:20", "events_reminder": "15:20", "margins": "15:00", "gap_check": "15:15",
    "level_close_run": "15:28", "official_catchup": "23:59", "official_recheck": "23:59",
    "lake_quotes_stop": "23:59", "lake_candles": "23:30", "lake_backfill": "23:30", "lake_nse": "23:59",
}
RETRY = timedelta(minutes=5)


@dataclass
class DailyJob:
    name: str
    due: time
    deadline: time
    needs_session: bool
    fn: Callable[[], object]


def is_daily(fields: dict) -> bool:
    """A cron spec that fires once a day at a fixed time (not a repeating or weekly job)."""
    return isinstance(fields.get("hour"), int) and isinstance(fields.get("minute"), int) and "day_of_week" not in fields


class DailyRunner:
    def __init__(self, p, jobs: List[DailyJob], lock, awake=None, quotes_window: Optional[tuple] = None,
                 start_quotes: Optional[Callable[[], str]] = None, quotes_running: Optional[Callable[[], bool]] = None):
        self.p, self.jobs, self.lock, self.awake = p, jobs, lock, awake
        self.quotes_window, self.start_quotes, self.quotes_running = quotes_window, start_quotes, quotes_running
        self._quotes_next: Optional[datetime] = None
        self._ticking = threading.Lock()                 # one tick at a time (the startup tick and the minute tick)
        self.started = p.ds.now()

    # ------------------------------------------------------------------ state
    def _key(self, name: str) -> str:
        return f"daily:{name}"

    def state(self, name: str, today: date) -> dict:
        st = get_state(self.p.pengine, self._key(name)) or {}
        return st if st.get("date") == today.isoformat() else {"date": today.isoformat(), "status": "due", "attempts": 0}

    def put(self, name: str, st: dict) -> None:
        put_state(self.p.pengine, self._key(name), st, self.p.ds.now())

    def _alert(self, text: str, kind: str = "warning") -> None:
        try:
            self.p.ctx.notifier.send(text, kind)
        except Exception:
            log.warning("alert failed: %s", text)

    # ------------------------------------------------------------------ the tick
    def tick(self) -> Optional[str]:
        """One pass; returns the name of the job it ran, if any."""
        if not self._ticking.acquire(blocking=False):
            return None
        try:
            return self._tick()
        finally:
            self._ticking.release()

    def _tick(self) -> Optional[str]:
        now = self.p.ds.now()
        today = now.date()
        trading = self.p.ds.calendar().is_trading_day(today)
        sc = self.p.cfg.service
        if self.awake is not None:
            self.awake.want(trading and parse_hhmm(sc.awake_from) <= now.time() <= parse_hhmm(sc.awake_until))
        if not trading:
            return None
        self._ensure_quotes(now)
        session = None
        waiting = []
        for job in sorted(self.jobs, key=lambda j: (j.deadline, j.due)):
            st = self.state(job.name, today)
            if st["status"] in ("done", "missed"):
                continue
            if now.time() < job.due:
                continue
            if now.time() > job.deadline or (not self.p.cfg.service.catch_up and
                                             now > datetime.combine(today, job.due) + timedelta(minutes=5)):
                st["status"] = "missed"
                self.put(job.name, st)
                self._alert(f"{job.name} missed today: the laptop was off, asleep or not logged in to Kite between "
                            f"{job.due:%H:%M} and {job.deadline:%H:%M}.")
                continue
            if st.get("next") and now < datetime.fromisoformat(st["next"]):
                continue
            if job.needs_session:
                if session is None:
                    session = bool(self.p.ctx.broker.session_valid())
                if not session:
                    waiting.append(job.name)
                    continue
            if not self.lock.acquire(timeout=50):
                return None                              # another job is running: next tick
            try:
                job.fn()
                st.update(status="done", finished=self.p.ds.now().isoformat(timespec="seconds"),
                          late_min=int((now - datetime.combine(today, job.due)).total_seconds() // 60))
            except Exception as exc:
                log.exception("daily job %s failed", job.name)
                st["attempts"] = st.get("attempts", 0) + 1
                st["next"] = (now + RETRY).isoformat(timespec="seconds")
                st["error"] = str(exc)[:300]
                if st["attempts"] == 1:
                    self._alert(f"Job {job.name} failed: {exc}. Retrying every 5 minutes until "
                                f"{job.deadline:%H:%M}.", "error")
            finally:
                self.lock.release()
            self.put(job.name, st)
            return job.name
        if waiting:
            self._login_wait(now, waiting)
        return None

    def _login_wait(self, now: datetime, waiting: List[str]) -> None:
        """One reminder a day when jobs have waited 20 minutes for the Kite login."""
        key = "daily:login_wait"
        st = get_state(self.p.pengine, key) or {}
        if st.get("date") == now.date().isoformat():
            return
        due = min(j.due for j in self.jobs if j.name in waiting)
        if now < datetime.combine(now.date(), due) + timedelta(minutes=20):
            return
        put_state(self.p.pengine, key, {"date": now.date().isoformat()}, now)
        broker = self.p.ctx.broker
        self._alert(f"Waiting for today's Kite login: {', '.join(sorted(waiting))} will run once you log in "
                    f"({broker.login_url()}, then /token).", "login")

    def _ensure_quotes(self, now: datetime) -> None:
        """Keep the live-quote recorder running inside its window (after a late start, a late login or a crash)."""
        if not (self.quotes_window and self.start_quotes and self.quotes_running):
            return
        start, end = self.quotes_window
        if not (start <= now.time() < end) or self.quotes_running():
            return
        if self._quotes_next and now < self._quotes_next:
            return
        self._quotes_next = now + RETRY
        if not self.p.ctx.broker.session_valid():
            return
        try:
            msg = self.start_quotes()
            log.info("quotes: %s", msg)
        except Exception as exc:
            log.warning("quote recorder did not start: %s", exc)

    # ------------------------------------------------------------------ reporting
    def summary(self) -> str:
        now = self.p.ds.now()
        today = now.date()
        if not self.p.ds.calendar().is_trading_day(today):
            return "not a trading day"
        due = [j.name for j in self.jobs if j.due <= now.time() <= j.deadline and
               self.state(j.name, today)["status"] not in ("done", "missed")]
        later = [j for j in self.jobs if j.due > now.time()]
        nxt = min(later, key=lambda j: j.due) if later else None
        return (f"{len(due)} job(s) due now" + (f" ({', '.join(due)})" if due else "")
                + (f"; next {nxt.name} at {nxt.due:%H:%M}" if nxt else ""))


def build_jobs(specs, deadlines: Dict[str, str] = DEADLINES) -> List[DailyJob]:
    out = []
    for name, fields, fn, needs in specs:
        if not is_daily(fields) or name == "lake_quotes_start":
            continue
        due = time(fields["hour"], fields["minute"])
        dl = parse_hhmm(deadlines.get(name, "23:59"))
        out.append(DailyJob(name, due, max(dl, due), needs, fn))
    return out
