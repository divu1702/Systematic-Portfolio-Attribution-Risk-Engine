"""Exchange-side data that Kite does not provide: corporate events, sectors, trading holidays,
and stock-option turnover (for the F1 universe ranking). All sources are free and public.

NSEClient     - nseindia.com JSON APIs and nsearchives.nseindia.com files. NSE expects a
                browser-like client: it sets cookies on the home page, then serves the APIs.
SyntheticNSE  - the same interface backed by the synthetic market, for mock mode.

Field names in NSE responses change occasionally, so parsers accept several spellings and
skip rows they cannot read (logged), rather than guessing.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

NSE_HOME = "https://www.nseindia.com"
API = NSE_HOME + "/api"
ARCHIVES = "https://nsearchives.nseindia.com"
SECTORS_CSV = ARCHIVES + "/content/indices/ind_nifty500list.csv"
BHAVCOPY_UDIFF = ARCHIVES + "/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
# A generic desktop browser identity; NSE rejects obvious script clients.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/",
}


class NSEError(RuntimeError):
    pass


def parse_nse_date(s) -> Optional[date]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    if not s or s in ("-", "NA"):
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y %H:%M:%S", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def classify_event(text: str) -> str:
    t = (text or "").lower()
    for needle, kind in (("result", "results"), ("dividend", "dividend"), ("bonus", "bonus"),
                         ("split", "split"), ("buyback", "buyback"), ("buy back", "buyback"),
                         ("rights", "rights"), ("agm", "agm"), ("annual general", "agm"),
                         ("egm", "egm"), ("record", "record_date"), ("merger", "merger"),
                         ("demerger", "demerger"), ("fund raising", "fund_raising")):
        if needle in t:
            return kind
    return "board_meeting"


def _first(row: dict, *keys):
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def parse_board_meetings(rows: Iterable[dict]) -> List[dict]:
    out = []
    for r in rows or []:
        sym = _first(r, "bm_symbol", "symbol")
        d = parse_nse_date(_first(r, "bm_date", "meetingDate", "date"))
        purpose = _first(r, "bm_purpose", "purpose") or ""
        if not sym or not d:
            log.debug("skipping board-meeting row: %s", r)
            continue
        desc = _first(r, "bm_desc", "desc") or purpose
        out.append({"symbol": sym.strip().upper(), "event_date": d,
                    "event_type": classify_event(f"{purpose} {desc}"),
                    "description": str(desc)[:500], "source": "nse"})
    return out


def parse_corporate_actions(rows: Iterable[dict]) -> List[dict]:
    out = []
    for r in rows or []:
        sym = _first(r, "symbol")
        subject = _first(r, "subject", "purpose") or ""
        if not sym:
            continue
        for field, suffix in (("exDate", "ex_date"), ("recDate", "record_date")):
            d = parse_nse_date(r.get(field))
            if d:
                out.append({"symbol": sym.strip().upper(), "event_date": d,
                            "event_type": f"{classify_event(subject)}:{suffix}",
                            "description": str(subject)[:500], "source": "nse"})
    return out


def parse_event_calendar(rows: Iterable[dict]) -> List[dict]:
    out = []
    for r in rows or []:
        sym = _first(r, "symbol")
        d = parse_nse_date(_first(r, "date", "bm_date"))
        purpose = _first(r, "purpose", "bm_purpose") or ""
        if not sym or not d:
            continue
        out.append({"symbol": sym.strip().upper(), "event_date": d, "event_type": classify_event(purpose),
                    "description": str(_first(r, "bm_desc", "purpose") or "")[:500], "source": "nse"})
    return out


def parse_holidays(payload: dict) -> List[Tuple[date, str]]:
    rows = (payload or {}).get("FO") or (payload or {}).get("CM") or []
    out = []
    for r in rows:
        d = parse_nse_date(_first(r, "tradingDate", "date"))
        if d:
            out.append((d, str(_first(r, "description") or "")))
    return out


def parse_sectors_csv(text: str) -> Dict[str, str]:
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        sym, ind = r.get("Symbol"), r.get("Industry")
        if sym and ind:
            out[sym.upper()] = ind
    return out


def parse_bhavcopy_option_turnover(csv_text: str) -> Dict[str, float]:
    """Stock-option premium turnover per underlying from the NSE F&O UDiFF bhavcopy:
    sum over stock-option rows of settlement (or close) price x traded volume. Used only to
    rank liquidity, so a consistent measure across stocks is what matters."""
    out: Dict[str, float] = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        kind = r.get("FinInstrmTp") or r.get("INSTRUMENT")
        if kind not in ("STO", "OPTSTK"):
            continue
        sym = (r.get("TckrSymb") or r.get("SYMBOL") or "").upper()
        try:
            px = float(r.get("SttlmPric") or r.get("ClsPric") or r.get("SETTLE_PR") or r.get("CLOSE") or 0)
            vol = float(r.get("TtlTradgVol") or r.get("CONTRACTS") or 0)
        except ValueError:
            continue
        if sym:
            out[sym] = out.get(sym, 0.0) + px * vol
    return out


class NSEClient:
    def __init__(self, timeout: float = 20.0, session=None):
        import requests

        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update(HEADERS)
        self._warm = False

    def _warmup(self) -> None:
        if not self._warm:
            self.s.get(NSE_HOME, timeout=self.timeout)
            self._warm = True

    def _get_json(self, url: str, params: dict):
        self._warmup()
        r = self.s.get(url, params=params, timeout=self.timeout)
        if r.status_code in (401, 403):
            self._warm = False           # cookies expired: warm up once and retry
            self._warmup()
            r = self.s.get(url, params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise NSEError(f"{url} returned HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise NSEError(f"{url} did not return JSON") from exc

    def events(self, start: date, end: date) -> List[dict]:
        p = {"index": "equities", "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}
        out = parse_board_meetings(self._get_json(f"{API}/corporate-board-meetings", p))
        out += parse_corporate_actions(self._get_json(f"{API}/corporates-corporateActions", p))
        try:
            out += parse_event_calendar(self._get_json(f"{API}/event-calendar", p))
        except NSEError as exc:     # supplementary; board meetings already cover results
            log.warning("event calendar unavailable: %s", exc)
        return out

    def sectors(self) -> Dict[str, str]:
        r = self.s.get(SECTORS_CSV, timeout=self.timeout)
        if r.status_code != 200:
            raise NSEError(f"sector list returned HTTP {r.status_code}")
        return parse_sectors_csv(r.text)

    def holidays(self) -> List[Tuple[date, str]]:
        return parse_holidays(self._get_json(f"{API}/holiday-master", {"type": "trading"}))

    def option_turnover(self, d: date) -> Dict[str, float]:
        url = BHAVCOPY_UDIFF.format(ymd=d.strftime("%Y%m%d"))
        r = self.s.get(url, timeout=self.timeout)
        if r.status_code == 404:
            return {}                  # holiday, or not published yet
        if r.status_code != 200:
            raise NSEError(f"bhavcopy {d} returned HTTP {r.status_code}")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            return parse_bhavcopy_option_turnover(z.read(name).decode("utf-8", errors="replace"))


class SyntheticNSE:
    def __init__(self, market, clock):
        self.market, self.clock = market, clock
        self.fail = False              # tests flip this to exercise fail-closed paths

    def _check(self):
        if self.fail:
            raise NSEError("synthetic NSE outage")

    def events(self, start: date, end: date) -> List[dict]:
        self._check()
        today = self.clock.today()
        rows = self.market.announced_events(today, ahead_days=(end - today).days,
                                            behind_days=(today - start).days)
        return [{"symbol": r["symbol"], "event_date": r["date"], "event_type": "results",
                 "description": r["purpose"], "source": "nse"} for r in rows]

    def sectors(self) -> Dict[str, str]:
        self._check()
        return self.market.sectors()

    def holidays(self) -> List[Tuple[date, str]]:
        self._check()
        return [(d, "Synthetic holiday") for d in self.market.holidays()]

    def option_turnover(self, d: date) -> Dict[str, float]:
        self._check()
        if d > self.clock.today():
            return {}
        return self.market.option_turnover(d)


def make_nse_source(ctx):
    if ctx.is_mock:
        return SyntheticNSE(ctx.broker.market, ctx.clock)
    return NSEClient()
