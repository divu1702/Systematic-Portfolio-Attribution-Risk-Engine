"""Weekly HTML report (spec: generated every Friday after close)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..fmt import inr, pct
from . import charts, metrics

TEMPLATES = Path(__file__).resolve().parent / "templates"


def build_report(ctx, asof: Optional[date] = None, days: int = 7) -> str:
    asof = asof or ctx.clock.today()
    start = asof - timedelta(days=days - 1)
    book = ctx.strategy
    all_closed = metrics.closed_trades(ctx.engine, strategy=book)
    week_closed = [t for t in all_closed if start <= t.exit_ts.date() <= asof]
    trades = metrics.all_trades(ctx.engine, book)
    open_ = [t for t in trades if t.status == "open"]
    eq = metrics.equity_series(ctx.engine, book)
    scorecard = metrics.go_live(ctx.engine, ctx.cfg.capital, all_closed, strategy=book)
    cal = metrics.calibration(all_closed)
    sp = metrics.splits(all_closed)
    fun_week = metrics.funnel(ctx.engine, datetime.combine(start, time(0)), datetime.combine(asof, time(23, 59)),
                              strategy=book)
    fun_all = metrics.funnel(ctx.engine, strategy=book)
    books = metrics.compare_books(ctx) if len((ctx.parent or ctx).cfg.strategy_names) > 1 else []
    mae = metrics.mae_multiples(all_closed)
    wr = (sum(1 for t in all_closed if t.pnl_net > 0) / len(all_closed)) if all_closed else None
    be = [t.breakeven_wr for t in all_closed if t.breakeven_wr is not None]
    be_avg = sum(be) / len(be) if be else None

    def funnel_rows(f):
        total = sum(f.values())
        remaining, rows = total, []
        for stage, n in f.items():
            if stage == "accepted":
                continue
            rows.append((stage, n, remaining - n))
            remaining -= n
        return total, rows, f["accepted"]

    split_charts = {name: charts.bar_rows([(r["key"], r["net"]) for r in rows], fmt=inr, diverging=True)
                    for name, rows in sp.items()}
    wt, wrows, wacc = funnel_rows(fun_week)
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    env.filters.update(inr=inr, pct=pct)
    return env.get_template("weekly.html").render(
        asof=asof, start=start, generated=ctx.clock.now(), mode=ctx.cfg.mode, mock=ctx.is_mock,
        banner=getattr(ctx, "banner", ""), period_days=days, strategy=book, books=books,
        capital=ctx.cfg.capital, equity=eq[-1].equity if eq else ctx.cfg.capital, scorecard=scorecard,
        go_live=all(m.passed for m in scorecard if m.passed is not None),
        equity_svg=charts.line_chart([(e.date, e.equity) for e in eq], label="equity"),
        dd_svg=charts.line_chart([(e.date, -e.drawdown * 100) for e in eq], fmt=lambda v: f"{v:.1f}%",
                                 color="var(--series-neg)", invert=True, label="drawdown"),
        week_closed=week_closed, open_trades=open_, all_closed=all_closed, win_rate=wr, be_avg=be_avg,
        splits=sp, split_charts=split_charts,
        funnel_week=(wt, wrows, wacc), funnel_week_svg=charts.bar_rows([(s, n) for s, n, _ in wrows if n],
                                                                       fmt=lambda v: f"{v:.0f}"),
        funnel_all=funnel_rows(fun_all),
        mae_svg=charts.histogram(mae, [0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0],
                                 xlabel=lambda a, b: f"{a:g}-{b:g}"),
        mae=mae, stop_line=ctx.cfg.exit.stop_mult - 1, calibration=cal, cal_svg=charts.calibration_chart(cal),
    )


def write_report(ctx, asof: Optional[date] = None, out_dir: Optional[Path] = None, days: int = 7,
                 name: Optional[str] = None) -> Path:
    asof = asof or ctx.clock.today()
    out_dir = out_dir or ctx.cfg.resolve(ctx.cfg.reporting.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    book = "" if ctx.strategy == "main" else f"_{ctx.strategy}"
    path = out_dir / f"{name or 'weekly_' + asof.isoformat() + book + ('_mock' if ctx.is_mock else '')}.html"
    path.write_text(build_report(ctx, asof, days=days), encoding="utf-8")
    return path
