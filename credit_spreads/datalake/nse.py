"""NSE's daily files into the repository (18:40), and the bhavcopy backfill.

Every file is kept exactly as published under nse_raw/<kind>/<year>/. The F&O and cash-market
bhavcopies are also parsed into bhav_fo and bhav_cm: every contract (expired ones too) with open,
high, low, close, settlement price, OI, volume and value, plus every original column as text.
NSE renames columns from time to time and changed formats on 8 July 2024 (UDiFF); both formats are
read. Downloads use an honest client identity at a polite pace; NSE may refuse automated clients,
in which case files saved by hand (or uploaded from the phone) into imports/bhavcopy are used.

Whether NSE's OI counts lots or shares is settled empirically on the first evening with a Kite
session: bhavcopy OI against Kite's (quantity) for the day's most active stock options.
"""

from __future__ import annotations

import csv
import io
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from platform_core import nse_files

from .store import Lake

log = logging.getLogger(__name__)

ARCH = "https://nsearchives.nseindia.com"
UDIFF_START = date(2024, 7, 8)
# kind -> URL template ({dmy} = DDMMYYYY). The first two switch to their legacy names before UDIFF_START.
URLS: Dict[str, str] = {
    "fo_bhav": ARCH + "/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
    "cm_bhav": ARCH + "/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip",
    "cm_delivery": ARCH + "/products/content/sec_bhavdata_full_{dmy}.csv",
    "fo_participant_oi": ARCH + "/content/nsccl/fao_participant_oi_{dmy}.csv",
    "fo_secban": ARCH + "/archives/fo/sec_ban/fo_secban_{dmy}.csv",
    "fo_volatility": ARCH + "/archives/nsccl/volt/FOVOLT_{dmy}.csv",
    "index_close": ARCH + "/content/indices/ind_close_all_{dmy}.csv",
    "fo_lots": ARCH + "/content/fo/fo_mktlots.csv",
}
LEGACY = {
    "fo_bhav": ARCH + "/content/historical/DERIVATIVES/{y}/{mon}/fo{dd}{mon}{y}bhav.csv.zip",
    "cm_bhav": ARCH + "/content/historical/EQUITIES/{y}/{mon}/cm{dd}{mon}{y}bhav.csv.zip",
}
BACKFILL_KINDS = ("fo_bhav", "cm_bhav")


class Missing(Exception):
    """No file for that date (a holiday, or not published yet)."""


class Refused(Exception):
    """NSE refused the request (403, a challenge page, or a timeout)."""


def url_for(kind: str, d: date) -> str:
    if kind in LEGACY and d < UDIFF_START:
        return LEGACY[kind].format(y=d.year, mon=d.strftime("%b").upper(), dd=d.strftime("%d"))
    return URLS[kind].format(ymd=d.strftime("%Y%m%d"), dmy=d.strftime("%d%m%Y"))


def download(url: str, timeout: float = 25.0) -> bytes:
    import requests

    try:
        r = requests.get(url, headers=nse_files.HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise Refused(f"{type(exc).__name__}: {exc}") from exc
    if r.status_code == 404:
        raise Missing(url)
    if r.status_code != 200 or not r.content:
        raise Refused(f"HTTP {r.status_code}")
    return r.content


def text_of(blob: bytes) -> str:
    """CSV text from a zip or a plain CSV; rejects web pages."""
    try:
        return nse_files._unzip(blob)
    except RuntimeError as exc:
        raise Refused(str(exc)) from exc


# ---------------------------------------------------------------------- parsing
FO_COLS = {
    "symbol": ("TckrSymb", "SYMBOL"), "instrument": ("FinInstrmTp", "INSTRUMENT"), "expiry": ("XpryDt", "EXPIRY_DT"),
    "strike": ("StrkPric", "STRIKE_PR"), "cp": ("OptnTp", "OPTION_TYP"), "open": ("OpnPric", "OPEN"),
    "high": ("HghPric", "HIGH"), "low": ("LwPric", "LOW"), "close": ("ClsPric", "CLOSE"),
    "last": ("LastPric",), "prev_close": ("PrvsClsgPric",), "settle": ("SttlmPric", "SETTLE_PR"),
    "underlying_price": ("UndrlygPric",), "oi_raw": ("OpnIntrst", "OPEN_INT"), "chg_oi_raw": ("ChngInOpnIntrst", "CHG_IN_OI"),
    "volume_raw": ("TtlTradgVol", "CONTRACTS"), "value": ("TtlTrfVal", "VAL_INLAKH"), "trades": ("TtlNbOfTxsExctd",),
    "lot": ("NewBrdLotQty",), "trade_date": ("TradDt", "TIMESTAMP"),
}
CM_COLS = {
    "symbol": ("TckrSymb", "SYMBOL"), "series": ("SctySrs", "SERIES"), "isin": ("ISIN",), "open": ("OpnPric", "OPEN"),
    "high": ("HghPric", "HIGH"), "low": ("LwPric", "LOW"), "close": ("ClsPric", "CLOSE"), "last": ("LastPric", "LAST"),
    "prev_close": ("PrvsClsgPric", "PREVCLOSE"), "volume": ("TtlTradgVol", "TOTTRDQTY"),
    "value": ("TtlTrfVal", "TOTTRDVAL"), "trades": ("TtlNbOfTxsExctd", "TOTALTRADES"), "trade_date": ("TradDt", "TIMESTAMP"),
}
NUMERIC = {"strike", "open", "high", "low", "close", "last", "prev_close", "settle", "underlying_price", "oi_raw",
           "chg_oi_raw", "volume_raw", "value", "trades", "lot", "volume"}


def parse(kind: str, text: str, d: date) -> pd.DataFrame:
    """A bhavcopy as rows: normalised typed columns plus every original column as text (src_*)."""
    cols = FO_COLS if kind == "fo_bhav" else CM_COLS
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return pd.DataFrame()
    raw = pd.DataFrame(rows).fillna("")
    raw.columns = [str(c).strip() for c in raw.columns]
    raw = raw.loc[:, [c for c in raw.columns if c]]
    out = pd.DataFrame(index=raw.index)
    for name, aliases in cols.items():
        src = next((a for a in aliases if a in raw.columns), None)
        vals = raw[src].astype(str).str.strip() if src else pd.Series([""] * len(raw), index=raw.index)
        if name in NUMERIC:
            out[name] = pd.to_numeric(vals.str.replace(",", ""), errors="coerce")
        else:
            out[name] = vals.replace({"": None})
    if "expiry" in out:
        out["expiry"] = pd.to_datetime(out["expiry"], errors="coerce", format="mixed", dayfirst=False).dt.date
    out["trade_date"] = d
    out["format"] = "udiff" if any(c in raw.columns for c in ("TckrSymb", "TradDt")) else "legacy"
    for c in raw.columns:
        out["src_" + c] = raw[c].astype(str)
    out["row"] = range(len(out))
    out["date"] = d
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------- jobs
@dataclass
class NseReport:
    saved: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    rows: int = 0
    bytes: int = 0
    oi_unit: Optional[str] = None

    def text(self) -> str:
        s = f"NSE files: {len(self.saved)} saved ({self.rows:,} rows parsed, {self.bytes / 1e6:.1f} MB)"
        if self.missing:
            s += f"; not published or holiday: {', '.join(self.missing[:8])}{'…' if len(self.missing) > 8 else ''}"
        if self.refused:
            s += f"; refused by NSE: {', '.join(self.refused[:8])}{'…' if len(self.refused) > 8 else ''}"
        if self.oi_unit:
            s += f"; NSE OI unit: {self.oi_unit}"
        return s


def _import_dir(p) -> Path:
    return p.ctx.cfg.resolve(p.ctx.cfg.paths.imports) / "bhavcopy"


def get_file(p, kind: str, d: date, fetch: Callable[[str], bytes]) -> Tuple[bytes, str, str]:
    """(bytes, source, file name): NSE first; for bhavcopies, then the import folder."""
    url = url_for(kind, d)
    name = url.rsplit("/", 1)[1]
    try:
        return fetch(url), "nse", name
    except (Missing, Refused) as exc:
        if kind in ("fo_bhav", "cm_bhav"):
            folder = _import_dir(p)
            ymd = d.strftime("%Y%m%d")
            tag = "FO" if kind == "fo_bhav" else "CM"
            if folder.exists():
                for f in sorted(folder.iterdir()):
                    if ymd in f.name and f"_{tag}_" in f.name.upper():
                        return f.read_bytes(), "import", f.name
        raise exc


def save_one(p, lake: Lake, kind: str, d: date, fetch: Callable[[str], bytes], rep: NseReport) -> str:
    """Fetch, keep raw, parse bhavcopies. Returns 'ok', 'missing' or 'refused'."""
    try:
        blob, source, name = get_file(p, kind, d, fetch)
        if kind in ("fo_bhav", "cm_bhav"):
            df = parse(kind, text_of(blob), d)
            if df.empty:
                raise Missing("empty file")
    except Missing as exc:
        lake.catalog.execute("INSERT OR REPLACE INTO nse_files VALUES (?,?,?,?,?,?,?,?)",
                             (kind, str(d), "missing", None, 0, 0, datetime.now().isoformat(timespec="seconds"), str(exc)[:200]))
        rep.missing.append(f"{kind} {d}")
        return "missing"
    except Refused as exc:
        lake.catalog.execute("INSERT OR REPLACE INTO nse_files VALUES (?,?,?,?,?,?,?,?)",
                             (kind, str(d), "refused", None, 0, 0, datetime.now().isoformat(timespec="seconds"), str(exc)[:200]))
        rep.refused.append(f"{kind} {d}")
        return "refused"
    raw = lake.root / "nse_raw" / kind / str(d.year) / (name if kind != "fo_lots" else f"{d:%Y%m%d}_{name}")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(blob)
    rows = 0
    if kind in ("fo_bhav", "cm_bhav"):
        lake.write("bhav_fo" if kind == "fo_bhav" else "bhav_cm", df, tag=source)
        rows = len(df)
    rep.saved.append(f"{kind} {d}")
    rep.rows += rows
    rep.bytes += len(blob)
    lake.catalog.execute("INSERT OR REPLACE INTO nse_files VALUES (?,?,?,?,?,?,?,?)",
                         (kind, str(d), "ok", source, rows, len(blob), datetime.now().isoformat(timespec="seconds"), name))
    return "ok"


def run_daily(p, d: Optional[date] = None, fetch: Optional[Callable[[str], bytes]] = None,
              pause: Optional[float] = None) -> NseReport:
    """Today's files (and any of the last 5 sessions that are still missing)."""
    cfg = p.cfg.lake
    rep = NseReport()
    if not (cfg.enabled and cfg.nse.daily):
        return rep
    fetch = fetch or download
    pause = cfg.nse.pause_s if pause is None else pause
    lake = Lake(p.cfg.resolve(cfg.root))
    cal = p.ds.calendar()
    d = d or p.ds.today()
    days = [d] + [x for x in cal.last_n(cal.prev_trading_day(d), 5)][::-1] if cal.is_trading_day(d) else \
        cal.last_n(cal.prev_trading_day(d), 5)[::-1]
    done = {(k, dd) for k, dd, st in lake.catalog.execute("SELECT kind, date, status FROM nse_files")
            if st == "ok"}
    started = datetime.now()
    for kind in URLS:
        for x in (days if kind != "fo_lots" else [d]):
            if (kind, str(x)) in done:
                continue
            save_one(p, lake, kind, x, fetch, rep)
            if pause:
                _time.sleep(pause)
    try:
        rep.oi_unit = detect_oi_unit(p, lake, d)
    except Exception as exc:                        # needs Kite; tried again tomorrow
        log.info("OI unit check skipped: %s", exc)
    lake.catalog.run("nse_daily", started, datetime.now(), 0, rep.rows, rep.bytes, rep.text())
    return rep


def backfill(p, files: Optional[int] = None, fetch: Optional[Callable[[str], bytes]] = None,
             pause: Optional[float] = None, today: Optional[date] = None) -> NseReport:
    """Past bhavcopies, newest first, `files` downloads per run; stops after 3 refusals in a row."""
    cfg = p.cfg.lake
    rep = NseReport()
    files = cfg.nse.backfill_files_per_run if files is None else files
    if not cfg.enabled or not cfg.nse.backfill_years or not files:
        return rep
    fetch = fetch or download
    pause = cfg.nse.pause_s if pause is None else pause
    lake = Lake(p.cfg.resolve(cfg.root))
    today = today or p.ds.today()
    floor = today - timedelta(days=round(365.25 * cfg.nse.backfill_years))
    final = {(k, dd) for k, dd, st in lake.catalog.execute("SELECT kind, date, status FROM nse_files")
             if st in ("ok", "missing")}
    started, used, refusals = datetime.now(), 0, 0
    d = today - timedelta(days=1)
    while d >= floor and used < files and refusals < 3:
        if d.weekday() < 5:
            for kind in BACKFILL_KINDS:
                if refusals >= 3:
                    break
                if (kind, str(d)) in final or used >= files:
                    continue
                st = save_one(p, lake, kind, d, fetch, rep)
                used += 1
                refusals = refusals + 1 if st == "refused" else 0
                if pause:
                    _time.sleep(pause)
        d -= timedelta(days=1)
    if refusals >= 3:
        rep.refused.append("stopped after 3 refusals in a row")
    lake.catalog.run("nse_backfill", started, datetime.now(), used, rep.rows, rep.bytes, rep.text())
    return rep


def detect_oi_unit(p, lake: Lake, d: date) -> Optional[str]:
    """Compare the bhavcopy's stock-option OI with Kite's (always quantity) for up to 25 active
    contracts. Saves 'lots' or 'quantity' in the catalog and the platform state."""
    from platform_core.db import put_state

    known = lake.catalog.get_meta("nse_oi_unit")
    if known:
        return known
    files = list((lake.root / "bhav_fo" / f"date={d.isoformat()}").glob("*.parquet"))
    if not files:
        return None
    df = pd.concat([pd.read_parquet(f) for f in files])
    df = df[df["instrument"].isin(["STO", "OPTSTK"]) & (df["oi_raw"] > 0)].nlargest(200, "oi_raw")
    master = {}
    from app.data.models import Instrument
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(p.ctx.engine) as s:
        for i in s.scalars(select(Instrument).where(Instrument.date == d, Instrument.instrument_type.in_(("CE", "PE")))):
            master[(i.underlying, i.expiry, round(float(i.strike), 2), i.instrument_type)] = (i.tradingsymbol, i.lot_size)
    pairs = []
    for r in df.itertuples():
        m = master.get((r.symbol, r.expiry, round(float(r.strike), 2), r.cp))
        if m:
            pairs.append((f"NFO:{m[0]}", float(r.oi_raw), m[1]))
        if len(pairs) >= 25:
            break
    if len(pairs) < 5:
        return None
    q = p.ds.quotes([k for k, _, _ in pairs])
    ratios = [(q[k].oi / raw, lot) for k, raw, lot in pairs if k in q and q[k].oi and raw]
    if len(ratios) < 5:
        return None
    near_one = sum(1 for r, _ in ratios if abs(r - 1) < 0.02)
    near_lot = sum(1 for r, lot in ratios if lot and abs(r / lot - 1) < 0.02)
    unit = "quantity" if near_one >= 0.8 * len(ratios) else "lots" if near_lot >= 0.8 * len(ratios) else None
    if unit:
        lake.catalog.set_meta("nse_oi_unit", unit)
        put_state(p.pengine, "nse_oi_unit", unit, p.ds.now())
    return unit
