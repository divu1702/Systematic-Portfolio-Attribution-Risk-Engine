"""Real-day fixtures for the spec's two required detector tests.

The build machine cannot reach Kite, so the daily candles for those days are recorded on the owner's
laptop after the first login (`python -m platform_core record-fixtures`) and committed under
tests/fixtures/realdays/. The tests skip, rather than fake data, until the files exist.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

REAL_DAYS: Dict[str, date] = {"ANGELONE": date(2026, 11, 17), "KALYANKJIL": date(2026, 11, 11)}
HISTORY_DAYS = 520          # calendar days before Day 0: >= 250 sessions of swings plus the 20-day volume average


def record(ds, out_dir: Path, days: Dict[str, date] = REAL_DAYS) -> Dict[str, int]:
    """Save each stock's daily cash-market candles around its Day 0. Returns rows saved per symbol."""
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for sym, d0 in days.items():
        tok = ds.equity_token(sym)
        if not tok:
            raise LookupError(f"{sym} is not in today's instrument master")
        rows = ds.broker.historical(tok, datetime.combine(d0 - timedelta(days=HISTORY_DAYS), time(0)),
                                    datetime.combine(d0 + timedelta(days=7), time(23, 59)), "day")
        data = [{"date": (r["date"].date() if hasattr(r["date"], "date") else r["date"]).isoformat(),
                 "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                 "volume": int(r.get("volume") or 0)} for r in rows]
        (out_dir / f"{sym}.json").write_text(json.dumps({"symbol": sym, "day0": d0.isoformat(), "candles": data}),
                                             encoding="utf-8")
        saved[sym] = len(data)
    return saved


def load(out_dir: Path, symbol: str) -> Optional[pd.DataFrame]:
    path = out_dir / f"{symbol}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))["candles"]
    df = pd.DataFrame(data)
    df.index = [date.fromisoformat(x) for x in df.pop("date")]
    return df
