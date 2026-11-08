"""Broker interface. Data comes back in Kite Connect's shapes so the mock and the real
broker are interchangeable, and recorded Kite responses can be replayed as fixtures.

Quote dict (per "EXCHANGE:TRADINGSYMBOL" key), as Kite returns it:
    {"instrument_token", "timestamp", "last_price", "volume", "oi",
     "depth": {"buy": [{"price", "quantity", "orders"} x5], "sell": [... x5]}, "ohlc": {...}}
Volume and OI are in shares (units), not lots.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple


class BrokerError(RuntimeError):
    pass


class SessionInvalid(BrokerError):
    """No valid broker session for today: the owner must complete the morning login."""


@dataclass(frozen=True)
class Level:
    price: float
    quantity: int


@dataclass(frozen=True)
class Quote:
    key: str
    ts: Optional[datetime]
    last: float
    bids: Tuple[Level, ...]     # best first
    asks: Tuple[Level, ...]     # best first
    volume: int
    oi: int

    @property
    def bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid(self) -> Optional[float]:
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return 0.5 * (self.bid + self.ask)
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        return (self.ask - self.bid) / m if m else None


def parse_quote(key: str, raw: dict) -> Quote:
    depth = raw.get("depth") or {}

    def side(levels) -> Tuple[Level, ...]:
        out = []
        for lv in levels or []:
            px, qty = float(lv.get("price") or 0), int(lv.get("quantity") or 0)
            if px > 0 and qty > 0:
                out.append(Level(px, qty))
        return tuple(out)

    return Quote(
        key=key,
        ts=raw.get("timestamp"),
        last=float(raw.get("last_price") or 0.0),
        bids=side(depth.get("buy")),
        asks=side(depth.get("sell")),
        volume=int(raw.get("volume") or raw.get("volume_traded") or 0),
        oi=int(raw.get("oi") or 0),
    )


class RateLimiter:
    """Minimum spacing between calls (thread-safe)."""

    def __init__(self, per_second: float, sleep=time.sleep, monotonic=time.monotonic):
        self.interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()
        self._sleep, self._mono = sleep, monotonic

    def wait(self) -> None:
        with self._lock:
            now = self._mono()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class Broker(ABC):
    name: str = "broker"

    # --- session
    @abstractmethod
    def login_url(self) -> str: ...

    @abstractmethod
    def complete_login(self, request_token: str) -> None: ...

    @abstractmethod
    def session_valid(self) -> bool: ...

    # --- market data
    @abstractmethod
    def instruments(self, exchange: str) -> List[dict]: ...

    @abstractmethod
    def quote_raw(self, keys: Sequence[str]) -> Dict[str, dict]: ...

    def quotes(self, keys: Sequence[str]) -> Dict[str, Quote]:
        raw = self.quote_raw(list(dict.fromkeys(keys)))
        return {k: parse_quote(k, v) for k, v in raw.items()}

    @abstractmethod
    def historical(self, instrument_token: int, from_dt: datetime, to_dt: datetime,
                   interval: str, continuous: bool = False) -> List[dict]:
        """Candles as dicts: date (naive IST datetime), open, high, low, close, volume[, oi].
        `continuous` asks for a futures series stitched across expiries (ignored where unsupported)."""

    # --- account
    @abstractmethod
    def basket_margin(self, orders: List[dict]) -> float:
        """Total margin in rupees for a basket of Kite-format order dicts, with hedge benefit."""

    @abstractmethod
    def positions(self) -> List[dict]:
        """Net positions (Kite `positions()["net"]` shape)."""
