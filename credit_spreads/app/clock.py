"""IST time helpers. All timestamps in the database are naive IST (India has no DST)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime:
    """Current time in IST, as a naive datetime (the database convention)."""
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    return now_ist().date()
