"""The monthly comparison of discretionary and system trades (spec: Discretionary journal and monthly
comparison), generated after the last session of each month.

1. Matching: a discretionary trade and a strict-book trade match when they are on the same stock, in the
   same direction, with entries within `journal.match_window_sessions` sessions of each other.
2. Three groups: both took, only the owner took, only the system (strict book) took. The research book
   is reported separately.
3. Each group and the total: trades and win rate; average and total P&L after costs in rupees and in R;
   average return on margin captured (P&L / margin); average holding days; the worst trade.
4. Matched pairs side by side: entry timing, strikes and exits.
5. By setup tag, with the owner's "none" trades shown on their own.
Trades are those entered during the month; results use closed trades, open ones are listed as open.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fmt import inr
from platform_core.models import PTrade

from .report import CSS


def month_bounds(d: date) -> Tuple[date, date]:
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start, nxt - timedelta(days=1)


def _trades(pengine, account: str, start: date, end: date) -> List[PTrade]:
    with Session(pengine) as s:
        rows = s.scalars(select(PTrade).where(PTrade.account == account).order_by(PTrade.entry_ts)).all()
    return [t for t in rows if start <= t.entry_ts.date() <= end]


def match(owner: Sequence[PTrade], system: Sequence[PTrade], cal, window: int) -> List[Tuple[PTrade, PTrade]]:
    """Greedy one-to-one matching, nearest entries first."""
    cands = []
    for o in owner:
        for s in system:
            if o.symbol != s.symbol or o.direction != s.direction:
                continue
            a, b = sorted((o.entry_ts.date(), s.entry_ts.date()))
            gap = cal.trading_days_between(a, b)
            if gap <= window:
                cands.append((gap, abs((o.entry_ts - s.entry_ts).total_seconds()), o.id, s.id, o, s))
    cands.sort(key=lambda x: x[:4])
    used_o, used_s, pairs = set(), set(), []
    for _, _, oid, sid, o, s in cands:
        if oid in used_o or sid in used_s:
            continue
        used_o.add(oid)
        used_s.add(sid)
        pairs.append((o, s))
    return pairs


@dataclass
class Stats:
    trades: int
    closed: int
    open: int
    win_rate: Optional[float]
    total_pnl: float
    avg_pnl: Optional[float]
    total_r: float
    avg_r: Optional[float]
    avg_rom: Optional[float]
    avg_days: Optional[float]
    worst: Optional[str]

    @classmethod
    def of(cls, trades: Sequence[PTrade]) -> "Stats":
        closed = [t for t in trades if t.status == "closed" and t.pnl_inr is not None]
        n = len(closed)
        wins = sum(1 for t in closed if t.pnl_inr > 0)
        rs = [t.pnl_r for t in closed if t.pnl_r is not None]
        roms = [t.pnl_inr / t.margin for t in closed if t.margin]
        days = [t.holding_days for t in closed if t.holding_days is not None]
        worst = min(closed, key=lambda t: t.pnl_inr) if closed else None
        return cls(len(trades), n, len(trades) - n, wins / n if n else None, sum(t.pnl_inr for t in closed),
                   sum(t.pnl_inr for t in closed) / n if n else None, sum(rs), sum(rs) / len(rs) if rs else None,
                   sum(roms) / len(roms) if roms else None, sum(days) / len(days) if days else None,
                   f"{worst.symbol} {inr(worst.pnl_inr)} ({(worst.pnl_r or 0):+.2f}R)" if worst else None)


def build(p, d: date) -> Dict[str, object]:
    """The comparison for the month containing `d`."""
    start, end = month_bounds(d)
    cal = p.ds.calendar()
    window = p.cfg.journal.match_window_sessions
    owner = _trades(p.pengine, "discretionary", start, end)
    strict = _trades(p.pengine, "level_strict", start, end)
    research = _trades(p.pengine, "level_research", start, end)
    pairs = match(owner, strict, cal, window)
    mo, ms = {o.id for o, _ in pairs}, {s.id for _, s in pairs}
    groups = {
        "both (owner's trades)": [o for o, _ in pairs],
        "both (system's trades)": [s for _, s in pairs],
        "only the owner": [o for o in owner if o.id not in mo],
        "only the system (strict)": [s for s in strict if s.id not in ms],
        "all owner trades": owner,
        "all strict trades": strict,
        "research book (separate)": research,
    }
    tags: Dict[str, List[PTrade]] = {}
    for t in owner:
        tags.setdefault(t.setup_tag or "none", []).append(t)
    return {"month": start.strftime("%Y-%m"), "start": start, "end": end, "window": window,
            "groups": {k: Stats.of(v) for k, v in groups.items()}, "pairs": pairs,
            "by_tag": {k: Stats.of(v) for k, v in sorted(tags.items(), key=lambda kv: (kv[0] == "none", kv[0]))}}


FILTERS = ("volume", "zone_oi", "rom")


def filter_review(p) -> Dict[str, object]:
    """Spec (Monitoring rule 3): each quality filter is judged only once at least `min_trades_per_filter_group`
    closed trades passed it and as many failed it, by comparing the groups' average R. Strict-book trades
    passed every filter; research trades carry the filters they failed. Counts run from go_live_date."""
    with Session(p.pengine) as s:
        rows = s.scalars(select(PTrade).where(PTrade.account.in_(("level_strict", "level_research")),
                                              PTrade.status == "closed")).all()
    need = p.cfg.review.min_trades_per_filter_group
    out = {}
    for f in FILTERS:
        passed, failed = [], []
        for t in rows:
            bad = json.loads(t.failed_filters) if t.failed_filters else {}
            (failed if f in bad else passed).append(t.pnl_r)
        passed = [r for r in passed if r is not None]
        failed = [r for r in failed if r is not None]
        out[f] = {"passed": len(passed), "failed": len(failed),
                  "avg_r_passed": sum(passed) / len(passed) if passed else None,
                  "avg_r_failed": sum(failed) / len(failed) if failed else None,
                  "ready": len(passed) >= need and len(failed) >= need}
    strict_closed = sum(1 for t in rows if t.account == "level_strict")
    return {"filters": out, "strict_closed": strict_closed, "need_strict": p.cfg.review.min_strict_trades_before_changes,
            "need_per_group": need}


def _legs(t: PTrade) -> str:
    legs = json.loads(t.legs_json)
    s = next(lg for lg in legs if lg["side"] == "SELL")
    b = next(lg for lg in legs if lg["side"] == "BUY")
    return f"{s['strike']:g}/{b['strike']:g}"


def _fmt_stats(name: str, s: Stats) -> str:
    e = html.escape
    f = lambda v, fn: "–" if v is None else fn(v)  # noqa: E731
    return (f"<tr><td>{e(name)}</td><td>{s.trades}{f' ({s.open} open)' if s.open else ''}</td>"
            f"<td>{f(s.win_rate, lambda v: f'{v:.0%}')}</td><td>{inr(s.total_pnl)}</td><td>{f(s.avg_pnl, inr)}</td>"
            f"<td>{s.total_r:+.2f}R</td><td>{f(s.avg_r, lambda v: f'{v:+.2f}R')}</td><td>{f(s.avg_rom, lambda v: f'{v:.1%}')}</td>"
            f"<td>{f(s.avg_days, lambda v: f'{v:.1f}')}</td><td>{e(s.worst or '–')}</td></tr>")


def write(p, d: date) -> Path:
    data = build(p, d)
    out = p.cfg.resolve(p.cfg.paths.reports) / "monthly"
    out.mkdir(parents=True, exist_ok=True)
    head = ("<tr><th>Group</th><th>Trades</th><th>Win rate</th><th>Total P&amp;L</th><th>Avg P&amp;L</th><th>Total R</th>"
            "<th>Avg R</th><th>Avg RoM captured</th><th>Avg days</th><th>Worst</th></tr>")
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Comparison {data['month']}</title><style>{CSS}</style></head><body>",
             f"<h1>Discretion vs rules · {data['month']}</h1>",
             f"<p class='mute'>Trades entered {data['start']} to {data['end']}. A match: same stock, same direction, entries within "
             f"{data['window']} sessions. The system here is the strict book; the research book is shown separately.</p>",
             "<h2>Groups</h2><div style='overflow-x:auto'><table>" + head +
             "".join(_fmt_stats(k, v) for k, v in data["groups"].items()) + "</table></div>",
             "<h2>Matched pairs</h2>"]
    if data["pairs"]:
        parts.append("<table><tr><th>Stock</th><th>Owner</th><th>System</th></tr>")
        for o, s in data["pairs"]:
            def side(t):
                res = f"{inr(t.pnl_inr)} ({(t.pnl_r or 0):+.2f}R), {t.exit_reason}" if t.status == "closed" else "open"
                return f"entry {t.entry_ts:%d %b %H:%M}, {_legs(t)} exp {t.expiry}, {res}"
            parts.append(f"<tr><td>{html.escape(o.symbol)} {o.direction.replace('_', ' ')}</td><td>{html.escape(side(o))}</td>"
                         f"<td>{html.escape(side(s))}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='mute'>No matched pairs this month.</p>")
    parts.append("<h2>Owner's trades by setup tag</h2><div style='overflow-x:auto'><table>" + head +
                 "".join(_fmt_stats(("tag " + k) + (" (discretion the rules don't capture)" if k == "none" else ""), v)
                         for k, v in data["by_tag"].items()) + "</table></div>")
    fr = filter_review(p)
    names = {"volume": "Day 0 volume", "zone_oi": "zone OI test", "rom": "10% return on margin"}
    parts.append(f"<h2>Quality filters (all closed level trades since go-live)</h2><p class='mute'>Strict book: "
                 f"{fr['strict_closed']} closed trades; no rule changes before {fr['need_strict']}. A filter is judged only "
                 f"with at least {fr['need_per_group']} trades on each side.</p><table><tr><th>Filter</th><th>Passed</th>"
                 "<th>Avg R</th><th>Failed</th><th>Avg R</th><th>Ready to judge</th></tr>")
    for f, v in fr["filters"].items():
        a, b = v["avg_r_passed"], v["avg_r_failed"]
        parts.append(f"<tr><td>{names[f]}</td><td>{v['passed']}</td><td>{'–' if a is None else f'{a:+.2f}R'}</td>"
                     f"<td>{v['failed']}</td><td>{'–' if b is None else f'{b:+.2f}R'}</td><td>{'yes' if v['ready'] else 'no'}</td></tr>")
    parts.append("</table></body></html>")
    path = out / f"comparison_{data['month']}.html"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
