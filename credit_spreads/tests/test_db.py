import sqlite3

import pytest
from sqlalchemy import inspect

from app.clock import now_ist
from app.data import migrations
from app.data.db import db_status, init_db, session_scope
from app.data.models import Base, Candidate, Trade


def test_fresh_database_is_migrated_and_idempotent(tmp_path):
    db = tmp_path / "t.db"
    applied = migrations.upgrade(db)
    assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6]
    assert migrations.upgrade(db) == []
    st = db_status(db)
    assert st["version"] == st["latest"] == 6
    assert all(n == 0 for n in st["tables"].values())


def test_migrated_schema_matches_orm_models(tmp_path):
    """The SQL migrations own the schema; the ORM must mirror them exactly."""
    engine = init_db(tmp_path / "t.db")
    insp = inspect(engine)
    db_tables = set(insp.get_table_names()) - {"schema_migrations"}
    assert db_tables == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        db_cols = {c["name"]: c for c in insp.get_columns(name)}
        assert set(db_cols) == set(table.columns.keys()), name
        pk = set(insp.get_pk_constraint(name)["constrained_columns"])
        assert pk == {c.name for c in table.primary_key.columns}, name
        for col in table.columns:
            if not col.primary_key:
                assert db_cols[col.name]["nullable"] == col.nullable, f"{name}.{col.name}"


def test_orm_roundtrip_and_constraints(tmp_path):
    engine = init_db(tmp_path / "t.db")
    ts = now_ist()
    with session_scope(engine) as s:
        s.add(Candidate(ts=ts, scan_id="scan-1", symbol="KALYANKJIL", side="bear_call",
                        short_k=600, long_k=640, width=40, credit_nat=11, f1_pass=True,
                        f2_pass=False, reject_reason="F2"))
    with session_scope(engine) as s:
        c = s.query(Candidate).one()
        assert c.f2_pass is False and c.f3_pass is None and c.accepted is False

    # CHECK constraints reject bad enum values.
    with pytest.raises(Exception):
        with session_scope(engine) as s:
            s.add(Trade(trade_id="t1", mode="real", symbol="X", side="bull_put", expiry=ts.date(),
                        legs_json="[]", lots=1, lot_size=1, entry_ts=ts, entry_credit=1, max_loss=1))


def test_failed_migration_rolls_back(tmp_path):
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_ok.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mig_dir / "0002_bad.sql").write_text("CREATE TABLE b (x INTEGER); NOT VALID SQL;")
    db = tmp_path / "t.db"
    with pytest.raises(sqlite3.Error):
        migrations.upgrade(db, mig_dir)
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert migrations.current_version(conn) == 1
    assert "a" in names and "b" not in names
    conn.close()


def test_migration_numbering_must_be_contiguous(tmp_path):
    (tmp_path / "0001_a.sql").write_text("")
    (tmp_path / "0003_c.sql").write_text("")
    with pytest.raises(ValueError, match="no gaps"):
        migrations.discover(tmp_path)


def test_status_does_not_create_database(tmp_path):
    db = tmp_path / "missing.db"
    assert db_status(db)["exists"] is False
    assert not db.exists()
