from pathlib import Path

import pytest
import yaml

from app.config import DEFAULT_CONFIG_PATH, load_config


@pytest.fixture
def raw_config() -> dict:
    """The shipped config.yaml as a dict, for tests that tweak one key."""
    return yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict to a temp dir and return its path. DB and logs land in tmp_path."""

    def _write(raw: dict) -> Path:
        raw = dict(raw)
        raw["paths"] = {**raw.get("paths", {}), "db": "data/test.db", "logs": "logs"}
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return p

    return _write


@pytest.fixture
def cfg(raw_config, write_config):
    return load_config(write_config(raw_config))


# ----------------------------------------------------------------------------- mock market
from datetime import datetime

from app.broker.synthetic import SyntheticMarket
from app.clock import SimClock

SIM_NOW = datetime(2026, 11, 6, 15, 0)
_MARKETS = {}


def shared_market(n_symbols: int = 8, seed: int = 7, anchor=SIM_NOW.date()) -> SyntheticMarket:
    """Synthetic markets are deterministic and read-only, so tests share them for speed."""
    key = (n_symbols, seed, anchor)
    if key not in _MARKETS:
        _MARKETS[key] = SyntheticMarket(seed=seed, n_symbols=n_symbols, anchor=anchor)
    return _MARKETS[key]


@pytest.fixture
def mock_ctx(cfg, tmp_path):
    from app.context import build_context

    clock = SimClock(SIM_NOW)
    return build_context(cfg, mock=True, clock=clock, db_path=tmp_path / "mock.db", env={},
                         market=shared_market())
