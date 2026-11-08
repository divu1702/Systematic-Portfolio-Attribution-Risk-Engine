"""Plain-text tables for the CLI demos."""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Sequence

from . import market
from .data import instruments as inst


def table(headers: Sequence[str], rows: List[Sequence], align: Optional[str] = None) -> str:
    cells = [[str(h) for h in headers]] + [["" if c is None else str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    align = align or ("l" + "r" * (len(headers) - 1))
    def fmt(r):
        return "  ".join(c.ljust(w) if a == "l" else c.rjust(w) for c, w, a in zip(r, widths, align))
    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(cells[0]), sep] + [fmt(r) for r in cells[1:]])


def _px(x: Optional[float]) -> str:
    return f"{x:.2f}" if x else "-"


def _iv(x: Optional[float]) -> str:
    return f"{x * 100:.1f}" if x else "-"


def option_chain_report(ctx, symbol: str, expiry: Optional[date] = None, rows: int = 10) -> str:
    today = market.ensure_instruments(ctx)
    exps = inst.expiries(ctx.engine, today, symbol)
    if not exps:
        stocks = inst.fno_stocks(ctx.engine, today)
        hint = ", ".join(stocks[:12]) + (" ..." if len(stocks) > 12 else "")
        return f"{symbol} has no stock options in today's instrument master. F&O stocks: {hint}"
    expiry = expiry or market.pick_expiry(exps, today, ctx.cfg.iv.atm_min_dte) or exps[-1]
    snap = market.snapshot_chain(ctx, symbol, expiry)
    ch = snap.chain
    ref = snap.fut_price or snap.spot
    strikes = ch.strikes
    centre = min(range(len(strikes)), key=lambda i: abs(strikes[i] - ref))
    shown = strikes[max(0, centre - rows): centre + rows + 1]
    out_rows = []
    for k in shown:
        c, p = snap.quote(k, "CE"), snap.quote(k, "PE")
        ci, pi = snap.iv(k, "CE"), snap.iv(k, "PE")
        lot = ch.lot_size
        out_rows.append([
            f"{c.oi // lot:,}" if c else "-", _px(c.bid if c else None), _px(c.ask if c else None),
            _iv(ci.iv if ci else None), f"{ci.delta:.2f}" if ci and ci.delta else "-",
            f"{'>' if abs(k - ref) == min(abs(s - ref) for s in shown) else ' '}{k:g}",
            f"{pi.delta:.2f}" if pi and pi.delta else "-", _iv(pi.iv if pi else None),
            _px(p.bid if p else None), _px(p.ask if p else None), f"{p.oi // lot:,}" if p else "-",
        ])
    head = (f"{symbol}  expiry {expiry} ({market.dte(expiry, today)} DTE)  as of {snap.ts:%Y-%m-%d %H:%M} IST"
            f"{'  [SYNTHETIC MARKET]' if ctx.is_mock else ''}\n"
            f"spot {_px(snap.spot)}  futures {_px(snap.fut_price)}  lot {ch.lot_size}  "
            f"strike step {ch.strike_step:g}  tick {ch.tick_size}  ATM IV {_iv(snap.atm_iv)}%\n")
    headers = ["CE OI(lots)", "bid", "ask", "IV%", "delta", "strike", "delta", "IV%", "bid", "ask", "PE OI(lots)"]
    return head + "\n" + table(headers, out_rows, align="rrrrrrrrrrr")


def scan_report(ctx, result, show_rejections: int = 0) -> str:
    from .fmt import inr

    lines = [f"Scan {result.scan_id}  at {result.ts:%Y-%m-%d %H:%M} IST"
             f"{'  [SYNTHETIC MARKET]' if ctx.is_mock else ''}", ""]
    if result.ranked:
        rows = []
        selected = {id(c) for c in result.selected}
        for i, c in enumerate(result.ranked, 1):
            be_wr = c.inputs.get("breakeven_win_rate")
            rows.append([
                f"{i}{'*' if id(c) in selected else ''}", c.symbol, c.side.replace("_", " "),
                f"{c.short_k:g}/{c.long_k:g}", str(c.expiry), c.dte, f"{c.credit_nat:.2f}", f"{c.width:g}",
                f"{c.credit_nat / c.width:.0%}", inr(c.max_loss), f"{c.breakeven:.1f}",
                f"{be_wr:.0%}" if be_wr else "-", f"{c.p_market:.0%}", f"{c.p_model:.0%}", inr(c.ev),
                f"{c.ev_ratio:.1%}", c.inputs.get("ivp_method", ""),
            ])
        lines.append(table(["#", "symbol", "side", "short/long", "expiry", "DTE", "credit", "width", "cr/w",
                            "max loss/lot", "BE", "BE win%", "p_mkt", "p_model", "EV/lot", "EV/ML", "IVP"],
                           rows, align="llllrrrrrrrrrrrrl"))
        lines.append(f"\n* = top {ctx.cfg.scan.top_n} sent to sizing and portfolio checks. Prices per share, "
                     "credit after round-trip costs.")
    else:
        lines.append("No candidates passed every filter today.")
    lines += ["", "Rejection funnel (stocks removed at each stage)", ""]
    total = len(result.outcomes)
    remaining = total
    frows = []
    for stage, n in result.funnel.items():
        if stage == "accepted":
            continue
        frows.append([stage, n, remaining - n])
        remaining -= n
    frows.append(["accepted", "", result.funnel["accepted"]])
    lines.append(f"{total} F&O stocks scanned")
    lines.append(table(["stage", "removed", "remaining"], frows))
    if show_rejections:
        lines += ["", "Rejections"]
        for o in result.outcomes:
            if o.stage != "accepted" and o.stage != "F1 universe":
                lines.append(f"  {o.symbol:<12} {o.stage:<18} {o.reason}")
    for name, book in (getattr(result, "books", None) or {}).items():
        if name == "main":
            continue
        f = book.funnel
        top = ", ".join(f"{c.symbol} {c.side.replace('_', ' ')} {c.short_k:g}/{c.long_k:g} EV/ML {c.ev_ratio:.1%}"
                        for c in book.ranked[:5]) or "none"
        stages = ", ".join(f"{s} {n}" for s, n in f.items() if n and s not in ("accepted", "F1 universe"))
        lines += ["", f"Paper sleeve [{name}]: {f['accepted']} accepted ({top}). Removed: {stages or '-'}"]
    return "\n".join(lines)


def status_report(ctx) -> str:
    from .fmt import inr
    from .risk.limits import kill_switches, open_trades
    from .risk.sizing import current_equity

    lines = []
    for book in (ctx.parent or ctx).books():
        eq = current_equity(book)
        head = (f"Mode {book.cfg.mode.upper()}{'  [SYNTHETIC MARKET]' if ctx.is_mock else ''}   " if book.strategy == "main"
                else f"\nPaper sleeve [{book.strategy}]   ")
        lines.append(f"{head}equity {inr(eq)}   per-trade budget {inr(book.cfg.risk.per_trade * eq)}")
        blocks = kill_switches(book, eq)
        lines.append("Entries: " + ("ALLOWED" if not blocks else "BLOCKED - " + "; ".join(blocks)))
        trades = open_trades(ctx.engine, book.strategy)
        if trades:
            rows = [[t.symbol, t.side.replace("_", " "), f"{t.short_k:g}/{t.long_k:g}", str(t.expiry), t.lots,
                     f"{t.entry_credit:.2f}", inr(t.risk or t.max_loss), inr(t.max_loss), inr(t.mfe or 0),
                     inr(t.mae or 0)] for t in trades]
            lines += ["", table(["symbol", "side", "strikes", "expiry", "lots", "credit", "risk at stop", "max loss",
                                 "MFE", "MAE"], rows, align="lllllrrrrr")]
        else:
            lines.append("No open spreads.")
    return "\n".join(lines)


def books_table(ctx) -> str:
    """The main book and every paper sleeve side by side ('' when there are no sleeves)."""
    from .fmt import inr
    from .reporting.metrics import compare_books

    if len((ctx.parent or ctx).cfg.strategy_names) < 2:
        return ""
    rows = [[r["strategy"], r["closed"], r["open"], f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "-",
             f"{r['breakeven_win_rate']:.0%}" if r["breakeven_win_rate"] is not None else "-", inr(r["net"]),
             f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "-", inr(r["equity"]),
             f"{r['max_drawdown']:.1%}"] for r in compare_books(ctx)]
    return table(["strategy", "closed", "open", "win rate", "breakeven", "net P&L", "profit factor", "equity",
                  "max DD"], rows, align="lrrrrrrrr")


def sim_summary(ctx, results) -> str:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from .data.models import Equity, Trade
    from .fmt import inr

    with Session(ctx.engine) as s:
        trades = list(s.scalars(select(Trade).where(Trade.strategy == "main").order_by(Trade.entry_ts)))
        eq = list(s.scalars(select(Equity).where(Equity.strategy == "main").order_by(Equity.date)))
    closed = [t for t in trades if t.status == "closed"]
    lines = ["", "SIMULATION (synthetic market: tests the machinery, says nothing about real edge)", ""]
    n_entries = sum(1 for r in results for m in r["entries"] if m.startswith("ENTRY"))
    n_rej = sum(1 for r in results for m in r["entries"] if not m.startswith(("ENTRY", "[")))
    lines.append(f"Entries {n_entries}, entry rejections {n_rej}, closed {len(closed)}, "
                 f"open {len(trades) - len(closed)}, outage days {sum(1 for r in results if r.get('outage'))}")
    if closed:
        wins = [t for t in closed if t.pnl_net > 0]
        net = sum(t.pnl_net for t in closed)
        reasons = {}
        for t in closed:
            reasons[t.exit_reason.split(":")[0]] = reasons.get(t.exit_reason.split(":")[0], 0) + 1
        lines.append(f"Win rate {len(wins) / len(closed):.0%}, net P&L {inr(net)}, "
                     f"replayed exits {sum(1 for t in closed if t.replayed)}, gap exits {sum(1 for t in closed if t.gap_flag)}")
        lines.append("Exit reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
        rows = [[t.entry_ts.strftime("%Y-%m-%d"), t.symbol, t.side.replace("_", " "), f"{t.short_k:g}/{t.long_k:g}",
                 t.lots, f"{t.entry_credit:.2f}", f"{t.exit_cost:.2f}", t.exit_reason[:22],
                 (t.exit_ts.strftime("%m-%d %H:%M") if t.exit_ts else ""), inr(t.pnl_net)] for t in closed[-25:]]
        lines += ["", table(["entry", "symbol", "side", "strikes", "lots", "credit", "exit", "reason", "exit at", "net P&L"],
                            rows, align="llllrrrlrr")]
    if eq:
        lines.append(f"\nEquity {inr(eq[0].equity)} -> {inr(eq[-1].equity)}, max drawdown "
                     f"{max(e.drawdown for e in eq):.1%}")
    if (cmp := books_table(ctx)):
        lines += ["", "Strategies side by side (each a separate paper book):", cmp]
    return "\n".join(lines)


def backtest_summary(run) -> str:
    from collections import Counter

    from .fmt import inr
    from .reporting import metrics

    ctx = run.ctx
    closed = metrics.closed_trades(ctx.engine)
    eq = metrics.equity_series(ctx.engine)
    b = ctx.broker
    lines = ["", f"BACKTEST ({run.seconds / 60:.1f} min): real end-of-day data, stops checked at each close", ""]
    trading = run.days[run.warmup:]
    lines.append(f"Trading days {len(trading)} ({trading[0]} to {trading[-1]}), entries {len(run.entries)}, "
                 f"entry rejections {len(run.rejections)}, closed {len(closed)}, "
                 f"open at end {len(metrics.all_trades(ctx.engine)) - len(closed)}")
    if closed:
        net = sum(t.pnl_net for t in closed)
        wins = sum(1 for t in closed if t.pnl_net > 0)
        reasons = Counter((t.exit_reason or "").split(":")[0] for t in closed)
        lines.append(f"Win rate {wins / len(closed):.0%}, net P&L {inr(net)}, average {inr(net / len(closed))} per trade")
        lines.append("Exit reasons: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()))
    if eq:
        lines.append(f"Equity {inr(eq[0].equity)} -> {inr(eq[-1].equity)}, max drawdown {max(e.drawdown for e in eq):.1%}")
    total_q = getattr(b, "real_quotes", 0) + getattr(b, "modelled_quotes", 0)
    if total_q:
        lines.append(f"Option quotes from data {b.real_quotes:,}, modelled (leg outside the day's sample) "
                     f"{b.modelled_quotes:,} ({b.modelled_quotes / total_q:.1%})")
    if len(closed) >= 4:
        mid = trading[len(trading) // 2]
        lines += ["", f"Stability: first half vs second half of the trading period (split at {mid})"]
        for label, part in (("first half ", [t for t in closed if t.entry_ts.date() < mid]),
                            ("second half", [t for t in closed if t.entry_ts.date() >= mid])):
            if not part:
                lines.append(f"  {label}  no trades")
                continue
            w = sum(1 for t in part if t.pnl_net > 0) / len(part)
            exp = (sum(t.pnl_net for t in part) / len(part)) / (sum(t.max_loss for t in part) / len(part))
            gw = sum(t.pnl_net for t in part if t.pnl_net > 0)
            gl = -sum(t.pnl_net for t in part if t.pnl_net < 0)
            pf = f"{gw / gl:.2f}" if gl else "inf"
            lines.append(f"  {label}  {len(part):>3} trades, win {w:.0%}, expectancy {exp:+.1%} of max loss, "
                         f"profit factor {pf}, net {inr(sum(t.pnl_net for t in part))}")
    lines += ["", "Go-live scorecard"]
    for m in metrics.go_live(ctx.engine, ctx.cfg.capital, closed):
        status = "reported" if m.passed is None else ("PASS" if m.passed else "fail")
        lines.append(f"  {m.name:<24} {m.display:<40} bar {m.bar:<22} {status}")
    f = metrics.funnel(ctx.engine)
    total = sum(f.values())
    lines += ["", f"Rejection funnel ({total:,} stock-days scanned, warm-up included)"]
    remaining = total
    for stage, n in f.items():
        if stage == "accepted":
            continue
        remaining -= n
        if n:
            lines.append(f"  {stage:<20} -{n:>6,}   {remaining:>6,} left")
    lines.append(f"  {'accepted':<20}  {f['accepted']:>6,}")
    if (cmp := books_table(ctx)):
        lines += ["", "Strategies side by side (each a separate paper book):", cmp]
    return "\n".join(lines)
