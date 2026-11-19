"""Platform skeleton: config, schema and Parquet snapshots."""

from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import inspect

from platform_core.config import ACCOUNTS, PlatformConfigError, load_platform_config
from platform_core.db import get_state, init_platform_db, put_state
from platform_core.models import PBase
from platform_core.parquet_store import read_snapshots, write_snapshot

ROOT = Path(__file__).resolve().parents[1]

SPEC_VALUES = {   # the owner's starting values (System Spec, 19 Nov 2026), checked one by one
    "capital": 500000, "atr_period": 14, "volume_avg_sessions": 20,
    "swings.major.side_bars": 10, "swings.major.lookback": 250, "swings.minor.side_bars": 5,
    "setups.s1_failed_extension.min_vol_mult": 2.0, "setups.s2_breakout_hold.min_vol_mult": 2.0,
    "setups.s2_breakout_hold.armed_sessions": 20, "setups.s2_breakout_hold.retest_atr": 0.5,
    "setups.s2_breakout_hold.acceptance_closes": 3, "setups.s3_trend_pullback.min_vol_mult": 1.0,
    "setups.s4_range_rejection.min_vol_mult": 1.0, "setups.s4_range_rejection.cluster_atr": 0.5,
    "setups.s4_range_rejection.lookback": 60, "setups.s4_range_rejection.min_height_atr": 3.0,
    "setups.s4_range_rejection.touch_atr": 0.25, "confirmation.window_sessions": 2,
    "confirmation.zone_strikes_per_side": 3, "confirmation.oi_tolerance": 0.10, "strikes.min_rom": 0.10,
    "strikes.width_atr": 1.5, "liquidity.min_oi_value_inr": 30000000, "liquidity.max_spread_pct": 0.05,
    "sizing.risk_at_stop": 0.05, "sizing.iv_cushion_points": 5, "sizing.gap_cap_position": 0.15,
    "sizing.gap_cap_portfolio": 0.25, "sizing.max_positions": 3, "sizing.one_per_sector": True,
    "expiry.min_dte_current_month": 15, "exits.profit_target_capture": 0.70,
    "exits.time_stop_trading_days_before_expiry": 5, "screener.run_at": "15:20", "screener.report_top_n": 20,
    "screener.event_staleness_days": 2, "strength_signals.hvn_lookback": 60, "strength_signals.hvn_bucket_atr": 0.1,
    "paper.extra_slip_ticks": 1, "journal.match_window_sessions": 2, "books.research_enabled": True,
    "books.research_exempt_from_portfolio_limits": True, "market_filter.index": "NIFTY 50",
    "market_filter.swing_side_bars": 5, "review.min_strict_trades_before_changes": 30,
    "review.min_trades_per_filter_group": 30, "costs.brokerage_per_order": 20, "costs.stt_sell_premium": 0.0015,
    "costs.exchange_txn_pct": 0.0003553, "costs.sebi_fee_per_crore": 10, "costs.stamp_buy_pct": 0.00003,
    "costs.gst": 0.18, "control_centre.refresh_seconds": 30, "control_centre.pin_required": True,
}


def _get(obj, dotted):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_shipped_config_has_the_owners_values():
    c = load_platform_config()
    for key, want in SPEC_VALUES.items():
        assert _get(c, key) == pytest.approx(want) if isinstance(want, float) else _get(c, key) == want, key
    assert c.go_live_date is None and c.accounts == list(ACCOUNTS)
    assert c.oi_system.books == {"oi_tight": "main", "oi_loose": "v2"}
    assert (c.settlement.stt_exercise, c.settlement.physical_delivery_brokerage_pct) == (0.0015, 0.0025)
    assert c.data.sectors_file_fresh_days == 90 and c.data.events_file_max_age_days == 1
    assert not c.is_live(date(2026, 12, 1))


def test_config_rejects_unknown_keys_and_wrong_accounts(tmp_path):
    raw = yaml.safe_load((ROOT / "platform.yaml").read_text())
    raw["typo_key"] = 1
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(PlatformConfigError, match="typo_key"):
        load_platform_config(p)
    raw.pop("typo_key")
    raw["accounts"] = ["oi_tight", "level_strict"]
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(PlatformConfigError, match="accounts"):
        load_platform_config(p)
    raw["accounts"] = list(ACCOUNTS)
    raw["go_live_date"] = "2026-12-01"
    p.write_text(yaml.safe_dump(raw))
    c = load_platform_config(p)
    assert c.is_live(date(2026, 12, 1)) and not c.is_live(date(2026, 11, 30))


def test_schema_matches_models_and_state_roundtrip(tmp_path):
    engine = init_platform_db(tmp_path / "platform.db")
    insp = inspect(engine)
    assert set(insp.get_table_names()) - {"schema_migrations"} == set(PBase.metadata.tables)
    for name, table in PBase.metadata.tables.items():
        cols = {c["name"] for c in insp.get_columns(name)}
        assert cols == set(table.columns.keys()), name
    put_state(engine, "k", {"a": 1}, datetime(2026, 11, 19, 8, 45))
    assert get_state(engine, "k") == {"a": 1} and get_state(engine, "missing", 7) == 7


def test_parquet_snapshots(tmp_path):
    ts = datetime(2026, 11, 19, 15, 20)
    path = write_snapshot(tmp_path, ts, [
        {"symbol": "ANGELONE", "expiry": date(2026, 12, 29), "strike": 380, "type": "CE", "bid": 8.1, "ask": 8.4,
         "last_price": 8.3, "oi": 250000, "volume": 12500, "iv": 0.41},
        {"symbol": "KALYANKJIL", "expiry": date(2026, 12, 29), "strike": 640, "type": "PE", "bid": 12.0,
         "ask": 12.5, "last_price": 12.2, "oi": 100000, "volume": 900, "iv": None}])
    assert path.name == "152000.parquet" and path.parent.name == "date=2026-11-19"
    df = read_snapshots(tmp_path, date(2026, 11, 19), "ANGELONE")
    assert len(df) == 1 and df.oi.iloc[0] == 250000 and df.expiry.iloc[0] == "2026-12-29"
    assert read_snapshots(tmp_path, date(2026, 11, 20)).empty
