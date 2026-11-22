"""The spec's two required detector tests on real days (milestone 6).

The daily candles are recorded from Kite on the owner's laptop after the first login
(`python -m platform_core record-fixtures`) into tests/fixtures/realdays/. Until then these tests skip;
they never run on made-up data.
"""

from datetime import date

import pytest

from level_system import fixtures
from level_system.analytics import BEAR_CALL, BULL_PUT
from level_system.setups import detect
from platform_core.config import load_platform_config

CFG = load_platform_config()
FIXTURES = CFG.resolve(CFG.paths.fixtures)


def bars_or_skip(symbol: str):
    df = fixtures.load(FIXTURES, symbol)
    if df is None:
        pytest.skip(f"no recorded candles for {symbol}: run `python -m platform_core record-fixtures` on the laptop")
    return df


def test_angelone_17_nov_2026_arms_setup_1():
    d0 = date(2026, 11, 17)
    bars = bars_or_skip("ANGELONE")
    bars = bars[bars.index <= d0]
    row = bars.loc[d0]
    assert row["high"] == pytest.approx(379.15, abs=0.05)
    arms = [a for a in detect(bars, d0, CFG) if a.setup == 1 and a.direction == BEAR_CALL]
    assert arms, "Setup 1 did not arm"
    assert arms[0].level == pytest.approx(360.45, abs=0.05)
    # 66.6M shares against a 20-day average of 6.0M (from the recorded candles, not news reports).
    assert arms[0].vol_ok and arms[0].volume_mult == pytest.approx(11.1, abs=0.1)


def test_kalyan_jewellers_11_nov_2026_arms_setup_2():
    d0 = date(2026, 11, 11)
    bars = bars_or_skip("KALYANKJIL")
    bars = bars[bars.index <= d0]
    arms = [a for a in detect(bars, d0, CFG) if a.setup == 2 and a.direction == BULL_PUT]
    assert arms, "Setup 2 did not arm"
    assert arms[0].level == pytest.approx(648.95, abs=0.05)
    # Setup 2 needs no gap: it opened at 630.00, below the 648.95 level, so the detector's gap is False.
    assert arms[0].detail["gap"] is False
    assert arms[0].volume_mult >= 2.0
