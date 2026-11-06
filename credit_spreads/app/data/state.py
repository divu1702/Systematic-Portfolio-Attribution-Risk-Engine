"""Small persistent key/value state: kill switch, drawdown pause, equity peak, last runs."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from ..clock import now_ist
from .models import SystemState

KILLED = "killed"                 # value: reason
DD_PAUSED = "dd_paused"           # value: reason
EQUITY_PEAK = "equity_peak"


def get(engine: Engine, key: str, default: Any = None) -> Any:
    with engine.connect() as conn:
        v = conn.execute(select(SystemState.value).where(SystemState.key == key)).scalar()
    return default if v is None else json.loads(v)


def put(engine: Engine, key: str, value: Any, ts=None) -> None:
    stmt = insert(SystemState).values(key=key, value=json.dumps(value, default=str), updated_at=ts or now_ist())
    stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                      set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at})
    with engine.begin() as conn:
        conn.execute(stmt)


def clear(engine: Engine, key: str) -> None:
    with engine.begin() as conn:
        conn.execute(SystemState.__table__.delete().where(SystemState.key == key))


def flag(engine: Engine, key: str) -> Optional[str]:
    """Truthy reason string if a flag such as KILLED or DD_PAUSED is set."""
    v = get(engine, key)
    return v or None
