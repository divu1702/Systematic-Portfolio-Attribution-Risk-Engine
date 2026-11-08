"""Runtime context shared by every job: config, clock, database, broker, data sources, alerts.

Mock mode (synthetic market) always uses a separate database file so synthetic data can
never mix with real paper-trading records.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import Engine

from .broker.base import Broker
from .clock import Clock, SystemClock
from .config import MAIN, Config
from .data.db import init_db
from .env import load_env
from .reporting.notifier import LogNotifier, Notifier


@dataclass
class Context:
    cfg: Config
    env: Mapping[str, str]
    clock: Clock
    engine: Engine
    broker: Broker
    notifier: Notifier
    nse: Any = None                   # exchange data source (app/data/nse.py)
    is_mock: bool = False
    banner: str = ""                  # shown on reports, e.g. "Backtest on real US data"
    cache: Dict[str, Any] = field(default_factory=dict)
    strategy: str = MAIN              # the book this context acts for (config: strategies)
    parent: Optional["Context"] = None    # the main context, for a strategy's context

    def for_strategy(self, name: str, missing_ok: bool = False) -> "Context":
        """The same context acting for one strategy's book: that strategy's rules in `cfg`, and
        the same clock, database, broker, alerts and cache. With missing_ok, a strategy no longer in
        the config (open trades of a removed sleeve) gets the main rules under its own name."""
        main = self.parent or self
        if name == MAIN:
            return main
        if missing_ok and name not in main.cfg.strategy_names:
            return dataclasses.replace(main, strategy=name, parent=main)
        key = ("_strategy_cfg", name)
        hit = main.cache.get(key)
        if hit is None or hit[0] is not main.cfg:          # rebuilt if the main config was replaced
            hit = (main.cfg, main.cfg.for_strategy(name))
            main.cache[key] = hit
        return dataclasses.replace(main, cfg=hit[1], strategy=name, parent=main)

    def books(self) -> List["Context"]:
        """The main book first, then each paper sleeve."""
        main = self.parent or self
        return [main.for_strategy(n) for n in main.cfg.strategy_names]


def mock_db_path(cfg: Config) -> Path:
    p = cfg.db_path
    return p.with_name(f"{p.stem}_mock{p.suffix}")


def build_context(cfg: Config, *, mock: Optional[bool] = None, clock: Optional[Clock] = None,
                  db_path: Optional[Path] = None, notifier: Optional[Notifier] = None,
                  env: Optional[Mapping[str, str]] = None, market=None) -> Context:
    clock = clock or SystemClock()
    env = env if env is not None else load_env()
    is_mock = cfg.broker.provider == "mock" if mock is None else mock
    if is_mock:
        from .broker.mock import MockBroker
        from .broker.synthetic import SyntheticMarket

        market = market or SyntheticMarket(seed=cfg.broker.mock_seed, n_symbols=cfg.broker.mock_symbols,
                                           anchor=clock.today(), r=cfg.analytics.risk_free)
        broker: Broker = MockBroker(clock, market)
        path = db_path or mock_db_path(cfg)
    else:
        from .broker.kite import KiteBroker

        broker = KiteBroker(cfg, env, clock)
        path = db_path or cfg.db_path
    engine = init_db(path)
    from .fmt import set_currency

    set_currency(cfg.market.currency)
    if notifier is None:
        notifier = LogNotifier()
        if not is_mock and env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
            from .reporting.telegram_bot import TelegramNotifier

            notifier = TelegramNotifier(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], fallback=notifier)
    ctx = Context(cfg, env, clock, engine, broker, notifier, is_mock=is_mock)
    from .data.nse import make_nse_source  # local import: nse depends on context-free modules only

    ctx.nse = make_nse_source(ctx)
    return ctx
