"""Loader for DoltHub's free US market data (https://www.dolthub.com/repositories/post-no-preference):

- options/option_chain   end-of-day bid, ask and IV per strike, ~3 expiries x ~25 strikes per day
- stocks/ohlcv, stocks/split   daily bars and splits
- earnings/earnings_calendar   earnings dates with before/after-market timing

DoltHub serves a public SQL API meant for programmatic use. Results are capped at 1,000 rows per
query, so chains are fetched a few days at a time and a capped batch is split and retried.
Loading is resumable: days already fetched for a symbol are skipped.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .store import connect

log = logging.getLogger(__name__)

API = "https://www.dolthub.com/api/v1alpha1/post-no-preference/{repo}/master"
USER_AGENT = "credit-spreads-backtest/0.1 (research backtest; github.com/divu1702)"
BATCH_DAYS = 10
MAX_EXPIRY_DAYS = 40      # the scanner never needs expiries further out than dte_max (35)


class DoltHubError(RuntimeError):
    pass


_SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,9}$")


def check_symbols(symbols: Sequence[str]) -> List[str]:
    """Symbols go into SQL text, so only plain tickers are accepted."""
    bad = [s for s in symbols if not _SYMBOL.match(s)]
    if bad:
        raise ValueError(f"invalid ticker(s): {bad}")
    return list(symbols)


class DoltHubClient:
    def __init__(self, timeout: float = 120.0, min_interval: float = 0.25):
        import requests

        self._local = threading.local()
        self._requests = requests
        self.timeout = timeout
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self._local.s = s
        return s

    def _pace(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def query(self, repo: str, sql: str) -> dict:
        last_exc = None
        for attempt in range(4):
            self._pace()
            try:
                r = self._session().get(API.format(repo=repo), params={"q": sql}, timeout=self.timeout)
                if r.status_code == 200:
                    d = r.json()
                    status = d.get("query_execution_status")
                    if status in ("Success", "RowLimit"):
                        return d
                    last_exc = DoltHubError(f"{status}: {d.get('query_execution_message', '')[:200]}")
                else:
                    last_exc = DoltHubError(f"HTTP {r.status_code}")
            except Exception as exc:          # network hiccup: back off and retry
                last_exc = exc
            time.sleep(2 ** attempt)
        raise DoltHubError(f"query failed after retries: {last_exc}")


def _f(x) -> Optional[float]:
    return None if x in (None, "") else float(x)


def _weekdays(a: date, b: date) -> List[str]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(str(d))
        d += timedelta(days=1)
    return out


def _fetch_bars(client: DoltHubClient, sym: str, days: List[str]) -> List[dict]:
    # An explicit date list uses the (date, symbol) primary key; a date range scans every stock
    # and times out on DoltHub.
    in_list = ",".join(f"'{d}'" for d in days)
    return client.query("stocks", "SELECT date, open, high, low, close, volume FROM ohlcv WHERE "
                                  f"act_symbol='{sym}' AND date IN ({in_list})").get("rows", [])


def load_reference(client: DoltHubClient, store_path: Path, symbols: Sequence[str], bars_from: date,
                   to: date, workers: int = 3) -> None:
    """Bars (in 60-weekday batches), earnings dates and splits for each symbol."""
    symbols = check_symbols(symbols)
    conn = connect(store_path)
    have = {(r[0], r[1]) for r in conn.execute("SELECT symbol, date FROM bars")}
    weekdays = _weekdays(bars_from, to)
    tasks = []
    for sym in symbols:
        todo = [d for d in weekdays if (sym, d) not in have]
        for i in range(0, len(todo), 60):
            tasks.append((sym, todo[i:i + 60]))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_fetch_bars, client, sym, days): sym for sym, days in tasks}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                log.warning("bars batch for %s failed: %s (will retry on the next run)", sym, exc)
                continue
            conn.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)",
                             [(sym, r["date"], _f(r["open"]), _f(r["high"]), _f(r["low"]), _f(r["close"]),
                               int(float(r["volume"] or 0))) for r in rows])
            conn.commit()
    for sym in symbols:
        e = client.query("earnings", f"SELECT date, `when` FROM earnings_calendar WHERE act_symbol='{sym}'")
        conn.executemany("INSERT OR REPLACE INTO earnings VALUES (?,?,?)",
                         [(sym, r["date"], r.get("when")) for r in e.get("rows", [])])
        sp = client.query("stocks", f"SELECT ex_date, to_factor, for_factor FROM split WHERE act_symbol='{sym}'")
        conn.executemany("INSERT OR REPLACE INTO splits VALUES (?,?,?,?)",
                         [(sym, r["ex_date"], _f(r["to_factor"]), _f(r["for_factor"])) for r in sp.get("rows", [])])
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM bars WHERE symbol=?", (sym,)).fetchone()[0]
        log.info("%s: %d bars, %d earnings dates, %d splits", sym, n, len(e.get("rows", [])), len(sp.get("rows", [])))
    conn.close()


def _fetch_chain_days(client: DoltHubClient, sym: str, days: List[str]) -> Dict[str, List[dict]]:
    """Chains for a batch of dates; splits the batch when the 1,000-row cap is hit."""
    in_list = ",".join(f"'{d}'" for d in days)
    res = client.query("options", "SELECT date, expiration, strike, call_put, bid, ask, vol, delta FROM option_chain "
                                  f"WHERE act_symbol='{sym}' AND date IN ({in_list}) "
                                  f"AND expiration <= DATE_ADD(date, INTERVAL {MAX_EXPIRY_DAYS} DAY)")
    if res.get("query_execution_status") == "RowLimit":
        if len(days) == 1:
            log.warning("%s %s: more than 1,000 chain rows; keeping the first 1,000", sym, days[0])
        else:
            mid = len(days) // 2
            out = _fetch_chain_days(client, sym, days[:mid])
            out.update(_fetch_chain_days(client, sym, days[mid:]))
            return out
    out: Dict[str, List[dict]] = {d: [] for d in days}
    for r in res.get("rows", []):
        out.setdefault(r["date"], []).append(r)
    return out


def load_chains(client: DoltHubClient, store_path: Path, symbols: Sequence[str], start: date, end: date,
                workers: int = 3, progress: Optional[Callable[[int, int], None]] = None) -> int:
    """Fetch option chains for every trading day (taken from the stored bars) in [start, end]."""
    conn = connect(store_path)
    tasks = []
    for sym in check_symbols(symbols):
        days = [r[0] for r in conn.execute("SELECT date FROM bars WHERE symbol=? AND date BETWEEN ? AND ? ORDER BY date",
                                           (sym, str(start), str(end)))]
        done = {r[0] for r in conn.execute("SELECT date FROM chain_days_loaded WHERE symbol=?", (sym,))}
        todo = [d for d in days if d not in done]
        for i in range(0, len(todo), BATCH_DAYS):
            tasks.append((sym, todo[i:i + BATCH_DAYS]))
    total, n_rows = len(tasks), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_fetch_chain_days, client, sym, days): (sym, days) for sym, days in tasks}
        for i, fut in enumerate(as_completed(futs), 1):
            sym, days = futs[fut]
            try:
                by_day = fut.result()
            except Exception as exc:
                log.warning("chain batch %s %s..%s failed: %s (will retry on the next run)", sym, days[0], days[-1], exc)
                continue
            for d, rows in by_day.items():
                conn.executemany("INSERT OR REPLACE INTO chains VALUES (?,?,?,?,?,?,?,?,?)", [
                    (r["date"], sym, r["expiration"], float(r["strike"]), "CE" if r["call_put"] == "Call" else "PE",
                     _f(r["bid"]), _f(r["ask"]), _f(r["vol"]), _f(r["delta"])) for r in rows])
                conn.execute("INSERT OR REPLACE INTO chain_days_loaded VALUES (?,?,?)", (sym, d, len(rows)))
                n_rows += len(rows)
            conn.commit()
            if progress:
                progress(i, total)
    conn.close()
    return n_rows
