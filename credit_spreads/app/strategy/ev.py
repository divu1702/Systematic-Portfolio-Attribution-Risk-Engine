"""Win probability and expected value for a credit spread (spec: strike selection steps 5-7).

p_market: risk-neutral probability of finishing beyond breakeven at market IV (Black-76).
p_model:  the same with forecast vol = w * HV20 + (1 - w) * IV.
EV = p * credit - (1 - p) * average loss, where the average loss is the expected loss given a
loss under the stop rule (close at stop_mult x credit, i.e. lose (stop_mult - 1) x credit),
capped at maximum loss. Losses smaller than the stop (finishing just past breakeven) count
at their actual size.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..analytics import black76
from .spreads import BULL_PUT


def win_prob(side: str, F: float, breakeven: float, T: float, sigma: float) -> float:
    above = black76.prob_above(F, breakeven, T, sigma)
    return above if side == BULL_PUT else 1.0 - above


def forecast_vol(hv20: float, iv: float, hv_weight: float) -> float:
    return hv_weight * hv20 + (1.0 - hv_weight) * iv


def avg_loss_given_loss(side: str, F: float, breakeven: float, T: float, sigma: float, credit: float,
                        width: float, stop_mult: float, n: int = 400) -> float:
    """Expected loss per share conditional on finishing beyond breakeven, each loss capped at
    min(stop loss, max loss)."""
    max_loss = width - credit
    cap = min((stop_mult - 1.0) * credit, max_loss)
    if cap <= 0 or T <= 0 or sigma <= 0:
        return max(cap, 0.0)
    sv = sigma * math.sqrt(T)
    z_be = (math.log(breakeven / F) + 0.5 * sv * sv) / sv          # S_T = BE at this z
    if side == BULL_PUT:
        z = np.linspace(-8.0, z_be, n)                               # S below breakeven
    else:
        z = np.linspace(z_be, 8.0, n)                                # S above breakeven
    s = F * np.exp(-0.5 * sv * sv + sv * z)
    dist = (breakeven - s) if side == BULL_PUT else (s - breakeven)
    loss = np.clip(dist, 0.0, cap)
    pdf = np.exp(-0.5 * z * z)
    mass = np.trapezoid(pdf, z)
    if mass <= 0:
        return cap
    return float(np.trapezoid(loss * pdf, z) / mass)


@dataclass(frozen=True)
class EVResult:
    p_market: float
    p_model: float
    sigma_market: float
    sigma_model: float
    avg_loss: float           # per share
    ev: float                 # rupees per lot
    ev_ratio: float           # ev / max loss per lot


def evaluate(side: str, F: float, breakeven: float, T: float, iv: float, hv20: float, credit: float,
             width: float, lot: int, hv_weight: float, stop_mult: float) -> EVResult:
    p_mkt = win_prob(side, F, breakeven, T, iv)
    s_model = forecast_vol(hv20, iv, hv_weight)
    p_mod = win_prob(side, F, breakeven, T, s_model)
    avg_loss = avg_loss_given_loss(side, F, breakeven, T, s_model, credit, width, stop_mult)
    ev_ps = p_mod * credit - (1.0 - p_mod) * avg_loss
    max_loss_lot = (width - credit) * lot
    ev_lot = ev_ps * lot
    return EVResult(p_mkt, p_mod, iv, s_model, avg_loss, ev_lot,
                    ev_lot / max_loss_lot if max_loss_lot > 0 else float("-inf"))


# --------------------------------------------------------------------------- path simulation
#
# Owner decision (Nov 2026): replace the expiry-based EV with a simulation of the actual exit
# rules. A spread that touches its stop mid-trade is closed at a loss even if the stock would have
# recovered by expiry, so "finishes beyond breakeven" overstates the win rate.
#
# Model: the futures price follows a driftless lognormal path with the given vol; each leg is
# repriced with Black-76 at its entry IV (IV held constant, which ignores the IV fall the
# strategy hopes for, so it errs on the cautious side); the cost to close is at natural prices
# (short at mid + entry half-spread, long at mid - entry half-spread). The rules are checked
# `steps_per_day` times a day in the spec's priority: hard stop, thesis stop (once a day, at the
# close), profit target; whatever is still open is closed at the time stop. Event exits are not
# modelled. P&L per share = net credit (after round-trip costs) - cost to close at exit.

EXIT_NAMES = ("hard_stop", "thesis_stop", "profit_target", "time_stop")


@dataclass(frozen=True)
class PathEVResult:
    p_market: float           # share of paths with positive P&L at market IV
    p_model: float            # same at forecast vol (w x HV20 + (1 - w) x IV)
    sigma_market: float
    sigma_model: float
    avg_loss: float           # per share, mean loss of losing paths (model)
    ev: float                 # rupees per lot (model)
    ev_ratio: float           # ev / max loss per lot
    exit_mix: dict            # share of paths by exit rule (model)
    pnl_p05: float            # 5th percentile P&L per share (model)


def simulate_exits(side: str, F0: float, short_k: float, long_k: float, iv_short: float, iv_long: float,
                   sigma: float, T0: float, T_exit: float, n_days: int, credit_gross: float, credit_nat: float,
                   half_short: float, half_long: float, level: float | None, stop_mult: float, tp_frac: float,
                   thesis_stop: bool, r: float, z: np.ndarray, steps_per_day: int):
    """Returns (pnl per share array, exit-rule index array). `z` is (n_paths, n_days * steps_per_day)
    standard normals, shared between vols so the comparison uses the same paths."""
    from ..analytics.black76 import price_vec
    from .spreads import option_type

    cp = option_type(side)
    n_paths, n_steps = z.shape
    # One clock for both diffusion and repricing: calendar time, as in Black-76, so that with vol =
    # IV and no exit rules the expected P&L is zero (the spread is fairly priced). The trading days
    # only set how many checkpoints there are.
    dT = (T0 - T_exit) / max(n_steps, 1)
    dt = dT
    F = np.full(n_paths, float(F0))
    open_ = np.ones(n_paths, dtype=bool)
    exit_ctc = np.zeros(n_paths)
    reason = np.full(n_paths, 3)                             # default: time stop
    stop_at, tp_at = stop_mult * credit_gross, tp_frac * credit_gross
    drift = -0.5 * sigma * sigma * dt
    vol = sigma * math.sqrt(dt)
    ctc = np.zeros(n_paths)
    for j in range(n_steps):
        F = F * np.exp(drift + vol * z[:, j])
        T = max(T0 - (j + 1) * dT, 0.0)
        vs = price_vec(F, short_k, T, iv_short, r, cp)
        vl = price_vec(F, long_k, T, iv_long, r, cp)
        ctc = (vs + half_short) - np.maximum(vl - half_long, 0.0)
        hit_stop = open_ & (ctc >= stop_at)
        exit_ctc[hit_stop], reason[hit_stop] = ctc[hit_stop], 0
        open_ &= ~hit_stop
        if thesis_stop and level is not None and (j + 1) % steps_per_day == 0:
            beyond = (F < level) if side == BULL_PUT else (F > level)
            hit = open_ & beyond
            exit_ctc[hit], reason[hit] = ctc[hit], 1
            open_ &= ~hit
        hit_tp = open_ & (ctc <= tp_at)
        exit_ctc[hit_tp], reason[hit_tp] = ctc[hit_tp], 2
        open_ &= ~hit_tp
        if not open_.any():
            break
    exit_ctc[open_] = ctc[open_]
    return credit_nat - exit_ctc, reason


def evaluate_paths(side: str, F: float, short_k: float, long_k: float, iv_short: float, iv_long: float,
                   hv20: float, T0: float, T_exit: float, n_days: int, credit_gross: float, credit_nat: float,
                   width: float, lot: int, half_short: float, half_long: float, level: float | None, cfg,
                   seed: int) -> PathEVResult:
    e, x = cfg.ev, cfg.exit
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((e.n_paths, max(1, n_days) * e.steps_per_day))
    s_model = forecast_vol(hv20, iv_short, e.hv_weight)
    common = dict(side=side, F0=F, short_k=short_k, long_k=long_k, iv_short=iv_short, iv_long=iv_long, T0=T0,
                  T_exit=T_exit, n_days=n_days, credit_gross=credit_gross, credit_nat=credit_nat,
                  half_short=half_short, half_long=half_long, level=level, stop_mult=x.stop_mult,
                  tp_frac=x.tp_frac, thesis_stop=x.thesis_stop, r=cfg.analytics.risk_free, z=z,
                  steps_per_day=e.steps_per_day)
    pnl_mkt, _ = simulate_exits(sigma=iv_short, **common)
    pnl, why = simulate_exits(sigma=s_model, **common)
    losses = pnl[pnl < 0]
    ev_lot = float(pnl.mean()) * lot
    max_loss_lot = (width - credit_nat) * lot
    mix = {name: float((why == i).mean()) for i, name in enumerate(EXIT_NAMES)}
    return PathEVResult(float((pnl_mkt > 0).mean()), float((pnl > 0).mean()), iv_short, s_model,
                        float(-losses.mean()) if losses.size else 0.0, ev_lot,
                        ev_lot / max_loss_lot if max_loss_lot > 0 else float("-inf"), mix,
                        float(np.percentile(pnl, 5)))
