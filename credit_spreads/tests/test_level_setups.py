"""Setup detectors (all four and their mirrors) on hand-built price paths, and the setup state machine:
confirmation windows, Setup 2's two routes, cancellation, expiry and the strict and research books."""

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from level_system.analytics import BEAR_CALL, BULL_PUT, ZoneOI
from level_system.setups import detect
from level_system.state import (ARMED, CANCELLED, CONFIRMED, CONFIRMING, EXPIRED, Machine, Session, step)
from platform_core.config import load_platform_config

CFG = load_platform_config()


def sessions(n, start=date(2025, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_from(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=sessions(len(rows)))


def path(points, half_range=0.5, volume=1000):
    """Closes interpolated linearly between (index, price) waypoints; high/low = close +- half_range."""
    rows = []
    for (i0, p0), (i1, p1) in zip(points, points[1:]):
        for i in range(i0, i1):
            c = p0 + (p1 - p0) * (i - i0) / (i1 - i0)
            rows.append([c, c + half_range, c - half_range, c, volume])
    i_last, p_last = points[-1]
    rows.append([p_last, p_last + half_range, p_last - half_range, p_last, volume])
    return rows


def wave(n, base, amp, period, spread):
    return [[base + amp * math.sin(i / period)] * 1 + [base + amp * math.sin(i / period) + spread,
             base + amp * math.sin(i / period) - spread, base + amp * math.sin(i / period), 1_000_000] for i in range(n)]


def arms_of(bars, setup=None, direction=None):
    a = detect(bars, bars.index[-1], CFG)
    return [x for x in a if (setup is None or x.setup == setup) and (direction is None or x.direction == direction)]


# ----------------------------------------------------------------------------- Setup 1
def angelone_like(day0_close=350.0, day0_high=379.15, day0_volume=8_000_000, broken=False):
    rows = [[335 + 8 * math.sin(i / 9.0), 338 + 8 * math.sin(i / 9.0), 332 + 8 * math.sin(i / 9.0),
             335.5 + 8 * math.sin(i / 9.0), 1_000_000] for i in range(300)]
    rows[200] = [355, 360.45, 350, 352, 1_000_000]                    # the major swing high
    if broken:
        rows[250][3] = 361.0                                          # a later close above it
        rows[250][1] = 362.0
    rows.append([352, day0_high, 349, day0_close, day0_volume])
    return bars_from(rows)


def test_setup1_failed_extension_angelone_shape():
    a = arms_of(angelone_like(), setup=1)
    assert len(a) == 1 and a[0].direction == BEAR_CALL and a[0].level == pytest.approx(360.45)
    assert a[0].volume_mult == pytest.approx(8.0) and a[0].vol_ok and a[0].extreme == pytest.approx(379.15)
    broken = arms_of(angelone_like(broken=True), setup=1, direction=BEAR_CALL)
    assert all(x.level != pytest.approx(360.45) for x in broken)              # a broken level does not count
    weak = arms_of(angelone_like(day0_volume=1_500_000), setup=1)[0]
    assert not weak.vol_ok and weak.volume_mult == pytest.approx(1.5)          # armed; the filter decides the book


def test_setup1_takes_the_highest_crossed_level_and_mirrors():
    rows = [[100 + 3 * math.sin(i / 6.0), 101 + 3 * math.sin(i / 6.0), 99 + 3 * math.sin(i / 6.0),
             100 + 3 * math.sin(i / 6.0), 1000] for i in range(280)]
    rows[150] = [104, 108.0, 103, 104, 1000]
    rows[200] = [106, 112.0, 105, 106, 1000]
    rows.append([106, 115.0, 104.5, 105.0, 2500])
    a = arms_of(bars_from(rows), setup=1, direction=BEAR_CALL)
    assert a[0].level == pytest.approx(112.0) and a[0].detail["crossed"] >= 2
    low = [[100 + 3 * math.sin(i / 6.0), 101 + 3 * math.sin(i / 6.0), 99 + 3 * math.sin(i / 6.0),
            100 + 3 * math.sin(i / 6.0), 1000] for i in range(280)]
    low[200] = [94, 95, 88.0, 94, 1000]
    low.append([94, 95.5, 86.0, 91.0, 2500])
    m = arms_of(bars_from(low), setup=1, direction=BULL_PUT)
    assert m and m[0].level == pytest.approx(88.0) and m[0].extreme == pytest.approx(86.0) and m[0].mirror


# ----------------------------------------------------------------------------- Setup 2
def kalyan_like(gap=True):
    rows = [[610 + 10 * math.sin(i / 7.0), 614 + 10 * math.sin(i / 7.0), 606 + 10 * math.sin(i / 7.0),
             610 + 10 * math.sin(i / 7.0), 1_000_000] for i in range(300)]
    rows[230] = [640, 648.95, 636, 641, 1_000_000]
    rows.append([660 if gap else 645, 680, 655 if gap else 640, 672, 10_000_000])
    return bars_from(rows)


def test_setup2_breakout_hold_kalyan_shape():
    a = arms_of(kalyan_like(), setup=2)
    assert len(a) == 1 and a[0].direction == BULL_PUT and a[0].level == pytest.approx(648.95)
    assert a[0].volume_mult == pytest.approx(10.0) and a[0].vol_ok
    assert a[0].breakout_extreme == pytest.approx(655) and a[0].detail["gap"] is True
    assert arms_of(kalyan_like(gap=False), setup=2)[0].detail["gap"] is False


def test_setup2_mirror_breakdown():
    rows = [[610 + 10 * math.sin(i / 7.0), 614 + 10 * math.sin(i / 7.0), 606 + 10 * math.sin(i / 7.0),
             610 + 10 * math.sin(i / 7.0), 1_000_000] for i in range(300)]
    rows[230] = [580, 584, 571.0, 579, 1_000_000]
    rows.append([565, 568, 548, 552, 3_000_000])
    a = arms_of(bars_from(rows), setup=2, direction=BEAR_CALL)
    assert a and a[0].level == pytest.approx(571.0) and a[0].breakout_extreme == pytest.approx(568)


# ----------------------------------------------------------------------------- Setup 3
UPTREND = [(0, 100), (10, 110), (18, 104), (28, 116), (36, 109), (46, 122), (50, 116)]


def uptrend(day0=(116.5, 119.0, 115.8, 118.5, 1000), points=UPTREND):
    rows = path(points)
    rows.append(list(day0))
    return bars_from(rows)


def test_setup3_trend_pullback():
    a = arms_of(uptrend(), setup=3)
    assert len(a) == 1 and a[0].direction == BULL_PUT
    assert a[0].level == pytest.approx(115.5)                         # the pullback's lowest low (session 50)
    assert a[0].detail["pullback_sessions"] == 4 and a[0].detail["swing_low"] == pytest.approx(108.5)
    assert a[0].vol_ok                                                # 1x average volume is enough
    assert not arms_of(uptrend(day0=(116.5, 116.8, 115.8, 116.4, 1000)), setup=3)   # no close above yesterday's high
    short = [(0, 100), (10, 110), (18, 104), (28, 116), (36, 109), (48, 122), (49, 120)]
    assert not arms_of(uptrend(day0=(120.0, 121.5, 119.6, 121.2, 1000), points=short), setup=3)  # 1-session pullback
    deep = [(0, 100), (10, 110), (18, 104), (28, 116), (36, 109), (46, 122), (50, 108)]
    assert not arms_of(uptrend(day0=(108.2, 110.0, 107.9, 109.0, 1000), points=deep), setup=3)  # below the swing low


def test_setup3_mirror_in_a_downtrend():
    pts = [(i, 300 - p) for i, p in UPTREND]
    rows = path(pts)
    rows.append([300 - 116.5, 300 - 115.8, 300 - 119.0, 300 - 118.5, 1000])
    a = arms_of(bars_from(rows), setup=3)
    assert len(a) == 1 and a[0].direction == BEAR_CALL and a[0].level == pytest.approx(300 - 115.5)


# ----------------------------------------------------------------------------- Setup 4
RANGE = [(0, 100), (8, 110.0), (18, 90.0), (28, 110.4), (38, 90.3), (48, 110.2)]


def ranged(day0):
    rows = path(RANGE)
    rows.append(list(day0))
    return bars_from(rows)


def test_setup4_range_rejection():
    a = arms_of(ranged((109.8, 110.0, 108.2, 108.5, 1000)), setup=4)
    assert len(a) == 1 and a[0].direction == BEAR_CALL
    assert a[0].level == pytest.approx((110.5 + 110.9) / 2)          # the average of the clustered swing highs
    assert a[0].detail["touch_date"] == ranged((0, 0, 0, 0, 0)).index[48].isoformat()
    assert not arms_of(ranged((110.0, 110.4, 109.8, 110.1, 1000)), setup=4)   # no close below yesterday's low


def test_setup4_yields_to_setup1_on_the_same_move():
    # Day 0 pushes above the major swing high at 110.9 (still within the touch band) and closes back below.
    bars = ranged((110.2, 111.0, 108.2, 108.5, 3000))
    kinds = {(a.setup, a.direction) for a in detect(bars, bars.index[-1], CFG)}
    assert (1, BEAR_CALL) in kinds and (4, BEAR_CALL) not in kinds


def test_setup4_mirror_at_the_bottom():
    pts = [(i, 200 - p) for i, p in RANGE]
    rows = path(pts)
    rows.append([200 - 109.8, 200 - 108.2, 200 - 110.0, 200 - 108.5, 1000])
    a = arms_of(bars_from(rows), setup=4)
    assert len(a) == 1 and a[0].direction == BULL_PUT and a[0].level == pytest.approx(200 - (110.5 + 110.9) / 2)


def test_not_enough_history_arms_nothing():
    b = bars_from(path([(0, 100), (10, 110)]))
    assert detect(b, b.index[-1], CFG) == [] and detect(b, date(1999, 1, 1), CFG) == []


# ----------------------------------------------------------------------------- state machine
def z(protected, other):
    return ZoneOI(protected=protected, other=other, calls=other, puts=protected, chain_total=10 * (protected + other))


DAY0 = z(100_000, 50_000)


def machine(setup=1, direction=BEAR_CALL, level=100.0, vol_ok=True):
    return Machine(setup, direction, level, date(2026, 11, 17), atr=2.0, vol_ok=vol_ok, day0_zone=DAY0)


def sess(i, close, high=None, low=None, zone=DAY0):
    return Session(date(2026, 11, 17) + timedelta(days=i), close, high if high is not None else close + 0.5,
                   low if low is not None else close - 0.5, zone)


def test_first_passing_session_confirms_into_the_strict_book():
    m = machine()
    r = step(m, sess(1, 98.0), CFG)
    assert r.state == CONFIRMED and r.strict and not r.research and r.record["oi"]["passed"]


def test_each_book_enters_at_its_own_first_qualifying_session():
    m = machine()
    r1 = step(m, sess(1, 98.0, zone=z(85_000, 50_000)), CFG)          # protected-side OI -15%
    assert r1.state == CONFIRMING and r1.research and not r1.strict and "zone_oi" in r1.failed_filters
    r2 = step(m, sess(2, 99.0, zone=z(95_000, 54_000)), CFG)
    assert r2.state == CONFIRMED and r2.strict and not r2.research    # the research book already entered


def test_a_close_beyond_the_level_cancels():
    m = machine()
    assert step(m, sess(1, 100.5), CFG).state == CANCELLED
    with pytest.raises(ValueError):
        step(m, sess(2, 99.0), CFG)
    bull = machine(direction=BULL_PUT)
    assert step(bull, sess(1, 99.9), CFG).state == CANCELLED


def test_window_expires_when_no_session_passes():
    m = machine()
    r1 = step(m, sess(1, 98.0, zone=z(100_000, 60_000)), CFG)         # other-side OI +20%
    r2 = step(m, sess(2, 98.0, zone=z(100_000, 58_000)), CFG)
    assert r1.research and r2.state == EXPIRED and not r2.research and not r2.strict
    m2 = machine()
    r = step(m2, sess(1, 98.0, zone=None), CFG)                        # no snapshot: the OI test cannot pass
    assert r.state == CONFIRMING and r.research and "no OI snapshot" in r.failed_filters["zone_oi"]["reason"]


def test_failed_volume_sends_a_passing_setup_to_the_research_book():
    m = machine(vol_ok=False)
    r = step(m, sess(1, 98.0), CFG)
    assert r.state == CONFIRMED and r.research and not r.strict and "volume" in r.failed_filters


def s2(direction=BULL_PUT, level=100.0):
    return machine(setup=2, direction=direction, level=level)


def test_setup2_route_b_three_acceptance_closes():
    m = s2()
    assert step(m, sess(1, 104.0, low=103.0), CFG).state == ARMED      # low stays more than 0.5 x ATR (1.0) away
    assert step(m, sess(2, 105.0, low=104.0), CFG).state == ARMED
    r = step(m, sess(3, 106.0, low=105.0), CFG)
    assert r.state == CONFIRMED and m.route == "B" and r.strict


def test_setup2_route_b_without_the_oi_test_then_route_a():
    m = s2()
    step(m, sess(1, 104.0, low=103.0), CFG)
    step(m, sess(2, 105.0, low=104.0, zone=z(80_000, 50_000)), CFG)  # OI test fails on an acceptance close
    r3 = step(m, sess(3, 106.0, low=105.0), CFG)
    assert r3.state == ARMED and r3.research and not r3.strict and m.route == "B-failed"
    r4 = step(m, sess(4, 101.5, low=100.8), CFG)                       # retest: low within 1.0 of the level
    assert r4.state == CONFIRMING and m.route == "A" and r4.record["event"] == "retest"
    r5 = step(m, sess(5, 102.0, low=101.0), CFG)
    assert r5.state == CONFIRMED and r5.strict and not r5.research


def test_setup2_route_a_and_cancel_before_retest():
    m = s2()
    r1 = step(m, sess(1, 100.4, low=99.5), CFG)                        # dips below intraday, closes above
    assert r1.state == CONFIRMING and m.route == "A" and not (r1.strict or r1.research)
    assert step(m, sess(2, 101.0), CFG).state == CONFIRMED
    c = s2()
    step(c, sess(1, 104.0, low=103.0), CFG)
    assert step(c, sess(2, 99.5, low=99.0), CFG).state == CANCELLED   # a close back below before any retest
    mirror = s2(direction=BEAR_CALL)
    assert step(mirror, sess(1, 99.6, high=100.6), CFG).state == CONFIRMING   # retest from below


def test_setup2_expires_after_twenty_armed_sessions():
    m = s2()
    step(m, sess(1, 104.0, low=103.0, zone=z(80_000, 50_000)), CFG)
    step(m, sess(2, 105.0, low=104.0), CFG)
    step(m, sess(3, 106.0, low=105.0), CFG)
    states = [step(m, sess(k, 110.0, low=109.0), CFG).state for k in range(4, 21)]
    assert states[:-1] == [ARMED] * 16 and states[-1] == EXPIRED


def test_setups_persist_and_resume(tmp_path):
    import json
    from datetime import datetime

    from level_system import store
    from level_system.setups import Arm
    from platform_core.db import init_platform_db

    pengine = init_platform_db(tmp_path / "p.db")
    arm = Arm(2, BULL_PUT, 648.95, date(2026, 11, 11), atr=12.0, volume_mult=10.0, min_vol_mult=2.0,
              breakout_extreme=655.0)
    m = Machine(2, BULL_PUT, 648.95, arm.day0, 12.0, True, DAY0)
    now = datetime(2026, 11, 11, 15, 21)
    sid = store.create(pengine, "KALYANKJIL", arm, m, expiry=date(2026, 12, 29),
                       zone={"protected": [640, 630, 620], "other": [650, 660, 670]}, day0_oi={"640PE": 1},
                       signals={"hvn_dist_atr": -0.4}, now=now)
    assert store.duplicate(pengine, "KALYANKJIL", 2, BULL_PUT, 649.0).id == sid
    row = store.active(pengine)[0]
    m2 = store.machine_from_row(row)
    assert m2 == m
    st = step(m2, Session(date(2026, 11, 12), 660.0, 662.0, 652.0, DAY0), CFG)   # low within 0.5 x 12 of the level
    store.save_step(pengine, sid, m2, st, date(2026, 11, 12), now)
    row = store.get(pengine, sid)
    assert store.machine_from_row(row).sessions_since_day0 == 1 and row.last_session == date(2026, 11, 12)
    assert json.loads(row.sessions_json)[0]["event"] == "retest" and row.route == "A"
    store.link_trade(pengine, sid, "strict", "T1")
    assert store.get(pengine, sid).strict_trade_id == "T1"
    with __import__("sqlalchemy.orm", fromlist=["Session"]).Session(pengine) as s:
        from platform_core.models import LevelRow
        lv = s.query(LevelRow).one()
        assert lv.setup_id == sid and lv.breakout_low == 655.0 and lv.hvn_dist_atr == -0.4
