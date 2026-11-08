"""Kite Connect wrapper: daily login, instrument master, quotes with depth, historical candles,
basket margin and positions.

SEBI's daily 2FA rule means there is no stored refresh token. The owner logs in each morning;
the access token for that day is kept in `paths.kite_session` so restarts during the day do
not need a new login. Kite access tokens expire at 06:00 the next morning, and so does ours.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from ..clock import Clock, to_naive_ist
from ..config import Config
from .base import Broker, BrokerError, RateLimiter, SessionInvalid

log = logging.getLogger(__name__)

TOKEN_EXPIRY = time(6, 0)
# Kite historical API: maximum days per request by interval.
HIST_MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "10minute": 100, "15minute": 200,
                 "30minute": 200, "60minute": 400, "day": 2000}


def extract_request_token(text: str) -> str:
    """Accept either the raw request_token or the full redirect URL pasted from the browser."""
    text = text.strip()
    if "request_token=" in text:
        qs = parse_qs(urlparse(text).query) or parse_qs(text.split("?", 1)[-1])
        vals = qs.get("request_token")
        if not vals or not vals[0]:
            raise ValueError("no request_token in that URL")
        if qs.get("status", ["success"])[0] != "success":
            raise ValueError(f"Kite login did not succeed (status={qs['status'][0]})")
        return vals[0]
    if not text or any(c in text for c in " /?&="):
        raise ValueError("that does not look like a request_token or a Kite redirect URL")
    return text


def session_start_boundary(now: datetime) -> datetime:
    """Most recent 06:00 at or before now: tokens issued before it have expired."""
    b = datetime.combine(now.date(), TOKEN_EXPIRY)
    return b if now >= b else b - timedelta(days=1)


class KiteBroker(Broker):
    name = "kite"

    def __init__(self, cfg: Config, env: Mapping[str, str], clock: Clock, kite=None):
        self.cfg, self.clock = cfg, clock
        self.api_key = env.get("KITE_API_KEY") or ""
        self.api_secret = env.get("KITE_API_SECRET") or ""
        if kite is None:
            if not self.api_key:
                raise BrokerError("KITE_API_KEY is not set in .env")
            from kiteconnect import KiteConnect

            kite = KiteConnect(api_key=self.api_key)
        self.kite = kite
        self.session_path: Path = cfg.resolve(cfg.paths.kite_session)
        self._quote_rl = RateLimiter(cfg.broker.quote_rps)
        self._hist_rl = RateLimiter(cfg.broker.hist_rps)
        self._token_loaded = False

    # ------------------------------------------------------------------ session
    def login_url(self) -> str:
        return self.kite.login_url()

    def complete_login(self, request_token: str) -> None:
        if not self.api_secret:
            raise BrokerError("KITE_API_SECRET is not set in .env")
        data = self.kite.generate_session(extract_request_token(request_token), api_secret=self.api_secret)
        token = data["access_token"]
        self.kite.set_access_token(token)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"access_token": token, "user_id": data.get("user_id"),
                   "login_time": self.clock.now().isoformat(sep=" ")}
        self.session_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            self.session_path.chmod(0o600)
        except OSError:  # pragma: no cover - Windows
            pass
        self._token_loaded = True
        log.info("Kite session established for %s", data.get("user_id"))

    def _load_session(self) -> Optional[dict]:
        if not self.session_path.exists():
            return None
        try:
            payload = json.loads(self.session_path.read_text(encoding="utf-8"))
            login_time = datetime.fromisoformat(payload["login_time"])
        except (ValueError, KeyError):
            return None
        if login_time < session_start_boundary(self.clock.now()):
            return None
        return payload

    def session_valid(self) -> bool:
        payload = self._load_session()
        if not payload:
            return False
        if not self._token_loaded:
            self.kite.set_access_token(payload["access_token"])
            self._token_loaded = True
        return True

    def _require_session(self) -> None:
        if not self.session_valid():
            raise SessionInvalid("no valid Kite session today: run `python -m app login`")

    # ------------------------------------------------------------------ market data
    def instruments(self, exchange: str) -> List[dict]:
        # The instrument dump does not need an access token.
        rows = self.kite.instruments(exchange)
        for r in rows:
            if isinstance(r.get("expiry"), datetime):
                r["expiry"] = r["expiry"].date()
            if r.get("expiry") == "":
                r["expiry"] = None
        return rows

    def quote_raw(self, keys: Sequence[str]) -> Dict[str, dict]:
        self._require_session()
        out: Dict[str, dict] = {}
        batch = self.cfg.broker.quote_batch
        for i in range(0, len(keys), batch):
            self._quote_rl.wait()
            chunk = list(keys[i:i + batch])
            data = self.kite.quote(chunk)
            for k, v in data.items():
                for f in ("timestamp", "last_trade_time"):
                    if isinstance(v.get(f), datetime):
                        v[f] = to_naive_ist(v[f])
                out[k] = v
        return out

    def historical(self, instrument_token: int, from_dt: datetime, to_dt: datetime,
                   interval: str, continuous: bool = False) -> List[dict]:
        """`continuous=True` (futures, day candles only) stitches the same underlying's expired
        futures onto the live contract; Kite serves no history for expired options."""
        self._require_session()
        span = timedelta(days=HIST_MAX_DAYS.get(interval, 60))
        candles: List[dict] = []
        start = from_dt
        while start <= to_dt:
            end = min(start + span, to_dt)
            self._hist_rl.wait()
            rows = self.kite.historical_data(instrument_token, start, end, interval, continuous=continuous, oi=True)
            for r in rows:
                r["date"] = to_naive_ist(r["date"]) if isinstance(r["date"], datetime) else r["date"]
            candles.extend(rows)
            start = end + timedelta(seconds=1)
        # Chunk edges can repeat a candle.
        seen, unique = set(), []
        for c in candles:
            if c["date"] not in seen:
                seen.add(c["date"])
                unique.append(c)
        return unique

    # ------------------------------------------------------------------ account
    def basket_margin(self, orders: List[dict]) -> float:
        self._require_session()
        res = self.kite.basket_order_margins(orders, consider_positions=False, mode="compact")
        for key in ("final", "initial"):
            total = (res.get(key) or {}).get("total")
            if total is not None:
                return float(total)
        raise BrokerError(f"unexpected basket margin response: {res}")

    def positions(self) -> List[dict]:
        self._require_session()
        return self.kite.positions().get("net", [])
