"""Guard for every code path that could place a real order.

Live orders require BOTH `mode: live` in config.yaml AND the manual confirmation flag
`LIVE_TRADING_CONFIRMED=<LIVE_CONFIRM_VALUE>` in the environment/.env. In paper mode
`require_live` always raises, so live order code is unreachable.
"""

from __future__ import annotations

from typing import Mapping

from .config import Config

LIVE_CONFIRM_ENV = "LIVE_TRADING_CONFIRMED"
LIVE_CONFIRM_VALUE = "yes-real-money"


class LiveTradingDisabled(RuntimeError):
    """Raised when a live-order code path is reached without full authorisation."""


def live_trading_allowed(cfg: Config, env: Mapping[str, str]) -> bool:
    return cfg.mode == "live" and env.get(LIVE_CONFIRM_ENV) == LIVE_CONFIRM_VALUE


def require_live(cfg: Config, env: Mapping[str, str]) -> None:
    """Call at the top of any function that sends a real order to the broker."""
    if cfg.mode != "live":
        raise LiveTradingDisabled(f"mode is {cfg.mode!r}; real orders are disabled")
    if env.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_VALUE:
        raise LiveTradingDisabled(
            f"mode is 'live' but {LIVE_CONFIRM_ENV} is not set to {LIVE_CONFIRM_VALUE!r}; "
            "real orders are disabled"
        )
