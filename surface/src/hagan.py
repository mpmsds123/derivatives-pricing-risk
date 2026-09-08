"""Hagan et al. (2002) lognormal SABR implied-volatility approximation.

SABR dynamics under the forward measure:

    dF = alpha * F^beta dW1
    dalpha = nu * alpha dW2
    dW1 dW2 = rho dt
"""
from __future__ import annotations

import numpy as np

from .config import CONFIG

_ATM_TOL = CONFIG["model"]["atm_tolerance"]


def _common_C(rho: float, nu: float) -> float:
    """The (2 - 3 rho^2) nu^2 / 24 term, shared by the ATM and non-ATM branches."""
    return (2.0 - 3.0 * rho * rho) * nu * nu / 24.0


def sabr_atm_vol(F: float, T: float, alpha: float, beta: float,
                 rho: float, nu: float) -> float:
    """Hagan ATM limit (K = F), the K -> F limit of `sabr_vol`.

        sigma_atm = alpha / F^(1-beta) * [1 + (A' + B' + C) T]
    """
    one_mb = 1.0 - beta
    A = one_mb * one_mb * alpha * alpha / (24.0 * F ** (2.0 - 2.0 * beta))
    B = rho * beta * alpha * nu / (4.0 * F ** one_mb)
    C = _common_C(rho, nu)
    return alpha / F ** one_mb * (1.0 + (A + B + C) * T)


def sabr_vol(K: np.ndarray | float, F: float, T: float, alpha: float,
             beta: float, rho: float, nu: float) -> np.ndarray:
    """Black lognormal implied vol under SABR, vectorised over K.

    Strikes within `atm_tolerance` of the forward take the explicit ATM branch;
    the general formula divides by x(z) which vanishes as K -> F.
    """
    K_arr = np.atleast_1d(np.asarray(K, dtype=float))
    out = np.empty_like(K_arr)

    one_mb = 1.0 - beta
    C = _common_C(rho, nu)

    atm_mask = np.abs(K_arr - F) < _ATM_TOL
    if atm_mask.any():
        out[atm_mask] = sabr_atm_vol(F, T, alpha, beta, rho, nu)

    gen = ~atm_mask
    if gen.any():
        Kg = K_arr[gen]
        FK = F * Kg
        FK_half = FK ** (one_mb / 2.0)      # (F K)^((1-beta)/2)
        log_FK = np.log(F / Kg)

        z = (nu / alpha) * FK_half * log_FK
        # x(z) = ln( (sqrt(1 - 2 rho z + z^2) + z - rho) / (1 - rho) )
        sqrt_term = np.sqrt(1.0 - 2.0 * rho * z + z * z)
        x_z = np.log((sqrt_term + z - rho) / (1.0 - rho))

        A = one_mb * one_mb * alpha * alpha / (24.0 * FK ** one_mb)
        B = rho * beta * nu * alpha / (4.0 * FK_half)
        D = one_mb * one_mb * log_FK ** 2 / 24.0
        E = one_mb ** 4 * log_FK ** 4 / 1920.0

        numerator = alpha * z * (1.0 + (A + B + C) * T)
        denominator = FK_half * x_z * (1.0 + D + E)

        vol = numerator / denominator

        # z -> 0 faster than x(z) only in exact arithmetic; in floating point a
        # strike a hair away from F can still produce z/x(z) = 0/0.
        degenerate = ~np.isfinite(vol)
        if degenerate.any():
            vol[degenerate] = sabr_atm_vol(F, T, alpha, beta, rho, nu)
        out[gen] = vol

    return out if np.ndim(K) else out[0]
