"""Trade proposals for one-tap approval (live mode). A proposal expires if not answered in
`live.approval_timeout_min`; only a pending, unexpired proposal can be approved."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import List, Optional, Tuple

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from ..data.models import Proposal


def create_proposal(ctx, symbol: str, side: str, summary: str, payload: dict) -> int:
    now = ctx.clock.now()
    with ctx.engine.begin() as conn:
        pid = conn.execute(insert(Proposal).values(
            ts=now, symbol=symbol, side=side, summary=summary, payload_json=json.dumps(payload, default=str),
            status="pending", expires_at=now + timedelta(minutes=ctx.cfg.live.approval_timeout_min))
        ).inserted_primary_key[0]
    ctx.notifier.send(f"PROPOSAL #{pid}: {summary}\nReply /approve {pid} or /reject {pid} "
                      f"(expires in {ctx.cfg.live.approval_timeout_min} min)", "proposal")
    return int(pid)


def expire_stale(ctx) -> int:
    with ctx.engine.begin() as conn:
        return conn.execute(update(Proposal).where(Proposal.status == "pending",
                                                   Proposal.expires_at < ctx.clock.now())
                            .values(status="expired", decided_at=ctx.clock.now())).rowcount


def pending(ctx) -> List[Proposal]:
    expire_stale(ctx)
    with Session(ctx.engine) as s:
        return list(s.scalars(select(Proposal).where(Proposal.status == "pending").order_by(Proposal.id)))


def decide(ctx, pid: int, approve: bool) -> Tuple[bool, str]:
    expire_stale(ctx)
    with Session(ctx.engine) as s:
        p: Optional[Proposal] = s.get(Proposal, pid)
        if p is None:
            return False, f"No proposal #{pid}."
        if p.status != "pending":
            return False, f"Proposal #{pid} is already {p.status}."
        p.status = "approved" if approve else "rejected"
        p.decided_at = ctx.clock.now()
        s.commit()
    return True, f"Proposal #{pid} {'approved' if approve else 'rejected'}."
