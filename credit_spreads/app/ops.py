"""Operational safety nets: daily database backups and the end-of-day heartbeat alert."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select

from .fmt import inr

log = logging.getLogger(__name__)


def backup_database(ctx, keep: Optional[int] = None) -> Optional[Path]:
    """Consistent copy of the trading database via SQLite's backup API, one per day, oldest pruned."""
    keep = ctx.cfg.ops.backup_keep if keep is None else keep
    db_file = ctx.engine.url.database
    if not keep or not db_file or ctx.is_mock or ctx.banner:
        return None
    src_path = Path(db_file)
    folder = src_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{src_path.stem}_{ctx.clock.today():%Y%m%d}.db"
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    for old in sorted(folder.glob(f"{src_path.stem}_*.db"))[:-keep]:
        old.unlink()
    return dest


def heartbeat(ctx, equity_point) -> Optional[str]:
    """One end-of-day message with the day's activity. If it does not arrive, the system is down."""
    if not ctx.cfg.ops.heartbeat or ctx.is_mock or ctx.banner:
        return None
    from .data.models import Candidate, Trade
    from .risk.limits import kill_switches, open_trades

    start = datetime.combine(ctx.clock.today(), time(0))
    with ctx.engine.connect() as conn:
        scanned = conn.execute(select(func.count(func.distinct(Candidate.symbol))).where(Candidate.ts >= start)).scalar()
        accepted = conn.execute(select(func.count()).select_from(Candidate).where(
            Candidate.ts >= start, Candidate.accepted.is_(True))).scalar()
        opened = conn.execute(select(func.count()).select_from(Trade).where(Trade.entry_ts >= start)).scalar()
        closed = conn.execute(select(func.count()).select_from(Trade).where(Trade.exit_ts >= start)).scalar()
    blocks = kill_switches(ctx, equity_point.equity)
    msg = (f"EOD {ctx.clock.today()}: equity {inr(equity_point.equity)} (drawdown {equity_point.drawdown:.1%}), "
           f"{len(open_trades(ctx.engine))} open; today scanned {scanned}, accepted {accepted}, opened {opened}, "
           f"closed {closed}." + (f" Entries blocked: {'; '.join(blocks)}" if blocks else ""))
    ctx.notifier.send(msg, "info")
    return msg
