"""Setups, levels and their sessions in the platform database (tables `setups` and `levels`).

A setup row keeps the state machine's working values in `machine_json`, so the 15:20 run can be
restarted at any point and resume exactly where it stopped. Every session's evaluation is appended to
`sessions_json` (close, beyond the level, OI test values, which books entered).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_core.models import LevelRow, SetupRow

from .analytics import ZoneOI
from .state import ACTIVE, Machine, Step


def _zone_dict(z: Optional[ZoneOI]) -> Optional[dict]:
    return None if z is None else {"protected": z.protected, "other": z.other, "calls": z.calls, "puts": z.puts,
                                   "chain_total": z.chain_total, "missing": list(z.missing)}


def _zone(d: Optional[dict]) -> Optional[ZoneOI]:
    return None if not d else ZoneOI(d["protected"], d["other"], d["calls"], d["puts"], d["chain_total"],
                                      list(d.get("missing") or []))


def machine_to_json(m: Machine) -> str:
    d = asdict(m)
    d["day0"] = m.day0.isoformat()
    d["retest_date"] = m.retest_date.isoformat() if m.retest_date else None
    d["day0_zone"] = _zone_dict(m.day0_zone)
    return json.dumps(d)


def machine_from_row(row: SetupRow) -> Machine:
    d = json.loads(row.machine_json)
    d["day0"] = date.fromisoformat(d["day0"])
    d["retest_date"] = date.fromisoformat(d["retest_date"]) if d.get("retest_date") else None
    d["day0_zone"] = _zone(d.get("day0_zone"))
    return Machine(**d)


def active(pengine, symbol: Optional[str] = None) -> List[SetupRow]:
    q = select(SetupRow).where(SetupRow.state.in_(ACTIVE))
    if symbol:
        q = q.where(SetupRow.symbol == symbol)
    with Session(pengine) as s:
        return list(s.scalars(q.order_by(SetupRow.id)))


def get(pengine, setup_id: int) -> Optional[SetupRow]:
    with Session(pengine) as s:
        return s.get(SetupRow, setup_id)


def duplicate(pengine, symbol: str, setup: int, direction: str, level: float) -> Optional[SetupRow]:
    """An active setup of the same kind on the same level (one arm per level until it finishes)."""
    for r in active(pengine, symbol):
        if r.setup == setup and r.direction == direction and abs(r.level - level) < 0.005 * max(level, 1):
            return r
    return None


def create(pengine, symbol: str, arm, machine: Machine, *, expiry: Optional[date], zone: Dict[str, list],
           day0_oi: Dict[str, float], signals: dict, now: datetime) -> int:
    z = machine.day0_zone
    failed = {} if arm.vol_ok else {"volume": {"multiple": arm.volume_mult, "needed": arm.min_vol_mult}}
    row = SetupRow(symbol=symbol, setup=arm.setup, direction=arm.direction, state=machine.state, level=arm.level,
                   day0_date=arm.day0, route=None, window_start=None, expiry=expiry, zone_json=json.dumps(zone),
                   day0_oi_json=json.dumps(day0_oi), zone_oi_share=z.share if z else None,
                   local_pcr=z.local_pcr if z else None, sessions_json="[]", volume_mult=arm.volume_mult,
                   failed_filters=json.dumps(failed) if failed else None, signals_json=json.dumps(signals, default=str),
                   note=arm.name, created_at=now, updated_at=now, machine_json=machine_to_json(machine),
                   last_session=arm.day0)
    with Session(pengine) as s:
        s.add(row)
        s.flush()
        s.add(LevelRow(date=arm.day0, symbol=symbol, setup=arm.setup, level=arm.level, direction=arm.direction,
                       day0_date=arm.day0, volume_mult=arm.volume_mult, vwap_dist_atr=signals.get("vwap_dist_atr"),
                       hvn_dist_atr=signals.get("hvn_dist_atr"), breakout_low=arm.breakout_extreme, setup_id=row.id))
        s.commit()
        return row.id


def save_step(pengine, setup_id: int, m: Machine, st: Step, session_date: date, now: datetime) -> None:
    with Session(pengine) as s:
        row = s.get(SetupRow, setup_id)
        sessions = json.loads(row.sessions_json or "[]")
        sessions.append(st.record)
        row.sessions_json = json.dumps(sessions, default=str)
        row.state, row.route = st.state, m.route
        if m.route == "A" and m.retest_date:
            row.window_start = m.retest_date
        elif row.window_start is None:
            row.window_start = m.day0
        if st.failed_filters:
            failed = json.loads(row.failed_filters or "{}")
            failed.update({k: v for k, v in st.failed_filters.items() if k != "volume"})
            failed.setdefault("sessions", []).append({"date": session_date.isoformat(), **st.failed_filters})
            row.failed_filters = json.dumps(failed, default=str)
        if st.note:
            row.note = f"{row.note}; {st.note}" if row.note else st.note
        row.machine_json, row.last_session, row.updated_at = machine_to_json(m), session_date, now
        s.commit()


def link_trade(pengine, setup_id: int, book: str, trade_id: str) -> None:
    with Session(pengine) as s:
        row = s.get(SetupRow, setup_id)
        if book == "strict":
            row.strict_trade_id = trade_id
        else:
            row.research_trade_id = trade_id
        s.commit()


def add_note(pengine, setup_id: int, note: str, now: datetime) -> None:
    with Session(pengine) as s:
        row = s.get(SetupRow, setup_id)
        row.note = f"{row.note}; {note}" if row.note else note
        row.updated_at = now
        s.commit()


def on_date(pengine, d: date) -> List[SetupRow]:
    """Setups armed on `d`, still active, or that changed state on `d` (for the daily report)."""
    with Session(pengine) as s:
        rows = s.scalars(select(SetupRow).where(
            (SetupRow.day0_date == d) | (SetupRow.state.in_(ACTIVE)) | (SetupRow.last_session == d))
            .order_by(SetupRow.id)).all()
    return list(rows)
