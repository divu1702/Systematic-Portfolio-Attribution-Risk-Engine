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


def trading_day(ctx, d, outage: Optional[tuple] = None) -> dict:
    """One simulated session with the same jobs the scheduler runs. `outage` = (start, end)
    times during which the machine is 'asleep' (no marks; the replay catches up)."""
    from datetime import timedelta

    from .jobs import monitor_job
    from .scheduler import monitor_slot

    s = ctx.cfg.schedule
    out = {"entries": [], "exits": []}
    ctx.clock.set(datetime.combine(d, parse_hhmm(s.events_fetch)))
    morning_refresh(ctx)
    ctx.clock.set(datetime.combine(d, parse_hhmm(s.gap_check)))
    out["exits"] += monitor_job(ctx, "gap")
    t = datetime.combine(d, parse_hhmm(s.gap_check)) + timedelta(minutes=s.monitor_every_min)
    end = datetime.combine(d, time(15, 30))
    scan_t = datetime.combine(d, parse_hhmm(s.scan))
    while t <= end:
        ctx.clock.set(t)
        asleep = outage and outage[0] <= t.time() < outage[1]
        if not asleep:
            out["exits"] += monitor_slot(ctx) or []
            if t == scan_t:
                res = scan_job(ctx, enter=True)
                out["entries"] += res.entry_messages
        t += timedelta(minutes=s.monitor_every_min)
    ctx.clock.set(datetime.combine(d, parse_hhmm(s.eod)))
    out["equity"] = eod_job(ctx)
    return out


def run_trading(ctx, days: List, outage_prob: float = 0.0, seed: int = 1,
                progress: Optional[Callable[[int, int, dict], None]] = None) -> List[dict]:
    import random

    rng = random.Random(seed)
    results = []
    for i, d in enumerate(days):
        outage = None
        if outage_prob and rng.random() < outage_prob:
            start_h = rng.choice([10, 11, 12, 13])
            outage = (time(start_h, 0), time(start_h + 2, 0))
        r = trading_day(ctx, d, outage)
        r["date"], r["outage"] = d, outage
        results.append(r)
        if progress:
            progress(i + 1, len(days), r)
    return results
