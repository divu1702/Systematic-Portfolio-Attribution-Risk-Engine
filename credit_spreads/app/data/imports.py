"""Owner-supplied event files: CSV exports downloaded from nseindia.com in a browser.

Drop the "Board Meetings" and "Corporate Actions" CSV exports (or any CSV with a symbol column
and a date column) into imports/events/. Column names are matched loosely because NSE renames them
from time to time. Each file counts as an event fetch made when the file was saved (its
modification time), so F2 treats the data as fresh for `events.max_staleness_days` after the
download and fails closed after that.
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .nse import classify_event, parse_nse_date

log = logging.getLogger(__name__)


def _col(headers: List[str], *needles: str, avoid: Tuple[str, ...] = ()) -> Optional[str]:
    low = {h: h.strip().lower() for h in headers}
    for needle in needles:
        for h, l in low.items():
            if needle in l and not any(a in l for a in avoid):
                return h
    return None


def parse_event_csv(path: Path) -> List[dict]:
    """Rows -> events. Recognises board-meeting exports (meeting date + purpose) and corporate-action
    exports (ex-date / record date + subject); anything with a symbol and a date column works."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        sym_c = _col(headers, "symbol")
        purpose_c = _col(headers, "purpose", "subject", "details", "description")
        date_cols = [c for c in (_col(headers, "meeting date", "board meeting"), _col(headers, "ex-date", "ex date"),
                                 _col(headers, "record date")) if c]
        if not date_cols:
            generic = _col(headers, "date", avoid=("submission", "broadcast", "filing", "announcement"))
            date_cols = [generic] if generic else []
        if not sym_c or not date_cols:
            raise ValueError(f"{path.name}: need a symbol column and a date column, got {headers}")
        out = []
        for r in reader:
            sym = (r.get(sym_c) or "").strip().upper()
            text = (r.get(purpose_c) or "") if purpose_c else ""
            if not sym:
                continue
            for c in dict.fromkeys(date_cols):
                d = parse_nse_date(r.get(c))
                if d:
                    kind = classify_event(text)
                    suffix = "" if c == date_cols[0] and "meeting" in c.lower() else f":{c.strip().lower()}"
                    out.append({"symbol": sym, "event_date": d, "event_type": kind + suffix,
                                "description": text[:500], "source": f"import:{path.name}"})
        return out


def scan_imports(folder: Path) -> List[Tuple[Path, datetime, List[dict]]]:
    """(file, saved at, events) for every readable CSV in the folder; unreadable files are logged."""
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob("*.csv")):
        try:
            rows = parse_event_csv(p)
        except Exception as exc:
            log.warning("could not read event import %s: %s", p.name, exc)
            continue
        out.append((p, datetime.fromtimestamp(p.stat().st_mtime), rows))
    return out
