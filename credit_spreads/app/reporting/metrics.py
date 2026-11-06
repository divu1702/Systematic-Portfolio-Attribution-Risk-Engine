"""Evaluation metrics and go-live criteria (spec: Evaluation metrics and go-live criteria)."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.models import Candidate, Equity, Order, Trade

STAGES = ["F1 universe", "DTE window", "data", "F2 events", "F3 IV/HV", "F4 IV percentile", "F5 cause passed",
          "direction", "F6 level buffer", "F7 distance/delta", "risk budget", "F9 liquidity", "F8 credit/width",
          "EV", "accepted"]


def closed_trades(engine, start: Optional[date] = None, end: Optional[date] = None) -> List[Trade]:
    q = select(Trade).where(Trade.status == "closed")
    if start:
        q = q.where(Trade.exit_ts >= datetime.combine(start, time(0)))
    if end:
        q = q.where(Trade.exit_ts <= datetime.combine(end, time(23, 59, 59)))
    with Session(engine) as s:
        return list(s.scalars(q.order_by(Trade.exit_ts)))


def all_trades(engine) -> List[Trade]:
    with Session(engine) as s:
        return list(s.scalars(select(Trade).order_by(Trade.entry_ts)))


def equity_series(engine) -> List[Equity]:
    with Session(engine) as s:
        return list(s.scalars(select(Equity).order_by(Equity.date)))


@dataclass
class Metric:
    name: str
    definition: str
    value: Optional[float]
    display: str
    bar: str
    passed: Optional[bool]        # None = reported only


EPS = 1e-9


def _gt(a, b) -> bool:
    """Strictly above a bar, ignoring float noise (0.75 - 0.70 is 0.05000000000000004)."""
    return a is not None and a > b + EPS


def _lt(a, b) -> bool:
    return a is not None and a < b - EPS


def _equity_on(eq: List[Equity], d: date, capital: float) -> float:
    prior = [e.equity for e in eq if e.date <= d]
    return prior[-1] if prior else capital


def slippage(engine, trade_ids: Optional[List[str]] = None) -> Tuple[Optional[float], Optional[float], int]:
    """Average adverse slippage per filled order vs the natural price: (rupees per share,
    fraction of natural price, number of orders)."""
    q = select(Order).where(Order.status == "filled", Order.limit_px.is_not(None), Order.limit_px > 0)
    with Session(engine) as s:
        orders = list(s.scalars(q))
    if trade_ids is not None:
        keep = set(trade_ids)
        orders = [o for o in orders if o.trade_id in keep]
    if not orders:
        return None, None, 0
    per = [(o.fill_px - o.limit_px) if o.side == "BUY" else (o.limit_px - o.fill_px) for o in orders]
    pct = [p / o.limit_px for p, o in zip(per, orders)]
    return sum(per) / len(per), sum(pct) / len(pct), len(orders)


def calibration(trades: List[Trade], bins: int = 10) -> List[dict]:
    """p_model by quantile bin vs actual win rate (net P&L > 0)."""
    pts = sorted((t.p_model, 1.0 if t.pnl_net > 0 else 0.0) for t in trades if t.p_model is not None)
    if not pts:
        return []
    bins = max(1, min(bins, len(pts) // 3 or 1))
    out = []
    for b in range(bins):
        chunk = pts[b * len(pts) // bins:(b + 1) * len(pts) // bins]
        if chunk:
            out.append({"bin": b + 1, "n": len(chunk), "p_model": sum(p for p, _ in chunk) / len(chunk),
                        "actual": sum(w for _, w in chunk) / len(chunk)})
    return out


def is_monotonic(rows: List[dict]) -> Optional[bool]:
    if len(rows) < 2:
        return None
    a = [r["actual"] for r in rows]
    return all(y >= x - 1e-12 for x, y in zip(a, a[1:]))


def go_live(engine, capital: float, trades: Optional[List[Trade]] = None) -> List[Metric]:
    trades = closed_trades(engine) if trades is None else trades
    eq = equity_series(engine)
    n = len(trades)
    out: List[Metric] = []
    span_days = (max(t.exit_ts for t in trades) - min(t.entry_ts for t in trades)).days if trades else 0
    out.append(Metric("Closed paper trades", "Count", n, f"{n}", ">= 30", n >= 30))
    out.append(Metric("Calendar span", "First entry to last exit", span_days / 30.44, f"{span_days / 30.44:.1f} months",
                      ">= 3 months", span_days / 30.44 >= 3 - EPS))
    if n:
        avg_pnl = sum(t.pnl_net for t in trades) / n
        avg_ml = sum(t.max_loss for t in trades) / n
        exp = avg_pnl / avg_ml if avg_ml else None
        out.append(Metric("Expectancy", "Average net P&L per trade / average maximum loss", exp,
                          f"{exp:+.1%}" if exp is not None else "-", "> +5%", _gt(exp, 0.05)))
        wr = sum(1 for t in trades if t.pnl_net > 0) / n
        be = [t.breakeven_wr for t in trades if t.breakeven_wr is not None]
        be_avg = sum(be) / len(be) if be else None
        edge = wr - be_avg if be_avg is not None else None
        out.append(Metric("Win rate vs breakeven", "Actual win rate minus average breakeven win rate", edge,
                          f"{wr:.0%} vs {be_avg:.0%} ({edge * 100:+.1f} pp)" if edge is not None else f"{wr:.0%}",
                          "> +5 pp", _gt(edge, 0.05)))
        wins = sum(t.pnl_net for t in trades if t.pnl_net > 0)
        losses = -sum(t.pnl_net for t in trades if t.pnl_net < 0)
        pf = wins / losses if losses else (math.inf if wins else None)
        out.append(Metric("Profit factor", "Gross wins / gross losses, net of costs", pf,
                          "inf" if pf == math.inf else (f"{pf:.2f}" if pf is not None else "-"), "> 1.3",
                          _gt(pf, 1.3)))
        worst = min(trades, key=lambda t: t.pnl_net)
        worst_pct = -worst.pnl_net / _equity_on(eq, worst.entry_ts.date(), capital) if worst.pnl_net < 0 else 0.0
        out.append(Metric("Worst single trade", "Net loss as % of equity", worst_pct,
                          f"{worst_pct:.2%} ({worst.symbol})", "< 3%", _lt(worst_pct, 0.03)))
    else:
        for name, bar in (("Expectancy", "> +5%"), ("Win rate vs breakeven", "> +5 pp"), ("Profit factor", "> 1.3"),
                          ("Worst single trade", "< 3%")):
            out.append(Metric(name, "", None, "no closed trades", bar, False))
    mdd = max((e.drawdown for e in eq), default=0.0)
    out.append(Metric("Maximum drawdown", "Peak-to-trough on paper equity", mdd, f"{mdd:.1%}", "< 12%", _lt(mdd, 0.12)))
    s_abs, s_pct, n_orders = slippage(engine, [t.trade_id for t in trades])
    out.append(Metric("Fill realism", "Average slippage per order vs natural price", s_pct,
                      f"{s_abs:.3f}/share ({s_pct:.2%}) over {n_orders} orders" if n_orders else "no fills",
                      "Measured and reported", None))
    cal = calibration(trades)
    mono = is_monotonic(cal)
    out.append(Metric("Calibration", "p_model by decile vs actual win rate", None,
                      {True: "monotonic", False: "not monotonic", None: "too few trades"}[mono] +
                      (f" ({len(cal)} bins)" if cal else ""), "Monotonic", mono))
    return out


def iv_hv_bucket(x: Optional[float]) -> str:
    if x is None:
        return "unknown"
    for hi, label in ((1.25, "< 1.25"), (1.5, "1.25-1.50"), (2.0, "1.50-2.00")):
        if x < hi:
            return label
    return ">= 2.00"


def splits(trades: List[Trade]) -> Dict[str, List[dict]]:
    def group(key) -> List[dict]:
        g: Dict[str, List[Trade]] = OrderedDict()
        for t in trades:
            g.setdefault(key(t), []).append(t)
        rows = []
        for k, ts in sorted(g.items()):
            rows.append({"key": k, "n": len(ts), "win_rate": sum(1 for t in ts if t.pnl_net > 0) / len(ts),
                         "net": sum(t.pnl_net for t in ts)})
        return rows
    return OrderedDict([
        ("Side", group(lambda t: t.side.replace("_", " "))),
        ("IV/HV at entry", group(lambda t: iv_hv_bucket(t.iv_hv))),
        ("IV percentile method", group(lambda t: t.ivp_method or "unknown")),
        ("Exit reason", group(lambda t: (t.exit_reason or "").split(":")[0] + (" (replayed)" if t.replayed else ""))),
    ])


def funnel(engine, start: Optional[datetime] = None, end: Optional[datetime] = None) -> "OrderedDict[str, int]":
    """Stock-scans removed at each stage (first failing stage per stock per scan)."""
    q = select(Candidate.scan_id, Candidate.symbol, Candidate.reject_reason, Candidate.accepted)
    if start:
        q = q.where(Candidate.ts >= start)
    if end:
        q = q.where(Candidate.ts <= end)
    with engine.connect() as conn:
        rows = conn.execute(q).all()
    best: Dict[tuple, int] = {}
    for sid, sym, reason, acc in rows:
        stage = "accepted" if acc else (reason or "").split(":")[0]
        idx = STAGES.index(stage) if stage in STAGES else -1
        key = (sid, sym)
        best[key] = max(best.get(key, -1), idx)
    out = OrderedDict((s, 0) for s in STAGES)
    for idx in best.values():
        if idx >= 0:
            out[STAGES[idx]] += 1
    return out


def mae_multiples(trades: List[Trade]) -> List[float]:
    """Worst unrealised loss in multiples of the entry credit (0 = never under water)."""
    out = []
    for t in trades:
        credit_total = t.entry_credit * t.lots * t.lot_size
        if credit_total > 0 and t.mae is not None:
            out.append(max(0.0, -t.mae) / credit_total)
    return out
