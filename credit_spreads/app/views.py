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
