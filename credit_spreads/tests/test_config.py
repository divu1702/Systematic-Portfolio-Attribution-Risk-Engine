import pytest

from app.config import Config, ConfigError, load_config


def test_shipped_config_loads_with_spec_defaults():
    cfg = load_config()
    assert cfg.mode == "paper"
    assert cfg.capital == 500_000
    assert cfg.universe.top_n is None                 # owner: all F&O stocks
    assert cfg.risk.sizing_basis == "stop" and cfg.risk.gap_cap_per_trade == 0.05
    assert (cfg.liquidity.min_oi_lots, cfg.liquidity.min_vol_lots) == (0, 0)
    assert (cfg.dte_min, cfg.dte_max) == (15, 35)
    assert cfg.iv.min_iv_hv_ratio == 1.25
    assert cfg.iv.min_ivp == 70
    assert (cfg.strikes.delta_min, cfg.strikes.delta_max) == (0.20, 0.30)
    assert cfg.spread.widths_steps == [1, 2, 3, 4]
    assert cfg.exit.stop_mult == 2.0 and cfg.exit.tp_frac == 0.5
    assert cfg.risk.per_trade == 0.02 and cfg.risk.max_total == 0.08
    assert cfg.live.max_ops == 2
    assert cfg.costs.stt_sell_premium == 0.0015


def test_exchange_txn_charge_is_set():
    cfg = load_config()
    assert cfg.costs.exchange_txn_pct == 0.0003503
    assert cfg.costs.missing == []


def test_pydantic_defaults_match_shipped_file():
    """config.yaml and the model defaults should not drift apart."""
    shipped = load_config()
    defaults = Config(base_dir=shipped.base_dir)
    assert shipped.model_dump() == defaults.model_dump()


def test_paths_resolve_relative_to_config_file(cfg, tmp_path):
    assert cfg.db_path == (tmp_path / "data" / "test.db").resolve()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.update(mode="yolo"), "mode"),
        (lambda r: r.update(typo_key=1), "typo_key"),
        (lambda r: r["iv"].update(min_ivp_typo=1), "min_ivp_typo"),
        (lambda r: r["strikes"].update(delta_min=0.4), "delta_min must be below"),
        (lambda r: r.update(dte_min=40), "dte_min must be below dte_max"),
        (lambda r: r["exit"].update(days_before_expiry=20), "days_before_expiry"),
        (lambda r: r["risk"].update(max_same_direction=9), "max_same_direction"),
        (lambda r: r["risk"].update(per_trade=0.2), "per_trade cannot exceed"),
        (lambda r: r["risk"].update(per_trade=1.5), "per_trade"),
        (lambda r: r["live"].update(max_ops=5), "max_ops"),
        (lambda r: r["live"].update(require_approval=False), "semi-automatic"),
        (lambda r: r["spread"].update(widths_steps=[1, 1]), "duplicates"),
        (lambda r: r["schedule"].update(scan="3pm"), "HH:MM"),
        (lambda r: r["iv"].update(ivp_min_history=400), "ivp_min_history"),
        (lambda r: r["costs"].update(gst=-1), "gst"),
    ],
)
def test_invalid_config_is_rejected(raw_config, write_config, mutate, message):
    mutate(raw_config)
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(raw_config))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_config_is_immutable(cfg):
    with pytest.raises(Exception):
        cfg.risk.per_trade = 0.5
