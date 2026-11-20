"""Official end-of-day data from NSE's free files: the F&O and cash-market bhavcopy.

Sources, in order:
1. nsearchives.nseindia.com, requested with an honest client identity (no browser impersonation or
   bot-check workarounds; NSE may refuse automated clients);
2. files the owner downloads in a browser into <imports>/bhavcopy/ (zip or csv, NSE's own names);
3. (in the data service) Kite's end-of-day candles and post-close quotes.

Parsers accept the UDiFF column names NSE has used since July 2024 and the older names, because NSE
renames fields from time to time; unreadable rows are skipped. NSE reports open interest in contracts
(lots) while Kite reports quantity (spec); everything is stored as quantity.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

ARCHIVES = "https://nsearchives.nseindia.com"
FO_URL = ARCHIVES + "/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
CM_URL = ARCHIVES + "/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
HEADERS = {"User-Agent": "credit-spreads-platform/0.1 (personal paper-trading system)",
           "Accept": "text/csv, application/zip, */*"}

ALIASES = {
    "symbol": ("TckrSymb", "SYMBOL"),
    "instrument": ("FinInstrmTp", "INSTRUMENT"),
    "series": ("SctySrs", "SERIES"),
    "expiry": ("XpryDt", "EXPIRY_DT"),
    "strike": ("StrkPric", "STRIKE_PR"),
    "cp": ("OptnTp", "OPTION_TYP"),
    "open": ("OpnPric", "OPEN"),
    "high": ("HghPric", "HIGH"),
    "low": ("LwPric", "LOW"),
    "close": ("ClsPric", "CLOSE"),
    "settle": ("SttlmPric", "SETTLE_PR"),
    "oi": ("OpnIntrst", "OPEN_INT"),
    "volume": ("TtlTradgVol", "TOTTRDQTY", "CONTRACTS"),
    "lot": ("NewBrdLotQty", "MARKET_LOT"),
    "trade_date": ("TradDt", "TIMESTAMP"),
}
STOCK_OPTION_TYPES = {"STO", "OPTSTK"}
STOCK_FUTURE_TYPES = {"STF", "FUTSTK"}


@dataclass
class OfficialEOD:
    """Official end-of-day values for one session."""
    date: date
    source: str
    closes: Dict[str, dict] = field(default_factory=dict)          # symbol -> open/high/low/close/volume
    oi: Dict[Tuple[str, date, float, str], int] = field(default_factory=dict)   # (symbol, expiry, strike, cp)
    settle: Dict[Tuple[str, date, float, str], float] = field(default_factory=dict)
    kite_oi: Dict[str, int] = field(default_factory=dict)         # "NFO:<tradingsymbol>" -> post-close OI


def _num(v) -> Optional[float]:
    try:
        s = str(v).strip().replace(",", "")
        return float(s) if s not in ("", "-", "NA") else None
    except (TypeError, ValueError):
        return None


def _date(v) -> Optional[date]:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d%m%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _rows(text: str) -> Iterable[Dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    for row in reader:
        yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def _get(row: Dict[str, str], name: str) -> str:
    for alias in ALIASES[name]:
        if alias in row:
            return row[alias]
    return ""


def parse_fo(text: str, lots: Optional[Dict[str, int]] = None, oi_unit: str = "lots") -> Tuple[dict, dict]:
    """F&O bhavcopy -> ({(symbol, expiry, strike, cp): OI quantity}, {(...): settlement price}) for stock options.
    `lots` (symbol -> lot size) converts OI from contracts to quantity when the file has no lot column."""
    oi, settle = {}, {}
    for row in _rows(text):
        if _get(row, "instrument").upper() not in STOCK_OPTION_TYPES:
            continue
        sym, exp, k, cp = _get(row, "symbol"), _date(_get(row, "expiry")), _num(_get(row, "strike")), _get(row, "cp")
        o = _num(_get(row, "oi"))
        if not sym or exp is None or k is None or cp not in ("CE", "PE") or o is None:
            continue
        lot = _num(_get(row, "lot")) or (lots or {}).get(sym)
        if oi_unit == "lots":
            if not lot:
                continue                              # cannot convert without the lot size
            o *= lot
        key = (sym, exp, float(k), cp)
        oi[key] = int(round(o))
        s = _num(_get(row, "settle"))
        if s is not None:
            settle[key] = s
    return oi, settle


def parse_cm(text: str) -> Dict[str, dict]:
    """Cash-market bhavcopy -> symbol: {open, high, low, close, volume} for the EQ series."""
    out = {}
    for row in _rows(text):
        series = _get(row, "series").upper()
        if series and series != "EQ":
            continue
        sym = _get(row, "symbol")
        vals = {k: _num(_get(row, k)) for k in ("open", "high", "low", "close", "volume")}
        if sym and vals["close"]:
            out[sym] = vals
    return out


def _unzip(blob: bytes) -> str:
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            text = z.read(name).decode("utf-8", errors="replace")
    else:
        text = blob.decode("utf-8", errors="replace")
    head = text.lstrip("\ufeff \r\n\t")[:200].lower()
    if head.startswith("<") or "," not in head.split("\n", 1)[0]:
        raise RuntimeError("not a bhavcopy CSV (NSE may have returned a web page)")
    return text


def download(url: str, timeout: float = 20.0) -> bytes:
    import requests

    r = requests.get(url, headers=HEADERS, timeout=timeout)
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"NSE returned HTTP {r.status_code} for {url}")
    return r.content


def from_imports(folder: Path, kind: str, d: date) -> Optional[str]:
    """A bhavcopy the owner saved into <imports>/bhavcopy/: NSE's file name for that date, zip or csv."""
    if not folder.exists():
        return None
    ymd = d.strftime("%Y%m%d")
    for p in sorted(folder.iterdir()):
        name = p.name.upper()
        if ymd in name and f"_{kind.upper()}_" in name and p.suffix.lower() in (".zip", ".csv"):
            return _unzip(p.read_bytes())
    return None


def official_eod(d: date, import_dir: Path, lots: Optional[Dict[str, int]] = None, fetch=None,
                 oi_unit: str = "lots") -> Optional[OfficialEOD]:
    """NSE's official values for `d`, from the archive or the import folder; None if neither has them.
    `oi_unit`: what the bhavcopy's OI counts, "lots" or "quantity" (the data repository checks it
    against Kite on its first evening)."""
    fetch = fetch or download
    texts, source = {}, None
    for kind, url in (("FO", FO_URL), ("CM", CM_URL)):
        text = None
        try:
            text = _unzip(fetch(url.format(ymd=d.strftime("%Y%m%d"))))
            source = source or "nse"
        except Exception as exc:
            log.warning("NSE %s bhavcopy for %s not downloaded (%s); trying the import folder", kind, d, exc)
            try:
                text = from_imports(import_dir, kind, d)
            except RuntimeError as bad:
                log.warning("imported %s bhavcopy for %s unreadable: %s", kind, d, bad)
                text = None
            if text is not None:
                source = source or "import"
        if text is not None:
            texts[kind] = text
    if not texts:
        return None
    eod = OfficialEOD(d, source or "import")
    if "CM" in texts:
        eod.closes = parse_cm(texts["CM"])
    if "FO" in texts:
        eod.oi, eod.settle = parse_fo(texts["FO"], lots, oi_unit=oi_unit)
    return eod
