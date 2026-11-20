"""Reading the repository for backtests (DuckDB over the Parquet files).

    from datalake.api import Lake
    lk = Lake("data/lake")
    lk.options("ANGELONE", expiry="2026-12-29", start="2026-11-17", end="2026-11-18")   # 1-minute candles + OI
    lk.chain_at("ANGELONE", "2026-12-29", "2026-11-17 15:20")                          # every strike at a moment
    lk.futures("ANGELONE", start="2026-11-17"); lk.equity("ANGELONE", "2026-11-01", "2026-11-18")
    lk.quotes("2026-11-17", underlying="ANGELONE")                                      # minute bid/ask
    lk.bhav_fo(symbol="ANGELONE", start="2025-01-01")                                   # NSE end of day
    lk.sql("SELECT underlying, count(*) FROM options_1m GROUP BY 1")                    # ad hoc

Each query keeps one copy of each row (the newest file's) until a partition has been compacted.
The views (options_1m, futures_1m, equity_1m, quotes_1m, instruments, bhav_fo, bhav_cm) read every
file as stored, so between compactions they can repeat a row.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Sequence, Union

import pandas as pd

from .store import DATASETS, part_value

Day = Union[str, date, datetime, None]


def _glob(root: Path, dataset: str, **fixed) -> str:
    parts = [f"{k}={part_value(fixed[k])}" if fixed.get(k) is not None else "*" for k in DATASETS[dataset].partitions]
    return (root / dataset / "/".join(parts) / "*.parquet").as_posix()


def _glob_files(pattern: str) -> List[str]:
    import glob

    return glob.glob(pattern)


def _ts(v: Day, end: bool = False) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    s = str(v)
    if len(s) == 10 and end:
        return s + " 23:59:59"
    return s


class Lake:
    def __init__(self, root: Union[str, Path]):
        import duckdb

        self.root = Path(root)
        self.con = duckdb.connect()
        for name, ds in DATASETS.items():
            pattern = (self.root / name).as_posix() + "/" + "/".join(["*"] * len(ds.partitions)) + "/*.parquet"
            if not _glob_files(pattern):
                continue
            self.con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{pattern}', "
                             f"hive_partitioning=true, union_by_name=true)")

    def sql(self, query: str, params: Sequence = ()) -> pd.DataFrame:
        return self.con.execute(query, list(params)).df()

    def _read(self, dataset: str, where: List[str], params: list, fixed: dict, order: str) -> pd.DataFrame:
        ds = DATASETS[dataset]
        pattern = _glob(self.root, dataset, **fixed)
        if not _glob_files(pattern):
            return pd.DataFrame()
        keys = ", ".join(ds.partitions + ds.key)          # rows in different partitions are different rows
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        q = (f"SELECT * EXCLUDE (filename) FROM read_parquet('{pattern}', hive_partitioning=true, filename=true, "
             f"union_by_name=true){clause} QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY filename DESC) = 1 "
             f"ORDER BY {order}")
        return self.con.execute(q, params).df()

    # ------------------------------------------------------------------ candles
    def options(self, underlying: str, expiry: Day = None, start: Day = None, end: Day = None,
                strikes: Optional[Sequence[float]] = None, cp: Optional[str] = None) -> pd.DataFrame:
        """1-minute candles (OHLC, volume, OI) of one stock's options."""
        where, params = [], []
        if start is not None:
            where.append("ts >= CAST(? AS TIMESTAMP)")
            params.append(_ts(start))
        if end is not None:
            where.append("ts <= CAST(? AS TIMESTAMP)")
            params.append(_ts(end, True))
        if strikes:
            where.append("strike IN (" + ", ".join("?" for _ in strikes) + ")")
            params += [float(k) for k in strikes]
        if cp:
            where.append("cp = ?")
            params.append(cp)
        return self._read("options_1m", where, params, {"underlying": underlying, "expiry": expiry},
                          "expiry, strike, cp, ts")

    def futures(self, underlying: str, expiry: Day = None, start: Day = None, end: Day = None) -> pd.DataFrame:
        where, params = ["underlying = ?"], [underlying]
        if start is not None:
            where.append("ts >= CAST(? AS TIMESTAMP)")
            params.append(_ts(start))
        if end is not None:
            where.append("ts <= CAST(? AS TIMESTAMP)")
            params.append(_ts(end, True))
        return self._read("futures_1m", where, params, {"expiry": expiry}, "expiry, ts")

    def equity(self, symbol: str, start: Day = None, end: Day = None) -> pd.DataFrame:
        """1-minute candles of a stock or an index (e.g. 'NIFTY 50', 'INDIA VIX')."""
        where, params = ["symbol = ?"], [symbol]
        if start is not None:
            where.append("ts >= CAST(? AS TIMESTAMP)")
            params.append(_ts(start))
        if end is not None:
            where.append("ts <= CAST(? AS TIMESTAMP)")
            params.append(_ts(end, True))
        return self._read("equity_1m", where, params, {}, "ts")

    def chain_at(self, underlying: str, expiry: Day, at: Day) -> pd.DataFrame:
        """Each strike's last candle at or before `at` that day: price, volume so far and OI."""
        at_s = _ts(at)
        day = at_s[:10]
        df = self.options(underlying, expiry, start=day, end=at_s)
        if df.empty:
            return df
        last = df.sort_values("ts").groupby(["strike", "cp"]).tail(1)
        vol = df.groupby(["strike", "cp"])["volume"].sum().rename("volume_day")
        return last.merge(vol, on=["strike", "cp"]).sort_values(["strike", "cp"]).reset_index(drop=True)

    # ------------------------------------------------------------------ quotes and end of day
    def quotes(self, day: Day, underlying: Optional[str] = None, kind: Optional[str] = None) -> pd.DataFrame:
        """Minute quotes recorded live on `day` (bid, ask, sizes, last, volume, OI, buy/sell totals)."""
        where, params = [], []
        if underlying:
            where.append("underlying = ?")
            params.append(underlying)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        return self._read("quotes_1m", where, params, {"date": str(day)[:10]}, "token, ts")

    def bhav_fo(self, symbol: Optional[str] = None, start: Day = None, end: Day = None) -> pd.DataFrame:
        """NSE's F&O bhavcopy rows (every contract), parsed; one row per contract per day."""
        return self._bhav("bhav_fo", symbol, start, end)

    def bhav_cm(self, symbol: Optional[str] = None, start: Day = None, end: Day = None) -> pd.DataFrame:
        return self._bhav("bhav_cm", symbol, start, end)

    def _bhav(self, dataset: str, symbol, start, end) -> pd.DataFrame:
        where, params = [], []
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        if start is not None:
            where.append("trade_date >= CAST(? AS DATE)")
            params.append(str(start)[:10])
        if end is not None:
            where.append("trade_date <= CAST(? AS DATE)")
            params.append(str(end)[:10])
        return self._read(dataset, where, params, {}, "trade_date, row")

    def instruments(self, day: Day) -> pd.DataFrame:
        return self._read("instruments", [], [], {"date": str(day)[:10]}, "segment, tradingsymbol")
