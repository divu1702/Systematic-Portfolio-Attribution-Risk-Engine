"""Black-76 pricing for European options on a forward.

Indian stock options are European. Using the same-expiry futures price as the forward
avoids estimating dividends and cost of carry (spec: "Computing IV").

Conventions: F forward, K strike, T years to expiry, sigma annualised vol, r continuously
compounded risk-free rate, cp = "CE" (call) or "PE" (put). Scalar math uses math.erf
because scipy.stats.norm is slow per call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from scipy.optimize import brentq

SQRT2 = math.sqrt(2.0)
INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _check(cp: str) -> bool:
    if cp == "CE":
        return True
    if cp == "PE":
        return False
    raise ValueError(f"cp must be 'CE' or 'PE', got {cp!r}")


def d1_d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    sv = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / sv
    return d1, d1 - sv


def price(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> float:
    is_call = _check(cp)
    df = math.exp(-r * T)
    if T <= 0 or sigma <= 0:
        intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
        return df * intrinsic
    d1, d2 = d1_d2(F, K, T, sigma)
    if is_call:
        return df * (F * norm_cdf(d1) - K * norm_cdf(d2))
    return df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float      # dV/dF
    gamma: float      # d2V/dF2
    vega: float       # dV/dsigma, per 1.00 of vol (divide by 100 for per vol point)
    theta: float      # dV/dt per calendar year (negative = decay)


def greeks(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> Greeks:
    is_call = _check(cp)
    df = math.exp(-r * T)
    p = price(F, K, T, sigma, r, cp)
    if T <= 0 or sigma <= 0:
        itm = (F > K) if is_call else (F < K)
        delta = (df if is_call else -df) if itm else 0.0
        return Greeks(p, delta, 0.0, 0.0, 0.0)
    d1, _ = d1_d2(F, K, T, sigma)
    sqrt_t = math.sqrt(T)
    pdf = norm_pdf(d1)
    delta = df * norm_cdf(d1) if is_call else -df * norm_cdf(-d1)
    gamma = df * pdf / (F * sigma * sqrt_t)
    vega = df * F * pdf * sqrt_t
    theta = -df * F * pdf * sigma / (2.0 * sqrt_t) + r * p
    return Greeks(p, delta, gamma, vega, theta)


def delta(F: float, K: float, T: float, sigma: float, r: float, cp: str) -> float:
    return greeks(F, K, T, sigma, r, cp).delta


def implied_vol(px: float, F: float, K: float, T: float, r: float, cp: str,
                lo: float = 0.01, hi: float = 5.0) -> Optional[float]:
    """Back out sigma with Brent's method. Returns None if no vol in [lo, hi] fits the price
    (price below discounted intrinsic, above the no-arbitrage bound, or T <= 0)."""
    is_call = _check(cp)
    if T <= 0 or px <= 0 or F <= 0 or K <= 0:
        return None
    df = math.exp(-r * T)
    intrinsic = df * (max(F - K, 0.0) if is_call else max(K - F, 0.0))
    upper = df * (F if is_call else K)
    if px <= intrinsic or px >= upper:
        return None
    f = lambda s: price(F, K, T, s, r, cp) - px  # noqa: E731
    f_lo, f_hi = f(lo), f(hi)
    if f_lo > 0 or f_hi < 0:
        return None
    if f_lo == 0:
        return lo
    return brentq(f, lo, hi, xtol=1e-8, rtol=1e-10, maxiter=200)


def prob_above(F: float, X: float, T: float, sigma: float) -> float:
    """Risk-neutral probability that the underlying finishes above X at expiry (lognormal
    forward with volatility sigma): N(d2)."""
    if X <= 0:
        return 1.0
    if T <= 0 or sigma <= 0:
        return 1.0 if F > X else 0.0
    _, d2 = d1_d2(F, X, T, sigma)
    return norm_cdf(d2)


def year_fraction(seconds: float) -> float:
    """Calendar-time year fraction."""
    return max(seconds, 0.0) / (365.0 * 86400.0)
