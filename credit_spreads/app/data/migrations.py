"""Forward-only SQL migrations.

Each file in app/data/migrations/ named NNNN_description.sql is one migration. Applied
versions are recorded in `schema_migrations`. Each migration runs inside one transaction,
so a failure leaves the database at the previous version.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List

from ..clock import now_ist

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT     NOT NULL,
    applied_at DATETIME NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    found = []
    for p in sorted(directory.glob("*.sql")):
        m = _NAME.match(p.name)
        if not m:
            raise ValueError(f"bad migration filename {p.name!r}; expected NNNN_name.sql")
        found.append(Migration(int(m.group(1)), m.group(2), p))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise ValueError(f"migration versions must be 1..N with no gaps, got {versions}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    (v,) = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(v)


def pending(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    have = current_version(conn)
    return [m for m in discover(directory) if m.version > have]


def upgrade(db_path: Path, directory: Path = MIGRATIONS_DIR) -> List[Migration]:
    """Apply all pending migrations. Returns the migrations that were applied."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    applied: List[Migration] = []
    try:
        conn.executescript(_BOOKKEEPING)
        for m in pending(conn, directory):
            stamp = now_ist().isoformat(sep=" ")
            script = (
                "BEGIN;\n"
                f"{m.sql()}\n;\n"
                f"INSERT INTO schema_migrations (version, name, applied_at) "
                f"VALUES ({m.version}, '{m.name}', '{stamp}');\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            log.info("applied migration %04d_%s", m.version, m.name)
            applied.append(m)
    finally:
        conn.close()
    return applied
