"""NSE trading calendar: weekdays minus exchange holidays (from NSE plus holidays_manual.csv)."""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import FrozenSet, List

from sqlalchemy import Engine, select

from .models import Holiday


def holiday_set(engine: Engine) -> FrozenSet[date]:
    with engine.connect() as conn:
        return frozenset(r[0] for r in conn.execute(select(Holiday.date)))


class TradingCalendar:
    def __init__(self, holidays: FrozenSet[date]):
        self.holidays = holidays

    @classmethod
    def load(cls, engine: Engine) -> "TradingCalendar":
        return cls(holiday_set(engine))

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def prev_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days in (start, end]: e.g. sessions left from today until expiry day."""
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_trading_day(d):
                n += 1
        return n

    def last_n(self, end: date, n: int) -> List[date]:
        """The n trading days ending at `end` (inclusive if it is a trading day)."""
        out, d = [], end
        while len(out) < n:
            if self.is_trading_day(d):
                out.append(d)
            d -= timedelta(days=1)
        return list(reversed(out))
