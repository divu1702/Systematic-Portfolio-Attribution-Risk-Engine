import pytest

from app.config import load_config
from app.safety import (LIVE_CONFIRM_ENV, LIVE_CONFIRM_VALUE, LiveTradingDisabled,
                        live_trading_allowed, require_live)


def test_paper_mode_blocks_live_even_with_flag(cfg):
    env = {LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE}
    assert cfg.mode == "paper"
    assert not live_trading_allowed(cfg, env)
    with pytest.raises(LiveTradingDisabled, match="paper"):
        require_live(cfg, env)


@pytest.mark.parametrize("flag", [None, "", "yes", "true", "1"])
def test_live_mode_needs_exact_confirmation_flag(raw_config, write_config, flag):
    raw_config["mode"] = "live"
    cfg = load_config(write_config(raw_config))
    env = {} if flag is None else {LIVE_CONFIRM_ENV: flag}
    assert not live_trading_allowed(cfg, env)
    with pytest.raises(LiveTradingDisabled):
        require_live(cfg, env)


def test_live_mode_with_flag_is_allowed(raw_config, write_config):
    raw_config["mode"] = "live"
    cfg = load_config(write_config(raw_config))
    env = {LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE}
    assert live_trading_allowed(cfg, env)
    require_live(cfg, env)
