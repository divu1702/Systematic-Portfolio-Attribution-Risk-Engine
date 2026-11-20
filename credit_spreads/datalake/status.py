"""What the repository holds: size per dataset, contracts still to fetch, history depth, quote days,
NSE files, recent runs and growth. `python -m platform_core lake status` prints it; the 18:40 job
sends the short form to Telegram."""

from __future__ import annotations

from datetime import date, timedelta
from typing import List

from .store import DATASETS, Lake

DESCR = {"options_1m": "option 1-minute candles", "futures_1m": "futures 1-minute candles",
         "equity_1m": "stock and index 1-minute candles", "quotes_1m": "live minute quotes",
         "instruments": "instrument masters", "bhav_fo": "NSE F&O bhavcopies", "bhav_cm": "NSE cash bhavcopies"}


def _mb(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def report(root, today: date, short: bool = False) -> str:
    lake = Lake(root)
    cat = lake.catalog
    lines: List[str] = []
    total = lake.size()
    lines.append(f"Data repository at {lake.root}: {_mb(total)}")
    for name in DATASETS:
        n = lake.files(name)
        if n:
            lines.append(f"  {DESCR[name]:34s} {_mb(lake.size(name)):>10s}  {n:,} files, {len(lake.partitions(name)):,} partitions")
    raw = lake.size("nse_raw")
    if raw:
        lines.append(f"  {'NSE files as published':34s} {_mb(raw):>10s}")
    c = cat.execute("SELECT kind, passes, final, COUNT(*), COALESCE(SUM(rows), 0) FROM contracts GROUP BY 1, 2, 3")
    if c:
        state = {}
        for kind, passes, final, n, rows in c:
            label = ("complete" if final == 1 else "expired before the final pass" if final == 2 else
                     {0: "not yet fetched", 1: "first pass done", 2: "middle pass done", 3: "complete"}.get(passes, "?"))
            state[(kind, label)] = state.get((kind, label), 0) + n
        lines.append("  contracts: " + "; ".join(f"{k} {lab}: {n:,}" for (k, lab), n in sorted(state.items())))
    s = cat.execute("SELECT COUNT(*), SUM(backfill_done), MIN(first_ts), MAX(last_ts) FROM series")
    if s and s[0][0]:
        n, done, first, last = s[0]
        lines.append(f"  stocks and indices: {n} series, history back to {str(first)[:10]} ({done or 0} fully backfilled),"
                     f" up to {str(last)[:16]}")
    qd = cat.execute("SELECT date, SUM(instruments), MAX(minutes), SUM(rows), MAX(restarts) FROM quote_days "
                     "GROUP BY date ORDER BY date DESC LIMIT 5")
    if qd:
        lines.append("  live quotes: " + "; ".join(f"{d} {i:,} instruments, {m} minutes, {r:,} rows"
                                                   + (f", {rs} restarts" if rs else "") for d, i, m, r, rs in qd))
    since = (today - timedelta(days=14)).isoformat()
    nf = cat.execute("SELECT kind, status, COUNT(*) FROM nse_files WHERE date >= ? GROUP BY 1, 2", (since,))
    if nf:
        by = {}
        for kind, st, n in nf:
            by.setdefault(kind, {})[st] = n
        lines.append("  NSE files, last 14 days: " + "; ".join(
            f"{k} " + "/".join(f"{st} {n}" for st, n in sorted(v.items())) for k, v in sorted(by.items())))
    back = cat.execute("SELECT COUNT(*), MIN(date) FROM nse_files WHERE status='ok' AND kind='fo_bhav'")
    if back and back[0][0]:
        lines.append(f"  F&O bhavcopies held: {back[0][0]:,}, back to {back[0][1]}")
    unit = cat.get_meta("nse_oi_unit")
    lines.append(f"  NSE OI unit: {unit or 'not settled yet (checked against Kite on an evening with a session)'}")
    gaps = cat.execute("SELECT COUNT(*) FROM gaps")
    if gaps and gaps[0][0]:
        lines.append(f"  gaps recorded: {gaps[0][0]:,} (see the gaps table in catalog.db)")
    runs = cat.execute("SELECT job, started, finished, bytes FROM runs ORDER BY id DESC LIMIT 40")
    daily = {}
    for job, started, _, nbytes in runs:
        daily[started[:10]] = daily.get(started[:10], 0) + (nbytes or 0)
    if len(daily) >= 2:
        avg = sum(daily.values()) / len(daily)
        lines.append(f"  growth: about {_mb(int(avg))} a day from the nightly jobs (~{_mb(int(avg * 250))} a year), "
                     "plus the live quotes")
    if short:
        return "\n".join(lines[:1] + [l for l in lines[1:] if l.strip().startswith(("contracts", "live", "NSE files", "growth", "gaps"))])
    for job, started, finished, nbytes in runs[:8]:
        lines.append(f"  run {job:13s} {started} to {finished[11:]}  {_mb(nbytes or 0)}")
    return "\n".join(lines)
