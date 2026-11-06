"""Refresh sectors, holidays and stock-option turnover from the exchange source, merged with
the owner's CSV overrides. Failures are logged and leave the last good data in place."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.sqlite import insert

from . import csvs
from .calendar import TradingCalendar
from .models import Holiday, OptionTurnover, Sector

log = logging.getLogger(__name__)

UNKNOWN_SECTOR = "UNKNOWN"


def refresh_sectors(ctx, max_age_days: int = 7) -> int:
    now = ctx.clock.now()
    with ctx.engine.connect() as conn:
        newest = conn.execute(select(func.max(Sector.updated_at)).where(Sector.source == "nse")).scalar()
    rows: Dict[str, tuple] = {}
    if newest is None or (now - newest).days >= max_age_days:
        try:
            for sym, sec in ctx.nse.sectors().items():
                rows[sym] = (sec, "nse")
        except Exception as exc:  # network / parse failure: keep the old list
            log.warning("sector refresh failed: %s", exc)
    for sym, sec in csvs.manual_sectors(ctx.cfg.resolve(ctx.cfg.paths.sectors_manual)).items():
        rows[sym] = (sec, "manual")
    if not rows:
        return 0
    with ctx.engine.begin() as conn:
        conn.execute(delete(Sector).where(Sector.source == "manual"))
        for sym, (sec, src) in rows.items():
            stmt = insert(Sector).values(symbol=sym, sector=sec, source=src, updated_at=now)
            conn.execute(stmt.on_conflict_do_update(index_elements=["symbol"], set_={
                "sector": stmt.excluded.sector, "source": stmt.excluded.source,
                "updated_at": stmt.excluded.updated_at}))
    return len(rows)


def sector_of(engine: Engine, symbol: str) -> str:
    with engine.connect() as conn:
        return conn.execute(select(Sector.sector).where(Sector.symbol == symbol)).scalar() or UNKNOWN_SECTOR


def refresh_holidays(ctx) -> int:
    rows = []
    try:
        rows = [(d, desc, "nse") for d, desc in ctx.nse.holidays()]
    except Exception as exc:
        log.warning("holiday refresh failed: %s", exc)
    rows += [(d, desc, "manual") for d, desc in csvs.manual_holidays(ctx.cfg.resolve(ctx.cfg.paths.holidays_manual))]
    with ctx.engine.begin() as conn:
        for d, desc, src in rows:
            stmt = insert(Holiday).values(date=d, description=desc, source=src)
            conn.execute(stmt.on_conflict_do_update(index_elements=["date"], set_={
                "description": stmt.excluded.description, "source": stmt.excluded.source}))
    return len(rows)


def refresh_turnover(ctx, through: date, window: int) -> int:
    """Fetch any missing bhavcopy days among the last `window` trading days up to `through`."""
    cal = TradingCalendar.load(ctx.engine)
    days = cal.last_n(through, window)
    with ctx.engine.connect() as conn:
        have = {r[0] for r in conn.execute(select(OptionTurnover.date).where(OptionTurnover.date >= days[0]).distinct())}
    added = 0
    for d in days:
        if d in have:
            continue
        try:
            data = ctx.nse.option_turnover(d)
        except Exception as exc:
            log.warning("bhavcopy %s unavailable: %s", d, exc)
            continue
        if not data:
            continue
        with ctx.engine.begin() as conn:
            for sym, val in data.items():
                stmt = insert(OptionTurnover).values(date=d, symbol=sym, turnover=float(val), source="bhavcopy")
                conn.execute(stmt.on_conflict_do_nothing())
        added += 1
    return added


def average_turnover(engine: Engine, through: date, window: int, cal: TradingCalendar) -> Dict[str, float]:
    days = cal.last_n(through, window)
    with engine.connect() as conn:
        rows = conn.execute(select(OptionTurnover.symbol, func.avg(OptionTurnover.turnover), func.count())
                            .where(OptionTurnover.date >= days[0], OptionTurnover.date <= through)
                            .group_by(OptionTurnover.symbol)).all()
    return {sym: float(avg) for sym, avg, _n in rows}


def universe(engine: Engine, stocks: List[str], through: date, top_n: int, window: int,
             cal: TradingCalendar) -> List[str]:
    """F1: F&O stocks ranked by average daily stock-option turnover; top N."""
    avg = average_turnover(engine, through, window, cal)
    ranked = sorted((s for s in stocks if s in avg), key=lambda s: avg[s], reverse=True)
    return ranked[:top_n]
