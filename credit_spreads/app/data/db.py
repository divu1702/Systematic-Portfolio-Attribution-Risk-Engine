"""SQLite engine and session helpers. All state lives in one database file."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import migrations
from .models import Base


def make_engine(db_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")   # scanner and monitor may run concurrently
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def init_db(db_path: Path) -> Engine:
    """Bring the schema up to date and return an engine."""
    migrations.upgrade(db_path)
    return make_engine(db_path)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional session: commit on success, roll back on error."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_status(db_path: Path) -> Dict[str, object]:
    """Read-only status for `doctor`. Never creates or modifies the database."""
    latest = len(migrations.discover())
    if not db_path.exists():
        return {"exists": False, "version": 0, "latest": latest, "tables": {}}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        version = migrations.current_version(conn)
        tables: Dict[str, int] = {}
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in Base.metadata.tables:
            if name in existing:
                (n,) = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
                tables[name] = int(n)
            else:
                tables[name] = -1
    finally:
        conn.close()
    return {"exists": True, "version": version, "latest": latest, "tables": tables,
            "size_kb": db_path.stat().st_size / 1024}
