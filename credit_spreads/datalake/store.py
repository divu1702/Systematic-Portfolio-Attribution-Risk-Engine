"""Writing to the repository: datasets, part files, compaction, and the catalog (SQLite)."""

from __future__ import annotations

import itertools
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Dataset:
    name: str
    partitions: Tuple[str, ...]
    key: Tuple[str, ...]                 # identifies a row (duplicates keep the newest file's copy)
    sort: Tuple[str, ...]
    schema: Dict[str, str]               # column -> arrow type name (partition columns excluded)


PRICE = ("open", "high", "low", "close")
DATASETS: Dict[str, Dataset] = {d.name: d for d in (
    Dataset("options_1m", ("underlying", "expiry"), ("tradingsymbol", "ts"), ("tradingsymbol", "ts"),
            {"tradingsymbol": "string", "strike": "float64", "cp": "string", "ts": "timestamp[s]", "open": "float64",
             "high": "float64", "low": "float64", "close": "float64", "volume": "int64", "oi": "int64"}),
    Dataset("futures_1m", ("expiry",), ("tradingsymbol", "ts"), ("underlying", "ts"),
            {"underlying": "string", "tradingsymbol": "string", "ts": "timestamp[s]", "open": "float64",
             "high": "float64", "low": "float64", "close": "float64", "volume": "int64", "oi": "int64"}),
    Dataset("equity_1m", ("month",), ("symbol", "ts"), ("symbol", "ts"),
            {"symbol": "string", "ts": "timestamp[s]", "open": "float64", "high": "float64", "low": "float64",
             "close": "float64", "volume": "int64"}),
    Dataset("quotes_1m", ("date",), ("token", "ts"), ("token", "ts"), {}),          # schema set by the recorder
    Dataset("instruments", ("date",), ("instrument_token",), ("segment", "tradingsymbol"), {}),
    Dataset("bhav_fo", ("date",), ("row",), ("row",), {}),
    Dataset("bhav_cm", ("date",), ("row",), ("row",), {}),
)}
_SAFE = re.compile(r"[\\/:*?\"<>|]")
_counter = itertools.count()
_lock = threading.Lock()


def part_value(v) -> str:
    """A partition value as a directory name: dates as YYYY-MM-DD, path characters removed."""
    if hasattr(v, "isoformat"):
        v = v.isoformat()
    return _SAFE.sub("_", str(v))


def to_table(df: pd.DataFrame, ds: Dataset) -> pa.Table:
    cols = [c for c in df.columns if c not in ds.partitions]
    if ds.schema:
        missing = [c for c in ds.schema if c not in df.columns]
        if missing:
            raise ValueError(f"{ds.name}: missing columns {missing}")
        fields = [pa.field(c, pa.type_for_alias(ds.schema[c])) for c in ds.schema]
        out = {}
        for f in fields:
            s = df[f.name]
            if pa.types.is_timestamp(f.type):
                s = pd.to_datetime(s)
            out[f.name] = pa.array(s, type=f.type, from_pandas=True)
        return pa.table(out)
    return pa.Table.from_pandas(df[cols], preserve_index=False)


class Lake:
    """One repository folder."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(self.root / "catalog.db")

    # ------------------------------------------------------------------ paths
    def partition_dir(self, dataset: str, **values) -> Path:
        ds = DATASETS[dataset]
        p = self.root / dataset
        for k in ds.partitions:
            p = p / f"{k}={part_value(values[k])}"
        return p

    def partitions(self, dataset: str) -> List[Path]:
        base = self.root / dataset
        if not base.exists():
            return []
        depth = len(DATASETS[dataset].partitions)
        return sorted(p for p in base.glob("/".join(["*"] * depth)) if p.is_dir())

    # ------------------------------------------------------------------ writing
    def write(self, dataset: str, df: pd.DataFrame, tag: str = "") -> List[Path]:
        """Write rows as new part files, one per partition. Returns the files written."""
        if df is None or not len(df):
            return []
        ds = DATASETS[dataset]
        out = []
        groups = df.groupby(list(ds.partitions), sort=False, dropna=False) if ds.partitions else [((), df)]
        for keys, g in groups:
            keys = keys if isinstance(keys, tuple) else (keys,)
            folder = self.partition_dir(dataset, **dict(zip(ds.partitions, keys)))
            folder.mkdir(parents=True, exist_ok=True)
            with _lock:
                n = next(_counter)
            name = f"part-{datetime.now():%Y%m%d%H%M%S}-{os.getpid()}-{n:06d}{('-' + tag) if tag else ''}.parquet"
            table = to_table(g.sort_values(list(ds.sort)) if set(ds.sort) <= set(g.columns) else g, ds)
            floats = [f.name for f in table.schema if pa.types.is_floating(f.type)]
            tmp = folder / (name + ".tmp")
            pq.write_table(table, tmp, compression="zstd", compression_level=6,
                           use_byte_stream_split=floats or False)
            os.replace(tmp, folder / name)
            out.append(folder / name)
        return out

    def compact(self, folder: Path, dataset: str) -> Optional[Path]:
        """Merge a partition's files into one data.parquet, newest copy of each key, sorted."""
        import duckdb

        ds = DATASETS[dataset]
        files = sorted(str(p) for p in folder.glob("*.parquet"))
        if len(files) <= 1 and (not files or files[0].endswith("data.parquet")):
            return Path(files[0]) if files else None
        target, tmp = folder / "data.parquet", folder / "data.parquet.tmp"
        keys, order = ", ".join(ds.key), ", ".join(ds.sort)
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT * EXCLUDE (filename) FROM read_parquet(?, hive_partitioning=false, filename=true, "
                f"union_by_name=true) QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY filename DESC) = 1 "
                f"ORDER BY {order}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 6)",
                [files])
        finally:
            con.close()
        os.replace(tmp, target)
        for f in files:
            if not f.endswith("data.parquet"):
                os.remove(f)
        return target

    def size(self, dataset: Optional[str] = None) -> int:
        base = self.root / dataset if dataset else self.root
        return sum(p.stat().st_size for p in base.rglob("*") if p.is_file()) if base.exists() else 0

    def files(self, dataset: str) -> int:
        base = self.root / dataset
        return sum(1 for _ in base.rglob("*.parquet")) if base.exists() else 0


SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    tradingsymbol TEXT PRIMARY KEY, token INTEGER, kind TEXT, underlying TEXT, expiry TEXT, strike REAL, cp TEXT,
    lot INTEGER, last_fetched TEXT, passes INTEGER DEFAULT 0, final INTEGER DEFAULT 0, requests INTEGER DEFAULT 0,
    rows INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_contracts_expiry ON contracts (expiry, final);
CREATE TABLE IF NOT EXISTS series (
    symbol TEXT PRIMARY KEY, token INTEGER, kind TEXT, first_ts TEXT, last_ts TEXT, backfill_done INTEGER DEFAULT 0,
    empty_chunks INTEGER DEFAULT 0, rows INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS nse_files (
    kind TEXT, date TEXT, status TEXT, source TEXT, rows INTEGER, bytes INTEGER, fetched_at TEXT, note TEXT,
    PRIMARY KEY (kind, date));
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, started TEXT, finished TEXT, requests INTEGER, rows INTEGER,
    bytes INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS gaps (dataset TEXT, key TEXT, day TEXT, reason TEXT, PRIMARY KEY (dataset, key, day));
CREATE TABLE IF NOT EXISTS quote_days (
    date TEXT, conn INTEGER, instruments INTEGER, minutes INTEGER, rows INTEGER, restarts INTEGER DEFAULT 0,
    PRIMARY KEY (date, conn));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Catalog:
    """What the repository holds and what is still to fetch (SQLite, one writer at a time)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        with self.connect() as c:
            c.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def execute(self, sql: str, args: Sequence = ()) -> List[tuple]:
        with self._lock, self.connect() as c:
            return c.execute(sql, args).fetchall()

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        with self._lock, self.connect() as c:
            c.executemany(sql, list(rows))

    def get_meta(self, key: str, default=None):
        r = self.execute("SELECT value FROM meta WHERE key=?", (key,))
        return r[0][0] if r else default

    def set_meta(self, key: str, value) -> None:
        self.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def run(self, job: str, started: datetime, finished: datetime, requests: int, rows: int, nbytes: int,
            note: str = "") -> None:
        self.execute("INSERT INTO runs (job, started, finished, requests, rows, bytes, note) VALUES (?,?,?,?,?,?,?)",
                     (job, started.isoformat(timespec="seconds"), finished.isoformat(timespec="seconds"), requests,
                      rows, nbytes, note[:2000]))

    def gap(self, dataset: str, key: str, day: str, reason: str) -> None:
        self.execute("INSERT OR REPLACE INTO gaps VALUES (?,?,?,?)", (dataset, key, day, reason))
