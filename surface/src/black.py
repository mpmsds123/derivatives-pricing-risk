"""Black-76 pricing on the forward, and implied-vol inversion by Brent."""
from __future__ import annotations

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from .config import CONFIG

_VOL_LO = 1e-6
_VOL_HI = 5.0


def black_price(
    F: float | np.ndarray,
    K: float | np.ndarray,
    T: float,
    vol: float | np.ndarray,
    is_call: bool = True,
    discount: float = 1.0,
) -> np.ndarray:
    """Black-76 price. `discount` = 1.0 returns the undiscounted price.

    The Breeden-Litzenberger density is recovered from undiscounted calls, so
    the default deliberately leaves the discount factor out.
    """
    F = np.asarray(F, dtype=float)
    K = np.asarray(K, dtype=float)
    vol = np.asarray(vol, dtype=float)

    v = vol * np.sqrt(T)
    intrinsic = np.maximum(F - K, 0.0) if is_call else np.maximum(K - F, 0.0)

    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * v * v) / v
        d2 = d1 - v
        if is_call:
            price = F * norm.cdf(d1) - K * norm.cdf(d2)
        else:
            price = K * norm.cdf(-d2) - F * norm.cdf(-d1)

    # v -> 0 collapses to intrinsic; guard the 0/0.
    price = np.where(v > _VOL_LO, price, intrinsic)
    return discount * price


def black_vega(
    F: float | np.ndarray,
    K: float | np.ndarray,
    T: float,
    vol: float | np.ndarray,
    discount: float = 1.0,
) -> np.ndarray:
    """dPrice/dVol. Same for calls and puts."""
    F = np.asarray(F, dtype=float)
    K = np.asarray(K, dtype=float)
    v = np.asarray(vol, dtype=float) * np.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(F / K) + 0.5 * v * v) / v
        vega = F * norm.pdf(d1) * np.sqrt(T)
    return discount * np.where(v > _VOL_LO, vega, 0.0)


def implied_vol(
    price: float,
    F: float,
    K: float,
    T: float,
    is_call: bool = True,
    discount: float = 1.0,
) -> float:
    """Invert Black-76 for volatility with Brent. NaN if the quote is not
    strictly above intrinsic (no arbitrage-free vol exists)."""
    if not np.isfinite(price) or T <= 0.0:
        return float("nan")

    intrinsic = discount * (max(F - K, 0.0) if is_call else max(K - F, 0.0))
    if price <= intrinsic + 1e-10:
        return float("nan")

    def objective(s: float) -> float:
        return float(black_price(F, K, T, s, is_call, discount)) - price

    try:
        return float(brentq(objective, _VOL_LO, _VOL_HI, xtol=1e-12, maxiter=500))
    except ValueError:
        # Quote lies outside the bracket: above the vol cap or below intrinsic.
        return float("nan")


def bs_abs_delta(F: float, K: np.ndarray, T: float, vol: float,
                 is_call: np.ndarray) -> np.ndarray:
    """|delta| on the forward, used only to define the smile's strike range."""
    v = vol * np.sqrt(T)
    d1 = (np.log(F / np.asarray(K, dtype=float)) + 0.5 * v * v) / v
    call_delta = norm.cdf(d1)
    return np.where(is_call, call_delta, 1.0 - call_delta)


def price_to_bp(vol_error: float) -> float:
    """Vol difference in decimals -> basis points."""
    return vol_error * CONFIG["output"]["bp"]
