"""Daily jobs. The scheduler (real clock) and the simulator (SimClock + mock market) call the
same functions, so what is tested offline is what runs live."""

from __future__ import annotations

import logging
from datetime import date

from . import market
from .data import daily, events, instruments as inst, refdata
from .data.calendar import TradingCalendar
from .data.levels_store import refresh_levels

log = logging.getLogger(__name__)


def morning_refresh(ctx) -> None:
    """Instrument master, holidays, sectors, events. Safe to call more than once a day."""
    market.ensure_instruments(ctx)
    if ctx.cache.get("holidays_refreshed") != ctx.clock.today().isoformat()[:7]:
        refdata.refresh_holidays(ctx)
        ctx.cache["holidays_refreshed"] = ctx.clock.today().isoformat()[:7]
    refdata.refresh_sectors(ctx)
    events.fetch_events(ctx)


def catch_up_daily(ctx, through: date) -> None:
    """Make sure completed daily bars, indicators, levels and turnover exist up to `through`
    (covers days the machine was off)."""
    today = market.ensure_instruments(ctx)
    stocks = inst.fno_stocks(ctx.engine, today)
    daily.update_underlying_daily(ctx, stocks, through=through)
    marker = f"levels_{through.isoformat()}"
    if not ctx.cache.get(marker):
        refresh_levels(ctx, stocks, through)
        ctx.cache[marker] = True
    u = ctx.cfg.universe
    if u.top_n or u.min_avg_turnover:          # only needed when F1 ranks by turnover
        refdata.refresh_turnover(ctx, through, u.turnover_window)


def scan_job(ctx, enter: bool = False):
    """15:00: scan; with enter=True, send the top candidates through sizing, limits and fills."""
    from .strategy.scanner import run_scan

    cal = TradingCalendar.load(ctx.engine)
    catch_up_daily(ctx, cal.prev_trading_day(ctx.clock.today()))
    if not events.is_fresh(ctx.engine, ctx.clock.now(), ctx.cfg.events.max_staleness_days):
        events.fetch_events(ctx)
    result = run_scan(ctx)
    if enter:
        from .execution.entries import enter_from_scan
        from .execution.router import get_executor

        result.entry_messages = enter_from_scan(ctx, get_executor(ctx), result)
    return result


def monitor_job(ctx, phase: str = "regular"):
    from .execution.router import get_executor
    from .monitor.exits import run_monitor

    return run_monitor(ctx, get_executor(ctx), phase)


def eod_job(ctx):
    """15:35: today's final candles, levels and turnover; events refresh; equity and drawdown."""
    from .ops import backup_database, heartbeat
    from .risk.equity import eod_equity

    catch_up_daily(ctx, ctx.clock.today())
    events.fetch_events(ctx)
    point = eod_equity(ctx)
    try:
        backup_database(ctx)
    except Exception as exc:          # a failed backup must not stop the end-of-day job
        log.warning("database backup failed: %s", exc)
        ctx.notifier.send(f"Database backup failed: {exc}", "warning")
    try:
        from .data.market_recorder import record_eod

        record_eod(ctx)
    except Exception as exc:
        log.warning("could not record end-of-day market data: %s", exc)
    heartbeat(ctx, point)
    return point
