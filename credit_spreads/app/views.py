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
    return "\n".join(lines)


def status_report(ctx) -> str:
    from .data import state
    from .fmt import inr
    from .risk.limits import kill_switches, open_trades
    from .risk.sizing import current_equity

    eq = current_equity(ctx)
    lines = [f"Mode {ctx.cfg.mode.upper()}{'  [SYNTHETIC MARKET]' if ctx.is_mock else ''}   "
             f"equity {inr(eq)}   per-trade budget {inr(ctx.cfg.risk.per_trade * eq)}"]
    blocks = kill_switches(ctx, eq)
    lines.append("Entries: " + ("ALLOWED" if not blocks else "BLOCKED - " + "; ".join(blocks)))
    trades = open_trades(ctx.engine)
    if trades:
        rows = [[t.symbol, t.side.replace("_", " "), f"{t.short_k:g}/{t.long_k:g}", str(t.expiry), t.lots,
                 f"{t.entry_credit:.2f}", inr(t.max_loss), inr(t.mfe or 0), inr(t.mae or 0)] for t in trades]
        lines += ["", table(["symbol", "side", "strikes", "expiry", "lots", "credit", "max loss", "MFE", "MAE"], rows,
                            align="lllllrrrr")]
    else:
        lines.append("No open spreads.")
    return "\n".join(lines)


def sim_summary(ctx, results) -> str:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from .data.models import Equity, Trade
    from .fmt import inr

    with Session(ctx.engine) as s:
        trades = list(s.scalars(select(Trade).order_by(Trade.entry_ts)))
        eq = list(s.scalars(select(Equity).order_by(Equity.date)))
    closed = [t for t in trades if t.status == "closed"]
    lines = ["", "SIMULATION (synthetic market: tests the machinery, says nothing about real edge)", ""]
    n_entries = sum(1 for r in results for m in r["entries"] if m.startswith("ENTRY"))
    n_rej = sum(1 for r in results for m in r["entries"] if not m.startswith("ENTRY"))
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
    return "\n".join(lines)
