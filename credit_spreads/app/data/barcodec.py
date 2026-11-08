"""Compact storage for intraday data: one zlib-compressed, column-major blob per contract-day.

Intraday candles (1-minute by default) and intraday quote snapshots would be tens of millions of
rows a month as plain SQLite rows (~100 bytes each). Stored as one blob per contract and day, with
times and prices delta-encoded, they take roughly a tenth of that, and the blob decodes straight
into numpy arrays. Uses only the standard library and numpy.

Times are minutes after midnight IST; prices are stored in paise (exact for NSE's 5-paise ticks).
"""

from __future__ import annotations

import zlib
from datetime import date, datetime, timedelta
from typing import Dict, List, Sequence, Tuple

import numpy as np

VERSION = 1

# (name, dtype, kind): "time" minutes, "price" rupees -> paise, "int" plain integers.
# delta: stored as differences from the previous value (small numbers compress well).
CANDLE_COLS: Tuple[Tuple[str, str, bool], ...] = (
    ("minute", "time", True), ("close", "price", True), ("open", "price", False),
    ("high", "price", False), ("low", "price", False), ("volume", "int", False), ("oi", "int", True))
QUOTE_COLS: Tuple[Tuple[str, str, bool], ...] = (
    ("minute", "time", True), ("bid", "price", True), ("ask", "price", True), ("last", "price", True),
    ("bid_qty", "int", False), ("ask_qty", "int", False), ("bid_depth", "int", False),
    ("ask_depth", "int", False), ("volume", "int", True), ("oi", "int", True),
    ("buy_total", "int", False), ("sell_total", "int", False), ("last_qty", "int", False))
_SCHEMAS = {b"C": CANDLE_COLS, b"Q": QUOTE_COLS}


def _column(values: Sequence, kind: str) -> np.ndarray:
    if kind == "price":
        return np.rint(np.asarray(values, dtype=np.float64) * 100).astype(np.int64)
    return np.asarray(values, dtype=np.int64)


def encode(columns: Dict[str, Sequence], schema: bytes = b"C") -> bytes:
    """Columns of equal length (rows sorted by minute) -> blob. Open/high/low are stored relative
    to the close, which keeps them near zero."""
    cols = _SCHEMAS[schema]
    n = len(columns["minute"])
    arrays = {name: _column(columns[name], kind) for name, kind, _ in cols}
    parts = []
    for name, kind, delta in cols:
        a = arrays[name]
        if schema == b"C" and name in ("open", "high", "low"):
            a = a - arrays["close"]
        if delta and n:
            a = np.diff(a, prepend=0)
        parts.append(a.astype("<i8").tobytes())
    head = schema + bytes([VERSION]) + n.to_bytes(4, "little")
    return head + zlib.compress(b"".join(parts), 6)


def decode(blob: bytes) -> Dict[str, np.ndarray]:
    """Blob -> {column: numpy array}; prices back in rupees (float), the rest int64."""
    schema, version, n = blob[:1], blob[1], int.from_bytes(blob[2:6], "little")
    if version != VERSION or schema not in _SCHEMAS:
        raise ValueError(f"unknown intraday blob format {schema!r} v{version}")
    cols = _SCHEMAS[schema]
    raw = np.frombuffer(zlib.decompress(blob[6:]), dtype="<i8")
    out: Dict[str, np.ndarray] = {}
    for i, (name, kind, delta) in enumerate(cols):
        a = raw[i * n:(i + 1) * n].astype(np.int64)
        out[name] = np.cumsum(a) if delta else a
    if schema == b"C":
        for name in ("open", "high", "low"):
            out[name] = out[name] + out["close"]
    for name, kind, _ in cols:
        if kind == "price":
            out[name] = out[name] / 100.0
    return out


def candle_columns(candles: List[dict]) -> Dict[str, list]:
    """Kite-style candle dicts (one day, any order) -> columns for encode(), sorted by time."""
    rows = sorted(candles, key=lambda c: c["date"])
    return {"minute": [c["date"].hour * 60 + c["date"].minute for c in rows],
            "open": [c["open"] for c in rows], "high": [c["high"] for c in rows],
            "low": [c["low"] for c in rows], "close": [c["close"] for c in rows],
            "volume": [int(c.get("volume") or 0) for c in rows], "oi": [int(c.get("oi") or 0) for c in rows]}


def by_day(candles: List[dict]) -> Dict[date, List[dict]]:
    out: Dict[date, List[dict]] = {}
    for c in candles:
        d = c["date"].date() if isinstance(c["date"], datetime) else c["date"]
        out.setdefault(d, []).append(c)
    return out


def daily_from(candles: List[dict]) -> dict:
    """One day's candles -> a daily candle: traded minutes give open/high/low/close and volume;
    open interest is the day's last reading."""
    rows = sorted(candles, key=lambda c: c["date"])
    traded = [c for c in rows if (c.get("volume") or 0) > 0] or rows
    return {"open": traded[0]["open"], "high": max(c["high"] for c in traded),
            "low": min(c["low"] for c in traded), "close": traded[-1]["close"],
            "volume": int(sum(int(c.get("volume") or 0) for c in rows)), "oi": int(rows[-1].get("oi") or 0)}


def at_minute(cols: Dict[str, np.ndarray], minute: int, window: int) -> int:
    """Index of the row nearest `minute` within +-window minutes (earlier wins a tie), or -1."""
    m = cols["minute"]
    if not len(m):
        return -1
    i = int(np.searchsorted(m, minute, side="right")) - 1          # last row at or before
    best, gap = -1, window + 1
    for j in (i, i + 1):
        if 0 <= j < len(m):
            g = abs(int(m[j]) - minute)
            if g < gap or (g == gap and j == i):
                best, gap = j, g
    return best if gap <= window else -1


def minute_to_time(d: date, minute: int) -> datetime:
    return datetime.combine(d, datetime.min.time()) + timedelta(minutes=int(minute))
