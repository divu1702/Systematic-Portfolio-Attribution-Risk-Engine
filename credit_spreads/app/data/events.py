"""Corporate events: fetch from NSE, merge events_override.csv, and answer F2 fail-closed.

F2 fails a stock when:
- the last successful NSE fetch is older than `events.max_staleness_days` (stale), or
- there is no results-type event for the stock at all within the look-back window (missing:
  every listed company reports quarterly, so an empty history means the data is incomplete), or
- any event falls between today and expiry + `events.buffer_days`, or
- (optional) the next results date has not been announced yet but, estimated from the last
  results date + `results_cycle_days`, lands within `results_tolerance_days` of that window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from . import csvs
from .models import Event, EventFetch

log = logging.getLogger(__name__)

RESULTS_TYPES = ("results", "board_meeting")


def fetch_events(ctx) -> int:
    """Pull NSE intimations for the configured window and record the attempt. Returns rows seen."""
    ec = ctx.cfg.events
    now = ctx.clock.now()
    today = now.date()
    start, end = today - timedelta(days=ec.fetch_behind_days), today + timedelta(days=ec.fetch_ahead_days)
    try:
        rows = ctx.nse.events(start, end)
    except Exception as exc:
        log.warning("NSE event fetch failed: %s", exc)
        with ctx.engine.begin() as conn:
            conn.execute(insert(EventFetch).values(fetched_at=now, source="nse", ok=False, rows=0,
                                                   error=str(exc)[:500]))
        load_overrides(ctx)
        return 0
    _store(ctx.engine, rows, now)
    with ctx.engine.begin() as conn:
        conn.execute(insert(EventFetch).values(fetched_at=now, source="nse", ok=True, rows=len(rows)))
    load_overrides(ctx)
    return len(rows)


def load_overrides(ctx) -> int:
    rows = csvs.override_events(ctx.cfg.resolve(ctx.cfg.paths.events_override))
    with ctx.engine.begin() as conn:
        conn.execute(delete(Event).where(Event.source == "manual"))
    _store(ctx.engine, rows, ctx.clock.now())
    return len(rows)


def _store(engine: Engine, rows: List[dict], fetched_at: datetime) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        for r in rows:
            conn.execute(insert(Event).values(
                symbol=r["symbol"], event_date=r["event_date"], event_type=r["event_type"],
                description=r.get("description"), source=r["source"], fetched_at=fetched_at
            ).on_conflict_do_nothing())


def last_good_fetch(engine: Engine) -> Optional[datetime]:
    with engine.connect() as conn:
        return conn.execute(select(func.max(EventFetch.fetched_at)).where(EventFetch.ok.is_(True))).scalar()


def is_fresh(engine: Engine, now: datetime, max_staleness_days: int) -> bool:
    last = last_good_fetch(engine)
    return last is not None and (now - last) <= timedelta(days=max_staleness_days)


def events_between(engine: Engine, symbol: str, start: date, end: date) -> List[Event]:
    with Session(engine) as session:
        return list(session.scalars(select(Event).where(
            Event.symbol == symbol, Event.event_date >= start, Event.event_date <= end
        ).order_by(Event.event_date)))


def last_results_date(engine: Engine, symbol: str, before: date) -> Optional[date]:
    with engine.connect() as conn:
        return conn.execute(select(func.max(Event.event_date)).where(
            Event.symbol == symbol, Event.event_date < before, Event.event_type.in_(RESULTS_TYPES))).scalar()


@dataclass
class EventCheck:
    passed: bool
    reason: str = ""
    events: List[str] = field(default_factory=list)


def check_f2(ctx, symbol: str, expiry: date) -> EventCheck:
    ec = ctx.cfg.events
    now = ctx.clock.now()
    today = now.date()
    if not is_fresh(ctx.engine, now, ec.max_staleness_days):
        last = last_good_fetch(ctx.engine)
        return EventCheck(False, f"event data stale (last good fetch {last or 'never'})")
    window_end = expiry + timedelta(days=ec.buffer_days)
    upcoming = events_between(ctx.engine, symbol, today, window_end)
    if upcoming:
        desc = [f"{e.event_date} {e.event_type}" for e in upcoming]
        return EventCheck(False, f"event before expiry: {desc[0]}", desc)
    behind = today - timedelta(days=ec.fetch_behind_days)
    history = events_between(ctx.engine, symbol, behind, today)
    if not any(e.event_type in RESULTS_TYPES for e in history):
        return EventCheck(False, "event data missing (no results date in look-back window)")
    if ec.estimate_results:
        last = last_results_date(ctx.engine, symbol, today)
        if last:
            est = last + timedelta(days=ec.results_cycle_days)
            tol = timedelta(days=ec.results_tolerance_days)
            if today - tol <= est <= window_end + tol:
                return EventCheck(False, f"results likely near {est} (last results {last}, not yet announced)")
    return EventCheck(True)


def new_events_for_position(ctx, symbol: str, expiry: date) -> List[str]:
    """Exit rule 1: any event now known between today and expiry + buffer."""
    end = expiry + timedelta(days=ctx.cfg.events.buffer_days)
    return [f"{e.event_date} {e.event_type}" for e in events_between(ctx.engine, symbol, ctx.clock.today(), end)]
