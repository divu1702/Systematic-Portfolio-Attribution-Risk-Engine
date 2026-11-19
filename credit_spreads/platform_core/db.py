"""Platform database: SQLite with forward-only SQL migrations (platform_core/migrations), plus a
tiny key/value store for platform state (last reminders, PIN lockout)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.sqlite import insert

from app.data import migrations
from app.data.db import make_engine

from .models import PlatformState

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def init_platform_db(db_path: Path) -> Engine:
    """Bring the platform schema up to date and return an engine."""
    migrations.upgrade(db_path, MIGRATIONS_DIR)
    return make_engine(db_path)


def get_state(engine: Engine, key: str, default: Any = None) -> Any:
    with engine.connect() as conn:
        v = conn.execute(select(PlatformState.value).where(PlatformState.key == key)).scalar()
    return default if v is None else json.loads(v)


def put_state(engine: Engine, key: str, value: Any, ts: datetime) -> None:
    stmt = insert(PlatformState).values(key=key, value=json.dumps(value, default=str), updated_at=ts)
    stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                      set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at})
    with engine.begin() as conn:
        conn.execute(stmt)
