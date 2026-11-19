"""The daily screener report (spec: Screener, Daily output): an HTML page and a Telegram summary with
- confirmed setups ready to trade, each with its exact spread, return on margin and lots;
- armed setups in their confirmation window (with zone OI share and local PCR);
- the top 20 stocks by the ROI ranking, with any setup status marked.
Each run's full record is also saved as JSON under reports/platform/runs/.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import List, Optional

from app.fmt import inr

from . import store

CSS = """body{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:16px;color:#15181d;background:#fff}
h1{font-size:20px}h2{font-size:16px;margin-top:22px}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid #e3e6ea;vertical-align:top}th{color:#667085;font-weight:500;font-size:12px}
.mute{color:#667085}.neg{color:#c0262d}.pos{color:#0a7d43}.chip{background:#eef1f5;border-radius:9px;padding:1px 6px;font-size:12px}
@media (prefers-color-scheme: dark){body{background:#0f1216;color:#e8ebef}th,td{border-color:#2a3038}.chip{background:#222831}}"""


def _pct(x) -> str:
    return "–" if x is None else f"{x * 100:.1f}%"


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def save_run(root: Path, rep) -> Path:
    d = root / "runs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rep.ts:%Y-%m-%d_%H%M%S}_{rep.phase}.json"
    path.write_text(json.dumps(rep.as_dict(), indent=1, default=str), encoding="utf-8")
    return path


def write_daily(p, rep, top_n: Optional[int] = None) -> Path:
    """reports/platform/daily/screener_<date>.html from the 15:20 run's report."""
    top_n = top_n or p.cfg.screener.report_top_n
    root = p.cfg.resolve(p.cfg.paths.reports)
    out = root / "daily"
    out.mkdir(parents=True, exist_ok=True)
    active = [r for r in store.active(p.pengine)]
    parts: List[str] = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
                        f"initial-scale=1'><title>Screener {rep.date}</title><style>{CSS}</style></head><body>",
                        f"<h1>Screener · {rep.date:%d %b %Y} 15:20</h1>",
                        f"<p class='mute'>{'Live paper trading' if rep.live else 'Not live yet: entries below are what each book would have done'}"
                        f" · removed: {sum(rep.removed.values())} ({', '.join(f'{k} {v}' for k, v in rep.removed.items()) or 'none'})</p>"]
    parts.append("<h2>Confirmed, ready to trade</h2>")
    if rep.entries:
        parts.append("<table><tr><th>Stock</th><th>Book</th><th>Setup</th><th>Spread</th><th>Lots</th><th>RoM</th>"
                     "<th>1R</th><th>Failed filters</th></tr>")
        for e in rep.entries:
            parts.append(f"<tr><td><b>{_e(e['symbol'])}</b></td><td>{_e(e['book'])}{' (dry run)' if e.get('dry_run') else ''}</td>"
                         f"<td>S{e['setup']} {_e(e['direction']).replace('_', ' ')} @ {e['level']:.2f}</td>"
                         f"<td>{e['short']:g}/{e['long']:g} {_e(e['expiry'])}<br><span class='mute'>credit {e['credit']:.2f}</span></td>"
                         f"<td>{e['lots']} × {e['lot_size']}</td><td>{_pct(e.get('rom'))}</td><td>{inr(e['one_r'])}</td>"
                         f"<td>{_e(', '.join(e.get('failed_filters') or {}) or '–')}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='mute'>None today.</p>")
    if rep.skipped:
        parts.append("<h2>Confirmed but not traded</h2><table><tr><th>Stock</th><th>Book</th><th>Reason</th></tr>")
        for s in rep.skipped:
            parts.append(f"<tr><td>{_e(s['symbol'])} S{s['setup']}</td><td>{_e(s['book'])}</td><td>{_e(s['reason'])}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>Armed, in the confirmation window</h2>")
    if active:
        parts.append("<table><tr><th>Stock</th><th>Setup</th><th>Level</th><th>Day 0</th><th>State</th><th>Zone OI share</th>"
                     "<th>Local PCR</th><th>Volume</th></tr>")
        for r in active:
            parts.append(f"<tr><td><b>{_e(r.symbol)}</b></td><td>S{r.setup} {_e(r.direction).replace('_', ' ')}</td>"
                         f"<td>{r.level:.2f}</td><td>{r.day0_date}</td><td>{_e(r.state)}{' ' + _e(r.route) if r.route else ''}</td>"
                         f"<td>{_pct(r.zone_oi_share)}</td><td>{'–' if r.local_pcr is None else f'{r.local_pcr:.2f}'}</td>"
                         f"<td>{'–' if r.volume_mult is None else f'{r.volume_mult:.1f}x'}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='mute'>None.</p>")
    if rep.armed:
        parts.append("<p class='mute'>Armed today: " + ", ".join(f"{_e(a['symbol'])} S{a['setup']} {_e(a['direction'])} @ "
                                                               f"{a['level']:.2f}" for a in rep.armed) + "</p>")
    parts.append(f"<h2>Top {top_n} by ROI</h2><table><tr><th>#</th><th>Stock</th><th>Call RoM</th><th>Put RoM</th><th>Setups</th></tr>")
    for s in rep.screener[:top_n]:
        tags = " ".join(f"<span class='chip'>{_e(t)}</span>" for t in s.get("setups") or [])
        parts.append(f"<tr><td>{s['rank']}</td><td>{_e(s['symbol'])}<br><span class='mute'>{_e(s.get('sector') or '')}</span></td>"
                     f"<td>{_pct(s.get('rom_call'))}</td><td>{_pct(s.get('rom_put'))}</td><td>{tags}</td></tr>")
    parts.append("</table>")
    if rep.exits:
        parts.append("<h2>Exits</h2><table><tr><th>Account</th><th>Stock</th><th>Reason</th><th>P&amp;L</th></tr>")
        for x in rep.exits:
            pnl = "–" if x.get("pnl") is None else f"{inr(x['pnl'])} ({x['pnl_r']:+.2f}R)" if x.get("pnl_r") is not None else inr(x["pnl"])
            parts.append(f"<tr><td>{_e(x['account'])}</td><td>{_e(x['symbol'])}</td><td>{_e(x['reason'])}</td><td>{pnl}</td></tr>")
        parts.append("</table>")
    if rep.warnings:
        parts.append("<h2>Notes</h2><ul>" + "".join(f"<li>{_e(w)}</li>" for w in rep.warnings) + "</ul>")
    parts.append("</body></html>")
    path = out / f"screener_{rep.date.isoformat()}.html"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def telegram_summary(rep, top: int = 20) -> str:
    """The same three lists, in a message that fits Telegram (4096 characters)."""
    lines = [f"Screener {rep.date:%d %b} 15:20{'' if rep.live else ' (not live: dry run)'}"]
    if rep.entries:
        lines.append("Confirmed:")
        for e in rep.entries:
            lines.append(f"• {e['symbol']} {e['book']} S{e['setup']} {e['direction'].replace('_', ' ')} {e['short']:g}/{e['long']:g} "
                         f"{e['expiry']} x{e['lots']} RoM {_pct(e.get('rom'))}")
    else:
        lines.append("Confirmed: none")
    armed = [a for a in rep.advanced if a["state"] in ("armed", "confirming")] + rep.armed
    if armed:
        lines.append("Armed: " + ", ".join(f"{a['symbol']} S{a['setup']}" for a in armed[:30]))
    if rep.exits:
        lines.append("Exits: " + ", ".join(f"{x['symbol']} {x['reason']}" for x in rep.exits))
    if rep.screener:
        lines.append(f"Top {min(top, len(rep.screener))} by ROI: " + ", ".join(
            f"{s['symbol']} {_pct(max(v for v in (s.get('rom_call'), s.get('rom_put')) if v is not None))}"
            + (" *" if s.get("setups") else "") for s in rep.screener[:top]))
    if rep.removed:
        lines.append("Removed: " + ", ".join(f"{k} {v}" for k, v in rep.removed.items()))
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"
