"""IST time helpers and an injectable clock.

All timestamps in the database are naive IST (India has no DST). Code that needs "now"
takes a Clock, so the same logic runs against the real clock in production and a
simulated clock in tests and the end-to-end simulation.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    """Current time in IST, as a naive datetime (the database convention)."""
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    return now_ist().date()


def to_naive_ist(ts: datetime) -> datetime:
    """Convert an aware datetime to naive IST; naive input is assumed to be IST already."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(IST).replace(tzinfo=None)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError

    def today(self) -> date:
        return self.now().date()


class SystemClock(Clock):
    def now(self) -> datetime:
        return now_ist()


class SimClock(Clock):
    """A clock that only moves when told to."""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        if ts < self._now:
            raise ValueError(f"SimClock cannot go backwards: {ts} < {self._now}")
        self._now = ts

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)
