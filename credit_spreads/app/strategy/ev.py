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
