"""Implied volatility and Greeks for European index options (Black-76 on the forward).

Works on numpy arrays, so a whole expiry's candles are solved at once.

Why the forward, not the index: index options price off the forward F, and on
expiry afternoons off the expected settlement (a weighted average of the last 30
minutes), not the last index tick. The synthetic forward from put-call parity,
F = K + (C - P) * e^(rT) at a strike where both traded, captures both. With F,
the interest rate barely matters over a week (rT < 0.15%).

Conventions:
  T      years to expiry at the F&O close (15:30 IST; 15:40 from 3 Aug 2026),
         calendar time / 365 (as NSE's option chain)
  delta  per 1 point move in the underlying (call 0..1, put -1..0)
  gamma  change in delta per 1 point
  vega   price change per 1 volatility point (e.g. IV 15% -> 16%)
  theta  price change per calendar day
"""
from __future__ import annotations

import numpy as np
from scipy.special import ndtr

SQRT_2PI = np.sqrt(2 * np.pi)
RATE = 0.065              # roughly the 91-day T-bill; matters little, see above


def _pdf(x):
    return np.exp(-0.5 * x * x) / SQRT_2PI


def black76(F, K, T, sigma, is_call, r=RATE):
    """Option price. All arguments broadcast; is_call is a bool array."""
    F, K, T, sigma = (np.asarray(a, dtype=float) for a in (F, K, T, sigma))
    vt = sigma * np.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * vt * vt) / vt
    d2 = d1 - vt
    disc = np.exp(-r * T)
    call = disc * (F * ndtr(d1) - K * ndtr(d2))
    put = disc * (K * ndtr(-d2) - F * ndtr(-d1))
    return np.where(is_call, call, put)


def implied_vol(price, F, K, T, is_call, r=RATE, lo=1e-4, hi=5.0, iterations=80):
    """Solve for sigma by bisection (always converges; price is monotonic in sigma).

    NaN where no volatility can produce the price: below the discounted intrinsic
    value, at/above the no-arbitrage upper bound, or T <= 0.
    """
    price, F, K, T = (np.asarray(a, dtype=float) for a in (price, F, K, T))
    is_call = np.asarray(is_call, dtype=bool)
    disc = np.exp(-r * T)
    intrinsic = disc * np.where(is_call, np.maximum(F - K, 0), np.maximum(K - F, 0))
    upper = disc * np.where(is_call, F, K)
    valid = (T > 0) & (price > intrinsic) & (price < upper) & np.isfinite(F)
    a = np.full(price.shape, lo)
    b = np.full(price.shape, hi)
    for _ in range(iterations):
        mid = 0.5 * (a + b)
        too_high = black76(F, K, np.where(valid, T, 1.0), mid, is_call, r) > price
        b = np.where(too_high, mid, b)
        a = np.where(too_high, a, mid)
    sigma = 0.5 * (a + b)
    hit_ceiling = sigma > hi * 0.999                   # not a real solution
    return np.where(valid & ~hit_ceiling, sigma, np.nan)


def greeks(F, K, T, sigma, is_call, r=RATE) -> dict[str, np.ndarray]:
    """delta, gamma, vega, theta (see module docstring for units). Spot = F * e^(-rT)."""
    F, K, T, sigma = (np.asarray(a, dtype=float) for a in (F, K, T, sigma))
    is_call = np.asarray(is_call, dtype=bool)
    S = F * np.exp(-r * T)
    sqrt_t = np.sqrt(T)
    vt = sigma * sqrt_t
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * vt * vt) / vt
        d2 = d1 - vt
        pdf = _pdf(d1)
        disc_k = K * np.exp(-r * T)
        delta = np.where(is_call, ndtr(d1), ndtr(d1) - 1)
        gamma = pdf / (S * vt)
        vega = S * pdf * sqrt_t / 100
        decay = -S * pdf * sigma / (2 * sqrt_t)
        theta = np.where(is_call, decay - r * disc_k * ndtr(d2), decay + r * disc_k * ndtr(-d2)) / 365
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}
