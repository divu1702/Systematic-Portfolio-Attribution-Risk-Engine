"""Builds the running platform: the earlier system's context (one Kite session, its database and jobs),
the platform database, the shared data service, the shared paper broker with the earlier system's
books attached as oi_tight and oi_loose, and the level system's engine.

A mock run (synthetic market) uses separate database files, so synthetic data never mixes with real
paper-trading records.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .broker import PaperBroker
from .config import PlatformConfig, load_platform_config
from .data_service import DataService
from .db import init_platform_db


@dataclass
class Platform:
    cfg: PlatformConfig
    ctx: object                     # the earlier system's Context
    pengine: object
    ds: DataService
    broker: PaperBroker
    engine: object                  # level_system.engine.LevelEngine
    recorder: object = None         # datalake.quotes.Recorder while live quotes are being recorded

    @property
    def notifier(self):
        return self.ctx.notifier

    def today(self):
        return self.ds.today()

    def now(self):
        return self.ds.now()


def platform_db_path(cfg: PlatformConfig, mock: bool) -> Path:
    p = cfg.db_path
    return p.with_name(f"{p.stem}_mock{p.suffix}") if mock else p


def build(cfg: Optional[PlatformConfig] = None, *, ctx=None, mock: Optional[bool] = None, clock=None, notifier=None,
          db_path: Optional[Path] = None, market=None, env=None) -> Platform:
    from app.config import load_config
    from app.context import build_context
    from level_system.engine import LevelEngine

    from . import oi_bridge

    cfg = cfg or load_platform_config()
    if ctx is None:
        ocfg = load_config(cfg.resolve(cfg.oi_system.config))
        ctx = build_context(ocfg, mock=mock, clock=clock, notifier=notifier, market=market, env=env)
    pengine = init_platform_db(db_path or platform_db_path(cfg, ctx.is_mock))
    ds = DataService(ctx, cfg, pengine)
    broker = PaperBroker(pengine, cfg, ds, notifier=ctx.notifier)
    broker.ensure_accounts()
    oi_bridge.attach(ctx, broker, cfg)
    return Platform(cfg, ctx, pengine, ds, broker, LevelEngine(ds, broker, cfg, ctx.notifier))
