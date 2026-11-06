"""Owner-maintained CSV overrides: levels_manual.csv, events_override.csv, sectors_manual.csv,
holidays_manual.csv. Bad rows are reported, never silently half-applied."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)


class CSVError(ValueError):
    pass


def _rows(path: Path, required: Tuple[str, ...]) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise CSVError(f"{path.name}: missing column(s) {', '.join(missing)}")
        out = []
        for n, r in enumerate(reader, start=2):
            r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
            if not any(r.values()) or r.get(required[0], "").startswith("#"):
                continue
            r["_line"] = str(n)
            out.append(r)
        return out


def _date(path: Path, r: Dict[str, str], col: str) -> date:
    try:
        return date.fromisoformat(r[col])
    except ValueError as exc:
        raise CSVError(f"{path.name} line {r['_line']}: {col} must be YYYY-MM-DD, got {r[col]!r}") from exc


@dataclass(frozen=True)
class ManualLevel:
    symbol: str
    price: float
    kind: str        # support, resistance, or "" to classify by spot
    note: str


def manual_levels(path: Path) -> List[ManualLevel]:
    out = []
    for r in _rows(path, ("symbol", "price")):
        kind = r.get("kind", "").lower()
        if kind not in ("", "support", "resistance"):
            raise CSVError(f"{path.name} line {r['_line']}: kind must be support or resistance")
        try:
            price = float(r["price"])
        except ValueError as exc:
            raise CSVError(f"{path.name} line {r['_line']}: price must be a number") from exc
        out.append(ManualLevel(r["symbol"].upper(), price, kind, r.get("note", "")))
    return out


def override_events(path: Path) -> List[dict]:
    out = []
    for r in _rows(path, ("symbol", "event_date", "event_type")):
        out.append({"symbol": r["symbol"].upper(), "event_date": _date(path, r, "event_date"),
                    "event_type": r["event_type"].lower() or "manual",
                    "description": r.get("description", ""), "source": "manual"})
    return out


def manual_sectors(path: Path) -> Dict[str, str]:
    return {r["symbol"].upper(): r["sector"] for r in _rows(path, ("symbol", "sector")) if r["sector"]}


def manual_holidays(path: Path) -> List[Tuple[date, str]]:
    return [(_date(path, r, "date"), r.get("description", "")) for r in _rows(path, ("date",))]
