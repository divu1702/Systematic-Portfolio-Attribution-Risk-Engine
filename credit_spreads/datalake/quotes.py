"""Live quotes once a minute over Kite's WebSocket (09:14 to 15:31 on trading days).

Kite's candles carry trades only; the bid/ask a backtest needs for realistic fills exists only if it is
recorded live. Kite allows 3 WebSocket connections per API key with up to 3,000 instruments each, so
up to `lake.quotes.max_instruments` instruments are chosen each morning:

1. the market indices in `lake.indices`, every F&O stock and every stock future;
2. options, nearest strikes first: each stock's at-the-money call and put of the near month, then the
   next strikes out, with the next month's strikes ranked four steps behind (ahead of the near month
   in its last `roll_days` sessions), until the limit is reached.

Each connection runs in its own process (the WebSocket library can run one event loop per process). A
process keeps the latest full-mode tick of each instrument and, at every minute boundary, writes one
row per instrument that ticked in that minute: the state at the end of the minute (bid, ask, sizes and
order counts, last price and quantity, average price, cumulative volume, OI with the day's high and
low, total pending buy and sell quantity, last trade and exchange times, ticks in the minute), plus all
five depth levels when `lake.quotes.depth` is on. Rows reach the disk every `flush_min` minutes, so a
crash loses at most that much; the parent restarts a dead connection.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import threading
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

MAX_RESTARTS = 10


@dataclass(frozen=True)
class Inst:
    token: int
    tradingsymbol: str
    kind: str                    # INDEX | EQ | FUT | CE | PE
    underlying: str
    expiry: Optional[str] = None
    strike: Optional[float] = None


def select(p, today: Optional[date] = None, spots: Optional[Dict[str, float]] = None) -> List[Inst]:
    """The day's instruments, within the WebSocket limit (see the module docstring)."""
    from sqlalchemy import select as sql_select
    from sqlalchemy.orm import Session

    from app import market
    from app.data.models import Instrument

    cfg = p.cfg.lake
    today = today or market.ensure_instruments(p.ctx)
    with Session(p.ctx.engine) as s:
        master = list(s.scalars(sql_select(Instrument).where(Instrument.date == today)))
    fixed: List[Inst] = []
    for name in cfg.indices:
        tok = p.ds.index_token(name)
        if tok:
            fixed.append(Inst(int(tok), name, "INDEX", name))
    eq = {i.underlying: i for i in master if i.instrument_type == "EQ" and i.instrument_token}
    fixed += [Inst(int(i.instrument_token), i.tradingsymbol, "EQ", u) for u, i in sorted(eq.items())]
    fixed += [Inst(int(i.instrument_token), i.tradingsymbol, "FUT", i.underlying, str(i.expiry))
              for i in sorted(master, key=lambda x: (x.underlying, x.expiry or today))
              if i.instrument_type == "FUT" and i.instrument_token and i.expiry and i.expiry >= today]
    room = cfg.quotes.max_instruments - len(fixed)
    if room <= 0:
        return fixed[: cfg.quotes.max_instruments]
    if spots is None:
        q = p.ds.spot_quotes(sorted(eq))
        spots = {k: (v.last or v.mid or v.prev_close) for k, v in q.items()}
    cal = p.ds.calendar()
    by_series: Dict[tuple, List] = {}
    for i in master:
        if i.instrument_type in ("CE", "PE") and i.instrument_token and i.expiry and i.expiry >= today:
            by_series.setdefault((i.underlying, i.expiry), []).append(i)
    cands = []
    for und in sorted({u for u, _ in by_series}):
        spot = spots.get(und)
        if not spot:
            continue
        exps = sorted(e for u, e in by_series if u == und)[:2]
        if len(exps) == 2 and cal.trading_days_between(today, exps[0]) <= cfg.quotes.roll_days:
            exps = [exps[1], exps[0]]
        for rank, exp in enumerate(exps):
            legs = by_series[(und, exp)]
            strikes = sorted({float(i.strike) for i in legs})
            atm = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
            for i in legs:
                dist = abs(strikes.index(float(i.strike)) - atm)
                cands.append(((dist + 4 * rank, dist, und, i.instrument_type), i, exp))
    cands.sort(key=lambda c: c[0])
    opts = [Inst(int(i.instrument_token), i.tradingsymbol, i.instrument_type, i.underlying, str(exp), float(i.strike))
            for _, i, exp in cands[:room]]
    return fixed + opts


def split(insts: List[Inst], per_connection: int, connections: int = 3) -> List[List[Inst]]:
    groups = [insts[i:i + per_connection] for i in range(0, len(insts), per_connection)]
    return groups[:connections]


def ist_now() -> datetime:
    from app.clock import IST

    return datetime.now(IST).replace(tzinfo=None)


class MinuteBook:
    """Latest tick per instrument; one row per instrument and minute in which it ticked."""

    def __init__(self, meta: Dict[int, Inst], depth: bool = False):
        self.meta, self.depth = meta, depth
        self.state: Dict[int, dict] = {}
        self.count: Dict[int, int] = {}
        self.minute: Optional[datetime] = None
        self.rows: List[dict] = []

    def on_ticks(self, ticks: List[dict], now: datetime) -> None:
        self.tick(now)
        for t in ticks:
            tok = t.get("instrument_token")
            if tok is None:
                continue
            self.state[tok] = t
            self.count[tok] = self.count.get(tok, 0) + 1

    def tick(self, now: datetime) -> None:
        m = now.replace(second=0, microsecond=0)
        if self.minute is None:
            self.minute = m
        elif m > self.minute:
            self.close_minute()
            self.minute = m

    def close_minute(self) -> None:
        if self.minute is None:
            return
        for tok, n in self.count.items():
            if n:
                self.rows.append(self.row(tok, n))
        self.count = {}

    def row(self, tok: int, n: int) -> dict:
        t, m = self.state[tok], self.meta.get(tok)
        depth = t.get("depth") or {}
        buy, sell = depth.get("buy") or [], depth.get("sell") or []

        def lv(side, i, f):
            return side[i].get(f) if len(side) > i and side[i] else None

        r = {"ts": self.minute, "token": int(tok), "tradingsymbol": m.tradingsymbol if m else None,
             "underlying": m.underlying if m else None, "kind": m.kind if m else None,
             "expiry": m.expiry if m else None, "strike": m.strike if m else None,
             "last": t.get("last_price"), "last_qty": t.get("last_traded_quantity") or t.get("last_quantity"),
             "avg_price": t.get("average_traded_price") or t.get("average_price"),
             "volume": t.get("volume_traded") or t.get("volume"), "oi": t.get("oi"),
             "oi_day_high": t.get("oi_day_high"), "oi_day_low": t.get("oi_day_low"),
             "buy_total": t.get("total_buy_quantity") or t.get("buy_quantity"),
             "sell_total": t.get("total_sell_quantity") or t.get("sell_quantity"),
             "bid": lv(buy, 0, "price"), "bid_qty": lv(buy, 0, "quantity"), "bid_orders": lv(buy, 0, "orders"),
             "ask": lv(sell, 0, "price"), "ask_qty": lv(sell, 0, "quantity"), "ask_orders": lv(sell, 0, "orders"),
             "last_trade_time": t.get("last_trade_time"), "exchange_time": t.get("exchange_timestamp"), "ticks": n}
        if self.depth:
            for i in range(5):
                for side, name in ((buy, "b"), (sell, "a")):
                    r[f"{name}{i + 1}_price"] = lv(side, i, "price")
                    r[f"{name}{i + 1}_qty"] = lv(side, i, "quantity")
                    r[f"{name}{i + 1}_orders"] = lv(side, i, "orders")
        return r

    def take(self) -> List[dict]:
        rows, self.rows = self.rows, []
        return rows


NUMERIC_INT = ("token", "last_qty", "volume", "oi", "oi_day_high", "oi_day_low", "buy_total", "sell_total",
               "bid_qty", "bid_orders", "ask_qty", "ask_orders", "ticks")


def frame(rows: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in df.columns:
        if c in NUMERIC_INT or c.endswith("_qty") or c.endswith("_orders"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        elif c in ("last", "avg_price", "bid", "ask", "strike") or c.endswith("_price"):
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in ("ts", "last_trade_time", "exchange_time"):
        if c in df:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    df["date"] = df["ts"].dt.date
    return df


def kite_ticker(api_key: str, access_token: str):
    from kiteconnect import KiteTicker

    return KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=300, reconnect_max_delay=30)


def worker(conn: int, insts: List[dict], api_key: str, access_token: str, root: str, depth: bool, end: str,
           flush_min: int, stop=None, ticker_factory: Optional[Callable] = None, clock: Callable[[], datetime] = ist_now,
           sleep: Callable[[float], None] = _time.sleep) -> dict:
    """One WebSocket connection (runs in its own process). Returns its counts for the day."""
    from .store import Lake

    meta = {d["token"]: Inst(**d) for d in insts}
    tokens = list(meta)
    book = MinuteBook(meta, depth)
    lake = Lake(Path(root))
    lock = threading.Lock()
    stats = {"conn": conn, "instruments": len(tokens), "rows": 0, "minutes": 0, "connects": 0, "errors": 0}
    ticker = (ticker_factory or kite_ticker)(api_key, access_token)

    def on_ticks(ws, ticks):
        with lock:
            book.on_ticks(ticks, clock())

    def on_connect(ws, response):
        stats["connects"] += 1
        ws.subscribe(tokens)
        ws.set_mode(getattr(ws, "MODE_FULL", "full"), tokens)

    def on_error(ws, code, reason):
        stats["errors"] += 1
        log.warning("quotes connection %s error %s: %s", conn, code, reason)

    ticker.on_ticks, ticker.on_connect, ticker.on_error = on_ticks, on_connect, on_error
    ticker.connect(threaded=True)
    end_dt = datetime.combine(clock().date(), datetime.strptime(end, "%H:%M").time())
    last_flush, minutes = clock(), set()

    def flush():
        with lock:
            rows = book.take()
        if rows:
            df = frame(rows)
            lake.write("quotes_1m", df, tag=f"c{conn}")
            stats["rows"] += len(rows)
            minutes.update(df["ts"].unique().tolist())
            stats["minutes"] = len(minutes)
        stats_path = lake.root / "_state" / "quotes" / f"{clock().date().isoformat()}_c{conn}.json"
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps(stats))

    try:
        while not (stop is not None and stop.is_set()) and clock() < end_dt:
            sleep(1.0)
            with lock:
                book.tick(clock())
            if clock() - last_flush >= timedelta(minutes=flush_min):
                flush()
                last_flush = clock()
    finally:
        with lock:
            book.close_minute()
        flush()
        try:
            ticker.close()
        except Exception:
            pass
    return stats


def _worker_entry(*args):                         # module-level target for spawned processes
    from app.logging_setup import setup_logging

    setup_logging(None, level="WARNING")
    worker(*args)


class Recorder:
    """Starts and stops the day's connections and restarts any that die."""

    def __init__(self, p, ticker_factory: Optional[Callable] = None, use_processes: bool = True):
        self.p, self.ticker_factory, self.use_processes = p, ticker_factory, use_processes
        self.procs: Dict[int, object] = {}
        self.args: Dict[int, tuple] = {}
        self.stop_event = None
        self.restarts = 0
        self._watch: Optional[threading.Thread] = None

    def start(self) -> str:
        cfg = self.p.cfg.lake
        if not (cfg.enabled and cfg.quotes.enabled):
            return "quote recording is off (lake.quotes.enabled)"
        if self.procs:
            return "quote recording is already running"
        broker = self.p.ctx.broker
        api_key = getattr(broker, "api_key", "")
        access_token = getattr(getattr(broker, "kite", None), "access_token", None) or ""
        if not (api_key and access_token) and self.ticker_factory is None:
            return "no Kite session: quote recording not started"
        insts = select(self.p)
        groups = split(insts, cfg.quotes.per_connection)
        root = str(self.p.cfg.resolve(cfg.root))
        ctx = mp.get_context("spawn")
        self.stop_event = ctx.Event() if self.use_processes else threading.Event()
        for n, g in enumerate(groups):
            self.args[n] = (n, [asdict(i) for i in g], api_key, access_token, root, cfg.quotes.depth, cfg.quotes.end,
                            cfg.quotes.flush_min, self.stop_event)
            self._spawn(n)
        self._watch = threading.Thread(target=self._watchdog, name="quotes-watchdog", daemon=True)
        self._watch.start()
        return f"recording quotes for {len(insts):,} instruments over {len(groups)} connection(s)"

    def _spawn(self, n: int) -> None:
        if self.use_processes:
            proc = mp.get_context("spawn").Process(target=_worker_entry, args=self.args[n], name=f"quotes-{n}",
                                                   daemon=True)
        else:
            proc = threading.Thread(target=worker, args=self.args[n], kwargs={"ticker_factory": self.ticker_factory},
                                    name=f"quotes-{n}", daemon=True)
        proc.start()
        self.procs[n] = proc

    def _watchdog(self) -> None:
        end = datetime.combine(ist_now().date(), datetime.strptime(self.p.cfg.lake.quotes.end, "%H:%M").time())
        while self.stop_event is not None and not self.stop_event.is_set() and ist_now() < end:
            _time.sleep(30)
            for n, proc in list(self.procs.items()):
                if not proc.is_alive() and ist_now() < end and self.restarts < MAX_RESTARTS:
                    self.restarts += 1
                    log.warning("quotes connection %s stopped; restarting (%d)", n, self.restarts)
                    self._spawn(n)

    def stop(self, timeout: float = 60.0) -> str:
        if not self.procs:
            return "quote recording was not running"
        if self.stop_event is not None:
            self.stop_event.set()
        for proc in self.procs.values():
            proc.join(timeout)
            if self.use_processes and proc.is_alive():
                proc.terminate()
        self.procs = {}
        return self.finish(ist_now().date())

    def finish(self, d: date) -> str:
        """Compact the day's quote files and record the day's counts."""
        from .store import Lake

        lake = Lake(self.p.cfg.resolve(self.p.cfg.lake.root))
        folder = lake.partition_dir("quotes_1m", date=d)
        if folder.exists():
            lake.compact(folder, "quotes_1m")
        total, conns = 0, 0
        for f in sorted((lake.root / "_state" / "quotes").glob(f"{d.isoformat()}_c*.json")):
            st = json.loads(f.read_text())
            total += st["rows"]
            conns += 1
            lake.catalog.execute("INSERT OR REPLACE INTO quote_days VALUES (?,?,?,?,?,?)",
                                 (d.isoformat(), st["conn"], st["instruments"], st["minutes"], st["rows"], self.restarts))
        return f"quotes: {total:,} minute rows over {conns} connection(s), {self.restarts} restart(s)"
