"""Account summaries for the control centre and the reports: value, today's and total P&L, P&L in R,
margin used and available, open positions, win rate, equity curve, positions laid out like Kite, and
the funds ledger."""

from __future__ import annotations

import json
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AccountDay, LedgerEntry, PTrade


def _closed(pengine, account: str) -> List[PTrade]:
    with Session(pengine) as s:
        return list(s.scalars(select(PTrade).where(PTrade.account == account, PTrade.status == "closed")
                              .order_by(PTrade.exit_ts)))


def equity_curve(pengine, account: str) -> List[dict]:
    with Session(pengine) as s:
        rows = s.scalars(select(AccountDay).where(AccountDay.account == account).order_by(AccountDay.date)).all()
    return [{"date": r.date.isoformat(), "value": r.value} for r in rows]


def summary(broker, account: str, d: Optional[date] = None, marks: Optional[Dict[str, dict]] = None) -> dict:
    """One account's headline numbers. Today's P&L is measured against the last recorded day before `d`."""
    d = d or broker.ds.today()
    v = broker.account_value(account, marks)
    closed = _closed(broker.pengine, account)
    wins = sum(1 for t in closed if (t.pnl_inr or 0) > 0)
    with Session(broker.pengine) as s:
        prev = s.scalars(select(AccountDay).where(AccountDay.account == account, AccountDay.date < d)
                         .order_by(AccountDay.date.desc()).limit(1)).first()
    opening = broker.cfg.capital if v["started"] else 0.0
    base = prev.value if prev else opening
    open_r = 0.0
    for t in broker.open_trades(account):
        m = (marks or {}).get(t.id)
        if m and t.one_r:
            open_r += m["unrealised"] / t.one_r
    return {
        **v,
        "opening_capital": opening,
        "today_pnl": round(v["value"] - base, 2) if v["started"] else 0.0,
        "total_pnl": round(v["value"] - opening, 2) if v["started"] else 0.0,
        "pnl_r": round(sum(t.pnl_r or 0.0 for t in closed), 2),
        "open_r": round(open_r, 2),
        "closed_trades": len(closed),
        "win_rate": wins / len(closed) if closed else None,
        "equity_curve": equity_curve(broker.pengine, account),
    }


def overview(broker, d: Optional[date] = None) -> List[dict]:
    """All five accounts side by side, marked once."""
    marks = broker.mark(persist=False)
    return [summary(broker, acc, d, marks) for acc in broker.cfg.accounts]


def positions(broker, account: str, marks: Optional[Dict[str, dict]] = None) -> List[dict]:
    """Open spreads with legs, lots, entry credit, live cost to close, live P&L and margin blocked."""
    trades = broker.open_trades(account)
    marks = marks if marks is not None else broker.mark(trades, persist=False)
    saved = broker.last_marks([t.id for t in trades if t.id not in marks])
    out = []
    for t in trades:
        m = marks.get(t.id) or saved.get(t.id)
        qty = t.lots * t.lot_size
        ctc = m["cost_to_close"] if m else None
        out.append({
            "id": t.id, "symbol": t.symbol, "direction": t.direction, "expiry": t.expiry.isoformat(),
            "lots": t.lots, "lot_size": t.lot_size, "legs": json.loads(t.legs_json), "credit": t.credit,
            "cost_to_close": ctc, "pnl": round((t.credit - ctc) * qty, 2) if ctc is not None else None,
            "pnl_r": round((t.credit - ctc) * qty / t.one_r, 2) if ctc is not None and t.one_r else None,
            "margin": t.margin, "max_loss": t.max_loss, "one_r": t.one_r, "level": t.level, "book": t.book,
            "setup_tag": t.setup_tag, "entry_ts": t.entry_ts.isoformat(timespec="minutes"),
            "spot": m.get("spot") if m else None, "stale_mark": bool(m and m.get("saved")),
        })
    return out


def funds(pengine, account: str, limit: int = 200) -> List[dict]:
    """The funds ledger, newest first, with running cash balance, used and available margin."""
    with Session(pengine) as s:
        rows = s.scalars(select(LedgerEntry).where(LedgerEntry.account == account)
                         .order_by(LedgerEntry.id.desc()).limit(limit)).all()
    return [{"ts": r.ts.isoformat(timespec="seconds"), "type": r.entry_type, "amount": r.amount,
             "cash_balance": r.cash_balance, "used_margin": r.used_margin,
             "available": round(r.cash_balance - r.used_margin, 2), "trade_id": r.trade_id, "note": r.note}
            for r in rows]
