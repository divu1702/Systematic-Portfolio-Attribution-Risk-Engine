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


@pytest.fixture
def relaxed_cfg(raw_config, write_config):
    """Thresholds loosened so that the synthetic market yields accepted spreads; each filter is
    still evaluated and recorded."""
    r = raw_config
    r["iv"].update(min_iv_hv_ratio=0.01, proxy_hv_pctile=1)
    r["events"].update(estimate_results=False, buffer_days=0)
    r["direction"] = {"max_dist_atr": 50}
    r["levels"].update(min_buffer_atr=0.0)
    r["strikes"] = {"delta_min": 0.05, "delta_max": 0.45, "min_dist_atr": 0.0}
    r["spread"]["min_credit_width"] = 0.05
    r["ev"]["min_ev_ratio"] = 0.0
    r["ev"]["method"] = "expiry"     # mechanics tests need accepted spreads; path EV is tested on its own
    r["liquidity"] = {"max_spread_pct": 0.2, "min_oi_lots": 0, "min_vol_lots": 0}
    return load_config(write_config(r))




def relaxed_ctx(cfg, tmp_path, when=SIM_NOW, name="relaxed.db"):
    """Mock context whose scan at `when` + 1 minute yields accepted spreads: a seeded low-IV
    snapshot and a past 'news' event satisfy F5 for every stock."""
    from datetime import timedelta

    from app.context import build_context
    from app.jobs import morning_refresh, scan_job

    ctx = build_context(cfg, mock=True, clock=SimClock(when), db_path=tmp_path / name, env={},
                        market=shared_market())
    morning_refresh(ctx)
    scan_job(ctx)
    with ctx.engine.begin() as conn:
        for sym in [s.name for s in ctx.broker.market.specs if not s.is_index]:
            d = when.date() - timedelta(days=4)
            conn.exec_driver_sql("INSERT OR REPLACE INTO iv_snapshots (ts, date, symbol, expiry, fut_price, atm_iv, "
                                 "ivp_252, method) VALUES (?, ?, ?, ?, 1, 0.1, 0, 'proxy')",
                                 (str(when), d.isoformat(), sym, "2026-11-24"))
            conn.exec_driver_sql("INSERT INTO events (symbol, event_date, event_type, source, fetched_at) "
                                 "VALUES (?, ?, 'news', 'test', ?)", (sym, (d + timedelta(days=1)).isoformat(),
                                                                        str(when)))
    ctx.clock.set(when + timedelta(minutes=1))
    return ctx
