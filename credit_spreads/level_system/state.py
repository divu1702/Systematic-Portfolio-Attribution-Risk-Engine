"""The setup state machine (spec: Confirmation and kill rule; Setup 2 routes; Hard rules, quality filters
and the two books).

States: armed -> confirming -> confirmed | cancelled | expired. No trade is entered on Day 0.

- Setups 1, 3 and 4: the confirmation window is the 2 sessions after Day 0.
- Setup 2 stays armed for 20 sessions after Day 0. Route A: a session whose low comes within 0.5 x ATR of
  the level (or dips below it) but closes above it is the retest; the window is the 2 sessions after it.
  Route B: the 3 sessions after Day 0 all close above the level without coming within 0.5 x ATR; it
  confirms at the third close if the zone OI test passes on each of the three. A close back below the
  level before any retest cancels the setup. (Mirrors: below/above swapped.)
- A session closing beyond the level cancels the setup. A session passes when it does not close beyond
  the level and the zone OI test passes against Day 0; the first passing session confirms the setup.
  If no session in the window passes, the setup expires.

Books (owner, 19 Nov 2026: each book enters at the first session that qualifies for it):
- strict qualifies on a session that passes and when Day 0 volume met its threshold;
- research qualifies on a session that keeps the hard rule (no close beyond the level) while at least one
  quality filter fails (Day 0 volume, or the zone OI test on that session).
The return-on-margin filter is applied later, at strike selection, by the trading engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from .analytics import BEAR_CALL, BULL_PUT, OITest, ZoneOI, oi_test

ARMED, CONFIRMING, CONFIRMED, CANCELLED, EXPIRED = "armed", "confirming", "confirmed", "cancelled", "expired"
ACTIVE = (ARMED, CONFIRMING)


@dataclass
class Session:
    """One session's values at the 15:20 snapshot (or from the official close when catching up)."""
    date: date
    close: float
    high: float
    low: float
    zone: Optional[ZoneOI] = None          # zone OI now (None: no snapshot)
    atr: Optional[float] = None            # ATR known at this session's open


@dataclass
class Machine:
    setup: int
    direction: str
    level: float
    day0: date
    atr: float                             # Day 0 ATR (fallback when a session has none)
    vol_ok: bool
    day0_zone: Optional[ZoneOI]
    state: str = ARMED
    route: Optional[str] = None            # Setup 2: "A" or "B"
    sessions_since_day0: int = 0
    window_seen: int = 0
    retest_date: Optional[date] = None
    route_b_oi: List[bool] = field(default_factory=list)
    strict_done: bool = False              # the strict book has entered (or no longer can)
    research_done: bool = False


@dataclass
class Step:
    """The outcome of one session for one setup."""
    state: str
    record: dict
    strict: bool = False                   # enter the strict book at this session
    research: bool = False                 # enter the research book at this session
    failed_filters: Dict[str, object] = field(default_factory=dict)
    note: str = ""


def beyond(direction: str, level: float, close: float) -> bool:
    """A close beyond the level: above it for bear calls, below it for bull puts."""
    return close > level if direction == BEAR_CALL else close < level


def near(direction: str, level: float, s: Session, dist: float) -> bool:
    """Setup 2 retest: the session's extreme comes within `dist` of the level or crosses it intraday."""
    return s.low <= level + dist if direction == BULL_PUT else s.high >= level - dist


def _oi(m: Machine, s: Session, tol: float) -> OITest:
    if m.day0_zone is None:
        return OITest(False, None, None, "no Day 0 OI snapshot")
    if s.zone is None:
        return OITest(False, None, None, "no OI snapshot for this session")
    return oi_test(m.day0_zone, s.zone, tol)


def _record(m: Machine, s: Session, oi: Optional[OITest], **extra) -> dict:
    r = {"date": s.date.isoformat(), "k": m.sessions_since_day0, "close": s.close, "high": s.high, "low": s.low,
         "beyond": beyond(m.direction, m.level, s.close)}
    if oi is not None:
        r["oi"] = {"passed": oi.passed, "protected_change": oi.protected_change, "other_change": oi.other_change,
                   "reason": oi.reason}
    r.update(extra)
    return r


def _books(m: Machine, s: Session, passes: bool, oi: Optional[OITest], record: dict, state: str,
           note: str = "") -> Step:
    """Which books enter at this session (the hard price rule already held)."""
    failed: Dict[str, object] = {}
    if not m.vol_ok:
        failed["volume"] = "Day 0 volume below its threshold"
    if oi is not None and not oi.passed:
        failed["zone_oi"] = {"protected_change": oi.protected_change, "other_change": oi.other_change,
                             "reason": oi.reason}
    strict = passes and m.vol_ok and not m.strict_done
    research = bool(failed) and not m.research_done
    if strict:
        m.strict_done = True
    if research:
        m.research_done = True
    record.update(strict=strict, research=research)
    return Step(state, record, strict, research, failed, note)


def step(m: Machine, s: Session, cfg) -> Step:
    """Advance one active setup by one session. Mutates `m` and returns what happened."""
    if m.state not in ACTIVE:
        raise ValueError(f"setup is {m.state}; only armed or confirming setups take sessions")
    m.sessions_since_day0 += 1
    tol = cfg.confirmation.oi_tolerance
    window = cfg.confirmation.window_sessions
    if beyond(m.direction, m.level, s.close):
        m.state = CANCELLED
        why = "closed beyond the level" + (" before any retest" if m.setup == 2 and m.state == ARMED else "")
        return Step(CANCELLED, _record(m, s, None, event="cancel"), note=why)
    if m.setup == 2 and m.route != "A":
        return _step_setup2_armed(m, s, cfg, tol)
    # Generic confirmation window (Setups 1, 3, 4, and Setup 2 after a Route A retest).
    m.window_seen += 1
    oi = _oi(m, s, tol)
    passes = oi.passed
    if passes:
        m.state = CONFIRMED
    elif m.window_seen >= window or (m.setup == 2 and m.sessions_since_day0 >= cfg.setups.s2_breakout_hold.armed_sessions):
        m.state = EXPIRED
    else:
        m.state = CONFIRMING
    rec = _record(m, s, oi, window_session=m.window_seen, route=m.route)
    return _books(m, s, passes, oi, rec, m.state)


def _step_setup2_armed(m: Machine, s: Session, cfg, tol: float) -> Step:
    s2 = cfg.setups.s2_breakout_hold
    dist = s2.retest_atr * (s.atr or m.atr)
    k = m.sessions_since_day0
    if near(m.direction, m.level, s, dist):
        m.route, m.retest_date, m.window_seen = "A", s.date, 0
        m.state = CONFIRMING if k < s2.armed_sessions else EXPIRED
        return Step(m.state, _record(m, s, None, event="retest", route="A"),
                    note="retest: confirmation window starts next session" if m.state == CONFIRMING else
                    "retest on the last armed session: no window left")
    if k <= s2.acceptance_closes and m.route is None:
        oi = _oi(m, s, tol)
        m.route_b_oi.append(oi.passed)
        rec = _record(m, s, oi, event="acceptance", acceptance=k, route="B?")
        if k < s2.acceptance_closes:
            return Step(ARMED, rec)
        passes = all(m.route_b_oi)
        if passes:
            m.state, m.route = CONFIRMED, "B"
            rec["route"] = "B"
            return _books(m, s, True, oi, rec, CONFIRMED, "Route B: three acceptance closes")
        m.route = "B-failed"                        # Route A can still confirm it later
        rec["route"] = "B-failed"
        failed_oi = OITest(False, oi.protected_change, oi.other_change,
                           "zone OI test failed on an acceptance session: " +
                           ", ".join(str(i + 1) for i, ok in enumerate(m.route_b_oi) if not ok))
        st = EXPIRED if k >= s2.armed_sessions else ARMED
        m.state = st
        return _books(m, s, False, failed_oi, rec, st, "Route B: acceptance closes without the OI test")
    rec = _record(m, s, None, event="armed")
    if k >= s2.armed_sessions:
        m.state = EXPIRED
        return Step(EXPIRED, rec, note=f"not confirmed within {s2.armed_sessions} sessions")
    return Step(ARMED, rec)


def machine_from_arm(arm, day0_zone: Optional[ZoneOI], vol_ok: Optional[bool] = None) -> Machine:
    return Machine(arm.setup, arm.direction, arm.level, arm.day0, arm.atr,
                   arm.vol_ok if vol_ok is None else vol_ok, day0_zone)
