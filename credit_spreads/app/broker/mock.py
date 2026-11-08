"""Offline brokers.

MockBroker   - the synthetic market (app/broker/synthetic.py), driven by an injectable clock.
               Used by the test-suite, the demos and `python -m app sim`.
FixtureBroker - replays Kite responses recorded with `python -m app record` on a machine
               that has a Kite session, so real-data shapes can be tested offline.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..clock import Clock
from .base import Broker, BrokerError
from .synthetic import CLOSE_T, SyntheticMarket


def _approx_basket_margin(orders: List[dict], ref_price) -> float:
    """SPAN-like approximation: a matched short/long pair needs about its width x qty; a naked
    short about 18% of notional; a bought option only its premium."""
    groups = defaultdict(list)
    for o in orders:
        groups[(o.get("_underlying"), o.get("_expiry"), o.get("_cp"))].append(o)
    total = 0.0
    for (und, _exp, _cp), legs in groups.items():
        sells = [o for o in legs if o["transaction_type"] == "SELL"]
        buys = [o for o in legs if o["transaction_type"] == "BUY"]
        for s in sells:
            hedge = next((b for b in buys if b["quantity"] >= s["quantity"]), None)
            if hedge is not None:
                total += abs(s["_strike"] - hedge["_strike"]) * s["quantity"] * 1.05
                buys.remove(hedge)
            else:
                total += 0.18 * ref_price(und) * s["quantity"]
        for b in buys:
            total += (b.get("price") or 0.0) * b["quantity"]
    return round(total, 2)


class MockBroker(Broker):
    name = "mock"

    def __init__(self, clock: Clock, market: Optional[SyntheticMarket] = None, seed: int = 7,
                 n_symbols: int = 20):
        self.clock = clock
        self.market = market or SyntheticMarket(seed=seed, n_symbols=n_symbols, anchor=clock.today())
        self.fail_margin = False          # tests flip this to exercise the fallback path
        self.net_positions: List[dict] = []
        self.quote_calls = 0

    # session: always valid offline
    def login_url(self) -> str:
        return "https://kite.zerodha.com/connect/login?v=3&api_key=mock"

    def complete_login(self, request_token: str) -> None:
        return None

    def session_valid(self) -> bool:
        return True

    def instruments(self, exchange: str) -> List[dict]:
        return self.market.instruments(exchange, self.clock.today())

    def quote_raw(self, keys: Sequence[str]) -> Dict[str, dict]:
        self.quote_calls += 1
        now = self.clock.now()
        out = {}
        for k in keys:
            try:
                info = self.market.info(k)
            except KeyError:
                continue                     # Kite silently omits unknown instruments
            if info.expiry and datetime.combine(info.expiry, CLOSE_T) < now:
                continue                     # expired contracts are not quotable
            out[k] = self.market.quote(k, now)
        return out

    def historical(self, instrument_token: int, from_dt: datetime, to_dt: datetime,
                   interval: str, continuous: bool = False) -> List[dict]:
        self.hist_calls = getattr(self, "hist_calls", 0) + 1
        return self.market.candles(instrument_token, from_dt, to_dt, interval, self.clock.now())

    def basket_margin(self, orders: List[dict]) -> float:
        if self.fail_margin:
            raise BrokerError("mock basket margin failure")
        enriched = []
        for o in orders:
            info = self.market.info(f"{o['exchange']}:{o['tradingsymbol']}")
            enriched.append({**o, "_underlying": info.name, "_expiry": info.expiry,
                             "_cp": info.kind, "_strike": info.strike or 0.0})
        return _approx_basket_margin(enriched, lambda und: self.market.spot(und, self.clock.now()))

    def positions(self) -> List[dict]:
        return list(self.net_positions)


class FixtureBroker(Broker):
    """Serves responses saved by `python -m app record`. Quotes are a single snapshot."""

    name = "fixture"

    def __init__(self, fixture_dir: Path):
        self.dir = Path(fixture_dir)
        if not (self.dir / "meta.json").exists():
            raise BrokerError(f"no recorded fixtures in {self.dir} (run `python -m app record` first)")
        self.meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))

    def _load(self, name: str):
        return json.loads((self.dir / name).read_text(encoding="utf-8"), object_hook=_decode)

    def login_url(self) -> str:
        return "fixture://login"

    def complete_login(self, request_token: str) -> None:
        return None

    def session_valid(self) -> bool:
        return True

    def instruments(self, exchange: str) -> List[dict]:
        return self._load(f"instruments_{exchange}.json")

    def quote_raw(self, keys: Sequence[str]) -> Dict[str, dict]:
        q = self._load("quotes.json")
        return {k: q[k] for k in keys if k in q}

    def historical(self, instrument_token: int, from_dt: datetime, to_dt: datetime,
                   interval: str) -> List[dict]:
        path = self.dir / "historical" / f"{instrument_token}_{interval}.json"
        if not path.exists():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"), object_hook=_decode)
        return [r for r in rows if from_dt <= r["date"] <= to_dt]

    def basket_margin(self, orders: List[dict]) -> float:
        raise BrokerError("basket margin is not recorded in fixtures")

    def positions(self) -> List[dict]:
        return []


# JSON helpers shared with the recorder ---------------------------------------------------

def encode(obj):
    if isinstance(obj, datetime):
        return {"__dt__": obj.isoformat()}
    if isinstance(obj, date):
        return {"__d__": obj.isoformat()}
    if isinstance(obj, float) and math.isnan(obj):
        return None
    raise TypeError(f"cannot encode {type(obj)}")


def _decode(d: dict):
    if "__dt__" in d:
        return datetime.fromisoformat(d["__dt__"])
    if "__d__" in d:
        return date.fromisoformat(d["__d__"])
    return d
