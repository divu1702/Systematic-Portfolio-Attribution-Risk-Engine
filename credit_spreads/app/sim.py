"""Offline simulation on the synthetic market: the real jobs, driven day by day with a SimClock.

warmup(): morning refresh, 15:00 scan (records IV snapshots and candidates) and 15:35 EOD for
N past trading days, so that IV history exists for F4/F5. Milestone 6 adds trading days with
entries, 5-minute monitoring and exits.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from pathlib import Path
from typing import Callable, List, Optional

from .broker.synthetic import SyntheticMarket
from .clock import SimClock, parse_hhmm
from .context import build_context, mock_db_path
from .jobs import eod_job, morning_refresh, scan_job

log = logging.getLogger(__name__)


def sim_days(market: SyntheticMarket, end_day, n: int):
    """The n synthetic trading days ending at end_day (inclusive)."""
    idx = market.day_index[end_day]
    if idx - n + 1 < 300:
        raise ValueError("not enough synthetic history before the simulation start")
    return market.days[idx - n + 1: idx + 1]


def make_sim_context(cfg, start_day, db_path: Optional[Path] = None, fresh: bool = True, market=None):
    market = market or SyntheticMarket(seed=cfg.broker.mock_seed, n_symbols=cfg.broker.mock_symbols,
                                       anchor=start_day, r=cfg.analytics.risk_free)
    path = db_path or mock_db_path(cfg)
    if fresh:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
    clock = SimClock(datetime.combine(start_day, time(8, 0)))
    return build_context(cfg, mock=True, clock=clock, db_path=path, env={}, market=market)


def warmup(ctx, days: List, progress: Optional[Callable[[int, int], None]] = None) -> None:
    s = ctx.cfg.schedule
    for i, d in enumerate(days):
        ctx.clock.set(datetime.combine(d, parse_hhmm(s.events_fetch)))
        morning_refresh(ctx)
        ctx.clock.set(datetime.combine(d, parse_hhmm(s.scan)))
        scan_job(ctx)
        ctx.clock.set(datetime.combine(d, parse_hhmm(s.eod)))
        eod_job(ctx)
        if progress:
            progress(i + 1, len(days))
