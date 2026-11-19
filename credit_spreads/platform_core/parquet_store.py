"""Option chain snapshots as Parquet (spec: chain_snap). One file per snapshot, partitioned by date:
<paths.chain_snap>/date=YYYY-MM-DD/HHMMSS.parquet, columns timestamp, symbol, expiry, strike, type, bid,
ask, last_price, oi, volume, iv. OI is total quantity, as Kite reports it."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

COLUMNS = ["timestamp", "symbol", "expiry", "strike", "type", "bid", "ask", "last_price", "oi", "volume", "iv"]


def write_snapshot(root: Path, ts: datetime, rows: Iterable[dict]) -> Optional[Path]:
    rows = list(rows)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    for c in COLUMNS:
        if c not in df:
            df[c] = None
    df = df[COLUMNS]
    df["timestamp"] = pd.Timestamp(ts)
    df["expiry"] = pd.to_datetime(df["expiry"]).dt.date.astype(str)
    df["strike"] = df["strike"].astype(float)
    for c in ("bid", "ask", "last_price", "iv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ("oi", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
    folder = root / f"date={ts.date().isoformat()}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ts:%H%M%S}.parquet"
    df.to_parquet(path, index=False)
    return path


def read_snapshots(root: Path, d: date, symbol: Optional[str] = None) -> pd.DataFrame:
    folder = root / f"date={d.isoformat()}"
    files: List[Path] = sorted(folder.glob("*.parquet")) if folder.exists() else []
    if not files:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df[df.symbol == symbol].reset_index(drop=True) if symbol else df
